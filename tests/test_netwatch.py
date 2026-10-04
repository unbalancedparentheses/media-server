"""Tests for mediaserver/netwatch.py against a fake Cleanuparr: retries,
following config.toml, restarts while offline, and staying out of the way
of a running install.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mediaserver import common as c
from mediaserver.netwatch import Netwatch

c.log = lambda message: None  # quiet; the tests check results, not logs


class FakeNetwatch(Netwatch):
    """Cleanuparr's queue cleaner is self.cleaner_on; put_fails makes
    changing it fail; offline makes the connection check fail"""

    def __init__(self, root: Path):
        super().__init__(root / "config", root / "state/netwatch", "http://fake", root / "state/lock")
        self.online, self.put_fails, self.cleaner_on, self.reconnects = True, False, True, 0

    def probe(self):
        return self.online

    def cleaner(self, method="GET", body=None):
        if method == "PUT":
            if self.put_fails:
                raise OSError("PUT failed")
            self.cleaner_on = (body or {})["enabled"]
        return {"enabled": self.cleaner_on}

    def after_reconnect(self):
        self.reconnects += 1


class NetwatchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.nw = FakeNetwatch(self.root)
        # What setup writes from config.toml ([cleanuparr] enabled = true)
        c.write_atomic(self.root / "state/netwatch/cleanuparr-wanted", "true\n")

    def rounds(self, n=1):
        for _ in range(n):
            self.nw.round()

    def test_failed_pause_is_retried(self):
        """Pausing fails the first time the Mac is seen offline: the next
        round retries it (it used to be tried once per transition)"""
        self.rounds()  # online
        self.nw.online, self.nw.put_fails = False, True
        self.rounds(3)
        self.assertEqual(self.nw.state, "offline")
        self.assertTrue(self.nw.cleaner_on)
        self.nw.put_fails = False
        self.rounds()
        self.assertFalse(self.nw.cleaner_on)

    def test_online_follows_config(self):
        """Online, the cleaner follows config.toml (via setup's file), even
        "off": a stale pause never turns it on against the setting"""
        wanted = self.root / "state/netwatch/cleanuparr-wanted"
        c.write_atomic(wanted, "false\n")
        self.rounds()
        self.assertFalse(self.nw.cleaner_on)
        c.write_atomic(wanted, "true\n")
        self.rounds()
        self.assertTrue(self.nw.cleaner_on)

    def test_restart_while_offline_keeps_cleaner_paused(self):
        """Restarted while offline with the cleaner paused: nothing turns it
        on before a check succeeds; coming back online resumes it once"""
        self.nw.cleaner_on, self.nw.online = False, False
        for _ in range(2):
            self.rounds()
            self.assertFalse(self.nw.cleaner_on)
        self.rounds()
        self.assertEqual(self.nw.state, "offline")
        self.nw.online = True
        self.rounds()
        self.assertTrue(self.nw.cleaner_on)
        self.assertEqual(self.nw.reconnects, 1)
        self.assertEqual(c.read_text(self.root / "state/netwatch/connection"), "online")

    def test_waits_for_running_operation(self):
        """Leaves Cleanuparr alone during an install, and runs the
        post-reconnect re-tests once the install is done"""
        self.nw.online = False
        self.rounds(3)
        self.assertFalse(self.nw.cleaner_on)
        owner = subprocess.Popen(["sleep", "30"])
        self.addCleanup(owner.kill)
        c.write_atomic(self.root / "state/lock/pid", f"{owner.pid}\n")
        self.nw.online = True
        self.rounds()
        self.assertTrue(self.nw.cleaner_on is False and self.nw.reconnects == 0)
        owner.kill()
        owner.wait()
        shutil.rmtree(self.root / "state/lock")
        self.rounds()
        self.assertTrue(self.nw.cleaner_on)
        self.assertEqual(self.nw.reconnects, 1)

    def test_blips_dont_count(self):
        self.rounds()
        self.nw.online = False
        self.rounds(2)
        self.assertEqual(self.nw.state, "online")
        self.assertTrue(self.nw.cleaner_on)


class KeyTests(unittest.TestCase):
    def test_bazarr_key_from_yaml(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        c.write_atomic(root / "bazarr/config/config.yaml",
                       "general:\n  apikey: wrong\nauth:\n  type: form\n  apikey: 'abc123'\nother:\n  apikey: x\n")
        self.assertEqual(c.bazarr_key(root), "abc123")
        self.assertEqual(c.bazarr_key(root / "missing"), "")


if __name__ == "__main__":
    unittest.main()
