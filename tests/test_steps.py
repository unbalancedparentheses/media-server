# Tests use possibly-None results directly (a None fails the test anyway):
# pyright: reportOptionalSubscript=false, reportArgumentType=false
"""Tests for setup's Python steps (mediaserver/steps/), against a scratch
~/media with launchd replaced by fakes.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mediaserver import api, arr, creds, jellyfin, launchd, logins
from mediaserver import common as c
from mediaserver.config import Config, Keys, Paths
from mediaserver.steps import arrs, bazarr, cleanuparr, downloads, introskipper, moonbase, postimport_settings, prowlarr, seerr, unpackerr
from mediaserver.steps import jellyfin as jellyfin_step
from mediaserver.ui import SetupError


def scratch(data=None) -> Config:
    root = Path(tempfile.mkdtemp())
    return Config(data or {}, Paths(root))


def run_value(fn, *args):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*args)


def run(fn, *args):
    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
        fn(*args)
    return out.getvalue()


class Unpackerr(unittest.TestCase):
    def setUp(self):
        self.cfg = scratch()
        self.addCleanup(shutil.rmtree, self.cfg.paths.media)
        for app, key in (("sonarr", "skey"), ("radarr", "rkey")):
            (self.cfg.paths.config / app).mkdir(parents=True)
            (self.cfg.paths.config / app / "config.xml").write_text(f"<Config><ApiKey>{key}</ApiKey></Config>")
        self.restarts = []
        patcher = mock.patch.object(launchd, "restart", lambda config, name: self.restarts.append(name) or True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_written_once_then_unchanged(self):
        out = run(unpackerr.run, self.cfg)
        conf = self.cfg.paths.config / "unpackerr/unpackerr.conf"
        self.assertIn('api_key = "skey"', conf.read_text())
        self.assertIn('api_key = "rkey"', conf.read_text())
        self.assertEqual(conf.stat().st_mode & 0o777, 0o600)  # holds API keys
        self.assertIn("Config written", out)
        self.assertEqual(self.restarts, ["unpackerr"])
        out = run(unpackerr.run, self.cfg)
        self.assertIn("Config unchanged", out)
        self.assertEqual(self.restarts, ["unpackerr"])


class PostimportSettings(unittest.TestCase):
    def test_follows_config(self):
        cfg = scratch({"library": {"stereo_audio": False}, "subtitles": {"languages": ["es"], "want": "all"},
                       "playback": {"audio_language": "jpn"}})
        self.addCleanup(shutil.rmtree, cfg.paths.media)
        out = run(postimport_settings.run, cfg)
        saved = json.loads((cfg.paths.state / "postimport/settings.json").read_text())
        self.assertFalse(saved["stereo_audio"])
        self.assertTrue(saved["check_downloads"])
        self.assertEqual((saved["subtitle_languages"], saved["want"], saved["audio_language"]), (["es"], "all", "jpn"))
        self.assertIn("Settings written", out)
        self.assertIn("bad downloads replaced, picture subtitles read into text", out)
        self.assertNotIn("Settings written", run(postimport_settings.run, cfg))


if __name__ == "__main__":
    unittest.main()


class CleanuparrSafeguard(unittest.TestCase):
    """The "no login for local addresses" bypass must be off before
    Cleanuparr runs; if that can't be done, setup stops"""

    def setUp(self):
        self.cfg = scratch()
        self.addCleanup(shutil.rmtree, self.cfg.paths.media)
        d = self.cfg.paths.config / "cleanuparr"
        d.mkdir(parents=True)
        self.db = d / "cleanuparr.db"
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE general_configs (auth_disable_auth_for_local_addresses INTEGER)")
            db.execute("INSERT INTO general_configs VALUES (1)")
        with sqlite3.connect(d / "users.db") as db:
            db.execute("CREATE TABLE users (api_key TEXT)")
            db.execute("INSERT INTO users VALUES ('k')")

    def bypass(self):
        with sqlite3.connect(self.db) as db:
            return db.execute("SELECT auth_disable_auth_for_local_addresses FROM general_configs").fetchone()[0]

    def test_turned_on_while_stopped(self):
        with mock.patch.object(launchd, "loaded", return_value=False):
            run(cleanuparr.require_login, self.cfg)
        self.assertEqual(self.bypass(), 0)

    def test_stops_setup_when_it_cant_be_turned_on(self):
        """Running, not answering, and won't stop: setup must stop"""
        with mock.patch.object(launchd, "loaded", return_value=True), mock.patch.object(launchd, "stop", return_value=False), \
                mock.patch.object(c, "request", return_value=c.Response(0, {}, b"")), redirect_stderr(io.StringIO()):
            with self.assertRaises(SetupError):
                run(cleanuparr.require_login, self.cfg)
        self.assertEqual(self.bypass(), 1)


