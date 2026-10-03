"""netwatch and diskwatch beyond their rounds: probing the connection,
the re-tests after reconnecting, and their main loops.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from mediaserver import common as c
from mediaserver import diskwatch, netwatch


class Scratch(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.logs: list[str] = []
        patcher = mock.patch.object(c, "log", self.logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)


class Probe(Scratch):
    def watcher(self):
        return netwatch.Netwatch(self.dir, self.dir / "state", "http://127.0.0.1:1", self.dir / "lock")

    def test_simulated_offline(self):
        flag = self.dir / "offline"
        with mock.patch.dict(os.environ, {"NETWATCH_SIMULATE_OFFLINE": str(flag)}):
            self.assertTrue(self.watcher().probe())
            flag.touch()
            self.assertFalse(self.watcher().probe())

    def test_any_probe_answering_is_online(self):
        answers = iter([OSError("down"), b"ok"])

        def http(url, timeout=10):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        with mock.patch.dict(os.environ, {"NETWATCH_SIMULATE_OFFLINE": ""}), mock.patch.object(c, "http", http):
            self.assertTrue(self.watcher().probe())
        with mock.patch.dict(os.environ, {"NETWATCH_SIMULATE_OFFLINE": ""}), \
                mock.patch.object(c, "http", side_effect=urllib.error.URLError("no route")):
            self.assertFalse(self.watcher().probe())

    def test_no_cleanuparr_key_is_retried(self):
        self.assertFalse(self.watcher().set_cleaner(True))


class Reconnect(Scratch):
    def test_retests_indexers_and_clears_bazarr(self):
        calls = []

        def http(url, headers=None, method="GET", body=None, timeout=15, form=None):
            calls.append(url)
            if "sonarr" in url or ":8989" in url:
                raise urllib.error.HTTPError(url, 400, "an indexer still fails", {}, None)  # type: ignore[arg-type]
            if "radarr" in url or ":7878" in url:
                raise OSError("down")
            return b""
        pause = threading.Event()
        with mock.patch.object(c, "http", http), mock.patch.object(c, "arr_key", return_value="k"), \
                mock.patch.object(c, "bazarr_key", return_value="b"):
            netwatch.Netwatch(self.dir, self.dir, "http://x", self.dir / "lock").after_reconnect()
            # The re-tests run in the background
            for _ in range(100):
                if len(self.logs) >= 4:
                    break
                pause.wait(0.05)
        self.assertEqual(len([u for u in calls if "testall" in u]), 3)
        self.assertTrue(any("/api/providers?apikey=b" in u for u in calls))
        self.assertIn("re-tested Prowlarr's indexers", self.logs)
        self.assertIn("re-tested Sonarr's indexers", self.logs)   # a 400 still means it ran
        self.assertIn("couldn't re-test Radarr's indexers", self.logs)
        self.assertIn("cleared Bazarr's provider throttling", self.logs)


class Loops(Scratch):
    def test_netwatch_main(self):
        state = self.dir / "netwatch"
        state.mkdir()
        (state / "cleanuparr-paused").touch()
        rounds = []
        with mock.patch.dict(os.environ, {"NETWATCH_CONFIG": str(self.dir), "NETWATCH_STATE": str(state)}), \
                mock.patch.object(netwatch.Netwatch, "round", lambda nw: rounds.append(1)), \
                mock.patch.object(netwatch.time, "sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            netwatch.main()
        self.assertEqual(rounds, [1])
        self.assertFalse((state / "cleanuparr-paused").exists())

    def test_diskwatch_main(self):
        with mock.patch.dict(os.environ, {"MEDIA_DIR": str(self.dir), "DISK_WARN_GB": "1000000000"}), \
                mock.patch.object(c, "notify") as notify, \
                mock.patch.object(diskwatch.time, "sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            diskwatch.main()
        notify.assert_called_once()
        self.assertTrue((self.dir / ".state/diskwatch/last-warning").exists())

    def test_unreadable_last_warning_warns_again(self):
        (self.dir / "last-warning").write_text("garbage")
        with mock.patch.object(c, "notify") as notify:
            self.assertTrue(diskwatch.check(self.dir, self.dir, 10 ** 9, 10))
        notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
