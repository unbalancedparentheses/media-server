"""Deleting a title from the dashboard (mediaserver/control.py: Library),
against the fake services: through Radarr/Sonarr with its files, its
torrents, its Seerr entry, a Jellyfin rescan; only the dashboard's own requests.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import http.client
import json

from mediaserver import control
from tests.test_e2e import first
from tests.test_integration import Stack


class Delete(Stack):
    def setUp(self):
        super().setUp()
        s = self.stack
        s.jellyfin.add_user("admin", "admin-pass")
        s.jellyfin.wizard_done = True
        (self.cfg.paths.state / "dashstatus").mkdir(parents=True, exist_ok=True)
        (self.cfg.paths.state / "dashstatus/jellyfin-key").write_text("jf-key")
        self.cfg.paths.config_file.write_text('[jellyfin]\nusername = "admin"\npassword = "admin-pass"\n')
        self.movie = s.radarr.resources["movie"].add({"title": "Skyfall", "year": 2012, "tmdbId": 37724, "sizeOnDisk": 30 * 1024 ** 3})
        self.series = s.sonarr.resources["series"].add({"title": "Kaiji", "tvdbId": 100, "statistics": {"sizeOnDisk": 11 * 1024 ** 3},
                                                       "seasons": [{"seasonNumber": 1, "monitored": True, "statistics": {"sizeOnDisk": 11 * 1024 ** 3}},
                                                                   {"seasonNumber": 2, "monitored": True, "statistics": {"sizeOnDisk": 0}}]})
        s.sonarr.episodes = [{"id": 501, "seriesId": self.series["id"], "seasonNumber": 1}, {"id": 502, "seriesId": self.series["id"], "seasonNumber": 2}]
        s.jellyfin.items = [{"Id": "jfmovie", "Type": "Movie", "Name": "Skyfall", "ProviderIds": {"Tmdb": "37724"}},
                            {"Id": "jfseries", "Type": "Series", "Name": "Kaiji", "ProviderIds": {"Tvdb": "100", "Tmdb": "42951"}}]
        self.deleted_hashes, self.seerr_deleted, self.refreshed = [], [], []

        @first(s.jellyfin, "GET", "/Items")
        def items(req):
            ids = req.arg("Ids").split(",")
            return {"Items": [i for i in s.jellyfin.items if i["Id"] in ids]}

        # Grabs (eventType 1) and imports (3, with each file's original
        # path: the proof a torrent's files are this title's)
        self.radarr_history = {"1": [{"downloadId": "ABCDEF", "sourceTitle": "Skyfall.2012.1080p"}],
                               "3": [{"downloadId": "ABCDEF", "data": {"droppedPath": "/dl/Skyfall.2012.1080p/Skyfall.mkv"}}]}
        self.sonarr_history = {"1": [{"downloadId": "S1PACK", "episodeId": 501, "sourceTitle": "Kaiji S01"},
                                     {"downloadId": "S2EP", "episodeId": 502, "sourceTitle": "Kaiji S02E01"}],
                               "3": [{"downloadId": "S1PACK", "episodeId": 501, "data": {"droppedPath": f"/dl/Kaiji S01/Kaiji.S01E{n:02d}.mkv"}}
                                     for n in range(1, 4)]
                                    + [{"downloadId": "S2EP", "episodeId": 502, "data": {"droppedPath": "/dl/Kaiji.S02E01.mkv"}}]}

        @first(s.radarr, "GET", r"/api/v3/history/movie")
        def radarr_history(req):
            return self.radarr_history[req.arg("eventType")]

        @first(s.sonarr, "GET", r"/api/v3/history/series")
        def sonarr_history(req):
            return self.sonarr_history[req.arg("eventType")]

        @first(s.sonarr, "GET", r"/api/v3/episodefile")
        def files(req):
            return [{"id": 47, "seasonNumber": 1, "size": 5 * 1024 ** 3}, {"id": 48, "seasonNumber": 1, "size": 6 * 1024 ** 3},
                    {"id": 60, "seasonNumber": 2, "size": 1}]

        # What's inside each torrent, and which ones qBittorrent has
        self.contents = {"abcdef": [{"name": "Skyfall.2012.1080p/Skyfall.mkv", "size": 30 * 1024 ** 3}],
                         "s1pack": [{"name": f"Kaiji S01/Kaiji.S01E{n:02d}.mkv", "size": 400 * 1024 ** 2} for n in range(1, 4)],
                         "s2ep": [{"name": "Kaiji.S02E01.mkv", "size": 1}]}

        @first(s.qbittorrent, "GET", "/api/v2/torrents/files")
        def contents(req):
            files = self.contents.get(req.arg("hash"))
            return (200, files) if files is not None else (404, None)

        @first(s.qbittorrent, "GET", "/api/v2/torrents/info")
        def info(req):
            return [{"hash": req.arg("hashes"), "save_path": "/dl"}] if req.arg("hashes") in self.contents else []

        @first(s.qbittorrent, "POST", "/api/v2/torrents/delete")
        def qdelete(req):
            self.deleted_hashes.append((req.form()["hashes"], req.form()["deleteFiles"]))
            return 200, b""

        @first(s.seerr, "GET", r"/api/v1/(movie|tv)/(\d+)")
        def details(req, kind, tmdb):
            return {"mediaInfo": {"id": 900 if kind == "movie" else 901}}

        @first(s.seerr, "DELETE", r"/api/v1/media/(\d+)")
        def media(req, mid):
            self.seerr_deleted.append(int(mid))
            return 204, None

        self.library = control.Library(self.cfg.paths.config)

    def test_a_film_goes_completely(self):
        preview = self.library.resolve("jfmovie")
        self.assertEqual((preview["title"], preview["size"]), ("Skyfall (2012)", 30 * 1024 ** 3))
        result = self.library.delete("jfmovie", None, False)
        self.assertEqual((result["deleted"], result["files_removed"], result["torrents"], result["torrents_kept"]),
                         ("Skyfall (2012)", 30 * 1024 ** 3, 1, []))
        self.assertIn("disk_freed", result)   # measured, separately from the estimate
        self.assertFalse(self.library.plans())   # nothing left to finish
        self.assertEqual(self.stack.radarr.resources["movie"].items, [])
        self.assertIn(("DELETE", f"/api/v3/movie/{self.movie['id']}"), self.stack.radarr.writes)
        self.assertEqual(self.deleted_hashes, [("abcdef", "true")])   # the seeding copy, with its data
        self.assertEqual(self.seerr_deleted, [900])                    # requestable again
        self.assertIn(("POST", "/Library/Refresh"), self.stack.jellyfin.writes)

    def test_one_season_only(self):
        preview = self.library.resolve("jfseries")
        self.assertEqual([x["number"] for x in preview["seasons"]], [1])   # only seasons with files
        result = self.library.delete("jfseries", 1, False)
        self.assertEqual((result["deleted"], result["files_removed"]), ("Kaiji season 1", 11 * 1024 ** 3))
        series = self.stack.sonarr.resources["series"].items[0]
        self.assertEqual([(x["seasonNumber"], x["monitored"]) for x in series["seasons"]], [(1, False), (2, True)])
        self.assertEqual(sorted(self.stack.sonarr.deleted_files), [("episodefile", 47), ("episodefile", 48)])
        self.assertEqual(self.deleted_hashes, [("s1pack", "true")])   # not season 2's
        self.assertEqual(self.seerr_deleted, [])                      # the series stays

    def test_a_torrent_with_other_seasons_is_kept(self):
        """A pack of seasons 1 and 2: deleting season 1 mustn't take it"""
        self.sonarr_history["1"] = [{"downloadId": "BOTH", "episodeId": 501, "sourceTitle": "Kaiji S01-S02"},
                                    {"downloadId": "BOTH", "episodeId": 502, "sourceTitle": "Kaiji S01-S02"}]
        self.sonarr_history["3"] = [{"downloadId": "BOTH", "episodeId": 501, "data": {"droppedPath": "/dl/x/Kaiji.S01E01.mkv"}},
                                    {"downloadId": "BOTH", "episodeId": 502, "data": {"droppedPath": "/dl/x/Kaiji.S02E01.mkv"}}]
        self.contents["both"] = [{"name": "x/Kaiji.S01E01.mkv"}, {"name": "x/Kaiji.S02E01.mkv"}]
        result = self.library.delete("jfseries", 1, False)
        self.assertEqual(self.deleted_hashes, [])
        self.assertEqual(result["torrents_kept"], ["Kaiji S01-S02"])

    def test_a_video_that_wasnt_imported_for_it_keeps_the_torrent(self):
        """Proof, not a guess: a file in the torrent that no import of this
        season came from (no season in its name at all) keeps it"""
        self.contents["s1pack"].append({"name": "Kaiji S01/Bonus Movie.mkv", "size": 2 * 1024 ** 3})
        result = self.library.delete("jfseries", 1, False)
        self.assertEqual(self.deleted_hashes, [])
        self.assertEqual(result["torrents_kept"], ["Kaiji S01"])

    def test_same_file_names_in_different_season_folders(self):
        """Season 1/01.mkv proves nothing about Season 2/01.mkv: whole paths
        are compared, so a pack of both seasons is kept"""
        self.sonarr_history["1"] = [{"downloadId": "PACK", "episodeId": 501, "sourceTitle": "Kaiji Complete"}]
        self.sonarr_history["3"] = [{"downloadId": "PACK", "episodeId": 501, "data": {"droppedPath": "/dl/Kaiji/Season 1/01.mkv"}}]
        self.contents["pack"] = [{"name": "Kaiji/Season 1/01.mkv"}, {"name": "Kaiji/Season 2/01.mkv"}]
        result = self.library.delete("jfseries", 1, False)
        self.assertEqual(self.deleted_hashes, [])
        self.assertEqual(result["torrents_kept"], ["Kaiji Complete"])

    def test_a_video_called_sample_still_needs_proof(self):
        """'Sample' is no exemption: a film titled that, or a sample nobody
        imported, keeps the torrent"""
        self.contents["abcdef"].append({"name": "Skyfall.2012.1080p/Sample/skyfall-sample.mkv", "size": 50 * 1024 ** 2})
        result = self.library.delete("jfmovie", None, False)
        self.assertEqual(self.deleted_hashes, [])
        self.assertEqual(len(result["torrents_kept"]), 1)

    def test_a_pack_of_films_is_kept_whatever_the_sizes(self):
        self.contents["abcdef"] = [{"name": "Bond/Skyfall.mkv", "size": 30 * 1024 ** 3}, {"name": "Bond/Spectre.mkv", "size": 100 * 1024 ** 2}]
        result = self.library.delete("jfmovie", None, False)
        self.assertEqual(self.deleted_hashes, [])
        self.assertEqual(len(result["torrents_kept"]), 1)
        self.assertEqual(self.stack.radarr.resources["movie"].items, [])   # the film itself goes

    def test_whole_series_still_needs_proof(self):
        """Deleting the whole series doesn't take a torrent with another series in it"""
        self.contents["s2ep"].append({"name": "Other.Show.S01E01.mkv"})
        self.library.delete("jfseries", None, False)
        self.assertEqual(self.deleted_hashes, [("s1pack", "true")])

    def test_unfinished_downloads_stay_in_qbittorrent(self):
        removed = []
        first(self.stack.radarr, "GET", "/api/v3/queue")(lambda req: {"records": [{"id": 5, "title": "Skyfall.2160p"}]})
        first(self.stack.radarr, "DELETE", r"/api/v3/queue/(\d+)")(lambda req, qid: (removed.append(req.arg("removeFromClient")), (200, {}))[1])
        result = self.library.delete("jfmovie", None, False)
        self.assertEqual(removed, ["false"])   # out of Radarr's queue, left in qBittorrent
        self.assertTrue(any("still downloading" in k for k in result["torrents_kept"]))

    def test_a_damaged_plans_file_is_kept_not_emptied(self):
        self.library.plans_file.write_text("{damaged")
        with self.assertRaises(control.PlanTrouble):
            self.library.delete("jfmovie", None, False)
        self.assertEqual(self.library.plans_file.read_text(), "{damaged")
        self.assertEqual(len(self.stack.radarr.resources["movie"].items), 1)   # nothing deleted

    def test_seerr_not_answering_leaves_its_step(self):
        broken = first(self.stack.seerr, "GET", r"/api/v1/movie/\d+")(lambda req: (503, None))
        from mediaserver.api import ApiError
        with self.assertRaises(ApiError):
            self.library.delete("jfmovie", None, False)
        self.assertEqual(self.library.plans()["jfmovie"]["steps"], ["seerr", "refresh"])
        self.stack.seerr.routes = [r for r in self.stack.seerr.routes if r[2] is not broken]
        self.library.delete("jfmovie", None, False)
        self.assertEqual(self.seerr_deleted, [900])

    def test_a_failure_after_the_title_is_gone_is_finished_by_trying_again(self):
        s = self.stack
        broken = first(s.qbittorrent, "POST", "/api/v2/torrents/delete")(lambda req: (500, None))
        from mediaserver.api import ApiError
        with self.assertRaises(ApiError):
            self.library.delete("jfmovie", None, False)
        self.assertEqual(s.radarr.resources["movie"].items, [])   # already gone from Radarr
        plan = self.library.plans()["jfmovie"]
        self.assertEqual(plan["steps"], ["torrents", "seerr", "refresh"])
        # Radarr no longer knows it, yet trying again finishes the rest
        s.qbittorrent.routes = [r for r in s.qbittorrent.routes if r[2] is not broken]
        result = self.library.delete("jfmovie", None, False)
        self.assertTrue(result["resumed"])
        self.assertEqual(self.deleted_hashes, [("abcdef", "true")])
        self.assertEqual(self.seerr_deleted, [900])
        self.assertFalse(self.library.plans())

    def test_the_whole_series(self):
        self.library.delete("jfseries", None, True)
        self.assertEqual(self.stack.sonarr.resources["series"].items, [])
        self.assertEqual(self.seerr_deleted, [901])

    def test_by_radarr_or_sonarr_id_for_what_never_arrived(self):
        film = self.library.resolve(f"radarr{self.movie['id']}")
        self.assertEqual((film["kind"], film["title"], film["tmdb"]), ("movie", "Skyfall (2012)", 37724))
        series = self.library.resolve(f"sonarr{self.series['id']}")
        # A season with nothing downloaded is offered too: to stop looking for it
        self.assertEqual([x["number"] for x in series["seasons"]], [1, 2])
        self.assertEqual([x["number"] for x in self.library.resolve("jfseries")["seasons"]], [1])
        with self.assertRaises(LookupError):
            self.library.resolve("radarr999")

    def test_stop_looking_for_a_season_deletes_nothing(self):
        deletes = []

        @first(self.stack.sonarr, "DELETE", r"/api/v3/episodefile/(\d+)")
        def gone(req, fid):
            deletes.append(fid)
            return {}
        result = self.library.delete(f"sonarr{self.series['id']}", 2, False, stop=True)
        self.assertEqual(result["stopped"], "Kaiji season 2")
        series = self.stack.sonarr.resources["series"].items[0]
        self.assertFalse(next(x for x in series["seasons"] if x["seasonNumber"] == 2)["monitored"])
        self.assertTrue(next(x for x in series["seasons"] if x["seasonNumber"] == 1)["monitored"])
        self.assertEqual(deletes, [])   # the season's downloaded episode (file 60) stays
        self.assertEqual(self.deleted_hashes, [])

    def test_stop_looking_for_a_film_keeps_it(self):
        self.library.delete(f"radarr{self.movie['id']}", None, False, stop=True)
        movie = self.stack.radarr.resources["movie"].items[0]
        self.assertFalse(movie["monitored"])
        with self.assertRaises(LookupError):   # a series needs the season
            self.library.plan(f"sonarr{self.series['id']}", None, False, stop=True)

    def test_an_episode_card_means_its_series(self):
        self.stack.jellyfin.items.append({"Id": "jfep", "Type": "Episode", "SeriesId": "jfseries"})
        self.assertEqual(self.library.resolve("jfep")["title"], "Kaiji")


