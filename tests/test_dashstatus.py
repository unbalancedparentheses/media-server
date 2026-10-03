"""Tests for mediaserver/dashstatus.py: when each part is gathered, what
"attention" reports, and the uptime numbers.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mediaserver import common as c
from mediaserver.dashstatus import Collector

c.log = lambda message: None


class FakeCollector(Collector):
    def __init__(self, root: Path):
        super().__init__(root / "config", root / "state", root, 50, 10)
        self.slow_calls = self.media_calls = 0

    def slow_data(self):
        self.slow_calls += 1
        return {"disk": {"free_gb": 500, "warn_gb": 50, "min_gb": 10}}

    def media_part(self):
        self.media_calls += 1
        if self.media_calls < 3:
            return {"media_failed": ["requests_live"]}
        return {"requests_live": [], "media_failed": []}

    def system(self):
        return {}

    def playing(self):
        return []

    def downloads(self, torrents):
        return {}


class Rounds(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        patcher = mock.patch.object(c, "try_json", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failed_media_data_is_retried_a_minute_later(self):
        """Right after a restart part of the media data fails: it's asked
        again every 4th round (a minute), not after the 5-minute slow round,
        and the slow round (which records uptime samples) isn't repeated"""
        col, out = FakeCollector(self.root), self.root / "status.json"
        for _ in range(12):
            col.round(out)
        self.assertEqual(col.slow_calls, 1)
        self.assertEqual(col.media_calls, 3)  # rounds 0 and 4 fail, 8 works, then it waits
        status = json.loads(out.read_text())
        self.assertEqual(status["requests_live"], [])
        self.assertLess(time.time() - status["updated"], 5)


class Attention(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.col = Collector(self.root / "config", self.root / "state", self.root, 50, 10)

    def texts(self, fast=None, slow=None, torrents=()):
        return [a["text"] for a in self.col.attention(fast or {}, slow or {}, list(torrents))]

    def test_disk(self):
        self.assertIn("Only 5 GB free: imports have stopped", self.texts(slow={"disk": {"free_gb": 5, "warn_gb": 50, "min_gb": 10}}))
        self.assertIn("40 GB free on the media disk", self.texts(slow={"disk": {"free_gb": 40, "warn_gb": 50, "min_gb": 10}}))
        self.assertEqual(self.texts(slow={"disk": {"free_gb": 400, "warn_gb": 50, "min_gb": 10}}), [])

    def test_offline_and_rejections(self):
        c.write_atomic(self.root / "state/netwatch/connection", "offline\n")
        c.write_json(self.root / "state/postimport/status.json", {"looking": [{"title": "Film", "reason": "it's dubbed"}]})
        texts = self.texts()
        self.assertTrue(any("offline" in t for t in texts))
        self.assertIn("Rejected Film: it's dubbed", texts)

    def test_stuck_torrents_and_burned_in_subtitles(self):
        old = time.time() - 2 * 86400
        torrents = [{"progress": 0.2, "state": "stalledDL", "added_on": old, "name": "Show S01", "num_seeds": 0},
                    {"progress": 0.2, "state": "stalledDL", "added_on": time.time(), "name": "New", "num_seeds": 0}]
        fast = {"playing": [{"method": "Transcode", "reasons": ["SubtitleCodecNotSupported"], "user": "ana", "title": "Film"}]}
        texts = self.texts(fast=fast, torrents=torrents)
        self.assertEqual(sum("Not moving" in t for t in texts), 1)
        self.assertTrue(any("burned into the video" in t for t in texts))


class Uptime(unittest.TestCase):
    def test_percent_ignores_unknown_samples(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        hist = [{"t": i, "up": {"Byparr": v, "Jellyfin": True}} for i, v in enumerate([True, None, None, False])]
        c.write_json(root / "dashstatus/uptime.json", hist)
        col = Collector(root, root, root, 50, 10)
        with mock.patch("mediaserver.dashstatus.UPTIME_CHECKS", [("Byparr", "x"), ("Jellyfin", "y")]), \
                mock.patch.object(c, "status_code", return_value=200):
            up = {u["name"]: u for u in col.uptime()}
        self.assertEqual(up["Byparr"]["pct"], 66)  # True, False, True (new sample); None skipped
        self.assertEqual(up["Jellyfin"]["pct"], 100)
        self.assertTrue(up["Byparr"]["up_now"])
        self.assertEqual(len(json.loads((root / "dashstatus/uptime.json").read_text())), 5)


if __name__ == "__main__":
    unittest.main()


class DiskWatch(unittest.TestCase):
    """mediaserver/diskwatch.py: warns below the threshold, at most every 6 hours"""

    def test_warns_then_waits(self):
        from mediaserver import diskwatch
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        shown = []
        with mock.patch.object(c, "notify", lambda t, m, sound=None: shown.append(m)):
            t = 1_800_000_000
            self.assertFalse(diskwatch.check(root, root, 0, 0, now=t))            # plenty of space
            self.assertTrue(diskwatch.check(root, root, 10 ** 9, 10, now=t))      # "low"
            self.assertFalse(diskwatch.check(root, root, 10 ** 9, 10, now=t + 60))  # too soon
            self.assertTrue(diskwatch.check(root, root, 10 ** 9, 10, now=t + 6 * 3600))
        self.assertEqual(len(shown), 2)
