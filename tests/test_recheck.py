"""Re-checking old files at night (postimport Worker.recheck) and replacing
a damaged one from the dashboard (control Library.replace): only files the
re-check flagged, through Radarr/Sonarr.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mediaserver import common as c
from mediaserver import control
from mediaserver import postimport as pi

pi.log = lambda message: None
NIGHT = time.mktime((2026, 10, 7, 3, 0, 0, 0, 0, -1))


class Recheck(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        movies = self.dir / "movies"
        (movies / "Good (2001)").mkdir(parents=True)
        (movies / "Bad (2002)").mkdir()
        self.good = movies / "Good (2001)/Good (2001).mkv"
        self.bad = movies / "Bad (2002)/Bad (2002).mkv"
        self.good.write_bytes(b"g")
        self.bad.write_bytes(b"b")
        self.worker = pi.Worker([], dict(pi.DEFAULTS, library_dirs=[str(movies)]), {})
        self.checked: list = []

        def problem(info, path, minutes, japanese):
            self.checked.append(Path(path).name)
            return "the video is damaged" if Path(path) == self.bad else None
        for patcher in (mock.patch.object(pi, "STATE", self.dir / "postimport"), mock.patch.object(pi, "probe", return_value={}),
                        mock.patch.object(pi, "problem_with", problem), mock.patch.object(pi, "operation_running", return_value=False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_damaged_found_listed_and_nothing_deleted(self):
        self.worker.recheck(now=NIGHT)
        self.assertEqual(sorted(self.checked), ["Bad (2002).mkv", "Good (2001).mkv"])
        self.assertEqual(list(self.worker.state["damaged"]), [str(self.bad)])
        self.assertTrue(self.bad.exists())   # only reported
        damaged = self.worker.status()["damaged"]
        self.assertEqual((damaged[0]["title"], damaged[0]["problem"]), ("Bad (2002)", "the video is damaged"))

    def test_once_a_month_at_night_only(self):
        self.worker.recheck(now=NIGHT + 12 * 3600)   # afternoon
        self.assertEqual(self.checked, [])
        self.worker.recheck(now=NIGHT)
        self.worker.recheck(now=NIGHT + 86400)        # next night: not yet due
        self.assertEqual(len(self.checked), 2)
        self.worker.recheck(now=NIGHT + 31 * 86400)   # a month on
        self.assertEqual(len(self.checked), 4)

    def test_gone_files_are_forgotten(self):
        self.worker.recheck(now=NIGHT)
        self.bad.unlink()   # replaced
        self.worker.recheck(now=NIGHT + 86400)
        self.assertEqual(self.worker.state["damaged"], {})


class Replace(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "config").mkdir()
        self.lib = control.Library(self.root / "config")
        c.write_json(self.lib.state / "postimport/status.json", {"damaged": [
            {"path": "/m/movies/Bad (2002)/Bad (2002).mkv", "title": "Bad (2002)"},
            {"path": "/m/tv/Show/Season 01/Show - S01E02.mkv", "title": "Show - S01E02"}]})
        self.calls: list = []

        def arr(app, method, path, body=None):
            self.calls.append((app, method, path, body))
            if path == "movie":
                return [{"id": 5, "title": "Bad", "year": 2002, "movieFile": {"id": 50, "path": "/m/movies/Bad (2002)/Bad (2002).mkv"}}]
            if path == "series":
                return [{"id": 9, "title": "Show", "path": "/m/tv/Show"}]
            if path.startswith("episodefile?"):
                return [{"id": 90, "path": "/m/tv/Show/Season 01/Show - S01E02.mkv"}]
            if path.startswith("episode?"):
                return [{"id": 902, "episodeFileId": 90}, {"id": 901, "episodeFileId": 1}]
            return {}
        self.lib.arr = arr   # type: ignore[method-assign]

    def test_a_film(self):
        self.assertEqual(self.lib.replace("/m/movies/Bad (2002)/Bad (2002).mkv"), {"replacing": "Bad (2002)"})
        self.assertIn(("radarr", "DELETE", "moviefile/50", None), self.calls)
        self.assertIn(("radarr", "POST", "command", {"name": "MoviesSearch", "movieIds": [5]}), self.calls)

    def test_an_episode(self):
        self.assertEqual(self.lib.replace("/m/tv/Show/Season 01/Show - S01E02.mkv")["replacing"], "Show (1 episode)")
        self.assertIn(("sonarr", "DELETE", "episodefile/90", None), self.calls)
        self.assertIn(("sonarr", "POST", "command", {"name": "EpisodeSearch", "episodeIds": [902]}), self.calls)

    def test_only_files_the_recheck_flagged(self):
        with self.assertRaises(LookupError):
            self.lib.replace("/m/movies/Good (2001)/Good (2001).mkv")
        self.assertEqual(self.calls, [])   # nothing asked, nothing deleted


if __name__ == "__main__":
    unittest.main()
