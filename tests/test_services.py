"""The read-only parts against fake services with a library in them:
doctor, the dashboard's data (dashstatus, dashmedia), the full checks,
netwatch's reconnect, and postimport's side of Sonarr/Radarr.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import io
import json
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from mediaserver import dashmedia, doctor, launchd, netwatch, postimport, verify
from mediaserver import common as c
from mediaserver.dashstatus import Collector
from tests.fakes import FakeService
from tests.test_integration import Stack, quiet

DAY = 86400


def iso(days: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * DAY))


class Library(Stack):
    """After a fresh install, a small library and some activity"""

    def setUp(self):
        super().setUp()
        self.run_steps()
        s, p = self.stack, self.cfg.paths
        future = (date.today() + timedelta(days=30)).isoformat()
        movies = s.radarr.resources["movie"]
        self.ready = movies.add({"title": "Ready Film", "year": 2020, "tmdbId": 11, "hasFile": True, "isAvailable": True,
                                 "path": str(p.movies / "Ready Film (2020)"), "titleSlug": "ready-film", "runtime": 100,
                                 "originalLanguage": {"name": "English"}, "genres": []})
        self.coming = movies.add({"title": "Downloading Film", "year": 2021, "tmdbId": 12, "hasFile": False, "isAvailable": True,
                                  "path": str(p.movies / "Downloading Film (2021)"), "titleSlug": "downloading-film"})
        movies.add({"title": "Future Film", "year": 2027, "tmdbId": 13, "hasFile": False, "isAvailable": False,
                    "digitalRelease": future + "T00:00:00Z", "calendar": True, "titleSlug": "future-film"})
        s.radarr.queue = [{"movieId": self.coming["id"], "size": 1000, "sizeleft": 400, "trackedDownloadState": "downloading",
                           "trackedDownloadStatus": "ok", "timeleft": "00:20:00", "title": "Downloading.Film.2021"}]
        self.show = s.sonarr.resources["series"].add({"title": "Anime Show", "tvdbId": 21, "seriesType": "anime", "titleSlug": "anime-show",
                                                       "originalLanguage": {"name": "Japanese"}, "path": str(p.anime / "Anime Show"),
                                                       "statistics": {"episodeFileCount": 2, "episodeCount": 4}, "qualityProfileId": 1})
        s.sonarr.episodes = [{"id": 1, "seriesId": self.show["id"], "seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "runtime": 24}]
        s.seerr.requests_list = [{"type": "movie", "status": 2, "createdAt": iso(-1), "requestedBy": {"displayName": "ana"},
                                  "media": {"tmdbId": 11, "status": 5, "jellyfinMediaId": "jf11"}},
                                 {"type": "movie", "status": 2, "createdAt": iso(-1), "requestedBy": {"displayName": "ana"},
                                  "media": {"tmdbId": 12, "status": 3}},
                                 {"type": "tv", "status": 2, "createdAt": iso(-2), "requestedBy": {"displayName": "bo"},
                                  "media": {"tmdbId": 31, "tvdbId": 21, "status": 4}}]
        s.seerr.details = {("movie", 11): {"title": "Ready Film", "releaseDate": "2020-01-01", "posterPath": "/r.jpg"},
                           ("movie", 12): {"title": "Downloading Film", "releaseDate": "2021-01-01"},
                           ("tv", 31): {"name": "Anime Show", "firstAirDate": "2019-01-01"}}
        anime = next(f["ItemId"] for f in s.jellyfin.folders if f["Name"] == "Anime")
        s.jellyfin.items = [
            {"Id": "jf11", "Name": "Ready Film", "Type": "Movie", "ProductionYear": 2020, "ImageTags": {"Primary": "t"},
             "DateCreated": iso(-1), "UserData": {"PlaybackPositionTicks": 100, "PlayedPercentage": 40, "LastPlayedDate": iso(-0.1)},
             "MediaStreams": [{"Type": "Audio", "Language": "eng"}, {"Type": "Subtitle", "Language": "eng", "IsTextSubtitleStream": False}]},
            {"Id": "ep1", "Name": "Pilot", "Type": "Episode", "SeriesName": "Anime Show", "SeriesId": "s1", "SeriesPrimaryImageTag": "st",
             "ParentIndexNumber": 1, "IndexNumber": 1, "ParentId": anime, "DateCreated": iso(-2), "next_up": True,
             "MediaStreams": [{"Type": "Audio", "Language": "eng"}]}]
        s.jellyfin.sessions = [{"UserName": "ana", "Client": "Moonfin", "DeviceName": "Brave",
                                "NowPlayingItem": {"Id": "jf11", "Name": "Ready Film", "RunTimeTicks": 1000, "ProductionYear": 2020, "ImageTags": {"Primary": "t"}},
                                "PlayState": {"PositionTicks": 250, "PlayMethod": "Transcode"},
                                "TranscodingInfo": {"IsVideoDirect": False, "TranscodeReasons": ["SubtitleCodecNotSupported"]}}]
        s.bazarr.wanted_episodes = [{"seriesTitle": "Anime Show", "sonarrSeriesId": self.show["id"]}]
        s.bazarr.wanted_movies = [{"title": "Ready Film", "radarrId": self.ready["id"]}]
        indexer = s.prowlarr.resources["indexer"].items[0]
        s.prowlarr.statuses = [{"indexerId": indexer["id"], "disabledTill": iso(0.5)}]
        self.stuck_torrent = {"name": "Stuck.Show.S01", "progress": 0.1, "state": "stalledDL", "added_on": time.time() - 3 * DAY,
                              "num_seeds": 0, "dlspeed": 0, "upspeed": 0}
        s.qbittorrent.torrents = [self.stuck_torrent]
        c.write_json(p.state / "postimport/status.json", {"updated": time.time(), "recent": [{"time": time.time(), "title": "Ready Film", "what": "added stereo audio"}],
                                                           "looking": [], "kept": [], "current": None, "queued": []})
        c.write_atomic(p.state / "netwatch/connection", "online\n")


class DoctorTests(Library):
    def test_findings(self):
        with mock.patch.object(launchd, "state", return_value="running (pid 1)"):
            code, out = quiet(doctor.main, self.cfg)
        self.assertEqual(code, 1)  # something needs attention
        self.assertIn("Prowlarr switched off indexers after failures", out)
        self.assertIn("Download not moving for over a day: Stuck.Show.S01", out)
        self.assertIn("Anime with no Japanese audio track: Anime Show (1 episodes)", out)
        self.assertIn('Subtitles in "eng" only as pictures', out)
        self.assertIn("Still without subtitles in your language: episodes of Anime Show (1); films: Ready Film", out)
        self.assertIn("All services running", out)
        self.assertIn("Checks after each download: running (1 fixes in the last day)", out)


class DashboardTests(Library):
    def test_media_data(self):
        dashmedia.configure(self.cfg.paths.config, self.cfg.paths.state)
        data = dashmedia.collect()
        self.assertEqual(data["media_failed"], [])
        reqs = {r["title"]: r for r in data["requests_live"]}
        self.assertEqual(reqs["Ready Film"]["state"], "available")
        self.assertEqual(reqs["Downloading Film"]["text"], "downloading · 60%")
        stages = {st["name"]: st for st in reqs["Downloading Film"]["stages"]}
        self.assertEqual((stages["downloading"]["state"], stages["downloading"]["detail"]), ("active", "60% · 00:20:00 left"))
        self.assertEqual(reqs["Anime Show"]["text"], "2 of 4 episodes · looking for the rest")
        self.assertEqual(data["upcoming"][0]["title"], "Future Film")
        self.assertEqual(data["upcoming"][0]["detail"], "2027 · digital")
        self.assertEqual([x["title"] for x in data["continue"]], ["Ready Film", "Anime Show"])
        self.assertEqual(data["health"]["dubs"], [{"title": "Anime Show", "episodes": 1, "admin": "sonarr:/series/anime-show"}])
        self.assertEqual([x["title"] for x in data["latest"]], ["Ready Film", "Anime Show"])

    def test_status_file(self):
        out = self.cfg.paths.config / "nginx/www/status.json"
        col = Collector(self.cfg.paths.config, self.cfg.paths.state, self.cfg.paths.media, 50, 10)
        with mock.patch.object(c, "status_code", return_value=200):
            col.round(out)
        status = json.loads(out.read_text())
        texts = [a["text"] for a in status["attention"]]
        self.assertTrue(any(t.startswith("Indexers switched off after failures") for t in texts))
        self.assertTrue(any(t.startswith("Not moving for over a day: Stuck.Show.S01") for t in texts))
        self.assertTrue(any("picture subtitles burned into the video" in t for t in texts))
        self.assertEqual(status["playing"][0]["progress"], 25)
        self.assertEqual(status["sonarr"]["queue"], 0)
        self.assertEqual(status["radarr"]["queue"], 1)
        self.assertEqual(status["subtitles"], {"missing_episodes": 1, "missing_movies": 1})
        self.assertEqual(status["indexer_stats"], {"queries": 10, "grabs": 2, "failed": 1})
        self.assertEqual(status["downloads"]["stalled"], 1)
        self.assertIn("requests_live", status)


class FullChecks(Library):
    """nix run .#test against the fakes, with a stand-in for nginx"""

    def dashboard(self) -> FakeService:
        d = FakeService("Dashboard")
        landing = (Path(__file__).resolve().parent.parent / "landing.html").read_bytes()
        status = {"updated": time.time(), "requests_live": [], "upcoming": [], "health": {}}

        @d.route("GET", "/")
        def index(req):
            return 200, landing, {"Content-Type": "text/html"}

        @d.route("GET", "/status.json")
        def status_json(req):
            return status

        @d.route("GET", "/health")  # (Byparr's)
        def health(req):
            return {"status": "ok"}

        for path in ("/api/qbt/torrents/info", "/api/jellyfin/Items"):
            d.route("GET", path)(lambda req: [])

        @d.route("GET", "/api/sabnzbd")
        def sab(req):
            return (200, {"queue": {}}) if req.arg("mode") in ("queue", "history") else (403, None)

        @d.route("POST", "/api/jellyfin/Items")
        def no_writes(req):
            return 403, None
        d.start()
        self.addCleanup(d.stop)
        return d

    def test_all_checks_pass(self):
        d = self.dashboard()
        self.stack.prowlarr.statuses = []
        with mock.patch.dict("os.environ", {"MEDIASERVER_URL_DASHBOARD": d.url, "MEDIASERVER_URL_BYPARR": d.url}), \
                mock.patch.object(launchd, "state", return_value="running (pid 1)"), \
                mock.patch.object(verify.shutil, "which", return_value=None), mock.patch.object(verify.os, "access", return_value=False), \
                mock.patch.object(verify.subprocess, "run", return_value=mock.Mock(stdout="", returncode=1)), \
                mock.patch.object(verify.time, "sleep"):
            failed, out = quiet(verify.main, self.cfg)
        failures = [line.strip() for line in out.splitlines() if "✗" in line]
        # The Moonfin checks need the plugin's real web files and repository
        expected = {"Moonbase pinned", "Moonfin web app references nothing on the internet"}
        unexpected = [f for f in failures if not any(e in f for e in expected)]
        self.maxDiff = None
        self.assertEqual(unexpected, [])
        self.assertIn("checks", out)


