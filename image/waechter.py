#!/usr/bin/env python3
"""
Netzlaufwerk-Wächter für umbrelOS

Das Problem
-----------
Reißt die Verbindung zu einer SMB-Freigabe ab (z. B. weil der Router nachts
eine neue IP bekommt), bleibt im Kernel ein toter CIFS-Mount zurück.
umbrelOS prüft jede Minute mit `mountpoint`, ob die Freigabe eingehängt ist.
`mountpoint` greift dabei auf den Pfad zu, bekommt EHOSTDOWN und meldet
"nicht eingehängt". umbrelOS will daraufhin neu einhängen, scheitert aber
schon beim Anlegen des Ordners (mkdir auf dem toten Pfad) und hängt den
toten Mount auch beim Aufräumen nicht aus, weil `mountpoint` ihn ja nicht
als eingehängt erkennt. Ergebnis: Das Backup bleibt bis zum Neustart stehen.
(Siehe packages/umbreld/source/modules/files/network-storage.ts)

Die Lösung
----------
Der Wächter liest die Mount-Tabelle des Hosts, ohne die Pfade anzufassen,
prüft jede SMB-Freigabe im Netzwerkordner von umbrelOS mit einem
Verzeichnis-Listing mit Zeitlimit und hängt sie nach mehreren Fehlschlägen
in Folge aus (umount -l). Den Rest erledigt umbrelOS selbst: Seine
Minutenschleife hängt die Freigabe wieder ein, sobald der Server erreichbar ist.

Nur Python-Standardbibliothek. Läuft als Container mit `pid: host` und
`privileged: true`, weil er die Mount-Tabelle des Hosts lesen und dort
aushängen muss.
"""

from __future__ import annotations

import html
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

VERSION = "0.1.0"
CIFS_TYPES = {"cifs", "smb3"}


# --------------------------------------------------------------------------
# Einstellungen
# --------------------------------------------------------------------------

def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(minimum, value)


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "ja", "yes", "on"}


@dataclass
class Config:
    network_root: str = "/home/umbrel/umbrel/network"
    host_pid: int = 1
    interval: int = 60          # Sekunden zwischen zwei Prüfdurchläufen
    check_timeout: int = 20     # Sekunden, die eine Freigabe zum Antworten hat
    fail_threshold: int = 3     # Fehlschläge in Folge bis zum Aushängen
    remount_warn: int = 600     # Sekunden ohne Wiedereinhängen bis zur Warnung
    forget_after: int = 86400   # Sekunden, nach denen verschwundene Freigaben aus der Liste fallen
    status_port: int = 8080
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            network_root=os.environ.get("NETWORK_ROOT", cls.network_root).rstrip("/") or "/",
            host_pid=_env_int("HOST_PID", cls.host_pid),
            interval=_env_int("CHECK_INTERVAL", cls.interval, minimum=10),
            check_timeout=_env_int("CHECK_TIMEOUT", cls.check_timeout, minimum=5),
            fail_threshold=_env_int("FAIL_THRESHOLD", cls.fail_threshold),
            remount_warn=_env_int("REMOUNT_WARN", cls.remount_warn, minimum=60),
            forget_after=_env_int("FORGET_AFTER", cls.forget_after, minimum=600),
            status_port=_env_int("STATUS_PORT", cls.status_port),
            dry_run=_env_bool("DRY_RUN"),
        )


# --------------------------------------------------------------------------
# Mount-Tabelle lesen (ohne die Pfade anzufassen)
# --------------------------------------------------------------------------

_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def unescape_mountinfo(value: str) -> str:
    """mountinfo kodiert Leerzeichen, Tabs, Zeilenumbrüche und \\ oktal (\\040 usw.)."""
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), value)


