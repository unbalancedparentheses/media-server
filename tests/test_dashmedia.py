"""Tests for mediaserver/dashmedia.py: what each request's status says, which
release date a movie is waiting for, and grouping new episodes.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import json
import unittest
from pathlib import Path
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




class Abandoned(unittest.TestCase):
    def test_missing_for_weeks_with_nothing_usable(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        (tmp / "postimport").mkdir()
        day = 86400
        items = {"radarr:1": {"app": "Radarr", "title": "Old Film (1990)", "movie": 1, "since": 0, "searches": 9,
                              "diagnosis": {"code": "none", "text": "No releases found on your indexers"}},
                 "sonarr:9:2": {"app": "Sonarr", "title": "Kaiji season 2", "series": 9, "season": 2, "since": 10 * day, "searches": 7,
                                "episodes": 26, "diagnosis": {"code": "none", "text": "No releases found on your indexers"}},
                 "radarr:2": {"app": "Radarr", "title": "Recent (2026)", "movie": 2, "since": 30 * day, "searches": 2},
                 "radarr:3": {"app": "Radarr", "title": "Usable (2000)", "movie": 3, "since": 0, "diagnosis": {"code": "usable", "text": "x"}},
                 "radarr:4": {"app": "Radarr", "title": "Coming (2001)", "movie": 4, "since": 0, "downloading": True}}
        (tmp / "postimport/stuck.json").write_text(json.dumps({"items": items}))
        with mock.patch.object(dm, "STATE", tmp):
            out = dm.abandoned(now=40 * day)
        self.assertEqual([x["title"] for x in out], ["Old Film (1990)", "Kaiji season 2"])   # longest missing first
        self.assertEqual((out[0]["item"], out[0]["season"], out[0]["days"]), ("radarr1", None, 40))
        self.assertEqual((out[1]["item"], out[1]["season"], out[1]["admin"]), ("sonarr9", 2, "sonarr:/wanted/missing"))
        self.assertEqual(out[1]["why"], "No releases found on your indexers")

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


class Fixing(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)
        (self.state / "postimport").mkdir()

    def status(self, current, age=0):
        import json
        import time
        (self.state / "postimport/status.json").write_text(json.dumps({"updated": time.time() - age, "current": current}))

    def test_text(self):
        self.assertEqual(dm.fixing_text({"what": "adding stereo audio"}), "adding stereo audio")
        self.assertEqual(dm.fixing_text({"what": "adding stereo audio", "progress": 42, "eta": 185}),
                         "adding stereo audio · 42% · about 3 min left")
        self.assertEqual(dm.fixing_text({"what": "x", "progress": 97, "eta": 20}), "x · 97% · less than a minute left")
        self.assertEqual(dm.fixing_text({"what": "x", "progress": 2, "eta": None}), "x · 2%")

    def test_now_and_stale(self):
        self.assertIsNone(dm.fixing_now(self.state))
        current = {"path": "/m/Film (2020)/Film.mkv", "title": "Film", "what": "adding stereo audio", "progress": 40, "eta": 60, "since": 1}
        self.status(current)
        now = dm.fixing_now(self.state)
        assert now is not None
        self.assertEqual(now["text"], "adding stereo audio · 40% · about 1 min left")
        self.status(current, age=3600)   # postimport stopped reporting
        self.assertIsNone(dm.fixing_now(self.state))

    def test_progress_on_the_titles_stage(self):
        current = {"path": "/m/Film (2020)/Film.mkv", "what": "adding stereo audio", "progress": 40}
        stages = dm.movie_pipeline({"id": 1, "hasFile": True, "path": "/m/Film (2020)"}, None, {}, current, [], {}, "2026-01-01")
        checking = next(st for st in stages if st["name"] == "checking")
        self.assertEqual((checking["state"], checking["progress"]), ("active", 40))
        self.assertIn("40%", checking["detail"])
        # Another title's folder isn't touched (a prefix isn't enough)
        other = dm.movie_pipeline({"id": 2, "hasFile": True, "path": "/m/Film"}, None, {}, current, [], {}, "2026-01-01")
        self.assertNotIn("progress", next(st for st in other if st["name"] == "checking"))


class Display(unittest.TestCase):
    def test_quality_stripped(self):
        from mediaserver import common as c
        for name, want in (("Kaiji - S01E02 - Open Fire WEBDL-1080p v2", "Kaiji - S01E02 - Open Fire"),
                           ("Skyfall (2012) Bluray-1080p Proper", "Skyfall (2012)"),
                           ("Lain - S01E01 - Weird HDTV-720p Repack v3", "Lain - S01E01 - Weird"),
                           ("No quality here", "No quality here")):
            self.assertEqual(c.strip_quality(name), want)

    def test_fixes_grouped_per_series(self):
        what = "set the default audio en"
        recent = [{"title": "Kaiji - S01E02 - Open Fire WEBDL-1080p v2", "what": what, "time": 5},
                  {"title": "Skyfall (2012)", "what": what, "time": 4},
                  {"title": "Kaiji - S01E04 - Failure WEBDL-1080p v2", "what": what, "time": 3},
                  {"title": "Kaiji - S01E07 - Proclamation", "what": "added stereo audio", "time": 2},
                  {"title": "Kaiji - S01E19 - Limit", "what": what, "time": 1}]
        out = dm.grouped_fixes(recent)
        self.assertEqual([(f["title"], f["episodes"], f["what"]) for f in out],
                         [("Kaiji", 3, what), ("Skyfall (2012)", 1, what), ("Kaiji - S01E07 - Proclamation", 1, "added stereo audio")])
        self.assertEqual(out[0]["time"], 5)   # the newest

    def test_partly_there_series(self):
        """What's there went through every step; only the search is still on"""
        show = {"id": 1, "path": "/tv/Kaiji", "statistics": {"episodeFileCount": 26, "episodeCount": 52}}
        stages = {st["name"]: st["state"] for st in dm.series_pipeline(show, {"status": 2, "by": "admin"}, {}, {}, [], {})}
        self.assertEqual(stages, {"requested": "done", "searching": "active", "downloading": "done", "importing": "done",
                                  "checking": "done", "subtitles": "done", "ready": "done"})


