"""diskwatch's protection (mediaserver/diskwatch.py protect): downloads
paused below min_free_gb + reserve_gb, only those resumed, with room to spare.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mediaserver import common as c
from mediaserver import diskwatch as dw


class FakeClients:
    def __init__(self, downloading):
        self.down, self.stopped, self.started, self.sab = list(downloading), [], [], []
        self.answer = True

    def downloading(self):
        return [h for h in self.down if h not in self.stopped] if self.answer else None

    def torrents(self, action, hashes):
        (self.stopped if action == "stop" else self.started).extend(hashes)
        if action == "start":
            self.stopped = [h for h in self.stopped if h not in hashes]
        return self.answer

    def sabnzbd(self, mode):
        self.sab.append(mode)
        return self.answer


class Protect(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.clients = FakeClients(["a", "b"])
        for patcher in (mock.patch.object(c, "notify"), mock.patch.object(c, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def at(self, free, enabled=True):
        return dw.protect(self.dir, self.dir, 10, 20, enabled, self.clients, free_gb=free)  # type: ignore[arg-type]

    def test_paused_below_the_limit_and_resumed_with_room_to_spare(self):
        self.assertEqual(self.at(100), "")
        self.assertEqual(self.at(25), "paused")
        self.assertEqual(sorted(self.clients.stopped), ["a", "b"])
        self.assertEqual(self.clients.sab, ["pause"])
        self.assertEqual(c.read_json(self.dir / "paused.json")["resume_gb"], 40)
        self.assertEqual(self.at(35), "held")   # above the limit, not yet with room to spare
        self.assertEqual(self.at(41), "resumed")
        self.assertEqual(sorted(self.clients.started), ["a", "b"])
        self.assertEqual(self.clients.sab, ["pause", "resume"])
        self.assertFalse((self.dir / "paused.json").exists())

    def test_only_what_it_paused_is_resumed(self):
        self.clients = FakeClients(["a"])
        self.at(25)
        self.clients.down.append("new")   # started while paused: paused too
        self.at(24)
        self.assertEqual(sorted(self.clients.stopped), ["a", "new"])
        self.at(50)
        self.assertEqual(sorted(self.clients.started), ["a", "new"])   # not "b" or anything paused by hand

    def test_switched_off_resumes(self):
        self.at(25)
        self.assertEqual(self.at(25, enabled=False), "resumed")
        self.assertEqual(self.at(5, enabled=False), "")   # off: never paused

    def test_a_failed_resume_is_retried(self):
        self.at(25)
        self.clients.answer = False
        self.assertEqual(self.at(60), "held")
        self.assertTrue((self.dir / "paused.json").exists())
        self.clients.answer = True
        self.assertEqual(self.at(60), "resumed")

    def test_qbittorrent_not_answering(self):
        self.clients.answer = False
        self.assertEqual(self.at(25), "")
        self.assertFalse((self.dir / "paused.json").exists())


class ClientsCalls(unittest.TestCase):
    def test_qbittorrent_4_names(self):
        calls = []

        def request(url, method="GET", headers=None, body=None, form=None, timeout=15, follow=True):
            calls.append(url.rsplit("/", 1)[1])
            return c.Response(404 if url.endswith("/stop") else 200, {}, b"")
        with mock.patch.object(c, "request", request):
            self.assertTrue(dw.Clients(Path("/x")).torrents("stop", ["h"]))
        self.assertEqual(calls, ["stop", "pause"])   # qBittorrent 4 calls it pause


if __name__ == "__main__":
    unittest.main()