class CleanuparrLogin(unittest.TestCase):
    def test_recorded_only_once_it_works(self):
        cfg = scratch({"jellyfin": {"username": "admin", "password": "new"}})
        self.addCleanup(shutil.rmtree, cfg.paths.media)
        creds.record(cfg.paths.state, "cleanuparr", "admin", "old")
        works = {"now": False}
        cu = cleanuparr.Cleanuparr(cfg)
        with mock.patch.object(logins, "cleanuparr", lambda url, u, p: works["now"] and p == "new"), \
                mock.patch.object(launchd, "stop", return_value=True), mock.patch.object(launchd, "bootstrap", return_value=True), \
                mock.patch.object(cleanuparr, "write_login"), mock.patch.object(api, "wait_for", return_value=True):
            run(cu.set_login)
            self.assertEqual(creds.get(cfg.paths.state, "cleanuparr", "password"), "old")
            works["now"] = True
            run(cu.set_login)
        self.assertEqual(creds.get(cfg.paths.state, "cleanuparr", "password"), "new")

    def test_unreadable_lists_add_nothing(self):
        """A failed read isn't "none there": no second qBittorrent or stall rule"""
        cfg = scratch()
        self.addCleanup(shutil.rmtree, cfg.paths.media)
        cu = cleanuparr.Cleanuparr(cfg)
        posted = []

        def call(method, path, body=None):
            if method == "GET":
                raise api.ApiError("down")
            posted.append(path)
        cu.call = call
        out = run(cu.connect_qbittorrent) + run(cu.stall_rule)
        self.assertEqual(posted, [])
        self.assertNotIn("qBittorrent connected", out)

    def test_queue_cleaner_settings(self):
        want = cleanuparr.queue_cleaner_settings(
            {"enabled": False, "downloadingMetadataMaxStrikes": 0, "failedImport": {"maxStrikes": 5, "patterns": []}, "other": 1}, True)
        self.assertEqual(want["enabled"], True)
        self.assertEqual(want["downloadingMetadataMaxStrikes"], 3)
        self.assertEqual(want["failedImport"], {"maxStrikes": 5, "patterns": [], "ignorePrivate": True, "patternMode": "Exclude"})
        self.assertEqual(want["other"], 1)


class Credentials(unittest.TestCase):
    def test_upgrade_from_shared_record(self):
        """The older file (one shared login) upgrades per service, without
        guessing Cleanuparr's password"""
        cfg = scratch()
        self.addCleanup(shutil.rmtree, cfg.paths.media)
        c.write_json(cfg.paths.state / "credentials.json", {"jellyfin": {"username": "admin", "password": "pw"},
                                                            "qbittorrent": {"username": "q", "password": "qp"}})
        state = cfg.paths.state
        self.assertEqual(creds.get(state, "sonarr", "password"), "pw")
        self.assertEqual(creds.get(state, "qbittorrent", "username"), "q")
        self.assertEqual(creds.get(state, "cleanuparr", "password"), "")
        creds.record(state, "cleanuparr", "admin", "pw")
        self.assertEqual((state / "credentials.json").stat().st_mode & 0o777, 0o600)
        self.assertTrue(creds.match(state, "cleanuparr", "admin", "pw"))