def parse_mountinfo(text: str, network_root: str) -> dict[str, dict]:
    """Liefert alle SMB-Mounts unterhalb von network_root.

    Rückgabe: {Mountpunkt: {"source": "//server/freigabe", "layers": Anzahl}}
    "layers" > 1 heißt: mehrere Mounts übereinander am selben Pfad.
    """
    root = network_root.rstrip("/")
    mounts: dict[str, dict] = {}
    for line in text.splitlines():
        parts = line.split(" ")
        # Aufbau: id parent maj:min root mountpoint optionen [optionale Felder ...] - fstype quelle superoptionen
        try:
            separator = parts.index("-", 6)
        except ValueError:
            continue
        if len(parts) < separator + 3:
            continue
        fstype = parts[separator + 1]
        if fstype not in CIFS_TYPES:
            continue
        mount_point = unescape_mountinfo(parts[4])
        if not mount_point.startswith(root + "/"):
            continue
        entry = mounts.setdefault(
            mount_point, {"source": unescape_mountinfo(parts[separator + 2]), "layers": 0}
        )
        entry["layers"] += 1
    return mounts


# --------------------------------------------------------------------------
# Befehle im Mount-Namespace des Hosts ausführen
# --------------------------------------------------------------------------

_stuck_processes: list[subprocess.Popen] = []


def run_command(cmd: list[str], timeout: float) -> tuple[bool, str]:
    """Führt einen Befehl mit hartem Zeitlimit aus. Hängt er im Kernel fest, wird er
    abgeschossen und der Wächter macht trotzdem weiter."""
    # Früher hängengebliebene Prozesse einsammeln, sobald sie sich beendet haben
    _stuck_processes[:] = [p for p in _stuck_processes if p.poll() is None]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        return False, f"{cmd[0]} nicht ausführbar: {error}"
    try:
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _stuck_processes.append(proc)
        return False, f"keine Antwort nach {timeout:g} s"
    if proc.returncode == 0:
        return True, ""
    lines = stderr.decode(errors="replace").strip().splitlines()
    return False, lines[-1] if lines else f"Exit-Code {proc.returncode}"


def make_host_runner(host_pid: int) -> Callable[[list[str], float], tuple[bool, str]]:
    def run_on_host(args: list[str], timeout: float) -> tuple[bool, str]:
        return run_command(["nsenter", f"--target={host_pid}", "--mount", "--", *args], timeout)

    return run_on_host


# --------------------------------------------------------------------------
# Der Wächter
# --------------------------------------------------------------------------

STATUS_TEXT = {
    "ok": "Verbunden",
    "stoerung": "Antwortet nicht",
    "ausgehaengt": "Tote Verbindung ausgehängt, umbrelOS hängt neu ein",
    "wartet": "Nicht wieder eingehängt. Ist der Server erreichbar?",
    "nicht_eingehaengt": "Nicht eingehängt",
}


@dataclass
class ShareState:
    path: str
    source: str = ""
    status: str = "ok"
    mounted: bool = True
    failures: int = 0
    last_ok: float | None = None
    last_error: str = ""
    detached_at: float | None = None
    detach_count: int = 0
    gone_since: float | None = None


@dataclass
class Event:
    time: float
    level: str
    message: str


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h".replace(".", ",")