class Endpoint(Delete):
    def ask(self, method, path, body=None, headers=None):
        server = control.serve(self.cfg.paths.config, 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = json.loads(r.read() or b"{}")
        conn.close()
        return r.status, data

    def post(self, data):
        return self.ask("POST", "/delete", json.dumps(data), {"Content-Type": "application/json", "X-Requested-With": "media-server"})

    def test_deletes(self):
        status, data = self.post({"item": "jfmovie"})
        self.assertEqual((status, data["deleted"]), (200, "Skyfall (2012)"))

    def test_not_during_an_install(self):
        import subprocess
        import sys
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from mediaserver import lock; "
                "lock.acquire(Path(sys.argv[2])); print('held', flush=True); time.sleep(30)")
        from tests.test_integration import REPO
        owner = subprocess.Popen([sys.executable, "-c", code, str(REPO), str(self.cfg.paths.state)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(owner.kill)
        assert owner.stdout is not None
        self.assertEqual(owner.stdout.readline().strip(), "held")
        status, data = self.post({"item": "jfmovie"})
        self.assertEqual(status, 409)
        self.assertEqual(len(self.stack.radarr.resources["movie"].items), 1)

    def test_preview_and_bad_requests(self):
        status, data = self.ask("GET", "/delete?item=jfmovie")
        self.assertEqual((status, data["kind"]), (200, "movie"))
        self.assertEqual(self.ask("GET", "/delete?item=nope")[0], 404)
        for bad in ({"item": "../x"}, {}, {"item": "jfmovie", "season": "1"}, {"item": "jfmovie", "exclude": "yes"},
                    {"item": "jfmovie", "stop": "yes"}, [], "x"):
            self.assertEqual(self.post(bad)[0], 400, bad)
        # Not the dashboard's own request
        self.assertEqual(self.ask("POST", "/delete", json.dumps({"item": "jfmovie"}),
                                  {"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(len(self.stack.radarr.resources["movie"].items), 1)