class Recommended(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)
        patcher = mock.patch.object(dm, "STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rating_calls = []
        self.down = False
        self.ratings = {"movie/1/ratingscombined": {"rt": {"criticsScore": 92, "audienceScore": 88}, "imdb": {"criticsScore": 8.1}},
                        "movie/2/ratingscombined": {"rt": {"criticsScore": 40}},                 # rotten: left out
                        "movie/3/ratingscombined": {"imdb": {"criticsScore": 7.9}},               # IMDb only
                        "tv/10/ratings": {"criticsScore": 100, "audienceScore": 70},
                        "movie/4/ratingscombined": {}}                                             # nothing: TMDB's vote

    def fake_seerr(self, path):
        if "ratings" in path:
            self.rating_calls.append(path)
            if self.down:
                raise OSError("down")
            return self.ratings.get(path, {})

        def item(i, title, pop, vote=7.0, votes=500, kind="movie", **kw):
            date = kw.pop("date", "2026-09-01")
            return {"id": i, ("title" if kind == "movie" else "name"): title, "popularity": pop, "voteAverage": vote,
                    "voteCount": votes, "posterPath": f"/{i}.jpg", ("releaseDate" if kind == "movie" else "firstAirDate"): date, **kw}
        if path.startswith("discover/movies") and "page=1" in path and "genre=16" not in path:
            return {"results": [item(1, "Great", 90, mediaInfo={"status": 5, "jellyfinMediaId": "abc"}), item(2, "Rotten", 80),
                                item(3, "Good", 70), item(4, "Unrated", 60, vote=8.3),
                                item(5, "Trailer", 99, video=True), {"id": 6, "title": "No poster", "popularity": 50}]}
        if path.startswith("discover/tv") and "page=1" in path and "genre=16" not in path:
            return {"results": [item(10, "Show", 85, 7.5, kind="tv", mediaInfo={"status": 4})]}
        if path.startswith("discover/tv") and "genre=16" in path and "page=1" in path:
            return {"results": [item(20, "Anime Show", 40, 8.6, votes=40, kind="tv", genreIds=[16, 10765], originalLanguage="ja"),
                                item(21, "Barely Voted", 39, 9.5, votes=5, kind="tv", genreIds=[16], originalLanguage="ja")]}
        if path.startswith("discover/trending") and "page=1" in path:
            return {"results": [{"id": 99, "mediaType": "person", "name": "Someone"},
                                item(30, "Few Votes", 95, 9.1, votes=12, mediaType="movie"),
                                item(31, "Not Out Yet", 95, 9.0, mediaType="movie", date="2099-01-01")]}
        return {"results": []}

    def run_it(self, down=False):
        self.down = down
        with mock.patch.object(dm, "seerr", self.fake_seerr):
            return dm.recommended()

    def test_three_rows_scored_and_marked(self):
        rows = self.run_it()
        # Great: Rotten Tomatoes 92 and IMDb 8.1 average to 86.5
        self.assertEqual([(p["title"], p["score"]) for p in rows["movies"]], [("Great", 86), ("Unrated", 83), ("Good", 79)])
        self.assertEqual([(p["title"], p["score"], p["status"]) for p in rows["series"]], [("Show", 100, 4)])
        # Anime: TMDB's score with fewer votes counts; too few still doesn't
        self.assertEqual([p["title"] for p in rows["anime"]], ["Anime Show"])
        great = rows["movies"][0]
        self.assertEqual((great["rt"], great["rt_audience"], great["imdb"], great["status"], great["watch"]), (92, 88, 8.1, 5, "abc"))
        everything = [p["title"] for r in rows.values() for p in r]
        self.assertNotIn("Few Votes", everything)      # a high TMDB score from 12 votes
        self.assertNotIn("Not Out Yet", everything)    # trending but not released

    def test_critics_and_viewers_averaged(self):
        self.assertEqual(dm.score({"rt": 93, "imdb": 5.7}, 7.0), 75.0)
        self.assertEqual(dm.score({"rt": 80}, 9.0), 80.0)
        self.assertEqual(dm.score({"imdb": 7.9}, None), 79.0)
        self.assertEqual(dm.score({}, 8.3, 500), 83.0)
        self.assertIsNone(dm.score({}, 8.3, 50))
        self.assertEqual(dm.score({}, 8.3, 50, min_votes=20), 83.0)
        self.assertIsNone(dm.score({}, None))

    def test_ratings_asked_once_a_day_and_kept_when_seerr_is_down(self):
        self.run_it()
        first = len(self.rating_calls)
        self.run_it()
        self.assertEqual(len(self.rating_calls), first)   # from the cache
        # A day later Seerr doesn't answer: the old ratings are still used...
        cache = dm.c.read_json(self.state / "dashstatus/ratings.json")
        for v in cache.values():
            v["at"] -= 2 * 86400 - 10
        dm.c.write_json(self.state / "dashstatus/ratings.json", cache)
        rows = self.run_it(down=True)
        self.assertEqual([p["title"] for p in rows["movies"]], ["Great", "Unrated", "Good"])
        # ...and it isn't asked again for a while after failing
        asked = len(self.rating_calls)
        self.run_it(down=True)
        self.assertEqual(len(self.rating_calls), asked)


class StuckDownloads(unittest.TestCase):
    def test_why_a_download_isnt_moving(self):
        stalled = [{"size": 100, "sizeleft": 100, "trackedDownloadState": "downloading", "errorMessage": "The download is stalled with no connections"}]
        self.assertIn("stalled: nobody is sharing it", dm.queue_progress(stalled)[1])
        meta = [{"size": 0, "sizeleft": 0, "trackedDownloadState": "downloading", "errorMessage": "qBittorrent is downloading metadata"}]
        self.assertIn("getting the torrent's details", dm.queue_progress(meta)[1])
        fine = [{"size": 100, "sizeleft": 50, "trackedDownloadState": "downloading", "timeleft": "00:10:00"}]
        self.assertEqual(dm.queue_progress(fine), ("downloading", "50% · 00:10:00 left"))


class Tonight(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.media = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.media)
        patcher = mock.patch.object(dm, "STATE", self.media / ".state")
        patcher.start()
        self.addCleanup(patcher.stop)
        h = 600_000_000   # ticks in a minute
        anime = str(self.media / "anime")
        self.films = [
            {"Id": "f1", "Name": "Unwatched Film", "Type": "Movie", "ProductionYear": 2024, "RunTimeTicks": 135 * h,
             "Path": "/m/movies/U.mkv", "DateCreated": "2026-09-03", "ImageTags": {"Primary": "t"}, "UserData": {}},
            {"Id": "f2", "Name": "Started Film", "Type": "Movie", "RunTimeTicks": 90 * h, "Path": "/m/movies/S.mkv",
             "DateCreated": "2026-09-05", "UserData": {"PlaybackPositionTicks": 5}},
            {"Id": "f3", "Name": "Anime Film", "Type": "Movie", "RunTimeTicks": 100 * h, "Path": anime + "/Film/F.mkv",
             "DateCreated": "2026-09-01", "UserData": {}}]
        self.shows = [
            {"Id": "s1", "Name": "Fresh Show", "Type": "Series", "RunTimeTicks": 24 * h, "Path": anime + "/Fresh",
             "RecursiveItemCount": 8, "UserData": {"UnplayedItemCount": 8}, "ProviderIds": {"Tvdb": "100"}, "DateCreated": "2026-09-04"},
            {"Id": "s2", "Name": "Begun Show", "Type": "Series", "RecursiveItemCount": 10, "UserData": {"UnplayedItemCount": 4},
             "DateCreated": "2026-09-06"},
            {"Id": "s4", "Name": "Half An Episode", "Type": "Series", "RecursiveItemCount": 5, "UserData": {"UnplayedItemCount": 5},
             "DateCreated": "2026-09-07"},
            {"Id": "s3", "Name": "Full Show", "Type": "Series", "RunTimeTicks": 50 * h, "Path": "/m/tv/Full",
             "RecursiveItemCount": 6, "UserData": {"UnplayedItemCount": 6}, "ProviderIds": {"Tvdb": "200"}, "DateCreated": "2026-09-02"}]

    def jellyfin(self, path):
        if path == "Users":
            return [{"Id": "u1", "Name": "admin"}]
        if path.startswith("UserItems/Resume"):
            return {"Items": [{"Id": "e1", "SeriesId": "s4"}]}   # an episode half watched, none finished
        if path.startswith("Shows/NextUp"):
            return {"Items": []}
        if "IncludeItemTypes=Movie" in path:
            return {"Items": self.films}
        return {"Items": self.shows}

    def test_only_what_you_havent_started(self):
        sonarr = [{"tvdbId": 100, "statistics": {"episodeCount": 24}}, {"tvdbId": 200, "statistics": {"episodeCount": 6}}]
        with mock.patch.object(dm, "jellyfin", self.jellyfin), mock.patch.object(dm, "sonarr", return_value=sonarr):
            items = dm.tonight()
        self.assertEqual([(i["title"], i["kind"]) for i in items],
                         [("Fresh Show", "anime"), ("Unwatched Film", "film"), ("Full Show", "series"), ("Anime Film", "anime")])
        by = {i["title"]: i for i in items}
        self.assertEqual((by["Unwatched Film"]["minutes"], by["Unwatched Film"]["detail"]), (135, "2024 · 2 h 15 min"))
        self.assertEqual((by["Fresh Show"]["detail"], by["Fresh Show"]["partial"]), ("8 of 24 episodes · 24 min each", True))
        self.assertEqual((by["Full Show"]["detail"], by["Full Show"]["partial"]), ("6 episodes · 50 min each", False))


class Because(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)
        patcher = mock.patch.object(dm, "STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def jellyfin(self, path):
        if path == "Users":
            return [{"Id": "u1"}]
        if "Filters=IsResumable" in path:
            return {"Items": [{"Name": "Skyfall", "Type": "Movie", "ProviderIds": {"Tmdb": "37724"}, "UserData": {"LastPlayedDate": "2026-10-03"}},
                              {"Name": "Turning Point", "Type": "Episode", "SeriesId": "sg", "UserData": {"LastPlayedDate": "2026-10-02"}},
                              {"Name": "Ep 2", "Type": "Episode", "SeriesId": "sg", "UserData": {"LastPlayedDate": "2026-10-01"}}]}
        if "Filters=IsPlayed" in path:
            return {"Items": [{"Name": "Spectre", "Type": "Movie", "ProviderIds": {"Tmdb": "206647"}, "UserData": {"LastPlayedDate": "2026-09-01"}}]}
        if "Ids=sg" in path:
            return {"Items": [{"Id": "sg", "Name": "Steins;Gate", "ProviderIds": {"Tmdb": "42509"}}]}
        return {"Items": []}

    def seerr(self, path):
        def r(i, title, kind, vote=8.0, votes=500, **kw):
            return {"id": i, "mediaType": kind, ("title" if kind == "movie" else "name"): title, "voteAverage": vote,
                    "voteCount": votes, "posterPath": f"/{i}.jpg", **kw}
        if path.startswith("movie/37724/recommendations"):
            return {"results": [r(206647, "Spectre", "movie"),             # watched: left out
                                r(1, "Casino Royale", "movie", mediaInfo={"status": 5, "jellyfinMediaId": "cr"}),
                                r(2, "Weak Film", "movie", vote=5.0),     # badly rated: left out
                                r(5, "Decent Film", "movie", vote=6.8),   # good enough here (not for Worth watching)
                                r(3, "Shared", "movie")]}
        if path.startswith("tv/42509/recommendations"):
            return {"results": [r(3, "Shared", "movie"),                  # already in the first row
                                r(4, "Psycho-Pass", "tv", genreIds=[16], originalLanguage="ja", votes=40)]}
        if path.startswith("movie/206647/recommendations"):
            return {"results": []}
        return {}

    def test_rows_from_what_you_watched_last(self):
        with mock.patch.object(dm, "jellyfin", self.jellyfin), mock.patch.object(dm, "seerr", self.seerr), \
                mock.patch.object(dm, "ratings", return_value={}):
            rows = dm.because()
        self.assertEqual([r["because"] for r in rows], ["Skyfall", "Steins;Gate"])   # Spectre gave nothing
        self.assertEqual([i["title"] for i in rows[0]["items"]], ["Casino Royale", "Decent Film", "Shared"])
        self.assertEqual([i["title"] for i in rows[1]["items"]], ["Psycho-Pass"])   # anime: fewer votes count
        self.assertEqual((rows[0]["items"][0]["status"], rows[0]["items"][0]["watch"]), (5, "cr"))

    def test_recently_watched_counts_an_episode_as_its_series(self):
        with mock.patch.object(dm, "jellyfin", self.jellyfin):
            watched = dm.recently_watched()
        self.assertEqual([(w["title"], w["kind"], w["tmdb"]) for w in watched],
                         [("Skyfall", "movie", 37724), ("Steins;Gate", "tv", 42509), ("Spectre", "movie", 206647)])


class DubStatus(unittest.TestCase):
    def test_health_says_what_the_replacement_is_doing(self):
        import tempfile
        import shutil
        import json
        state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, state)
        (state / "postimport").mkdir()
        (state / "postimport/stuck.json").write_text(json.dumps({"dubs": {"13:1": {"title": "Kaiji season 1", "status": "waiting", "looks": 2}}}))
        sonarr = [{"title": "Kaiji", "titleSlug": "kaiji", "seriesType": "anime", "originalLanguage": {"name": "Japanese"}}]

        def jellyfin(path):
            if path == "Library/VirtualFolders":
                return [{"Name": "Anime", "ItemId": "a"}]
            return {"Items": [{"SeriesName": "Kaiji", "MediaStreams": [{"Type": "Audio", "Language": "eng"}]}]}
        with mock.patch.object(dm, "STATE", state), mock.patch.object(dm, "sonarr", return_value=sonarr), \
                mock.patch.object(dm, "jellyfin", jellyfin):
            dubs = dm.health()["dubs"]
        self.assertEqual(dubs[0]["status"], "No Japanese or Dual Audio release out yet (looked 2×; looked at less often each time, up to weekly)")


class UniqueRequests(unittest.TestCase):
    def test_one_row_per_title_dated_from_the_first_ask(self):
        reqs = [{"id": 9, "type": "movie", "media": {"tmdbId": 1}, "createdAt": "2026-10-03T22:00:00Z"},
                {"id": 8, "type": "movie", "media": {"tmdbId": 2}, "createdAt": "2026-10-01T00:00:00Z"},
                {"id": 3, "type": "movie", "media": {"tmdbId": 1}, "createdAt": "2026-09-28T00:00:00Z"},
                {"id": 2, "type": "tv", "media": {"tmdbId": 1}, "createdAt": "2026-09-27T00:00:00Z"}]   # a series, same number
        out = dm.unique_requests(reqs)
        self.assertEqual([(r["id"], r["times"], r["firstAsked"]) for r in out],
                         [(9, 2, "2026-09-28T00:00:00Z"), (8, 1, "2026-10-01T00:00:00Z"), (2, 1, "2026-09-27T00:00:00Z")])


class LibrarySizes(unittest.TestCase):
    def test_everything_with_its_size_biggest_first(self):
        def jellyfin(path):
            if path == "Users":
                return [{"Id": "u1"}]
            return {"Items": [{"Id": "a", "Type": "Movie", "Name": "Skyfall", "ProductionYear": 2012, "ProviderIds": {"Tmdb": "37724"},
                               "UserData": {"Played": True}},
                              {"Id": "b", "Type": "Series", "Name": "Kaiji", "ProviderIds": {"Tvdb": "100"}},
                              {"Id": "c", "Type": "Movie", "Name": "Home Video", "ProviderIds": {}}]}
        with mock.patch.object(dm, "jellyfin", jellyfin), \
                mock.patch.object(dm, "radarr", return_value=[{"tmdbId": 37724, "sizeOnDisk": 29 * 1024 ** 3}]), \
                mock.patch.object(dm, "sonarr", return_value=[{"tvdbId": 100, "statistics": {"sizeOnDisk": 11 * 1024 ** 3}}]):
            rows = dm.library_sizes()
        self.assertEqual([(r["title"], r["kind"], r["size"] // 1024 ** 3, r["watched"], r["managed"]) for r in rows],
                         [("Skyfall", "film", 29, True, True), ("Kaiji", "series", 11, False, True), ("Home Video", "film", 0, False, False)])
