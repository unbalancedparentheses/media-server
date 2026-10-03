"""Tests for mediaserver/dashmedia.py: what each request's status says, which
release date a movie is waiting for, and grouping new episodes.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import unittest
from unittest import mock

from mediaserver import dashmedia as dm

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


class Pipeline(unittest.TestCase):
    """Where a title is: requested → searching → downloading → importing →
    checking & fixing → subtitles → ready"""

    def states(self, stages):
        return {st["name"]: (st["state"], st["detail"]) for st in stages}

    def test_waiting_for_approval(self):
        s = self.states(dm.pipeline({"status": 1, "by": "ana"}, False, "", [], "", "", 0))
        self.assertEqual(s["requested"], ("active", "waiting for approval"))
        self.assertEqual(s["ready"][0], "todo")

    def test_searching_and_not_out_yet(self):
        s = self.states(dm.pipeline({"status": 2, "by": "ana"}, False, "", [], "", "", 0))
        self.assertEqual(s["searching"], ("active", "looking for a good release"))
        s = self.states(dm.pipeline(None, False, "", [], "not out yet · digital Nov 15", "", 0))
        self.assertNotIn("requested", s)
        self.assertEqual(s["searching"][1], "not out yet · digital Nov 15")

    def test_downloading_with_progress(self):
        queue = [{"size": 100, "sizeleft": 25, "trackedDownloadState": "downloading", "timeleft": "00:10:00"}]
        s = self.states(dm.pipeline({"status": 2, "by": "ana"}, False, "", queue, "", "", 0))
        self.assertEqual(s["searching"][0], "done")
        self.assertEqual(s["downloading"], ("active", "75% · 00:10:00 left"))
        self.assertEqual(s["importing"][0], "todo")

    def test_import_stuck_is_a_problem(self):
        queue = [{"size": 100, "sizeleft": 0, "trackedDownloadState": "importBlocked"}]
        s = self.states(dm.pipeline(None, False, "", queue, "", "", 0))
        self.assertEqual((s["downloading"][0], s["importing"][0]), ("done", "problem"))

    def test_checking_after_import(self):
        s = self.states(dm.pipeline(None, True, "", [], "", "adding stereo audio", 0))
        self.assertEqual(s["checking"], ("active", "adding stereo audio"))
        self.assertEqual(s["ready"][0], "todo")

    def test_ready_with_subtitles_still_missing(self):
        s = self.states(dm.pipeline({"status": 2, "by": "ana"}, True, "", [], "", "", 1))
        self.assertEqual((s["checking"][0], s["subtitles"][0], s["ready"][0]), ("done", "active", "done"))

    def test_partly_there(self):
        s = self.states(dm.pipeline(None, True, "26 of 52 episodes", [], "", "", 0))
        self.assertEqual(s["searching"], ("active", "26 of 52 episodes · looking for a good release"))
        self.assertEqual(s["ready"], ("done", "26 of 52 episodes"))

    def test_postimport_activity_matched_to_the_title(self):
        current = {"path": "/m/Film (2020)/Film.mkv", "what": "reading the en picture subtitles into text"}
        self.assertEqual(dm.checking_for(current, [], "/m/Film (2020)", "Radarr", 1), current["what"])
        self.assertEqual(dm.checking_for(current, [], "/m/Other (2020)", "Radarr", 1), "")
        queued = [{"app": "Radarr", "movieId": 1, "suspect": "the video is damaged"}]
        self.assertEqual(dm.checking_for({}, queued, "/m/x", "Radarr", 1), "checking again: the video is damaged")