class Watcher:
    def __init__(
        self,
        config: Config,
        read_mountinfo: Callable[[], str] | None = None,
        run_on_host: Callable[[list[str], float], tuple[bool, str]] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.read_mountinfo = read_mountinfo or self._read_host_mountinfo
        self.run_on_host = run_on_host or make_host_runner(config.host_pid)
        self.clock = clock
        self.shares: dict[str, ShareState] = {}
        self.events: deque[Event] = deque(maxlen=200)
        self.problems: list[str] = []
        self.last_cycle: float | None = None
        self.lock = threading.Lock()

    # -- Hilfsfunktionen ----------------------------------------------------

    def _read_host_mountinfo(self) -> str:
        with open(f"/proc/{self.config.host_pid}/mountinfo", encoding="utf-8", errors="surrogateescape") as f:
            return f.read()

    def log(self, message: str, level: str = "info") -> None:
        now = self.clock()
        self.events.append(Event(now, level, message))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))
        print(f"{stamp} UTC [{level}] {message}", flush=True)

    # -- Selbsttest beim Start ---------------------------------------------

    def self_test(self) -> bool:
        problems = []
        try:
            self.read_mountinfo()
        except OSError as error:
            problems.append(f"Mount-Tabelle des Hosts nicht lesbar ({error}). Läuft der Container mit pid: host?")
        ok, error = self.run_on_host(["true"], 10)
        if not ok:
            problems.append(f"Kein Zugriff auf den Host ({error}). Der Container braucht privileged: true und pid: host.")
        elif not self.run_on_host(["test", "-d", self.config.network_root], 10)[0]:
            problems.append(
                f"Netzwerkordner {self.config.network_root} nicht gefunden. "
                "Ist noch keine Freigabe eingerichtet, oder legt diese umbrelOS-Version ihn woanders ab?"
            )
        with self.lock:
            self.problems = problems
        for problem in problems:
            self.log(problem, "error")
        if not problems:
            mode = " (Testmodus: hängt nichts aus)" if self.config.dry_run else ""
            self.log(f"Wächter {VERSION} gestartet, überwacht {self.config.network_root}{mode}")
        return not problems

    # -- Ein Prüfdurchlauf --------------------------------------------------

    def cycle(self) -> None:
        mounts = parse_mountinfo(self.read_mountinfo(), self.config.network_root)
        now = self.clock()

        for path, info in mounts.items():
            share = self.shares.get(path)
            if share is None:
                share = ShareState(path=path, source=info["source"])
                with self.lock:
                    self.shares[path] = share
                self.log(f"Freigabe gefunden: {info['source']}")
            elif not share.mounted:
                if share.detached_at is not None:
                    self.log(
                        f"umbrelOS hat {share.source} wieder eingehängt "
                        f"({format_duration(now - share.detached_at)} nach dem Aushängen)"
                    )
                else:
                    self.log(f"{info['source']} ist wieder eingehängt")
            share.mounted = True
            share.gone_since = None
            share.detached_at = None
            share.source = info["source"]
            self._check(share, info["layers"], now)

        for path, share in list(self.shares.items()):
            if path in mounts:
                continue
            if share.mounted:
                share.mounted = False
                share.gone_since = now
                share.failures = 0
                if share.detached_at is None:
                    share.status = "nicht_eingehaengt"
                    self.log(f"{share.source} ist nicht mehr eingehängt", "warn")
            if share.detached_at is not None and now - share.detached_at >= self.config.remount_warn:
                if share.status != "wartet":
                    self.log(
                        f"{share.source} ist seit {format_duration(now - share.detached_at)} nicht wieder "
                        "eingehängt. umbrelOS versucht es jede Minute. Bleibt das so, kommt die Verbindung "
                        "zum Server nicht durch (z. B. Port 445 gesperrt).",
                        "warn",
                    )
                share.status = "wartet"
            if share.gone_since is not None and now - share.gone_since >= self.config.forget_after:
                with self.lock:
                    del self.shares[path]

        self.last_cycle = now

    def _check(self, share: ShareState, layers: int, now: float) -> None:
        ok, error = self.run_on_host(["ls", "-A", "--", share.path], self.config.check_timeout)
        if ok:
            if share.failures:
                self.log(f"{share.source} antwortet wieder")
            share.failures = 0
            share.last_ok = now
            share.last_error = ""
            share.status = "ok"
            return

        share.failures += 1
        share.last_error = error
        share.status = "stoerung"
        self.log(
            f"{share.source} antwortet nicht ({share.failures}/{self.config.fail_threshold}): {error}",
            "warn",
        )
        if share.failures >= self.config.fail_threshold:
            self._detach(share, layers, now)

    def _detach(self, share: ShareState, layers: int, now: float) -> None:
        share.failures = 0
        if self.config.dry_run:
            self.log(f"Testmodus: würde jetzt {share.source} aushängen ({share.path})", "warn")
            return
        detached = False
        error = ""
        # Liegen mehrere Mounts übereinander, jede Schicht einzeln aushängen
        for _ in range(max(1, layers)):
            ok, error = self.run_on_host(["umount", "-l", "--", share.path], 30)
            if not ok:
                break
            detached = True
        if detached:
            share.detached_at = now
            share.detach_count += 1
            share.status = "ausgehaengt"
            self.log(
                f"Tote Verbindung ausgehängt: {share.source}. umbrelOS hängt die Freigabe innerhalb "
                "etwa einer Minute neu ein, sobald der Server erreichbar ist."
            )
        else:
            self.log(f"Aushängen von {share.path} fehlgeschlagen: {error}", "error")

    # -- Status für die Webseite -------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            shares = [
                {
                    "source": s.source,
                    "path": s.path,
                    "status": s.status,
                    "status_text": STATUS_TEXT.get(s.status, s.status)
                    + (f" ({s.failures}/{self.config.fail_threshold})" if s.status == "stoerung" else ""),
                    "last_ok": s.last_ok,
                    "last_error": s.last_error,
                    "detach_count": s.detach_count,
                }
                for s in sorted(self.shares.values(), key=lambda s: s.path)
            ]
            events = [{"time": e.time, "level": e.level, "message": e.message} for e in reversed(self.events)]
            problems = list(self.problems)
        return {
            "version": VERSION,
            "network_root": self.config.network_root,
            "interval": self.config.interval,
            "check_timeout": self.config.check_timeout,
            "fail_threshold": self.config.fail_threshold,
            "dry_run": self.config.dry_run,
            "last_cycle": self.last_cycle,
            "problems": problems,
            "shares": shares,
            "events": events,
        }


