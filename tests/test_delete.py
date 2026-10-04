"""Deleting a title from the dashboard (mediaserver/control.py: Library),
against the fake services: through Radarr/Sonarr with its files, its
torrents, its Seerr entry, a Jellyfin rescan; and only with the password.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import http.client
import json
from pathlib import Path
from unittest import mock

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

        @first(s.radarr, "GET", r"/api/v3/history/movie")
        def radarr_grabs(req):
            return [{"downloadId": "ABCDEF"}]

        @first(s.sonarr, "GET", r"/api/v3/history/series")
        def sonarr_grabs(req):
            return [{"downloadId": "S1PACK", "episodeId": 501}, {"downloadId": "S2EP", "episodeId": 502}]

        @first(s.sonarr, "GET", r"/api/v3/episodefile")
        def files(req):
            return [{"id": 47, "seasonNumber": 1, "size": 5 * 1024 ** 3}, {"id": 48, "seasonNumber": 1, "size": 6 * 1024 ** 3},
                    {"id": 60, "seasonNumber": 2, "size": 1}]

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
        self.assertEqual(result, {"deleted": "Skyfall (2012)", "freed": 30 * 1024 ** 3, "torrents": 1, "warnings": []})
        self.assertEqual(self.stack.radarr.resources["movie"].items, [])
        self.assertIn(("DELETE", f"/api/v3/movie/{self.movie['id']}"), self.stack.radarr.writes)
        self.assertEqual(self.deleted_hashes, [("abcdef", "true")])   # the seeding copy, with its data
        self.assertEqual(self.seerr_deleted, [900])                    # requestable again
        self.assertIn(("POST", "/Library/Refresh"), self.stack.jellyfin.writes)

    def test_one_season_only(self):
        preview = self.library.resolve("jfseries")
        self.assertEqual([x["number"] for x in preview["seasons"]], [1])   # only seasons with files
        result = self.library.delete("jfseries", 1, False)
        self.assertEqual((result["deleted"], result["freed"]), ("Kaiji season 1", 11 * 1024 ** 3))
        series = self.stack.sonarr.resources["series"].items[0]
        self.assertEqual([(x["seasonNumber"], x["monitored"]) for x in series["seasons"]], [(1, False), (2, True)])
        self.assertEqual(sorted(self.stack.sonarr.deleted_files), [("episodefile", 47), ("episodefile", 48)])
        self.assertEqual(self.deleted_hashes, [("s1pack", "true")])   # not season 2's
        self.assertEqual(self.seerr_deleted, [])                      # the series stays

    def test_the_whole_series(self):
        self.library.delete("jfseries", None, True)
        self.assertEqual(self.stack.sonarr.resources["series"].items, [])
        self.assertEqual(self.seerr_deleted, [901])

    def test_an_episode_card_means_its_series(self):
        self.stack.jellyfin.items.append({"Id": "jfep", "Type": "Episode", "SeriesId": "jfseries"})
        self.assertEqual(self.library.resolve("jfep")["title"], "Kaiji")

    def test_password(self):
        self.assertTrue(self.library.password_ok("admin-pass"))
        self.assertFalse(self.library.password_ok("guess"))


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

    def test_wrong_password_deletes_nothing(self):
        with mock.patch("time.sleep"):
            status, data = self.post({"item": "jfmovie", "password": "guess"})
        self.assertEqual((status, data["error"]), (403, "That's not the Jellyfin password"))
        self.assertEqual(len(self.stack.radarr.resources["movie"].items), 1)

    def test_right_password_deletes(self):
        status, data = self.post({"item": "jfmovie", "password": "admin-pass"})
        self.assertEqual((status, data["deleted"]), (200, "Skyfall (2012)"))

    def test_preview_and_bad_requests(self):
        status, data = self.ask("GET", "/delete?item=jfmovie")
        self.assertEqual((status, data["kind"]), (200, "movie"))
        self.assertEqual(self.ask("GET", "/delete?item=nope")[0], 404)
        for bad in ({"item": "../x", "password": "p"}, {"item": "jfmovie"}, {"item": "jfmovie", "password": "p", "season": "1"}):
            self.assertEqual(self.post(bad)[0], 400, bad)
        # Not the dashboard's own request
        self.assertEqual(self.ask("POST", "/delete", json.dumps({"item": "jfmovie", "password": "admin-pass"}),
                                  {"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(len(self.stack.radarr.resources["movie"].items), 1)
