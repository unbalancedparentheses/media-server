"""The end-to-end test's own logic (mediaserver/e2e.py), run against the
fake services: a whole run that passes, its cleanup, and what happens when
a service doesn't answer or doesn't do its part.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import hashlib
import io
import json
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mediaserver import common as c
from mediaserver import e2e
from mediaserver.ui import SetupError
from tests.test_integration import Stack, quiet


class Clock:
    """time for the e2e module and Sonarr's wait: sleeping only moves the
    clock, so waits that time out finish at once"""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


def first(service, method, pattern):
    """A route checked before the fake's own"""
    import re

    def register(fn):
        service.routes.insert(0, (method, re.compile(pattern + r"/?$"), fn))
        return fn
    return register


def bdecode(data: bytes, i: int = 0):
    """(value, end) for one bencoded value starting at i"""
    if data[i:i + 1] == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if data[i:i + 1] in (b"l", b"d"):
        items, j = [], i + 1
        while data[j:j + 1] != b"e":
            value, j = bdecode(data, j)
            items.append(value)
        return (items if data[i:i + 1] == b"l" else dict(zip(items[::2], items[1::2]))), j + 1
    colon = data.index(b":", i)
    length = int(data[i:colon])
    return data[colon + 1:colon + 1 + length], colon + 1 + length


