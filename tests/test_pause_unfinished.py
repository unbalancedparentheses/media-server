"""Pausing searches or repairs (mediaserver/pause.py, its endpoint, where
it's obeyed) and the unfinished-work list (mediaserver/unfinished.py).

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mediaserver import common as c
from mediaserver import control, pause, postimport, stuck, unfinished


class Pause(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)

    def test_pause_and_resume_on_its_own(self):
        self.assertFalse(pause.paused(self.state, "searches"))
        pause.set_pause(self.state, "searches", 1000)
        self.assertTrue(pause.paused(self.state, "searches", now=999))
        self.assertFalse(pause.paused(self.state, "searches", now=1000))   # its time came
        self.assertFalse(pause.paused(self.state, "repairs", now=999))
        self.assertEqual(pause.status(self.state, now=10), {"searches": 1000})
        pause.set_pause(self.state, "searches", 0)
        self.assertEqual(pause.status(self.state, now=10), {})

    def test_next_morning(self):
        evening = time.mktime((2026, 10, 6, 22, 0, 0, 0, 0, -1))
        early = time.mktime((2026, 10, 6, 5, 0, 0, 0, 0, -1))
        self.assertEqual(time.localtime(pause.next_morning(evening))[2:4], (7, 7))
        self.assertEqual(time.localtime(pause.next_morning(early))[2:4], (6, 7))

    def test_parse(self):
        what, when = control.parse_pause(b'{"what": "searches", "for": "24h"}', now=100)
        self.assertEqual((what, when), ("searches", 100 + 86400))
        self.assertEqual(control.parse_pause(b'{"what": "repairs", "for": "resume"}'), ("repairs", 0))
        for bad in (b'{"what": "downloads", "for": "24h"}', b'{"what": "searches", "for": "forever"}', b"x", b"[]"):
            self.assertIsInstance(control.parse_pause(bad), str)

    def test_searches_paused_ask_the_indexers_nothing(self):
        stuck_file = self.state / "postimport/stuck.json"
        stuck_file.parent.mkdir()
        pause.set_pause(self.state, "searches", time.time() + 3600)
        app = mock.Mock(name="Radarr")
        self.assertIsNone(stuck.run([app], {}, stuck_file, offline=False))
        app.call.assert_not_called()

    def test_repairs_paused_fix_nothing_and_mark_nothing(self):
        pause.set_pause(self.state, "repairs", time.time() + 3600)
        worker = postimport.Worker([], dict(postimport.DEFAULTS), {})
        with mock.patch.object(postimport, "STATE", self.state / "postimport"):
            self.assertFalse(worker.fix(Path("/m/Film.mkv"), {"streams": []}, "Film"))
            worker.sweep()
        self.assertEqual(worker.state["seen"], {})   # fixed once repairs resume


class Unfinished(unittest.TestCase):
    def test_everything_left_half_done(self):
        state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, state)
        now = 10_000
        c.write_json(state / "postimport/state.json", {
            "repackage_pending": {"/m/Film (2010).mp4": {"title": "Film (2010)", "steps": ["rescan", "jellyfin"]}},
            "failures": {"/m/Show - S01E01 - Pilot Bluray-1080p.mkv": {"count": 2, "last": now - 600, "what": ["stereo audio"]}}})
        c.write_json(state / "deletions.json", {"jf1": {"title": "Old Film (1999)", "steps": ["torrents", "seerr"]},
                                                "sonarr9": {"title": "Kaiji season 2", "steps": ["unmonitor"], "stop": True}})
        c.write_json(state / "diskwatch/paused.json", {"free_gb": 25, "resume_gb": 40})
        items = unfinished.collect(state, now=now)
        whats = [i["what"] for i in items]
        self.assertEqual(whats, ["Repackaging Film (2010) as MP4", "Fixing Show - S01E01 - Pilot", "Deleting Old Film (1999)",
                                 "Stopping looking for Kaiji season 2", "Downloads paused for space"])
        self.assertEqual(items[0]["left"], "Sonarr/Radarr rescan, tell Jellyfin")
        self.assertIn("failed 2×", items[1]["error"])
        self.assertEqual(items[4]["left"], "resume above 40 GB free")
        self.assertEqual(unfinished.collect(Path("/nonexistent")), [])


class Endpoint(unittest.TestCase):
    def test_pause_through_the_control_server(self):
        import http.client
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "config").mkdir()
        (root / ".state").mkdir()
        server = control.serve(root / "config", 0)   # real objects: /pause asks no service
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def ask(method, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request(method, "/pause", body=body, headers={"Content-Type": "application/json", "X-Requested-With": "x"})
            r = conn.getresponse()
            data = json.loads(r.read())
            conn.close()
            return r.status, data
        status, data = ask("POST", json.dumps({"what": "repairs", "for": "morning"}))
        self.assertEqual(status, 200)
        self.assertIn("repairs", data["paused"])
        self.assertIn("repairs", ask("GET")[1]["paused"])
        self.assertEqual(ask("POST", json.dumps({"what": "repairs", "for": "resume"}))[1]["paused"], {})
        self.assertEqual(ask("POST", json.dumps({"what": "nope", "for": "24h"}))[0], 400)


if __name__ == "__main__":
    unittest.main()


class Orphans(unittest.TestCase):
    def test_leftovers_reported_never_deleted(self):
        from mediaserver import doctor
        from tests.test_integration import example_config
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        cfg = example_config(root)
        done = cfg.paths.downloads / "torrents/complete/radarr"
        (done / "Owned.2020").mkdir(parents=True)
        (done / "Leftover.2019").mkdir()
        (done / "Leftover.2019/film.mkv").write_bytes(b"x" * 2048)
        torrents = [{"content_path": str(done / "Owned.2020"), "save_path": str(done), "name": "Owned.2020"}]
        d = doctor.Doctor(cfg)
        with mock.patch.object(c, "request", return_value=c.Response(200, {}, json.dumps(torrents).encode())):
            d.orphans()
        self.assertEqual(len(d.notes), 1)
        self.assertIn("1 item(s) in the downloads folder no torrent owns", d.notes[0][0])
        self.assertIn("Leftover.2019", d.notes[0][0])
        self.assertTrue((done / "Leftover.2019/film.mkv").exists())   # only reported
        d2 = doctor.Doctor(cfg)
        with mock.patch.object(c, "request", return_value=c.Response(0, {}, b"")):
            d2.orphans()   # qBittorrent not answering: nothing said
        self.assertEqual(d2.notes, [])


class Leaks(unittest.TestCase):
    def test_a_served_secret_is_found(self):
        from mediaserver import leaks
        from tests.test_integration import example_config
        cfg = example_config(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, cfg.paths.media)
        served = {"/status.json": b'{"sonarr": {"apiKey": "abcdef123456"}}'}

        def request(url, *a, **kw):
            path = url.split("localhost", 1)[1] or "/"
            return c.Response(200, {}, served.get(path, b"<html></html>"))
        with mock.patch.object(leaks, "secrets", return_value={"sonarr API key": "abcdef123456", "Jellyfin password": "admin-pass"}), \
                mock.patch.object(c, "request", request), mock.patch.dict("os.environ", {"MEDIASERVER_URL_DASHBOARD": "http://localhost"}):
            self.assertEqual(leaks.find(cfg), ["sonarr API key in /status.json"])
            served.clear()
            self.assertEqual(leaks.find(cfg), [])
