#!/usr/bin/env python3
"""
Backup-Spiegel für umbrelOS

umbrelOS sichert sich verschlüsselt auf eine USB-Platte (Einstellungen →
Backups). Diese App kopiert die fertigen Backups einmal pro Nacht per SFTP
auf eine Hetzner Storage Box (oder jeden anderen SFTP-Server). Jeder Lauf
baut eine frische Verbindung auf, nichts bleibt dauerhaft eingehängt, und
Port 23 kommt auch durch VPNs, die SMB sperren.

Die Backups sind schon von umbrelOS verschlüsselt. Bei der Storage Box
landen nur unlesbare Blöcke. Zum Wiederherstellen holt die App ein Backup
auf eine USB-Platte zurück, danach geht es in umbrelOS wie gewohnt weiter.

Nur Python-Standardbibliothek plus rclone und ssh-keyscan.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.1.0"
BACKUP_DIR_NAME = "Umbrel Backup.backup"  # So nennt umbrelOS jedes Backup
REPO_MARKER = "kopia.repository.f"        # Datei, an der man ein Backup erkennt
RESTORE_FOLDER = "Von der Storage Box zurückgeholt"


# --------------------------------------------------------------------------
# Umgebung
# --------------------------------------------------------------------------

@dataclass
class Env:
    data_dir: Path = Path(os.environ.get("DATA_DIR", "/data"))
    # Über /proc/1/root sieht der Container das Dateisystem des Hosts samt
    # aller gerade eingehängten USB-Platten (braucht pid: host).
    external_root: Path = Path(os.environ.get("EXTERNAL_ROOT", "/proc/1/root/home/umbrel/umbrel/external"))
    host_proc: Path = Path(os.environ.get("HOST_PROC", "/proc"))
    rclone: str = os.environ.get("RCLONE", "rclone")
    port: int = int(os.environ.get("STATUS_PORT", "8080"))
    kopia_wait: int = int(os.environ.get("KOPIA_WAIT_SECONDS", "1800"))


# --------------------------------------------------------------------------
# Einstellungen
# --------------------------------------------------------------------------

@dataclass
class Settings:
    host: str = ""
    port: int = 23
    user: str = ""
    password: str = ""          # mit `rclone obscure` verschleiert, nicht im Klartext
    remote_dir: str = "umbrel-backup"
    time: str = "03:30"
    bwlimit: str = ""           # z. B. "5M" für 5 MB/s, leer = unbegrenzt

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password)

    @classmethod
    def load(cls, path: Path) -> "Settings":
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(self.__dict__, f, indent=2)
        os.replace(tmp, path)


HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
BWLIMIT_RE = re.compile(r"^(\d+(\.\d+)?[KMG]?)?$")
REMOTE_DIR_RE = re.compile(r"^[A-Za-z0-9._ -]+(/[A-Za-z0-9._ -]+)*$")


def validate(form: dict[str, str]) -> list[str]:
    errors = []
    if not HOST_RE.match(form.get("host", "")):
        errors.append("Server: nur Buchstaben, Ziffern, Punkt und Bindestrich, z. B. u123456.your-storagebox.de")
    if not USER_RE.match(form.get("user", "")):
        errors.append("Benutzer: z. B. u123456 oder u123456-sub1")
    if not form.get("port", "").isdigit() or not 1 <= int(form["port"]) <= 65535:
        errors.append("Port: eine Zahl, bei Hetzner 23")
    if not REMOTE_DIR_RE.match(form.get("remote_dir", "")) or ".." in form.get("remote_dir", "").split("/"):
        errors.append("Zielordner: z. B. umbrel-backup")
    if not TIME_RE.match(form.get("time", "")):
        errors.append("Uhrzeit: im Format HH:MM, z. B. 03:30")
    if not BWLIMIT_RE.match(form.get("bwlimit", "").strip().upper()):
        errors.append("Upload-Limit: leer lassen oder z. B. 5M für 5 MB/s")
    return errors


# --------------------------------------------------------------------------
# Backups auf den USB-Platten finden
# --------------------------------------------------------------------------

@dataclass
class LocalBackup:
    path: Path      # absoluter Pfad zum Ordner "Umbrel Backup.backup"
    rel: str        # Pfad relativ zum external-Ordner, z. B. "Extreme SSD/Umbrel/Umbrel Backup.backup"

    @property
    def drive(self) -> str:
        return self.rel.split("/", 1)[0]


def find_backups(external_root: Path, max_depth: int = 5) -> list[LocalBackup]:
    found: list[LocalBackup] = []
    if not external_root.is_dir():
        return found

    def walk(directory: Path, depth: int) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            path = Path(entry.path)
            if entry.name == BACKUP_DIR_NAME and (path / REPO_MARKER).is_file():
                found.append(LocalBackup(path, path.relative_to(external_root).as_posix()))
            elif depth < max_depth:
                walk(path, depth + 1)

    walk(external_root, 1)
    return found


def list_drives(external_root: Path) -> list[str]:
    try:
        return sorted(
            e.name for e in os.scandir(external_root) if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")
        )
    except OSError:
        return []


def kopia_running(host_proc: Path) -> bool:
    """Läuft gerade ein Backup von umbrelOS? (kopia-Prozess auf dem Host)"""
    try:
        pids = [p for p in os.listdir(host_proc) if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            if (host_proc / pid / "comm").read_text().strip() == "kopia":
                return True
        except OSError:
            continue
    return False


# --------------------------------------------------------------------------
# rclone
# --------------------------------------------------------------------------

def remote(settings: Settings, known_hosts: Path, path: str = "") -> str:
    """Remote-Angabe ohne Konfigurationsdatei. Das Passwort kommt per Umgebungsvariable."""
    target = "/".join(p for p in (settings.remote_dir.strip("/"), path.strip("/")) if p)
    return (
        f":sftp,host={settings.host},user={settings.user},port={settings.port},"
        f"shell_type=none,known_hosts_file={known_hosts}:{target}"
    )


def rclone_env(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    env["RCLONE_SFTP_PASS"] = settings.password
    env["RCLONE_CONFIG"] = "/dev/null"
    return env


COMMON_FLAGS = ["--contimeout", "30s", "--timeout", "10m", "--retries", "3", "--low-level-retries", "10"]
QUICK_FLAGS = ["--contimeout", "20s", "--timeout", "1m", "--retries", "1", "--low-level-retries", "2"]
STATS_FLAGS = ["--log-level", "NOTICE", "--stats", "20s", "--stats-one-line", "--stats-log-level", "NOTICE"]


def obscure(env: Env, password: str) -> str:
    proc = subprocess.run([env.rclone, "obscure", "-"], input=password, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "rclone obscure fehlgeschlagen")
    return proc.stdout.strip()


# --------------------------------------------------------------------------
# Aufträge (Abgleich, Verbindungstest, Zurückholen) - immer nur einer zur Zeit
# --------------------------------------------------------------------------

@dataclass
class Job:
    kind: str
    started: float
    log: deque = field(default_factory=lambda: deque(maxlen=300))
    progress: str = ""
    process: subprocess.Popen | None = None
    cancelled: bool = False


class App:
    def __init__(self, env: Env, clock=time.time):
        self.env = env
        self.clock = clock
        self.lock = threading.Lock()
        self.job: Job | None = None
        self.state_path = env.data_dir / "state.json"
        self.settings_path = env.data_dir / "settings.json"
        self.known_hosts = env.data_dir / "known_hosts"
        self.state = self._load_state()

    # -- Zustand ------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        self.env.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))
        os.replace(tmp, self.state_path)

    def settings(self) -> Settings:
        return Settings.load(self.settings_path)

    def record(self, key: str, ok: bool, message: str, **extra) -> None:
        with self.lock:
            self.state[key] = {"time": self.clock(), "ok": ok, "message": message, **extra}
            self._save_state()

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp} {message}"
        print(f"{time.strftime('%Y-%m-%d')} {line}", flush=True)
        if self.job:
            self.job.log.append(line)

    # -- Auftrag starten ----------------------------------------------------

    def start(self, kind: str, target, *args) -> bool:
        with self.lock:
            if self.job is not None:
                return False
            self.job = Job(kind=kind, started=self.clock())
        threading.Thread(target=self._run, args=(target, args), daemon=True).start()
        return True

    def _run(self, target, args) -> None:
        try:
            target(*args)
        except Exception as error:  # Ein Auftrag darf die App nie mitreißen
            self.log(f"Fehler: {error}")
        finally:
            with self.lock:
                if self.job:
                    self.state["last_log"] = list(self.job.log)[-60:]
                self._save_state()
                self.job = None

    def cancelled(self) -> bool:
        return bool(self.job and self.job.cancelled)

    def cancel(self) -> None:
        job = self.job
        if job:
            job.cancelled = True
            if job.process and job.process.poll() is None:
                job.process.terminate()

    def _rclone(self, settings: Settings, args: list[str], quick: bool = False) -> int:
        # Für Tests und Listen nicht minutenlang neu versuchen
        flags = QUICK_FLAGS if quick else COMMON_FLAGS
        cmd = [self.env.rclone, *args, *flags]
        if settings.bwlimit:
            cmd += ["--bwlimit", settings.bwlimit]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            env=rclone_env(settings),
        )
        if self.job:
            self.job.process = proc
        assert proc.stdout is not None
        with proc.stdout:
            self._read_output(proc.stdout)
        return proc.wait()

    def _read_output(self, stream) -> None:
        for raw in stream:
            line = re.sub(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\s+", "", raw.rstrip())
            if not line:
                continue
            if "Transferred:" in line or line.startswith("NOTICE: ") and "ETA" in line:
                if self.job:
                    self.job.progress = line.replace("NOTICE: ", "").strip()
            else:
                self.log(line)

    # -- Verbindungstest ----------------------------------------------------

    def test_connection(self) -> None:
        settings = self.settings()
        if not settings.configured:
            self.record("last_test", False, "Erst Zugangsdaten speichern.")
            return
        self.log(f"Prüfe {settings.host}:{settings.port} …")
        if not self.known_hosts.exists():
            scan = subprocess.run(
                ["ssh-keyscan", "-T", "15", "-p", str(settings.port), settings.host],
                capture_output=True,
                text=True,
                timeout=60,
            )
            keys = [l for l in scan.stdout.splitlines() if l and not l.startswith("#")]
            if not keys:
                self.record(
                    "last_test",
                    False,
                    f"{settings.host} antwortet nicht auf Port {settings.port}. "
                    "Stimmt die Adresse, und ist bei Hetzner „SSH-Support“ und „Externe Erreichbarkeit“ an?",
                )
                return
            self.known_hosts.write_text("\n".join(keys) + "\n")
            self.log("Hostschlüssel des Servers gespeichert. Künftige Verbindungen werden dagegen geprüft.")
        code = self._rclone(settings, ["mkdir", remote(settings, self.known_hosts)], quick=True)
        if code == 0:
            code = self._rclone(settings, ["lsf", "--max-depth", "1", remote(settings, self.known_hosts)], quick=True)
        if code == 0:
            self.record("last_test", True, f"Verbindung klappt. Zielordner: {settings.remote_dir}")
        else:
            hint = "Benutzer oder Passwort falsch?"
            if any("key mismatch" in l or "knownhosts" in l for l in (self.job.log if self.job else [])):
                hint = "Der Hostschlüssel des Servers hat sich geändert. Wenn du den Server bewusst gewechselt hast: Schlüssel zurücksetzen."
            self.record("last_test", False, f"Verbindung fehlgeschlagen. {hint}")

    # -- Abgleich -----------------------------------------------------------

    def sync(self, reason: str = "manuell") -> None:
        settings = self.settings()
        if not settings.configured or not self.known_hosts.exists():
            self.record("last_sync", False, "Noch nicht eingerichtet: Zugangsdaten speichern und Verbindung testen.")
            return

        backups = find_backups(self.env.external_root)
        if not backups:
            # Niemals mit leerer Quelle abgleichen: Das würde die Kopie bei Hetzner löschen.
            self.record(
                "last_sync",
                False,
                "Kein Umbrel-Backup auf einer USB-Platte gefunden. Die Kopie bei Hetzner bleibt unverändert.",
            )
            return

        waited = 0
        while kopia_running(self.env.host_proc) and waited < self.env.kopia_wait and not self.cancelled():
            if waited == 0:
                self.log("umbrelOS sichert gerade. Warte, bis das Backup fertig ist …")
            time.sleep(15)
            waited += 15

        failures = []
        for backup in backups:
            if self.cancelled():
                break
            self.log(f"Gleiche ab: {backup.rel}")
            code = self._rclone(
                settings,
                [
                    "sync",
                    str(backup.path),
                    remote(settings, self.known_hosts, backup.rel),
                    "--transfers",
                    "4",
                    "--checkers",
                    "8",
                    *STATS_FLAGS,
                ],
            )
            if code != 0:
                failures.append(backup.rel)

        names = ", ".join(b.rel for b in backups)
        if self.cancelled():
            self.record("last_sync", False, "Abgleich abgebrochen.", backups=names)
        elif failures:
            self.record("last_sync", False, f"Abgleich fehlgeschlagen für: {', '.join(failures)}", backups=names)
        else:
            self.record("last_sync", True, f"Abgleich erfolgreich ({len(backups)} Backup(s)).", backups=names)

    # -- Backups bei der Storage Box auflisten und zurückholen --------------

    def list_remote(self) -> None:
        settings = self.settings()
        if not settings.configured or not self.known_hosts.exists():
            self.record("remote_list", False, "Noch nicht eingerichtet.", items=[])
            return
        out: list[str] = []
        proc = subprocess.run(
            [
                self.env.rclone,
                "lsf",
                "-R",
                "--dirs-only",
                "--max-depth",
                "6",
                remote(settings, self.known_hosts),
                *QUICK_FLAGS,
            ],
            capture_output=True,
            text=True,
            env=rclone_env(settings),
            timeout=300,
        )
        if proc.returncode != 0:
            self.log(proc.stderr.strip()[-500:])
            self.record("remote_list", False, "Liste konnte nicht geladen werden.", items=[])
            return
        for line in proc.stdout.splitlines():
            line = line.rstrip("/")
            if line == BACKUP_DIR_NAME or line.endswith("/" + BACKUP_DIR_NAME):
                out.append(line)
        self.record(
            "remote_list",
            True,
            f"{len(out)} Backup(s) auf der Storage Box." if out else "Keine Backups auf der Storage Box.",
            items=out,
        )

    def restore(self, remote_rel: str, drive: str) -> None:
        settings = self.settings()
        items = self.state.get("remote_list", {}).get("items", [])
        if remote_rel not in items:
            self.record("last_restore", False, "Unbekanntes Backup. Bitte die Liste neu laden.")
            return
        if drive not in list_drives(self.env.external_root):
            self.record("last_restore", False, "Diese USB-Platte ist nicht (mehr) angeschlossen.")
            return

        drive_path = self.env.external_root / drive
        stamp = time.strftime("%Y-%m-%d %H-%M")
        folder = f"{RESTORE_FOLDER} {stamp}"
        destination = drive_path / folder / BACKUP_DIR_NAME

        size = subprocess.run(
            [self.env.rclone, "size", "--json", remote(settings, self.known_hosts, remote_rel), *COMMON_FLAGS],
            capture_output=True,
            text=True,
            env=rclone_env(settings),
            timeout=600,
        )
        if size.returncode == 0:
            needed = json.loads(size.stdout).get("bytes", 0)
            free = shutil.disk_usage(drive_path).free
            if free < needed * 1.05:
                self.record(
                    "last_restore",
                    False,
                    f"Zu wenig Platz auf {drive}: {format_bytes(needed)} nötig, {format_bytes(free)} frei.",
                )
                return
            self.log(f"Hole {format_bytes(needed)} zurück nach {drive}/{folder}")

        code = self._rclone(
            settings,
            [
                "copy",
                remote(settings, self.known_hosts, remote_rel),
                str(destination),
                "--transfers",
                "4",
                *STATS_FLAGS,
            ],
        )
        if code == 0 and (destination / REPO_MARKER).is_file():
            self.record(
                "last_restore",
                True,
                f"Fertig. In umbrelOS unter Einstellungen → Backups → Wiederherstellen die Platte {drive} "
                f"und dort den Ordner „{folder}“ auswählen.",
            )
        else:
            self.record("last_restore", False, "Zurückholen fehlgeschlagen oder abgebrochen.")

    # -- Zeitplan -----------------------------------------------------------

    def due(self, now: time.struct_time | None = None) -> bool:
        settings = self.settings()
        if not settings.configured or not TIME_RE.match(settings.time):
            return False
        now = now or time.localtime(self.clock())
        today = time.strftime("%Y-%m-%d", now)
        if self.state.get("last_scheduled_date") == today:
            return False
        return time.strftime("%H:%M", now) >= settings.time

    def scheduler(self, stop: threading.Event) -> None:
        while not stop.wait(20):
            try:
                if self.job is None and self.due():
                    self.state["last_scheduled_date"] = time.strftime("%Y-%m-%d")
                    self._save_state()
                    self.start("abgleich", self.sync, "zeitplan")
            except Exception as error:
                print(f"Zeitplan-Fehler: {error}", flush=True)


def format_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}".replace(".", ",") if unit != "B" else f"{int(value)} B"
        value /= 1024
    return str(value)


# --------------------------------------------------------------------------
# Weboberfläche
# --------------------------------------------------------------------------

def ts(value) -> str:
    if not value:
        return "–"
    return f'<time data-ts="{float(value):.0f}">{time.strftime("%d.%m.%Y %H:%M", time.localtime(value))}</time>'


def result_html(entry: dict | None, empty: str = "–") -> str:
    if not entry:
        return f"<span class='muted'>{empty}</span>"
    cls = "ok" if entry.get("ok") else "err"
    return f"<span class='{cls}'>{html.escape(entry.get('message', ''))}</span> <small>{ts(entry.get('time'))}</small>"


def render(app: App, notice: str = "", errors: list[str] | None = None) -> str:
    e = html.escape
    s = app.settings()
    job = app.job
    backups = find_backups(app.env.external_root)
    drives = list_drives(app.env.external_root)
    remote_list = app.state.get("remote_list", {})

    if backups:
        local = "<ul class='plain'>" + "".join(f"<li>{e(b.rel)}</li>" for b in backups) + "</ul>"
    else:
        local = (
            "<p class='warn'>Kein Umbrel-Backup auf einer USB-Platte gefunden. Richte zuerst unter "
            "<b>Einstellungen → Backups</b> ein Backup auf eine USB-Platte ein.</p>"
        )

    if job:
        kind = {"abgleich": "Abgleich", "test": "Verbindungstest", "zurueckholen": "Zurückholen", "liste": "Liste laden"}
        log_lines = "".join(f"<div>{e(l)}</div>" for l in list(job.log)[-15:])
        job_html = f"""<div class="card running"><b>{kind.get(job.kind, job.kind)} läuft</b>
          (seit {ts(job.started)})<div class="progress">{e(job.progress) or 'startet …'}</div>
          <div class="log">{log_lines}</div>
          <form method="post" action="/aktion/abbrechen"><button class="secondary">Abbrechen</button></form></div>"""
    else:
        last_log = app.state.get("last_log") or []
        job_html = (
            "<details><summary>Protokoll des letzten Auftrags</summary><div class='log'>"
            + "".join(f"<div>{e(l)}</div>" for l in last_log[-40:])
            + "</div></details>"
            if last_log
            else ""
        )

    disabled = " disabled" if job else ""
    restore_html = ""
    if remote_list.get("items"):
        options = "".join(f"<option value='{e(i)}'>{e(i)}</option>" for i in remote_list["items"])
        drive_options = "".join(f"<option value='{e(d)}'>{e(d)}</option>" for d in drives)
        restore_html = f"""<form method="post" action="/aktion/zurueckholen" class="grid">
            <label>Backup<select name="remote">{options}</select></label>
            <label>Auf USB-Platte<select name="drive">{drive_options or '<option value="">keine Platte angeschlossen</option>'}</select></label>
            <button{disabled}>Zurückholen</button></form>"""

    errors_html = "".join(f"<div class='alert'>{e(x)}</div>" for x in (errors or []))
    notice_html = f"<div class='notice'>{e(notice)}</div>" if notice else ""
    pw_placeholder = "gespeichert, leer lassen = unverändert" if s.password else ""

    return f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{'<meta http-equiv="refresh" content="5">' if job else ''}
<title>Backup-Spiegel</title>
<style>
  :root {{ color-scheme: light dark; --fg:#1b1f24; --bg:#f6f7f9; --card:#fff; --line:#e3e6ea; --muted:#6b7280;
          --ok:#127a3d; --warn:#9a5b00; --err:#b42318; --accent:#1f6f8b; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --fg:#e7e9ec; --bg:#111315; --card:#1a1d20; --line:#2b2f34;
          --muted:#9aa1a9; --ok:#4cc27a; --warn:#e3a33b; --err:#f07167; --accent:#4fb3d1; }} }}
  body {{ font: 15px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg); margin: 0; }}
  main {{ max-width: 860px; margin: 0 auto; padding: 24px 16px 48px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }} h2 {{ font-size: 16px; margin: 28px 0 8px; }}
  .muted, small, .sub {{ color: var(--muted); }} .ok {{ color: var(--ok); }} .warn {{ color: var(--warn); }} .err {{ color: var(--err); }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; margin: 8px 0; }}
  .running {{ border-color: var(--accent); }} .progress {{ font-family: ui-monospace, monospace; font-size: 13px; margin: 6px 0; }}
  .log {{ font-family: ui-monospace, monospace; font-size: 12px; color: var(--muted); max-height: 260px; overflow: auto; white-space: pre-wrap; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px 14px; align-items: end; }}
  label {{ display: flex; flex-direction: column; font-size: 13px; color: var(--muted); gap: 4px; }}
  input, select {{ font: inherit; padding: 7px 9px; border: 1px solid var(--line); border-radius: 7px; background: var(--bg); color: var(--fg); }}
  button {{ font: inherit; padding: 8px 14px; border: 0; border-radius: 7px; background: var(--accent); color: #fff; cursor: pointer; }}
  button.secondary {{ background: transparent; color: var(--accent); border: 1px solid var(--accent); }}
  button[disabled] {{ opacity: .5; cursor: default; }}
  .row {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }} .row form {{ margin: 0; }}
  .alert {{ border: 1px solid var(--err); color: var(--err); border-radius: 8px; padding: 10px 14px; margin: 10px 0; }}
  .notice {{ border: 1px solid var(--ok); color: var(--ok); border-radius: 8px; padding: 10px 14px; margin: 10px 0; }}
  ul.plain {{ margin: 0; padding-left: 18px; }} dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; margin: 0; }}
  dt {{ color: var(--muted); }} dd {{ margin: 0; }}
</style></head><body><main>
<h1>Backup-Spiegel</h1>
<div class="sub">Kopiert dein verschlüsseltes Umbrel-Backup jede Nacht um {e(s.time)} Uhr auf deine Storage Box.</div>
{errors_html}{notice_html}

<h2>Status</h2>
<div class="card"><dl>
  <dt>Letzter Abgleich</dt><dd>{result_html(app.state.get('last_sync'), 'noch keiner')}</dd>
  <dt>Verbindung</dt><dd>{result_html(app.state.get('last_test'), 'noch nicht getestet')}</dd>
  <dt>Backups auf USB</dt><dd>{local}</dd>
</dl>
<div class="row">
  <form method="post" action="/aktion/abgleich"><button{disabled}>Jetzt abgleichen</button></form>
  <form method="post" action="/aktion/test"><button class="secondary"{disabled}>Verbindung testen</button></form>
</div></div>
{job_html}

<h2>Storage Box</h2>
<form method="post" action="/aktion/speichern" class="card">
  <div class="grid">
    <label>Server<input name="host" value="{e(s.host)}" placeholder="u123456.your-storagebox.de" required></label>
    <label>Benutzer<input name="user" value="{e(s.user)}" placeholder="u123456" required></label>
    <label>Passwort<input name="password" type="password" placeholder="{pw_placeholder}" autocomplete="new-password"></label>
    <label>Port<input name="port" value="{s.port}" inputmode="numeric"></label>
    <label>Zielordner auf der Box<input name="remote_dir" value="{e(s.remote_dir)}"></label>
    <label>Uhrzeit für den Abgleich<input name="time" value="{e(s.time)}" placeholder="03:30"></label>
    <label>Upload-Limit (optional)<input name="bwlimit" value="{e(s.bwlimit)}" placeholder="z. B. 5M"></label>
  </div>
  <div class="row"><button{disabled}>Speichern und Verbindung testen</button></div>
  <p class="muted"><small>Bei Hetzner muss für die Storage Box „SSH-Support“ und „Externe Erreichbarkeit“
  eingeschaltet sein. Am besten einen eigenen Unterkonto-Zugang (Sub-Account) nur für dieses Backup anlegen.</small></p>
</form>

<h2>Zurückholen</h2>
<div class="card">
  <p class="muted">Holt ein Backup von der Storage Box auf eine USB-Platte. Danach in umbrelOS unter
  Einstellungen → Backups wiederherstellen.</p>
  <div class="row"><form method="post" action="/aktion/liste"><button class="secondary"{disabled}>Backups auf der Storage Box anzeigen</button></form></div>
  <p>{result_html(remote_list or None, '') if remote_list else ''}</p>
  {restore_html}
  <p>{result_html(app.state.get('last_restore'), '') if app.state.get('last_restore') else ''}</p>
</div>

<p class="muted"><small>Version {VERSION}
{' · <a href="#" onclick="document.getElementById(\'reset\').submit();return false">Hostschlüssel zurücksetzen</a>' if app.known_hosts.exists() else ''}
</small></p>
<form id="reset" method="post" action="/aktion/schluessel-zuruecksetzen"></form>
</main>
<script>
for (const t of document.querySelectorAll('time[data-ts]')) t.textContent = new Date(Number(t.dataset.ts) * 1000).toLocaleString('de-DE', {{dateStyle: 'short', timeStyle: 'short'}});
</script></body></html>"""


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: str, kind: str = "text/html; charset=utf-8", headers=None) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, notice: str = "") -> None:
            location = "/" + (f"?hinweis={urllib.parse.quote(notice)}" if notice else "")
            self._send(303, "", headers={"Location": location})

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                notice = urllib.parse.parse_qs(parsed.query).get("hinweis", [""])[0]
                self._send(200, render(app, notice=notice))
            elif parsed.path == "/status.json":
                with app.lock:
                    state = {k: v for k, v in app.state.items() if k != "last_log"}
                state["running"] = app.job.kind if app.job else None
                self._send(200, json.dumps(state, ensure_ascii=False), "application/json")
            else:
                self._send(404, "Nicht gefunden", "text/plain; charset=utf-8")

        def do_POST(self):
            # Nur Formulare von dieser Seite annehmen
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
                self._send(403, "Fremde Herkunft", "text/plain; charset=utf-8")
                return
            length = min(int(self.headers.get("Content-Length") or 0), 20000)
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace")).items()}
            action = self.path.split("?", 1)[0].removeprefix("/aktion/")
            busy = "Es läuft schon ein Auftrag."

            if action == "speichern":
                form.setdefault("password", "")
                form = {k: (v if k == "password" else v.strip()) for k, v in form.items()}
                errors = validate(form)
                current = app.settings()
                if not form["password"] and not current.password:
                    errors.append("Passwort fehlt.")
                if errors:
                    self._send(400, render(app, errors=errors))
                    return
                if form["host"] != current.host or str(form["port"]) != str(current.port):
                    app.known_hosts.unlink(missing_ok=True)  # Neuer Server: Schlüssel neu holen
                password = obscure(app.env, form["password"]) if form["password"] else current.password
                Settings(
                    host=form["host"],
                    port=int(form["port"]),
                    user=form["user"],
                    password=password,
                    remote_dir=form["remote_dir"].strip("/"),
                    time=form["time"],
                    bwlimit=form.get("bwlimit", "").upper(),
                ).save(app.settings_path)
                started = app.start("test", app.test_connection)
                self._redirect("Gespeichert. Verbindung wird getestet …" if started else busy)
            elif action == "test":
                self._redirect("" if app.start("test", app.test_connection) else busy)
            elif action == "abgleich":
                self._redirect("" if app.start("abgleich", app.sync, "manuell") else busy)
            elif action == "liste":
                self._redirect("" if app.start("liste", app.list_remote) else busy)
            elif action == "zurueckholen":
                started = app.start("zurueckholen", app.restore, form.get("remote", ""), form.get("drive", ""))
                self._redirect("" if started else busy)
            elif action == "abbrechen":
                app.cancel()
                self._redirect("Wird abgebrochen …")
            elif action == "schluessel-zuruecksetzen":
                app.known_hosts.unlink(missing_ok=True)
                self._redirect("Hostschlüssel gelöscht. Beim nächsten Verbindungstest wird er neu gespeichert.")
            else:
                self._send(404, "Unbekannte Aktion", "text/plain; charset=utf-8")

        def log_message(self, *args):
            pass

    return Handler


def main() -> int:
    env = Env()
    env.data_dir.mkdir(parents=True, exist_ok=True)
    app = App(env)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (app.cancel(), stop.set()))
    server = ThreadingHTTPServer(("0.0.0.0", env.port), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Backup-Spiegel {VERSION} gestartet, sucht Backups unter {env.external_root}", flush=True)
    app.scheduler(stop)
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