class World(Stack):
    """Fake services that do their part of the import: Seerr hands Radarr
    the movie, a pushed release is imported, Jellyfin and Bazarr pick it up"""

    def setUp(self):
        super().setUp()
        s = self.stack
        s.jellyfin.add_user("admin", "admin-pass")
        s.jellyfin.wizard_done = True
        self.clock = Clock()
        for target in ("mediaserver.e2e.time", "mediaserver.steps.arrs.time"):
            patcher = mock.patch(target, self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.src = self.root / "source.mov"
        self.src.write_bytes(b"film" * 5000)
        patcher = mock.patch.object(e2e.E2E, "source", lambda _: self.src)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.movies = s.radarr.resources["movie"]
        self.series = s.sonarr.resources["series"]
        self.radarr_indexers = s.radarr.resources["indexer"]
        self.radarr_indexers.add({"name": "Nyaa", "enableAutomaticSearch": True, "enableRss": True})
        self.radarr_indexers.add({"name": "Off", "enableAutomaticSearch": False, "enableRss": False})
        self.deleted_hashes: list = []
        self.seerr_status = 0
        self.seerr_media: dict | None = None
        self.imports = True      # Radarr/Sonarr import a pushed release
        self.subtitles = True    # Bazarr finds subtitles
        self.wire()

    def wire(self):
        s = self.stack
        radarr, sonarr, seerr, bazarr, jf, qbit = s.radarr, s.sonarr, s.seerr, s.bazarr, s.jellyfin, s.qbittorrent

        # Seerr: a request adds the movie to Radarr
        @first(seerr, "POST", "/api/v1/request")
        def request(req):
            self.movies.add({"tmdbId": e2e.MOVIE_TMDB, "title": "Tears of Steel", "rootFolderPath": str(self.cfg.paths.movies), "hasFile": False})
            self.seerr_media = {"id": 9, "status": 2}
            return 201, {"id": 4, "media": {"id": 9}}

        @first(seerr, "GET", rf"/api/v1/movie/{e2e.MOVIE_TMDB}")
        def details(req):
            return {"title": "Tears of Steel", **({"mediaInfo": {**self.seerr_media, "status": self.seerr_status}} if self.seerr_media else {})}

        @first(seerr, "GET", "/api/v1/settings/jobs")
        def jobs(req):
            return [{"id": "jellyfin-recently-added-scan", "running": False}]

        @first(seerr, "POST", r"/api/v1/settings/jobs/([\w-]+)/run")
        def run_job(req, job):
            if any(i.get("Type") == "Movie" for i in jf.items):
                self.seerr_status = 5
            return 200, {}

        @first(seerr, "DELETE", r"/api/v1/media/(\d+)")
        def delete_media(req, mid):
            self.seerr_media = None
            return 204, None

        # Radarr: a pushed release is imported, renamed into the library
        @first(radarr, "POST", "/api/v3/release/push")
        def push_movie(req):
            movie = self.movies.items[0]
            if self.imports:
                folder = self.cfg.paths.movies / "Tears of Steel (2012)"
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / "Tears of Steel (2012).mov"
                path.write_bytes(b"x")
                movie.update(hasFile=True, movieFile={"path": str(path), "relativePath": path.name})
                jf.items.append({"Type": "Movie", "Name": "Tears of Steel", "Path": str(path), "ProviderIds": {"Tmdb": str(e2e.MOVIE_TMDB)}})
            return [{"title": req.json()["title"], "approved": True}]

        @first(radarr, "DELETE", r"/api/v3/queue/(\d+)")
        @first(sonarr, "DELETE", r"/api/v3/queue/(\d+)")
        def delete_queue(req, qid):
            return 200, {}

        # Sonarr
        @first(sonarr, "GET", "/api/v3/series/lookup")
        def lookup(req):
            return [{"title": "Pioneer One", "tvdbId": e2e.SERIES_TVDB, "seasons": []}]

        @first(sonarr, "POST", "/api/v3/series")
        def add_series(req):
            series = self.series.add(req.json())
            sonarr.episodes.append({"id": 31, "seriesId": series["id"], "seasonNumber": 1, "episodeNumber": 1, "monitored": False, "hasFile": False})
            return 201, series

        @first(sonarr, "PUT", "/api/v3/episode/monitor")
        def monitor(req):
            for e in sonarr.episodes:
                if e["id"] in req.json()["episodeIds"]:
                    e["monitored"] = req.json()["monitored"]
            return 202, {}

        @first(sonarr, "POST", "/api/v3/release/push")
        def push_episode(req):
            episode = sonarr.episodes[0]
            approved = episode["monitored"] and self.series.items[0].get("monitored")
            if approved and self.imports:
                path = self.cfg.paths.tv / "Pioneer One/Season 01/Pioneer One - S01E01 - Earthfall.mov"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
                episode.update(hasFile=True, path=str(path))
                jf.items.append({"Type": "Episode", "Name": "Earthfall", "Path": str(path)})
            return [{"approved": approved, "rejections": [] if approved else ["Episode wasn't requested"]}]

        @first(sonarr, "GET", "/api/v3/episodefile")
        def episode_files(req):
            return [{"path": e["path"], "relativePath": Path(e["path"]).name} for e in sonarr.episodes if e.get("hasFile")]

        @first(sonarr, "DELETE", r"/api/v3/series/(\d+)")
        def delete_series(req, sid):
            self.series.delete(int(sid))
            sonarr.episodes.clear()
            return 200, {}

        # Jellyfin forgets files Radarr/Sonarr deleted at the next scan
        @first(jf, "POST", "/Library/Refresh")
        def refresh(req):
            if not self.movies.items:
                jf.items[:] = [i for i in jf.items if i["Type"] != "Movie"]
            if not self.series.items:
                jf.items[:] = [i for i in jf.items if i["Type"] != "Episode"]
            return 204, None

        # Bazarr: knows the movie once Radarr has it; a search saves English subtitles
        @first(bazarr, "GET", "/api/movies")
        def bazarr_movies(req):
            return {"data": [{"radarrId": m["id"]} for m in self.movies.items]}

        @first(bazarr, "PATCH", "/api/movies")
        def search(req):
            movie = self.movies.items[0]
            if self.subtitles and movie.get("hasFile"):
                Path(movie["movieFile"]["path"]).with_suffix(".en.srt").write_text("1\n")
            return 204, None

        @first(qbit, "POST", "/api/v2/torrents/delete")
        def delete_torrents(req):
            self.deleted_hashes += req.form()["hashes"].split("|")
            return 200, b""

    def run_e2e(self, keep=False):
        test = e2e.E2E(self.cfg)
        failed, out = quiet(test.run, keep)
        return test, failed, out


class WholeRun(World):
    def test_passes_and_leaves_nothing_behind(self):
        test, failed, out = self.run_e2e()
        self.assertEqual(failed, 0, out)
        self.assertEqual(test.passed, 11, out)
        for line in ("Seerr → Radarr: movie added", "qBittorrent → Radarr: imported automatically (Tears of Steel (2012).mov)",
                     "Radarr → Jellyfin: in the library", "English subtitles downloaded (Tears of Steel (2012).en.srt)",
                     "Jellyfin → Seerr: shown as available", "Sonarr: series added (no indexer search)",
                     "qBittorrent → Sonarr: imported automatically (Pioneer One - S01E01 - Earthfall.mov)",
                     "Sonarr → Jellyfin: episode in the library", "End-to-end test passed: 11 steps"):
            self.assertIn(line, out)
        # Everything it added is gone; its torrents removed from qBittorrent
        self.assertEqual((self.movies.items, self.series.items, self.stack.jellyfin.items, self.seerr_media), ([], [], [], None))
        self.assertEqual(len(self.deleted_hashes), 2)
        self.assertFalse((self.cfg.paths.state / "e2e/owned.json").exists())
        self.assertEqual(list((self.cfg.paths.downloads / "torrents/complete/radarr").iterdir()), [])
        # Automatic search was off during the test and is back on after
        self.assertEqual([(i["enableAutomaticSearch"], i["enableRss"]) for i in self.radarr_indexers.items], [(True, True), (False, False)])
        self.assertIn("Paused automatic search on 1 Radarr indexers", out)
        self.assertFalse((self.cfg.paths.state / "e2e/paused-indexers.json").exists())

    def test_keep_leaves_the_items(self):
        _, failed, out = self.run_e2e(keep=True)
        self.assertEqual(failed, 0, out)
        self.assertIn("--keep", out)
        self.assertEqual(len(self.movies.items), 1)
        owned = c.read_json(self.cfg.paths.state / "e2e/owned.json")
        self.assertEqual((owned["movie_id"], owned["seerr_media_id"], len(owned["hashes"]), len(owned["library_paths"])), (1, 9, 2, 2))
        # The next run removes them first
        test, failed, out = self.run_e2e()
        self.assertIn("Removing what an interrupted earlier run left behind", out)
        self.assertEqual(failed, 0, out)

    def test_nothing_imported_is_reported_and_still_cleaned_up(self):
        self.imports = False
        test, failed, out = self.run_e2e()
        self.assertIn("Radarr did not import it within 5 minutes", out)
        self.assertIn("Jellyfin didn't pick it up within 3 minutes", out)
        self.assertIn("Sonarr did not import it within 5 minutes", out)
        self.assertIn("Bazarr found no English subtitles", out)
        self.assertGreaterEqual(failed, 5)
        self.assertEqual((self.movies.items, self.series.items), ([], []))
        self.assertIn("steps failed", out)

    def test_no_subtitles(self):
        self.subtitles = False
        _, failed, out = self.run_e2e()
        self.assertEqual(failed, 1, out)
        self.assertIn("Bazarr found no English subtitles within 4 minutes (providers: ", out)

    def test_existing_titles_are_never_touched(self):
        self.movies.add({"tmdbId": e2e.MOVIE_TMDB, "title": "Tears of Steel"})
        self.series.add({"tvdbId": e2e.SERIES_TVDB, "title": "Pioneer One"})
        with self.assertRaises(SetupError) as raised:
            self.run_e2e()
        self.assertIn("Tears of Steel is in Radarr; Pioneer One is in Sonarr;", raised.exception.message)
        self.assertEqual(len(self.movies.items), 1)
        self.assertNotIn(("POST", "/api/v3/release/push"), self.stack.radarr.writes)

    def test_crash_mid_run_still_restores_and_cleans_up(self):
        with mock.patch.object(e2e.E2E, "tv", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_e2e()
        self.assertEqual(self.movies.items, [])
        self.assertTrue(self.radarr_indexers.items[0]["enableAutomaticSearch"])
        self.assertFalse((self.cfg.paths.state / "e2e/owned.json").exists())

    def test_seerr_never_hands_radarr_the_movie(self):
        @first(self.stack.seerr, "POST", "/api/v1/request")
        def request(req):
            return 201, {"id": 4}
        _, failed, out = self.run_e2e()
        self.assertIn("Seerr → Radarr: movie never appeared in Radarr", out)
        # The TV part still ran
        self.assertIn("Sonarr → Jellyfin: episode in the library", out)
        self.assertEqual(failed, 1)


class Indexers(World):
    def paused(self, entries):
        f = self.cfg.paths.state / "e2e/paused-indexers.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(entries) if not isinstance(entries, str) else entries)
        return f

    def test_restore_failure_keeps_only_that_one(self):
        self.radarr_indexers.items[1]["enableAutomaticSearch"] = False
        f = self.paused([{"id": 1, "enableAutomaticSearch": True, "enableRss": True}, {"id": 2, "enableAutomaticSearch": True, "enableRss": False}])
        broken = first(self.stack.radarr, "PUT", r"/api/v3/indexer/2")(lambda req: (500, None))
        ok, _ = quiet(e2e.E2E(self.cfg).resume_indexers)
        self.assertFalse(ok)
        self.assertEqual([e["id"] for e in json.loads(f.read_text())], [2])
        self.assertTrue(self.radarr_indexers.items[0]["enableAutomaticSearch"])
        self.stack.radarr.routes = [r for r in self.stack.radarr.routes if r[2] is not broken]
        ok, _ = quiet(e2e.E2E(self.cfg).resume_indexers)
        self.assertTrue(ok)
        self.assertFalse(f.exists())
        self.assertTrue(self.radarr_indexers.items[1]["enableAutomaticSearch"])

    def test_deleted_indexer_counts_as_restored(self):
        f = self.paused([{"id": 99, "enableAutomaticSearch": True, "enableRss": True}])
        ok, _ = quiet(e2e.E2E(self.cfg).resume_indexers)
        self.assertTrue(ok)
        self.assertFalse(f.exists())

    def test_unreadable_record_kept(self):
        for broken in ("{not json", '{"id": 1}', '[{"id": "1"}]'):
            f = self.paused(broken)
            ok, out = quiet(e2e.E2E(self.cfg).resume_indexers)
            self.assertFalse(ok)
            self.assertTrue(f.exists())
            self.assertIn("re-enable automatic search on Radarr's indexers by hand", out)
        self.assertNotIn("PUT", [m for m, _ in self.stack.radarr.writes])

    def test_radarr_down_keeps_record(self):
        f = self.paused([{"id": 1, "enableAutomaticSearch": True, "enableRss": True}])
        first(self.stack.radarr, "GET", r"/api/v3/indexer/1")(lambda req: (503, None))
        ok, _ = quiet(e2e.E2E(self.cfg).resume_indexers)
        self.assertFalse(ok)
        self.assertTrue(f.exists())

    def test_pausing_fails_so_the_test_doesnt_run(self):
        calls = []

        @first(self.stack.radarr, "PUT", r"/api/v3/indexer/(\d+)")
        def put(req, iid):
            calls.append(req.json()["enableAutomaticSearch"])
            if len(calls) == 1:
                return 500, None
            self.radarr_indexers.put(int(iid), req.json())
            return 202, req.json()
        with self.assertRaises(SetupError) as raised:
            quiet(e2e.E2E(self.cfg).pause_indexers)
        self.assertIn("not running the test", raised.exception.message)
        self.assertEqual(calls, [False, True])  # tried to pause, then restored
        self.assertFalse((self.cfg.paths.state / "e2e/paused-indexers.json").exists())

    def test_earlier_pause_not_restorable_stops_the_test(self):
        self.paused("{bad")
        with self.assertRaises(SetupError) as raised:
            quiet(e2e.E2E(self.cfg).pause_indexers)
        self.assertIn("paused by an earlier test couldn't be re-enabled", raised.exception.message)

    def test_install_restores_through_the_command_line(self):
        self.paused([{"id": 2, "enableAutomaticSearch": True, "enableRss": True}])
        with mock.patch("mediaserver.e2e.Config.load", return_value=self.cfg), redirect_stdout(io.StringIO()):
            self.assertEqual(e2e.main(["--resume-indexers"]), 0)
        self.assertTrue(self.radarr_indexers.items[1]["enableRss"])


class Cleanup(World):
    def own(self, **record):
        f = self.cfg.paths.state / "e2e/owned.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(record))
        return f

    def test_radarr_down_keeps_record(self):
        f = self.own(movie_id=42)
        first(self.stack.radarr, "GET", r"/api/v3/movie/42")(lambda req: (503, None))
        ok, out = quiet(e2e.E2E(self.cfg).cleanup)
        self.assertFalse(ok)
        self.assertTrue(f.exists())
        self.assertIn("Couldn't check the test's movie", out)

    def test_already_gone_is_fine(self):
        f = self.own(movie_id=42, series_id=7, seerr_media_id=9)
        ok, _ = quiet(e2e.E2E(self.cfg).cleanup)
        self.assertTrue(ok)
        self.assertFalse(f.exists())
        self.assertNotIn(("DELETE", "/api/v3/movie/42"), self.stack.radarr.writes)

    def test_existing_movie_and_queue_deleted(self):
        movie = self.movies.add({"title": "Tears of Steel"})
        self.stack.radarr.queue = [{"id": 5, "movieId": movie["id"]}]
        first(self.stack.radarr, "GET", "/api/v3/queue")(lambda req: {"records": [{"id": 5}]})
        self.own(movie_id=movie["id"])
        ok, _ = quiet(e2e.E2E(self.cfg).cleanup)
        self.assertTrue(ok)
        self.assertEqual(self.movies.items, [])
        self.assertIn(("DELETE", "/api/v3/queue/5"), self.stack.radarr.writes)

    def test_only_download_folders_are_deleted(self):
        inside = self.cfg.paths.downloads / "torrents/complete/radarr/Test"
        inside.mkdir(parents=True)
        outside = self.cfg.paths.movies / "Keep"
        outside.mkdir(parents=True)
        self.own(paths=[str(inside), str(outside), str(self.cfg.paths.downloads / "torrents/complete"),
                        str(self.cfg.paths.downloads / "torrents/complete/../../../movies")])
        ok, _ = quiet(e2e.E2E(self.cfg).cleanup)
        self.assertTrue(ok)
        self.assertFalse(inside.exists())
        self.assertTrue(outside.exists())
        self.assertTrue((self.cfg.paths.downloads / "torrents/complete").exists())

    def test_qbittorrent_login_failing_keeps_record(self):
        f = self.own(hashes=["abc"])
        self.stack.qbittorrent.login = ("admin", "other")
        ok, _ = quiet(e2e.E2E(self.cfg).cleanup)
        self.assertFalse(ok)
        self.assertTrue(f.exists())

    def test_jellyfin_still_listing_a_test_file_keeps_record(self):
        f = self.own(library_paths=["/m/Tears.mov"])
        self.stack.jellyfin.items = [{"Type": "Movie", "Path": "/m/Tears.mov"}]
        first(self.stack.jellyfin, "POST", "/Library/Refresh")(lambda req: (204, None))
        test = e2e.E2E(self.cfg)
        test.jellyfin.login()
        ok, out = quiet(test.cleanup)
        self.assertFalse(ok)
        self.assertIn("Jellyfin still lists a test item", out)
        self.assertTrue(f.exists())

    def test_jellyfin_asked_to_scan_again_until_it_forgets(self):
        self.own(library_paths=["/m/Tears.mov"])
        self.stack.jellyfin.items = [{"Type": "Movie", "Path": "/m/Tears.mov"}]
        scans = []

        @first(self.stack.jellyfin, "POST", "/Library/Refresh")
        def refresh(req):
            # The first scan was already running and is dropped
            scans.append(1)
            if len(scans) >= 2:
                self.stack.jellyfin.items = []
            return 204, None
        test = e2e.E2E(self.cfg)
        test.jellyfin.login()
        ok, _ = quiet(test.cleanup)
        self.assertTrue(ok)
        self.assertEqual(len(scans), 2)

    def test_seerr_request_without_media_deleted_directly(self):
        self.own(seerr_request_id=4)
        deleted = []
        first(self.stack.seerr, "DELETE", r"/api/v1/request/(\d+)")(lambda req, rid: (deleted.append(rid), (204, None))[1])
        ok, _ = quiet(e2e.E2E(self.cfg).cleanup)
        self.assertTrue(ok)
        self.assertEqual(deleted, ["4"])

    def test_seerr_refusing_keeps_record(self):
        f = self.own(seerr_media_id=9)
        first(self.stack.seerr, "DELETE", r"/api/v1/media/9")(lambda req: (500, None))
        ok, out = quiet(e2e.E2E(self.cfg).cleanup)
        self.assertFalse(ok)
        self.assertIn("HTTP 500", out)
        self.assertTrue(f.exists())

    def test_clean_slate_check_needs_every_service(self):
        for app, path in (("Radarr", self.stack.radarr), ("Sonarr", self.stack.sonarr)):
            broken = first(path, "GET", r"/api/v3/(movie|series)")(lambda req, *a: (503, None))
            with self.assertRaises(SetupError) as raised:
                quiet(e2e.E2E(self.cfg).require_clean_slate)
            self.assertIn(f"Couldn't reach {app}", raised.exception.message)
            path.routes = [r for r in path.routes if r[2] is not broken]


class Pieces(unittest.TestCase):
    def test_torrent_is_valid_and_new_each_time(self):
        import tempfile
        folder = Path(tempfile.mkdtemp()) / "Release"
        self.addCleanup(__import__("shutil").rmtree, folder.parent)
        folder.mkdir()
        data = bytes(range(256)) * 9000   # a bit over 2 pieces
        (folder / "film.mov").write_bytes(data)
        (folder / ".DS_Store").write_bytes(b"skip")
        out = folder.parent / "r.torrent"
        first_hash = e2e.make_torrent(folder, out)
        raw = out.read_bytes()
        start = raw.index(b"4:infod") + 6
        _, end = bdecode(raw, start)
        self.assertEqual(hashlib.sha1(raw[start:end]).hexdigest(), first_hash)
        self.assertIn(b"4:name7:Release", raw)
        self.assertIn(b"4:pathl8:film.movee", raw)
        self.assertNotIn(b"DS_Store", raw)
        pieces = [hashlib.sha1(data[i:i + (1 << 20)]).digest() for i in range(0, len(data), 1 << 20)]
        self.assertIn(b"6:pieces%d:" % (20 * len(pieces)) + b"".join(pieces), raw)
        self.assertNotEqual(e2e.make_torrent(folder, out), first_hash)

    def test_bencode(self):
        self.assertEqual(e2e.bencode({"b": [1, "x"], "a": b"\x00"}), b"d1:a1:\x001:bli1e1:xee")

    def test_verdict(self):
        self.assertEqual(e2e.verdict([{"approved": True}]), "approved")
        self.assertEqual(e2e.verdict({"approved": False, "rejections": ["Unknown movie", "Bad"]}), "rejected: Unknown movie; Bad")
        self.assertEqual(e2e.verdict([]), "rejected: ")

    def test_wait_until(self):
        clock = Clock()
        answers = iter([None, c.json.JSONDecodeError("x", "", 0), 0, "done"])

        def check():
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        with mock.patch("mediaserver.e2e.time", clock):
            self.assertEqual(e2e.wait_until(60, check), "done")
            self.assertEqual(clock.now, 1_000_009.0)
            self.assertIsNone(e2e.wait_until(10, lambda: False))

    def test_served_torrents(self):
        import tempfile
        import urllib.request
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, folder)
        (folder / "a.torrent").write_bytes(b"d4:infodee")
        server = e2e.serve(folder, 0)
        try:
            port = server.server_address[1]
            self.assertEqual(urllib.request.urlopen(f"http://127.0.0.1:{port}/a.torrent").read(), b"d4:infodee")
        finally:
            server.shutdown()
            server.server_close()

    def test_release(self):
        r = e2e.release("Name-TAG", 1234)
        self.assertEqual((r["downloadUrl"], r["protocol"], r["size"]), ("http://127.0.0.1:1234/Name-TAG.torrent", "torrent", e2e.SOURCE_SIZE))
        self.assertRegex(r["publishDate"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class Logging(World):
    def test_main_writes_a_log_and_returns_failures(self):
        with mock.patch("mediaserver.e2e.Config.load", return_value=self.cfg), \
                mock.patch.object(e2e.E2E, "run", lambda self, keep: (print("ran", keep), 2)[1]), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(e2e.main(["--keep"]), 2)
        log = next(self.cfg.paths.logs.glob("e2e-*.log")).read_text()
        self.assertIn("ran True", log)
        self.assertIn("Log: ", log)

    def test_setup_error_counts_as_a_failure(self):
        def stop(self, keep):
            raise SetupError("no")
        with mock.patch("mediaserver.e2e.Config.load", return_value=self.cfg), mock.patch.object(e2e.E2E, "run", stop), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(e2e.main([]), 1)


if __name__ == "__main__":
    unittest.main()
