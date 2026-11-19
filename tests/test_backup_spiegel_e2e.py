"""Ende-zu-Ende-Test gegen einen echten SFTP-Server.

Läuft nur, wenn BACKUP_SPIEGEL_E2E gesetzt ist, z. B.:
  BACKUP_SPIEGEL_E2E="127.0.0.1:2223:u123456:Passwort:/home/u123456" python3 -m unittest tests.test_backup_spiegel_e2e -v
Format: host:port:benutzer:passwort:homeverzeichnis-auf-dem-server (das Verzeichnis muss lokal lesbar sein).
"""

import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest

SPEC = importlib.util.spec_from_file_location(
    "backup_spiegel", pathlib.Path(__file__).resolve().parent.parent / "image-backup-spiegel" / "backup_spiegel.py"
)
bs = importlib.util.module_from_spec(SPEC)
sys.modules["backup_spiegel"] = bs
SPEC.loader.exec_module(bs)

E2E = os.environ.get("BACKUP_SPIEGEL_E2E")


def wait(app, timeout=120):
    deadline = time.time() + timeout
    while app.job is not None and time.time() < deadline:
        time.sleep(0.2)
    assert app.job is None, "Auftrag läuft noch"


@unittest.skipUnless(E2E, "BACKUP_SPIEGEL_E2E nicht gesetzt")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        host, port, user, password, home = E2E.split(":", 4)
        self.home = pathlib.Path(home)
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.external = self.tmp / "external"
        self.repo = self.external / "Extreme SSD" / "Umbrel" / bs.BACKUP_DIR_NAME
        (self.repo / "p").mkdir(parents=True)
        (self.repo / bs.REPO_MARKER).write_text("format")
        (self.repo / "p" / "pack-1").write_bytes(os.urandom(200_000))
        (self.repo / "p" / "pack-2").write_bytes(os.urandom(50_000))
        (self.external / "Fotos" / "2024").mkdir(parents=True)  # kein Backup, darf nicht mit
        (self.external / "Restore Disk").mkdir()
        (self.tmp / "proc").mkdir()
        self.remote_dir = f"umbrel-backup-test-{os.getpid()}"
        env = bs.Env(
            data_dir=self.tmp / "data",
            external_root=self.external,
            host_proc=self.tmp / "proc",
            kopia_wait=2,
        )
        self.app = bs.App(env)
        bs.Settings(
            host=host,
            port=int(port),
            user=user,
            password=bs.obscure(env, password),
            remote_dir=self.remote_dir,
        ).save(self.app.settings_path)
        self.remote_repo = self.home / self.remote_dir / "Extreme SSD" / "Umbrel" / bs.BACKUP_DIR_NAME

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.home / self.remote_dir, ignore_errors=True)

    def run_job(self, kind, target, *args):
        self.assertTrue(self.app.start(kind, target, *args))
        wait(self.app)

    def test_full_cycle(self):
        # Verbindungstest speichert den Hostschlüssel und legt den Zielordner an
        self.run_job("test", self.app.test_connection)
        self.assertTrue(self.app.state["last_test"]["ok"], self.app.state["last_test"])
        self.assertIn(":2223", self.app.known_hosts.read_text())
        self.assertTrue((self.home / self.remote_dir).is_dir())

        # Erster Abgleich: nur das Backup, nicht der Fotos-Ordner
        self.run_job("abgleich", self.app.sync)
        self.assertTrue(self.app.state["last_sync"]["ok"], self.app.state.get("last_log"))
        self.assertEqual((self.remote_repo / "p" / "pack-1").read_bytes(), (self.repo / "p" / "pack-1").read_bytes())
        self.assertFalse((self.home / self.remote_dir / "Fotos").exists())

        # umbrelOS räumt einen alten Block weg und legt einen neuen an: Spiegel zieht nach
        (self.repo / "p" / "pack-2").unlink()
        (self.repo / "p" / "pack-3").write_bytes(b"neu")
        self.run_job("abgleich", self.app.sync)
        self.assertTrue(self.app.state["last_sync"]["ok"])
        self.assertFalse((self.remote_repo / "p" / "pack-2").exists())
        self.assertEqual((self.remote_repo / "p" / "pack-3").read_bytes(), b"neu")

        # USB-Platte abgezogen: nichts bei Hetzner löschen
        shutil.move(self.external / "Extreme SSD", self.tmp / "abgezogen")
        self.run_job("abgleich", self.app.sync)
        self.assertFalse(self.app.state["last_sync"]["ok"])
        self.assertIn("unverändert", self.app.state["last_sync"]["message"])
        self.assertTrue((self.remote_repo / "p" / "pack-1").exists())
        shutil.move(self.tmp / "abgezogen", self.external / "Extreme SSD")

        # Läuft gerade ein Backup von umbrelOS, wartet der Abgleich (hier max. 2 s)
        (self.tmp / "proc" / "4242").mkdir()
        (self.tmp / "proc" / "4242" / "comm").write_text("kopia\n")
        self.assertTrue(bs.kopia_running(self.tmp / "proc"))
        self.run_job("abgleich", self.app.sync)
        self.assertTrue(self.app.state["last_sync"]["ok"])

        # Liste der Backups auf der Box und Zurückholen auf eine andere Platte
        self.run_job("liste", self.app.list_remote)
        items = self.app.state["remote_list"]["items"]
        self.assertEqual(items, [f"Extreme SSD/Umbrel/{bs.BACKUP_DIR_NAME}"])
        self.run_job("zurueckholen", self.app.restore, items[0], "Restore Disk")
        self.assertTrue(self.app.state["last_restore"]["ok"], self.app.state.get("last_log"))
        restored = list((self.external / "Restore Disk").glob(f"{bs.RESTORE_FOLDER} */{bs.BACKUP_DIR_NAME}"))
        self.assertEqual(len(restored), 1)
        self.assertEqual((restored[0] / "p" / "pack-1").read_bytes(), (self.repo / "p" / "pack-1").read_bytes())

        # Zurückholen nur für Backups aus der geladenen Liste und auf angeschlossene Platten
        self.run_job("zurueckholen", self.app.restore, "../../etc", "Restore Disk")
        self.assertFalse(self.app.state["last_restore"]["ok"])
        self.run_job("zurueckholen", self.app.restore, items[0], "../..")
        self.assertFalse(self.app.state["last_restore"]["ok"])

    def test_wrong_password(self):
        settings = self.app.settings()
        settings.password = bs.obscure(self.app.env, "falsch")
        settings.save(self.app.settings_path)
        self.run_job("test", self.app.test_connection)
        self.assertFalse(self.app.state["last_test"]["ok"])
        self.assertIn("Passwort", self.app.state["last_test"]["message"])

    def test_changed_host_key_is_rejected(self):
        self.run_job("test", self.app.test_connection)
        self.assertTrue(self.app.state["last_test"]["ok"])
        # Ein anderer Server gibt sich als die Box aus
        line = self.app.known_hosts.read_text().split()
        fake = "AAAAC3NzaC1lZDI1NTE5AAAAIBn2y8Q3o5wPq0Uoa0yQ0pK2l9e2b7mVYw6c8zj4Xo1F"
        self.app.known_hosts.write_text(f"{line[0]} ssh-ed25519 {fake}\n")
        self.run_job("test", self.app.test_connection)
        self.assertFalse(self.app.state["last_test"]["ok"])
        self.assertIn("Hostschlüssel", self.app.state["last_test"]["message"])
        # Kein Abgleich gegen einen fremden Server
        self.run_job("abgleich", self.app.sync)
        self.assertFalse(self.app.state["last_sync"]["ok"])
        self.assertFalse(self.remote_repo.exists())


if __name__ == "__main__":
    unittest.main()