class NetwatchReconnect(Library):
    def test_retests_indexers_and_resets_bazarr(self):
        nw = netwatch.Netwatch(self.cfg.paths.config, self.cfg.paths.state / "netwatch", self.stack.cleanuparr.url, self.cfg.paths.state / "lock")
        quiet(nw.after_reconnect)
        deadline = time.time() + 10
        while time.time() < deadline and not all(a.tested for a in (self.stack.prowlarr, self.stack.sonarr, self.stack.radarr)):
            time.sleep(0.1)
        self.assertEqual([a.tested for a in (self.stack.prowlarr, self.stack.sonarr, self.stack.radarr)], [1, 1, 1])
        self.assertIn(("POST", "/api/providers"), self.stack.bazarr.writes)

    def test_cleaner_against_cleanuparr(self):
        nw = netwatch.Netwatch(self.cfg.paths.config, self.cfg.paths.state / "netwatch", self.stack.cleanuparr.url, self.cfg.paths.state / "lock")
        self.assertTrue(quiet(nw.set_cleaner, False)[0])
        self.assertFalse(self.stack.cleanuparr.queue_cleaner["enabled"])
        self.assertTrue(quiet(nw.set_cleaner, True)[0])
        self.assertTrue(self.stack.cleanuparr.queue_cleaner["enabled"])