class JellyfinHelpers(unittest.TestCase):
    def setUp(self):
        self.cfg = scratch({"jellyfin": {"username": "admin", "password": "pw"}})
        self.addCleanup(shutil.rmtree, self.cfg.paths.media)

    def test_repository_list_unreadable_nothing_written(self):
        """A guessed list would drop the other plugins' repositories"""
        jf = jellyfin.Jellyfin(self.cfg)
        posted = []
        with mock.patch.object(jellyfin.Jellyfin, "get", side_effect=api.ApiError("down")), \
                mock.patch.object(jellyfin.Jellyfin, "post", lambda self, path, body=None: posted.append(path)):
            self.assertFalse(jf.pin_repository("Moonbase", "https://example/manifest.json", "Moonfin"))
        self.assertEqual(posted, [])

    def test_repository_replaced_others_kept(self):
        jf = jellyfin.Jellyfin(self.cfg)
        repos = [{"Name": "Old", "Url": "https://raw/Moonfin-Client/Plugin/main/manifest.json", "Enabled": True},
                 {"Name": "Other", "Url": "https://other/manifest.json", "Enabled": True}]
        posted = []
        with mock.patch.object(jellyfin.Jellyfin, "get", return_value=repos), \
                mock.patch.object(jellyfin.Jellyfin, "post", lambda self, path, body=None: posted.append(body)):
            run(jf.pin_repository, "Moonbase", "https://pinned/manifest.json", "Moonfin-Client/Plugin")
        self.assertEqual([r["Url"] for r in posted[0]], ["https://other/manifest.json", "https://pinned/manifest.json"])

    def test_restart_waits_for_login(self):
        """After a restart Jellyfin is "healthy" before it accepts logins:
        wait for the login instead of carrying on without one (which
        skipped Intro Skipper and the Moonfin patch on a fresh install)"""
        jf = jellyfin.Jellyfin(self.cfg)
        attempts = []

        def login(tries=3):
            attempts.append(1)
            jf.token = "tok" if len(attempts) >= 3 else ""
            return bool(jf.token)
        with mock.patch.object(launchd, "restart", return_value=True), mock.patch.object(api, "wait_for", return_value=True), \
                mock.patch.object(jellyfin.time, "sleep"), mock.patch.object(jf, "login", login):
            self.assertTrue(run_value(jf.restart_ready))
        self.assertEqual((jf.token, len(attempts)), ("tok", 3))