# --------------------------------------------------------------------------
# Statusseite
# --------------------------------------------------------------------------

def _ts(value: float | None) -> str:
    """Zeitstempel als <time>; die Seite rechnet ihn per JavaScript in Ortszeit um."""
    if value is None:
        return "–"
    utc = time.strftime("%d.%m.%Y %H:%M:%S UTC", time.gmtime(value))
    return f'<time data-ts="{value:.0f}">{utc}</time>'


def render_html(state: dict) -> str:
    e = html.escape
    badge = {"ok": "ok", "stoerung": "warn", "ausgehaengt": "warn", "wartet": "err", "nicht_eingehaengt": "muted"}

    if state["shares"]:
        rows = "".join(
            f"<tr><td><b>{e(s['source'])}</b><br><small>{e(s['path'])}</small></td>"
            f"<td><span class='badge {badge.get(s['status'], 'muted')}'>{e(s['status_text'])}</span>"
            + (f"<br><small>{e(s['last_error'])}</small>" if s["last_error"] else "")
            + f"</td><td>{_ts(s['last_ok'])}</td><td>{s['detach_count']}</td></tr>"
            for s in state["shares"]
        )
        shares_html = (
            "<table><thead><tr><th>Freigabe</th><th>Zustand</th><th>Zuletzt erreichbar</th>"
            "<th>Ausgehängt</th></tr></thead><tbody>" + rows + "</tbody></table>"
        )
    else:
        shares_html = (
            "<p class='muted'>Gerade ist keine Netzwerkfreigabe eingehängt. Sobald du unter "
            "Dateien → Netzwerk eine Freigabe hinzufügst oder ein Backup auf ein Netzlaufwerk "
            "einrichtest, erscheint sie hier.</p>"
        )

    problems_html = "".join(f"<div class='alert'>{e(p)}</div>" for p in state["problems"])
    if state["dry_run"]:
        problems_html += "<div class='alert info'>Testmodus aktiv: Der Wächter meldet nur und hängt nichts aus.</div>"

    events_html = "".join(
        f"<li class='{e(ev['level'])}'>{_ts(ev['time'])} {e(ev['message'])}</li>" for ev in state["events"][:50]
    ) or "<li class='muted'>Noch keine Ereignisse.</li>"

    return f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Netzlaufwerk-Wächter</title>
