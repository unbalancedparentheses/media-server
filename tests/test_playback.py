# Tests use possibly-None results directly (a None fails the test anyway):
# pyright: reportOptionalSubscript=false
"""Playback verified, separate from imported (mediaserver/playback.py),
against the fake Jellyfin over HTTP.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mediaserver import playback
from tests.fakes import FakeJellyfin


def source(path, *streams, direct=True):
    return {"MediaSources": [{"Id": "src", "Path": path, "SupportsDirectPlay": direct, "MediaStreams": list(streams)}]}


VIDEO = {"Type": "Video", "Codec": "h264"}
AAC = {"Type": "Audio", "Codec": "aac", "Language": "jpn"}
SUBS = {"Type": "Subtitle", "Codec": "subrip", "Language": "eng", "IsTextSubtitleStream": True}


class Verify(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "state/dashstatus").mkdir(parents=True)
        (self.dir / "state/dashstatus/jellyfin-key").write_text("key")
        self.film = self.dir / "Film.mkv"
        self.film.write_bytes(b"x")
        self.jf = FakeJellyfin()
        self.jf.start()
        self.addCleanup(self.jf.stop)
        self.jf.add_user("admin", "p")
        self.jf.items = [{"Id": "i1", "Type": "Movie", "Path": str(self.film)}]
        patcher = mock.patch.object(playback, "local", lambda name: self.jf.url)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(playback.c, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self):
        return playback.verify(playback.Jellyfin(self.dir / "state"), str(self.film))

    def test_verified(self):
        self.jf.playback["i1"] = source(str(self.film), VIDEO, AAC, SUBS)
        self.assertEqual(self.check(), {"status": "verified", "detail": "plays directly: h264 · aac · subtitles eng"})

    def test_failures_say_why(self):
        self.jf.playback["i1"] = {"ErrorCode": "NoCompatibleStream"}
        self.assertEqual(self.check()["detail"], "Jellyfin can't open it (NoCompatibleStream)")
        self.jf.playback["i1"] = source(str(self.film), VIDEO)
        self.assertEqual(self.check()["detail"], "Jellyfin finds no audio track it can read")
        self.jf.playback["i1"] = source(str(self.film), VIDEO, AAC)
        self.jf.streams["i1"] = 500
        self.assertEqual(self.check()["detail"], "the stream didn't start (HTTP 500)")

    def test_not_listed_yet_then_a_failure_after_two_hours(self):
        self.jf.items = []
        self.assertIsNone(self.check())
        state: dict = {}
        playback.queue(state, self.film, "Film", now=1000)
        playback.run(state, self.dir / "state", now=1000 + 600)
        self.assertIn(str(self.film), state["playback_pending"])   # still waiting
        playback.run(state, self.dir / "state", now=1000 + playback.NOT_LISTED_AFTER + 1)
        self.assertEqual(state["playback_pending"], {})
        self.assertEqual(state["playback"][0]["detail"], "Jellyfin hasn't listed it (is the library scan stuck?)")

    def test_run_records_results_and_forgets_deleted_files(self):
        self.jf.playback["i1"] = source(str(self.film), VIDEO, AAC)
        state: dict = {}
        playback.queue(state, self.film, "Film")
        playback.queue(state, self.dir / "gone.mkv", "Gone")
        playback.run(state, self.dir / "state")
        self.assertEqual([(r["title"], r["status"]) for r in state["playback"]], [("Film", "verified")])
        self.assertEqual(state["playback_pending"], {})

    def test_jellyfin_down_waits(self):
        self.jf.stop()
        state: dict = {}
        playback.queue(state, self.film, "Film")
        playback.run(state, self.dir / "state")
        self.assertIn(str(self.film), state["playback_pending"])


class Dashboard(unittest.TestCase):
    def test_ready_says_playback_verified_or_the_problem(self):
        from mediaserver import dashmedia as dm
        stages = [{"name": "ready", "state": "done", "detail": ""}]
        ok = [{"path": "/m/Film (2020)/Film.mkv", "status": "verified", "title": "Film"}]
        self.assertEqual(dm.with_playback([dict(s) for s in stages], "/m/Film (2020)", ok)[0]["detail"], "playback verified")
        bad = [{"path": "/m/Film (2020)/Film.mkv", "status": "failed", "title": "Film", "detail": "the stream didn't start"}] + ok
        st = dm.with_playback([dict(s) for s in stages], "/m/Film (2020)", bad)[0]
        self.assertEqual((st["state"], st["detail"]), ("problem", "Film: the stream didn't start"))
        # Another folder's results don't count (a prefix isn't enough)
        self.assertEqual(dm.with_playback([dict(s) for s in stages], "/m/Film", bad)[0]["detail"], "")


if __name__ == "__main__":
    unittest.main()
