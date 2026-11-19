import importlib.util
import json
import pathlib
import sys
import threading
import unittest
import urllib.request

SPEC = importlib.util.spec_from_file_location(
    "waechter", pathlib.Path(__file__).resolve().parent.parent / "image" / "waechter.py"
)
w = importlib.util.module_from_spec(SPEC)
sys.modules["waechter"] = w
SPEC.loader.exec_module(w)

ROOT = "/home/umbrel/umbrel/network"

# Echte mountinfo-Zeilen im Format des Kernels, inkl. Leerzeichen (\040) im Pfad
MOUNTINFO_HEALTHY = "\n".join(
    [
        "22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw",
        "95 22 0:48 / /home/umbrel/umbrel/network/u1.your-storagebox.de/backup rw,relatime shared:60 - cifs //u1.your-storagebox.de/backup rw,vers=3.1.1",
        "96 22 0:49 / /home/umbrel/umbrel/network/nas.local/Backups\\040(Umbrel) rw,relatime shared:61 - smb3 //nas.local/Backups\\040(Umbrel) rw",
        "97 22 0:50 / /mnt/anderes rw,relatime shared:62 - cifs //fremd/share rw",
        "98 22 0:51 / /home/umbrel/umbrel/network/nas.local/nfs rw - nfs4 nas.local:/x rw",
    ]
)
BOX = "/home/umbrel/umbrel/network/u1.your-storagebox.de/backup"
NAS = "/home/umbrel/umbrel/network/nas.local/Backups (Umbrel)"


class FakeHost:
    """Simuliert den Host: Mount-Tabelle und Antworten der Freigaben."""

    def __init__(self, mountinfo):
        self.mountinfo = mountinfo
        self.dead = set()
        self.calls = []

    def read(self):
        return self.mountinfo

    def run(self, args, timeout):
        self.calls.append(args)
        if args[0] == "ls":
            path = args[-1]
            return (False, "ls: cannot access: Host is down") if path in self.dead else (True, "")
        if args[0] == "umount":
            path = args[-1]
            # Zeile mit diesem Mountpunkt aus der Tabelle entfernen
            escaped = path.replace(" ", "\\040")
            self.mountinfo = "\n".join(l for l in self.mountinfo.splitlines() if f" {escaped} " not in l)
            return True, ""
        return True, ""


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


def make(dry_run=False):
    host = FakeHost(MOUNTINFO_HEALTHY)
    clock = Clock()
    cfg = w.Config(network_root=ROOT, fail_threshold=3, remount_warn=600, dry_run=dry_run)
    watcher = w.Watcher(cfg, read_mountinfo=host.read, run_on_host=host.run, clock=clock)
    return watcher, host, clock


class ParseTests(unittest.TestCase):
    def test_only_smb_below_network_root(self):
        mounts = w.parse_mountinfo(MOUNTINFO_HEALTHY, ROOT)
        self.assertEqual(set(mounts), {BOX, NAS})
        self.assertEqual(mounts[NAS]["source"], "//nas.local/Backups (Umbrel)")

    def test_stacked_layers_counted(self):
        line = MOUNTINFO_HEALTHY.splitlines()[1]
        mounts = w.parse_mountinfo(line + "\n" + line.replace("95 22", "99 95"), ROOT)
        self.assertEqual(mounts[BOX]["layers"], 2)

    def test_optional_fields_and_garbage(self):
        text = "100 22 0:52 / /home/umbrel/umbrel/network/h/s rw shared:1 master:2 - cifs //h/s rw\nkaputt\n"
        self.assertIn("/home/umbrel/umbrel/network/h/s", w.parse_mountinfo(text, ROOT))

    def test_root_prefix_must_be_directory(self):
        text = "100 22 0:52 / /home/umbrel/umbrel/network-alt/h/s rw - cifs //h/s rw"
        self.assertEqual(w.parse_mountinfo(text, ROOT), {})