<style>
  :root {{ color-scheme: light dark; --fg:#1b1f24; --bg:#f6f7f9; --card:#fff; --line:#e3e6ea; --muted:#6b7280;
          --ok:#127a3d; --warn:#9a5b00; --err:#b42318; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --fg:#e7e9ec; --bg:#111315; --card:#1a1d20; --line:#2b2f34;
          --muted:#9aa1a9; --ok:#4cc27a; --warn:#e3a33b; --err:#f07167; }} }}
  body {{ font: 15px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg); margin: 0; }}
  main {{ max-width: 900px; margin: 0 auto; padding: 24px 16px 48px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }} h2 {{ font-size: 16px; margin: 28px 0 8px; }}
  .sub, .muted, small {{ color: var(--muted); }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 4px 0; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line); vertical-align: top; }}
  tr:last-child td {{ border-bottom: 0; }} th {{ font-size: 13px; color: var(--muted); font-weight: 600; }}
  .badge {{ font-weight: 600; }} .badge.ok {{ color: var(--ok); }} .badge.warn {{ color: var(--warn); }}
  .badge.err {{ color: var(--err); }} .badge.muted {{ color: var(--muted); }}
  .card p {{ padding: 6px 14px; }}
  .alert {{ border: 1px solid var(--err); color: var(--err); border-radius: 8px; padding: 10px 14px; margin: 12px 0; }}
  .alert.info {{ border-color: var(--warn); color: var(--warn); }}
  ul {{ list-style: none; padding: 0; margin: 0; }} li {{ padding: 6px 14px; border-bottom: 1px solid var(--line); }}
  li:last-child {{ border-bottom: 0; }} li time {{ color: var(--muted); margin-right: 6px; }}
  li.warn {{ color: var(--warn); }} li.error {{ color: var(--err); }}
</style></head><body><main>
<h1>Netzlaufwerk-Wächter</h1>
<div class="sub">Prüft alle {state['interval']} s, ob deine Netzwerkfreigaben antworten. Nach {state['fail_threshold']}
Fehlschlägen in Folge hängt er eine tote Verbindung aus, damit umbrelOS sie neu einhängen kann.
Letzte Prüfung: {_ts(state['last_cycle'])}</div>
{problems_html}
<h2>Freigaben</h2><div class="card">{shares_html}</div>
<h2>Ereignisse</h2><div class="card"><ul>{events_html}</ul></div>
<p class="muted"><small>Version {e(state['version'])} · überwacht {e(state['network_root'])} ·
Seite aktualisiert sich alle 30 s</small></p>
</main>
<script>
for (const t of document.querySelectorAll('time[data-ts]')) {{
  t.textContent = new Date(Number(t.dataset.ts) * 1000).toLocaleString('de-DE');
}}
</script></body></html>"""


def make_handler(watcher: Watcher):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                body, kind = render_html(watcher.snapshot()).encode(), "text/html; charset=utf-8"
            elif path == "/status.json":
                body, kind = json.dumps(watcher.snapshot(), ensure_ascii=False).encode(), "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


# --------------------------------------------------------------------------
# Start
# --------------------------------------------------------------------------

def main() -> int:
    config = Config.from_env()
    watcher = Watcher(config)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    server = ThreadingHTTPServer(("0.0.0.0", config.status_port), make_handler(watcher))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    watcher.self_test()
    while not stop.is_set():
        try:
            watcher.cycle()
        except Exception as error:  # Der Wächter selbst darf nie stehen bleiben
            watcher.log(f"Fehler im Prüfdurchlauf: {error}", "error")
        stop.wait(config.interval)

    server.shutdown()
    watcher.log("Wächter beendet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