class MoonfinWebApp(unittest.TestCase):
    def test_patch_is_applied_once(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        (d / "flutter_bootstrap.js").write_text("_flutter.loader.load({\n  onEntrypointLoaded: x\n});")
        (d / "index.html").write_text('<html><head><script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.0/dist/hls.min.js"></script></head></html>')
        hls = d / "hls-from-nix.js"
        hls.write_text("hls")
        for _ in range(2):
            moonbase.patch_web_app(d, str(hls), {"pref_prefer_default_audio_track": True, "pref_fallback_subtitle_language": "spa"})
        index = (d / "index.html").read_text()
        self.assertEqual(index.count('id="media-server-prefs"'), 1)
        self.assertIn('"pref_fallback_subtitle_language": "spa"', index)
        self.assertIn('src="vendor/hls/hls.min.js"', index)
        self.assertEqual(index.count('id="media-server-open"'), 1)
        self.assertEqual((d / "flutter_bootstrap.js").read_text().count("canvasKitBaseUrl"), 1)
        self.assertEqual((d / "vendor/hls/hls.min.js").read_text(), "hls")

    def test_moonfin_defaults_follow_config(self):
        cfg = scratch({"subtitles": {"languages": ["en", "es"]}})
        self.addCleanup(shutil.rmtree, cfg.paths.media)
        self.assertEqual(moonbase.moonfin_defaults(cfg), {"pref_prefer_default_audio_track": True, "pref_fallback_subtitle_language": "spa"})
        cfg.data["subtitles"]["languages"] = ["en"]
        self.assertNotIn("pref_fallback_subtitle_language", moonbase.moonfin_defaults(cfg))

    def test_intro_skipper_first_and_enabled(self):
        want = introskipper.with_intro_skipper({"MediaSegmentProviderOrder": ["Other", "Intro Skipper"],
                                                "DisabledMediaSegmentProviders": ["Intro Skipper", "X"], "Keep": 1})
        self.assertEqual(want["MediaSegmentProviderOrder"], ["Intro Skipper", "Other"])
        self.assertEqual(want["DisabledMediaSegmentProviders"], ["X"])
        self.assertEqual(want["Keep"], 1)


class Bazarr(unittest.TestCase):
    def test_settings_keep_the_rest(self):
        current = {"general": {"theme": "dark", "use_sonarr": False}, "sonarr": {"apikey": "old", "other": 1}}
        want = bazarr.settings(current, Keys(sonarr="s", radarr=""), ["opensubtitlescom"], ["en"], "127.0.0.1")
        self.assertEqual(want["sonarr"], {"apikey": "s", "other": 1, "ip": "localhost", "port": 8989, "base_url": "", "ssl": False})
        self.assertNotIn("radarr", want)
        self.assertEqual(want["general"]["theme"], "dark")
        self.assertTrue(want["general"]["use_sonarr"] and want["general"]["ignore_pgs_subs"])
        self.assertEqual(want["general"]["ip"], "127.0.0.1")
        self.assertEqual(current["sonarr"]["apikey"], "old")  # the original isn't modified
        self.assertEqual(bazarr.settings(want, Keys(sonarr="s"), ["opensubtitlescom"], ["en"], "127.0.0.1"), want)

    def test_language_profiles(self):
        """English first, Spanish as a fallback: cutoff on the first item
        (id 1; Bazarr treats 0 as no cutoff), flags as strings"""
        other = {"profileId": 3, "name": "Kids", "items": []}
        created = bazarr.language_profiles([other], ["en", "es"], "first")
        default = created[-1]
        self.assertEqual((default["profileId"], default["cutoff"]), (4, 1))
        self.assertEqual([i["id"] for i in default["items"]], [1, 2])
        self.assertEqual(default["items"][0]["hi"], "False")
        self.assertEqual(created[0], other)
        self.assertIsNone(bazarr.language_profiles(created, ["en", "es"], "first"))  # already right
        self.assertIsNone(bazarr.language_profiles(created, ["en", "es"], "all")[-1]["cutoff"])

    def test_login_settings(self):
        hashed = hashlib.md5(b"pw").hexdigest()
        self.assertEqual(bazarr.login_settings({}, "admin", "pw")["auth"], {"type": "form", "username": "admin", "password": hashed})
        self.assertIsNone(bazarr.login_settings({"auth": {"type": "form", "username": "admin", "password": hashed, "apikey": "k"}}, "admin", "pw"))
        # "forms" (an older setup's typo) isn't a type Bazarr knows
        self.assertIsNotNone(bazarr.login_settings({"auth": {"type": "forms", "username": "admin", "password": hashed}}, "admin", "pw"))


class SeerrTests(unittest.TestCase):
    def setUp(self):
        self.cfg = scratch()
        self.addCleanup(shutil.rmtree, self.cfg.paths.media)

    def test_existing_connection_follows_config(self):
        conn = {"id": 3, "name": "Sonarr", "port": 1234, "apiKey": "old", "enableSearch": False, "activeDirectory": "/old",
                "activeProfileId": 1, "activeProfileName": "Any", "other": "kept"}
        want = seerr.synced(conn, "sonarr", self.cfg, "key", {"id": 4, "name": "HD-1080p"}, {"id": 9, "name": "Anime"})
        p = self.cfg.paths
        self.assertEqual((want["port"], want["apiKey"], want["enableSearch"]), (8989, "key", True))
        self.assertEqual((want["activeDirectory"], want["activeAnimeDirectory"]), (str(p.tv), str(p.anime)))
        self.assertEqual((want["activeProfileName"], want["activeAnimeProfileName"], want["animeSeriesType"]), ("HD-1080p", "Anime", "anime"))
        self.assertEqual(want["other"], "kept")
        # The configured profile missing: the connection keeps its own
        self.assertEqual(seerr.synced(conn, "radarr", self.cfg, "key", None, None)["activeProfileName"], "Any")

    def test_auto_approve_bit(self):
        self.assertEqual(seerr.approval_permissions(32, True), 160)
        self.assertEqual(seerr.approval_permissions(160, False), 32)
        self.assertEqual(seerr.approval_permissions(160, True), 160)

    def test_new_sonarr_connection_routes_anime(self):
        conn = seerr.sonarr_connection(self.cfg, "k", {"id": 4, "name": "HD-1080p"}, {"id": 9, "name": "Anime"})
        self.assertEqual((conn["seriesType"], conn["animeSeriesType"], conn["activeAnimeProfileId"]), ("standard", "anime", 9))
        self.assertTrue(conn["enableSearch"])


class ProwlarrTests(unittest.TestCase):
    def setUp(self):
        self.cfg = scratch({"indexers": [{"name": "Nyaa.si", "definitionName": "nyaasi", "enable": True, "flaresolverr": True,
                                          "fields": {"sort": 2}}]})
        self.addCleanup(shutil.rmtree, self.cfg.paths.media)

    def test_unreadable_list_adds_nothing(self):
        """Prowlarr's indexer list can't be read: nothing is added (it used
        to look empty, so every indexer was added a second time)"""
        p = prowlarr.Prowlarr(self.cfg, "k")
        posted = []

        def call(method, path, body=None):
            if method == "GET" and path == "indexer":
                raise api.ApiError("down")
            if method == "POST":
                posted.append(path)
            return [{"definitionName": "nyaasi", "fields": []}] if path == "indexer/schema" else []
        p.call = call
        run(prowlarr.indexers, p, 7)
        self.assertEqual(posted, [])

    def test_existing_indexer_follows_config(self):
        existing = {"id": 1, "name": "Nyaa.si", "enable": False, "tags": [], "fields": [{"name": "sort", "value": 0}, {"name": "x", "value": 1}]}
        want = prowlarr.updated_indexer(existing, self.cfg.get("indexers")[0], 7)
        self.assertEqual((want["enable"], want["tags"]), (True, [7]))
        self.assertEqual(want["fields"], [{"name": "sort", "value": 2}, {"name": "x", "value": 1}])
        off = prowlarr.updated_indexer(dict(existing, tags=[7, 9]), {"name": "Nyaa.si", "enable": True}, 7)
        self.assertEqual(off["tags"], [9])

    def test_new_indexer_from_schema(self):
        body = prowlarr.new_indexer({"id": 5, "definitionName": "nyaasi", "fields": [{"name": "sort", "value": 0}]},
                                    self.cfg.get("indexers")[0], 7)
        self.assertNotIn("id", body)
        self.assertEqual((body["name"], body["enable"], body["tags"], body["fields"][0]["value"]), ("Nyaa.si", True, [7], 2))


class ArrLogin(unittest.TestCase):
    def test_recorded_only_when_verified(self):
        """The login change fails, then applies but doesn't work, then
        works: the record only advances at the end"""
        cfg = scratch({"jellyfin": {"username": "admin", "password": "new"}})
        self.addCleanup(shutil.rmtree, cfg.paths.media)
        creds.record(cfg.paths.state, "sonarr", "admin", "old")
        state = {"put_ok": False, "login_ok": False}

        def call(method, url, headers=None, body=None, form=None, timeout=60):
            if method == "GET":
                return {"id": 1, "username": "admin", "authenticationMethod": "forms"}
            if not state["put_ok"]:
                raise api.ApiError("PUT failed")
        with mock.patch.object(api, "call", call), mock.patch.object(logins, "arr", lambda u, user, p: state["login_ok"]):
            for change in ("put_ok", "login_ok", None):
                run(arr.set_login, cfg, "Sonarr", "http://s", "k", "v3", "sonarr")
                expected = "new" if change is None else "old"
                self.assertEqual(creds.get(cfg.paths.state, "sonarr", "password"), expected)
                if change:
                    state[change] = True


class Downloads(unittest.TestCase):
    def setUp(self):
        self.cfg = scratch({"qbittorrent": {"username": "admin", "password": "pw"}, "downloads": {"seeding_ratio": 1, "seeding_time_minutes": 60},
                            "usenet_providers": [{"name": "prov", "enable": False}]})
        self.addCleanup(shutil.rmtree, self.cfg.paths.media)
        (self.cfg.paths.config / "sabnzbd").mkdir(parents=True)
        (self.cfg.paths.config / "sabnzbd/sabnzbd.ini").write_text("api_key = k\n")

    def test_qbittorrent_ini_login(self):
        """Written like qBittorrent does (PBKDF2); the check reads it back"""
        downloads.ini_set_login(self.cfg.paths.config, "admin", "pw", "127.0.0.1")
        ini = logins.qbit_ini(self.cfg.paths.config)
        text = ini.read_text()
        self.assertTrue(text.startswith("[LegalNotice]"))
        self.assertIn("WebUI\\Address=127.0.0.1", text)
        self.assertTrue(logins.qbittorrent_password_is(self.cfg.paths.config, "pw"))
        self.assertFalse(logins.qbittorrent_password_is(self.cfg.paths.config, "other"))
        self.assertEqual(ini.stat().st_mode & 0o777, 0o600)
        downloads.ini_set_login(self.cfg.paths.config, "admin", "new")
        self.assertEqual(ini.read_text().count("WebUI\\Password_PBKDF2"), 1)
        self.assertIn("WebUI\\Address=127.0.0.1", ini.read_text())  # kept

    def test_preferences(self):
        prefs = downloads.preferences(self.cfg)
        self.assertEqual((prefs["web_ui_address"], prefs["up_limit"], prefs["max_ratio"]), ("*", 100 * 1024, 1))
        self.assertTrue(prefs["bypass_local_auth"] and prefs["auto_tmm_enabled"])

    def sab(self, answers):
        """A fake SABnzbd: answers[(mode, section)] → answer (None = no answer)"""
        def request(url, method="GET", headers=None, body=None, form=None, timeout=15, follow=True):
            answer = answers((form or {}).get("mode"), (form or {}).get("section"), form or {})
            return c.Response(0, {}, b"") if answer is None else c.Response(200, {}, json.dumps(answer).encode())
        return mock.patch.object(c, "request", request)

    def test_disable_checks_sabnzbd_really_did(self):
        """SABnzbd accepts the request but the server stays on: not
        reported as disabled"""
        def answers(mode, section, form):
            if mode == "get_config":
                return {"config": {"servers": [{"name": "prov", "enable": 1}]}}
            return {"status": False, "error": "nope"}
        with self.sab(answers), mock.patch.object(downloads.time, "sleep"):
            out = run(downloads.usenet_providers, self.cfg)
        self.assertNotIn("prov disabled", out)
        self.assertIn("Could not disable prov", out)

    def test_unreadable_server_list_reported(self):
        with self.sab(lambda mode, section, form: None), mock.patch.object(downloads.time, "sleep"):
            out = run(downloads.usenet_providers, self.cfg)
        self.assertIn("Couldn't read SABnzbd's servers", out)


class JellyfinStep(unittest.TestCase):
    def setUp(self):
        self.cfg = scratch({"jellyfin": {"username": "admin", "password": "new"}, "playback": {"audio_language": ""}})
        self.addCleanup(shutil.rmtree, self.cfg.paths.media)

    def test_password_change_retried_after_interruption(self):
        """The change is interrupted: the old password stays recorded
        (Jellyfin needs it to change the password later), and the next run
        finishes the change"""
        state = self.cfg.paths.state
        creds.record(state, "jellyfin", "admin", "old")
        server = {"password": "old", "change_ok": False}

        def login(self, tries=3):
            self.token = "tok" if self.password == server["password"] else ""
            return bool(self.token)

        def post(self, path, body=None):
            if path.endswith("/Password"):
                if not server["change_ok"]:
                    raise api.ApiError("failed")
                server["password"] = body["NewPw"]
        with mock.patch.object(jellyfin.Jellyfin, "login", login), mock.patch.object(jellyfin.Jellyfin, "post", post), \
                mock.patch.object(jellyfin.Jellyfin, "get", lambda self, path: {"Id": "u1"}):
            self.assertIsNone(run_value(jellyfin_step.sign_in, self.cfg))
            self.assertEqual(creds.get(state, "jellyfin", "password"), "old")
            server["change_ok"] = True
            self.assertIsNotNone(run_value(jellyfin_step.sign_in, self.cfg))
        self.assertEqual((server["password"], creds.get(state, "jellyfin", "password")), ("new", "new"))

    def test_playback_and_policy(self):
        want = jellyfin_step.playback_config({"Other": 1}, self.cfg)
        self.assertEqual((want.get("AudioLanguagePreference"), want["PlayDefaultAudioTrack"], want["RememberAudioSelections"]), (None, True, False))
        self.assertNotIn("AudioLanguagePreference", jellyfin_step.playback_config({"AudioLanguagePreference": "jpn"}, self.cfg))
        self.assertEqual(want["Other"], 1)
        policy = jellyfin_step.policy_config({"EnablePlaybackRemuxing": True}, False)
        self.assertEqual((policy["EnablePlaybackRemuxing"], policy["EnableContentDownloading"]), (False, True))
        self.assertEqual(jellyfin_step.encoding_config({}, True)["HardwareAccelerationType"], "videotoolbox")


class FakeApp:
    """A Sonarr/Radarr whose answers come from a function (method, path, body)"""

    def __init__(self, label, answer):
        self.label, self.url, self.key, self.version, self.answer = label, "http://x", "k", "v3", answer
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path.split("?")[0]))
        return self.answer(method, path, body)