class WatcherTests(unittest.TestCase):
    def test_healthy_shares_stay_mounted(self):
        watcher, host, _ = make()
        for _ in range(5):
            watcher.cycle()
        self.assertFalse([c for c in host.calls if c[0] == "umount"])
        self.assertEqual({s.status for s in watcher.shares.values()}, {"ok"})

    def test_short_outage_does_not_detach(self):
        watcher, host, _ = make()
        watcher.cycle()
        host.dead.add(BOX)
        watcher.cycle()
        watcher.cycle()
        host.dead.clear()  # Kernel hat selbst wieder verbunden
        watcher.cycle()
        self.assertFalse([c for c in host.calls if c[0] == "umount"])
        self.assertEqual(watcher.shares[BOX].status, "ok")

    def test_dead_share_detached_then_remounted(self):
        watcher, host, clock = make()
        watcher.cycle()
        host.dead.add(BOX)
        for _ in range(3):
            clock.now += 60
            watcher.cycle()
        self.assertIn(["umount", "-l", "--", BOX], host.calls)
        self.assertEqual(watcher.shares[BOX].status, "ausgehaengt")
        self.assertEqual(watcher.shares[NAS].status, "ok")  # andere Freigabe unberührt

        clock.now += 60
        watcher.cycle()  # Mount ist weg, wartet auf umbrelOS
        self.assertFalse(watcher.shares[BOX].mounted)
        self.assertEqual(watcher.shares[BOX].status, "ausgehaengt")

        # umbrelOS hängt neu ein
        host.dead.clear()
        host.mountinfo = MOUNTINFO_HEALTHY
        clock.now += 60
        watcher.cycle()
        self.assertEqual(watcher.shares[BOX].status, "ok")
        self.assertEqual(watcher.shares[BOX].detach_count, 1)
        self.assertTrue(any("wieder eingehängt" in e.message for e in watcher.events))

    def test_warns_when_umbrel_cannot_remount(self):
        watcher, host, clock = make()
        watcher.cycle()
        host.dead.add(BOX)
        for _ in range(3):
            clock.now += 60
            watcher.cycle()
        for _ in range(11):  # 11 Minuten ohne Wiedereinhängen
            clock.now += 60
            watcher.cycle()
        self.assertEqual(watcher.shares[BOX].status, "wartet")
        self.assertTrue(any("Port 445" in e.message for e in watcher.events))

    def test_space_in_path_is_passed_as_one_argument(self):
        watcher, host, clock = make()
        watcher.cycle()
        host.dead.add(NAS)
        for _ in range(3):
            clock.now += 60
            watcher.cycle()
        self.assertIn(["umount", "-l", "--", NAS], host.calls)

    def test_stacked_mounts_all_detached(self):
        watcher, host, clock = make()
        line = MOUNTINFO_HEALTHY.splitlines()[1]
        host.mountinfo = line + "\n" + line.replace("95 22", "99 95")
        calls = []

        def run(args, timeout):
            calls.append(args)
            return (False, "Host is down") if args[0] == "ls" else (True, "")

        watcher.run_on_host = run
        for _ in range(3):
            watcher.cycle()
        self.assertEqual(calls.count(["umount", "-l", "--", BOX]), 2)

    def test_dry_run_never_unmounts(self):
        watcher, host, clock = make(dry_run=True)
        host.dead.add(BOX)
        for _ in range(9):
            clock.now += 60
            watcher.cycle()
        self.assertFalse([c for c in host.calls if c[0] == "umount"])
        self.assertTrue(any("Testmodus" in e.message for e in watcher.events))

    def test_share_removed_in_umbrel_is_forgotten_after_a_day(self):
        watcher, host, clock = make()
        watcher.cycle()
        host.mountinfo = "\n".join(l for l in MOUNTINFO_HEALTHY.splitlines() if "nas.local/Backups" not in l)
        clock.now += 60
        watcher.cycle()
        self.assertEqual(watcher.shares[NAS].status, "nicht_eingehaengt")
        clock.now += 86400
        watcher.cycle()
        self.assertNotIn(NAS, watcher.shares)

    def test_self_test_reports_missing_privileges(self):
        watcher, host, _ = make()
        watcher.run_on_host = lambda args, timeout: (False, "nsenter: Operation not permitted")
        self.assertFalse(watcher.self_test())
        self.assertIn("privileged", watcher.snapshot()["problems"][0])


class RunCommandTests(unittest.TestCase):
    def test_timeout_kills_hanging_command(self):
        ok, error = w.run_command(["sleep", "30"], timeout=1)
        self.assertFalse(ok)
        self.assertIn("keine Antwort", error)

    def test_error_message_from_stderr(self):
        ok, error = w.run_command(["ls", "/gibt/es/nicht"], timeout=5)
        self.assertFalse(ok)
        self.assertIn("/gibt/es/nicht", error)

    def test_success(self):
        self.assertEqual(w.run_command(["true"], timeout=5), (True, ""))


class StatusPageTests(unittest.TestCase):
    def test_page_and_json(self):
        watcher, host, clock = make()
        watcher.cycle()
        host.dead.add(BOX)
        watcher.cycle()
        server = w.ThreadingHTTPServer(("127.0.0.1", 0), w.make_handler(watcher))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
            self.assertIn("//u1.your-storagebox.de/backup", page)
            self.assertIn("Antwortet nicht (1/3)", page)
            self.assertIn("Backups (Umbrel)", page)
            data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/status.json").read())
            self.assertEqual(len(data["shares"]), 2)
        finally:
            server.shutdown()

    def test_html_is_escaped(self):
        watcher, host, _ = make()
        watcher.log("<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)", w.render_html(watcher.snapshot()))


if __name__ == "__main__":
    unittest.main()
