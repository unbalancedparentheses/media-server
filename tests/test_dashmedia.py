"""Tests for scripts/dashmedia.py: what each request's status says, which
release date a movie is waiting for, and grouping new episodes.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests)
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dashmedia as dm  # noqa: E402

TODAY = "2026-10-03"


def movie(**kw):
    return {"id": 1, "titleSlug": "film", "hasFile": False, "isAvailable": True, **kw}


class Releases(unittest.TestCase):
    def test_next_release_skips_past_dates(self):
        m = {"inCinemas": "2026-07-15T00:00:00Z", "digitalRelease": "2026-11-15T00:00:00Z", "physicalRelease": "2026-11-17T00:00:00Z"}
        self.assertEqual(dm.next_release(m, TODAY), ("2026-11-15", "digital"))
        self.assertIsNone(dm.next_release({"inCinemas": "2020-01-01T00:00:00Z"}, TODAY))


class MovieRequests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(dm.movie_state(movie(hasFile=True), {}, TODAY)["state"], "available")
        self.assertEqual(dm.movie_state(movie(), {}, TODAY)["state"], "searching")
        s = dm.movie_state(movie(isAvailable=False, digitalRelease="2026-11-15T00:00:00Z"), {}, TODAY)
        self.assertEqual((s["state"], s["text"]), ("upcoming", "not out yet · digital Nov 15"))
        self.assertEqual(dm.movie_state(None, {}, TODAY)["state"], "searching")

    def test_downloading_and_stuck(self):
        q = {1: [{"size": 100, "sizeleft": 60, "trackedDownloadState": "downloading", "status": "downloading"}]}
        s = dm.movie_state(movie(), q, TODAY)
        self.assertEqual((s["state"], s["text"]), ("downloading", "downloading · 40%"))
        q = {1: [{"size": 100, "sizeleft": 0, "trackedDownloadState": "importBlocked", "status": "completed"}]}
        self.assertEqual(dm.movie_state(movie(), q, TODAY)["state"], "stuck")
        self.assertEqual(dm.movie_state(movie(), q, TODAY)["admin"], "radarr:/movie/film")


class SeriesRequests(unittest.TestCase):
    def show(self, have, aired):
        return {"id": 5, "titleSlug": "show", "statistics": {"episodeFileCount": have, "episodeCount": aired}}

    def test_states(self):
        self.assertEqual(dm.series_state(self.show(24, 24), {})["state"], "available")
        s = dm.series_state(self.show(26, 52), {})
        self.assertEqual((s["state"], s["text"]), ("partial", "26 of 52 episodes · looking for the rest"))
        self.assertEqual(dm.series_state(self.show(0, 10), {})["state"], "searching")
        q = {5: [{"size": 10, "sizeleft": 5}, {"size": 10, "sizeleft": 5}]}
        self.assertEqual(dm.series_state(self.show(0, 10), q)["text"], "downloading 2 episodes · 50%")


class Latest(unittest.TestCase):
    def test_episodes_grouped_into_their_series(self):
        items = [{"Id": f"e{n}", "Type": "Episode", "SeriesId": "s1", "SeriesName": "Show", "SeriesPrimaryImageTag": "t",
                  "ParentIndexNumber": 1, "IndexNumber": n, "Name": f"Ep {n}"} for n in (3, 2, 1)]
        items.append({"Id": "m1", "Type": "Movie", "Name": "Film", "ProductionYear": 2020, "ImageTags": {"Primary": "x"}})
        with mock.patch.object(dm, "jellyfin", return_value={"Items": items}):
            cards = dm.latest()
        self.assertEqual([(c["title"], c["detail"]) for c in cards], [("Show", "3 new episodes"), ("Film", "2020")])
        self.assertEqual((cards[0]["image"], cards[1]["image"]), ("s1", "m1"))


if __name__ == "__main__":
    unittest.main()