class Arrs(unittest.TestCase):
    def setUp(self):
        self.cfg = scratch({"quality": {"sonarr_anime_profile": "HD-1080p"}})
        self.addCleanup(shutil.rmtree, self.cfg.paths.media)

    def old_sonarr(self):
        d = self.cfg.paths.config / "sonarr-anime"
        d.mkdir(parents=True)
        (d / "sonarr.db").write_bytes(b"")

    def test_unmerged_old_anime_sonarr_stops_setup(self):
        self.old_sonarr()
        with self.assertRaises(SetupError) as raised:
            run(arrs.require_no_unmerged_anime_sonarr, self.cfg)
        self.assertIn(f"git checkout {arrs.LAST_WITH_MIGRATION}", raised.exception.message)
        # Merged by an older version (its marker), or never there: fine
        (self.cfg.paths.state / "sonarr-anime-migrated").parent.mkdir(parents=True, exist_ok=True)
        (self.cfg.paths.state / "sonarr-anime-migrated").touch()
        run(arrs.require_no_unmerged_anime_sonarr, self.cfg)
        shutil.rmtree(self.cfg.paths.config / "sonarr-anime")
        (self.cfg.paths.state / "sonarr-anime-migrated").unlink()
        run(arrs.require_no_unmerged_anime_sonarr, self.cfg)

    def test_rename_retried_after_interruption(self):
        """Renaming the existing library is recorded only once Sonarr accepted it"""
        accepted = {"now": False}

        def answer(method, path, body):
            if path == "config/naming":
                return {"renameEpisodes": True}
            if path == "series":
                return [{"id": 1}]
            if method == "POST" and not accepted["now"]:
                raise api.ApiError("failed")
        app = FakeApp("Sonarr", answer)
        marker = self.cfg.paths.state / "renamed-sonarr"
        run(arrs.set_renaming, self.cfg, app, "series")
        self.assertFalse(marker.exists())
        accepted["now"] = True
        run(arrs.set_renaming, self.cfg, app, "series")
        self.assertTrue(marker.exists())

    def test_unreadable_download_clients_not_readded(self):
        def answer(method, path, body):
            if path == "rootfolder":
                return []
            if path == "downloadclient" and method == "GET":
                raise api.ApiError("down")
            return {}
        app = FakeApp("Sonarr", answer)
        with mock.patch.object(arrs, "set_login"):
            run(arrs.configure_app, self.cfg, app, ["/tv"], "tvCategory", "sonarr")
        self.assertNotIn(("POST", "downloadclient"), app.calls)

    def test_profile_scores_by_scope(self):
        formats = [{"id": 1, "score": -10000, "scope": "all"}, {"id": 2, "score": -10000, "scope": "anime"},
                   {"id": 3, "score": 50, "scope": "standard"}]
        tv = arrs.scored_profile({"name": "HD-1080p", "minFormatScore": -5, "formatItems": [{"format": 1, "score": 0}]}, formats)
        self.assertEqual(tv["minFormatScore"], 0)
        self.assertEqual({i["format"]: i["score"] for i in tv["formatItems"]}, {1: -10000, 2: 0, 3: 50})
        anime = arrs.scored_profile({"name": "Anime", "formatItems": []}, formats)
        self.assertEqual({i["format"]: i["score"] for i in anime["formatItems"]}, {1: -10000, 2: -10000, 3: 0})


class FreshSabnzbd(unittest.TestCase):
    def test_no_server_list_means_no_servers(self):
        cfg = scratch()
        self.addCleanup(shutil.rmtree, cfg.paths.media)
        s = downloads.Sabnzbd(cfg, "k")
        with mock.patch.object(c, "request", return_value=c.Response(200, {}, b'{"config": {}}')):
            self.assertEqual(s.servers(), [])
        with mock.patch.object(c, "request", return_value=c.Response(0, {}, b"")):
            self.assertIsNone(s.servers())
