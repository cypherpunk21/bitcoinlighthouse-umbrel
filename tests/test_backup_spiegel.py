import importlib.util
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

SPEC = importlib.util.spec_from_file_location(
    "backup_spiegel", pathlib.Path(__file__).resolve().parent.parent / "image-backup-spiegel" / "backup_spiegel.py"
)
bs = importlib.util.module_from_spec(SPEC)
sys.modules["backup_spiegel"] = bs
SPEC.loader.exec_module(bs)


def make_app(tmp: pathlib.Path) -> "bs.App":
    (tmp / "external").mkdir(exist_ok=True)
    (tmp / "proc").mkdir(exist_ok=True)
    env = bs.Env(data_dir=tmp / "data", external_root=tmp / "external", host_proc=tmp / "proc", rclone="false")
    return bs.App(env)


class FindBackupsTests(unittest.TestCase):
    def test_finds_umbrel_backups_and_ignores_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            good = root / "Platte A" / "Ordner" / bs.BACKUP_DIR_NAME
            good.mkdir(parents=True)
            (good / bs.REPO_MARKER).write_text("x")
            (root / "Platte A" / "Leer" / bs.BACKUP_DIR_NAME).mkdir(parents=True)  # ohne Marker
            (root / "Platte B" / ".versteckt" / bs.BACKUP_DIR_NAME).mkdir(parents=True)
            deep = root / "Platte C" / "1" / "2" / "3" / "4" / "5" / bs.BACKUP_DIR_NAME
            deep.mkdir(parents=True)
            (deep / bs.REPO_MARKER).write_text("x")
            found = bs.find_backups(root)
            self.assertEqual([b.rel for b in found], [f"Platte A/Ordner/{bs.BACKUP_DIR_NAME}"])
            self.assertEqual(found[0].drive, "Platte A")
            self.assertEqual(bs.list_drives(root), ["Platte A", "Platte B", "Platte C"])

    def test_missing_external_root(self):
        self.assertEqual(bs.find_backups(pathlib.Path("/gibt/es/nicht")), [])
        self.assertEqual(bs.list_drives(pathlib.Path("/gibt/es/nicht")), [])

    def test_kopia_running(self):
        with tempfile.TemporaryDirectory() as d:
            proc = pathlib.Path(d)
            (proc / "1").mkdir()
            (proc / "1" / "comm").write_text("systemd\n")
            self.assertFalse(bs.kopia_running(proc))
            (proc / "77").mkdir()
            (proc / "77" / "comm").write_text("kopia\n")
            self.assertTrue(bs.kopia_running(proc))


class SettingsTests(unittest.TestCase):
    def test_validation(self):
        good = {"host": "u123456.your-storagebox.de", "user": "u123456-sub1", "port": "23",
                "remote_dir": "umbrel-backup", "time": "03:30", "bwlimit": "5M"}
        self.assertEqual(bs.validate(good), [])
        for key, bad in [("host", "evil,host=x"), ("user", "a b"), ("port", "99999"), ("remote_dir", "../x"),
                         ("remote_dir", "a:b"), ("time", "25:00"), ("bwlimit", "viel")]:
            self.assertTrue(bs.validate({**good, key: bad}), f"{key}={bad} hätte abgelehnt werden müssen")

    def test_settings_file_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "data" / "settings.json"
            bs.Settings(host="h", user="u", password="obscured").save(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertTrue(bs.Settings.load(path).configured)

    def test_remote_string(self):
        s = bs.Settings(host="u1.your-storagebox.de", user="u1", port=23, remote_dir="umbrel-backup")
        r = bs.remote(s, pathlib.Path("/data/known_hosts"), "Platte/Umbrel Backup.backup")
        self.assertEqual(
            r,
            ":sftp,host=u1.your-storagebox.de,user=u1,port=23,shell_type=none,"
            "known_hosts_file=/data/known_hosts:umbrel-backup/Platte/Umbrel Backup.backup",
        )
        self.assertNotIn("password", r)


class ScheduleTests(unittest.TestCase):
    def test_due_once_per_day_after_time(self):
        with tempfile.TemporaryDirectory() as d:
            app = make_app(pathlib.Path(d))
            bs.Settings(host="h", user="u", password="p", time="03:30").save(app.settings_path)
            before = time.strptime("2026-11-20 03:29", "%Y-%m-%d %H:%M")
            after = time.strptime("2026-11-20 03:31", "%Y-%m-%d %H:%M")
            self.assertFalse(app.due(before))
            self.assertTrue(app.due(after))
            app.state["last_scheduled_date"] = "2026-11-20"
            self.assertFalse(app.due(after))
            self.assertTrue(app.due(time.strptime("2026-11-21 04:00", "%Y-%m-%d %H:%M")))

    def test_not_due_without_settings(self):
        with tempfile.TemporaryDirectory() as d:
            app = make_app(pathlib.Path(d))
            self.assertFalse(app.due(time.strptime("2026-11-20 12:00", "%Y-%m-%d %H:%M")))

    def test_sync_refuses_without_backup_on_usb(self):
        with tempfile.TemporaryDirectory() as d:
            app = make_app(pathlib.Path(d))
            bs.Settings(host="h", user="u", password="p").save(app.settings_path)
            app.known_hosts.write_text("[h]:23 ssh-ed25519 AAAA\n")
            app.sync()
            self.assertFalse(app.state["last_sync"]["ok"])
            self.assertIn("unverändert", app.state["last_sync"]["message"])


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = make_app(pathlib.Path(self.tmp.name))
        self.server = bs.ThreadingHTTPServer(("127.0.0.1", 0), bs.make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def post(self, path, data: str, origin=None):
        req = urllib.request.Request(self.base + path, data=data.encode(), method="POST")
        if origin:
            req.add_header("Origin", origin)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            return opener.open(req)
        except urllib.error.HTTPError as error:
            return error

    def test_page_renders_and_explains_missing_backup(self):
        page = urllib.request.urlopen(self.base + "/").read().decode()
        self.assertIn("Backup-Spiegel", page)
        self.assertIn("Kein Umbrel-Backup auf einer USB-Platte gefunden", page)
        status = json.loads(urllib.request.urlopen(self.base + "/status.json").read())
        self.assertIsNone(status["running"])

    def test_foreign_origin_is_rejected(self):
        response = self.post("/aktion/abgleich", "", origin="https://evil.example")
        self.assertEqual(response.status, 403)

    def test_invalid_settings_are_not_saved(self):
        response = self.post("/aktion/speichern", "host=bad,host&user=u&port=23&remote_dir=x&time=03:30&password=p")
        self.assertEqual(response.status, 400)
        self.assertFalse(self.app.settings_path.exists())

    def test_password_is_never_shown(self):
        bs.Settings(host="h", user="u", password="GEHEIM-OBSCURED").save(self.app.settings_path)
        page = urllib.request.urlopen(self.base + "/").read().decode()
        self.assertNotIn("GEHEIM-OBSCURED", page)
        self.assertIn("gespeichert, leer lassen", page)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main()