class PostimportArr(Library):
    """postimport's Sonarr/Radarr client against the fakes"""

    def arr(self, name="Radarr"):
        app = postimport.Arr(name, self.stack.services[name.lower()].url, "movie" if name == "Radarr" else "series")
        patcher = mock.patch.object(postimport, "CONFIG", self.cfg.paths.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        return app

    def test_every_page_of_history(self):
        self.stack.radarr.history = [{"id": n, "eventTypeId": 3, "movieId": 1} for n in range(1, 131)]
        records = self.arr().imports_after(5, page_size=50)
        self.assertEqual([r["id"] for r in records], list(range(6, 131)))
        self.assertEqual(self.arr().latest_import_id(), 130)

    def test_reject_deletes_the_file_and_fails_the_release(self):
        self.stack.radarr.history = [{"id": 1, "eventTypeId": 1, "downloadId": "ABC", "movieId": self.ready["id"]}]
        app = self.arr()
        self.assertTrue(app.reject({"downloadId": "ABC", "data": {"fileId": "7"}}))
        self.assertEqual((self.stack.radarr.deleted_files, self.stack.radarr.failed), ([("moviefile", 7)], [1]))
        self.assertFalse(app.reject({"downloadId": "OTHER", "data": {"fileId": "8"}}))

    def test_item_details(self):
        title, minutes, japanese, key = self.arr().item({"movieId": self.ready["id"]})
        self.assertEqual((title, minutes, japanese, key), ("Ready Film (2020)", 100, False, f"radarr:{self.ready['id']}"))
        title, minutes, japanese, key = self.arr("Sonarr").item({"seriesId": self.show["id"], "episodeId": 1})
        self.assertEqual((title, minutes, japanese), ("Anime Show S01E01", 24, True))
        self.assertTrue(self.arr().has_file(f"radarr:{self.ready['id']}"))
        self.arr().rescan({"movieId": self.ready["id"]})
        self.assertEqual(self.stack.radarr.commands[-1], {"name": "RescanMovie", "movieId": self.ready["id"]})


if __name__ == "__main__":
    unittest.main()
