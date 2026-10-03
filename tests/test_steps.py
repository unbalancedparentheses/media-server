"""Tests for setup's Python steps (mediaserver/steps/), against a scratch
~/media with launchd replaced by fakes.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mediaserver import api, creds, jellyfin, launchd, logins
from mediaserver import common as c
from mediaserver.config import Config, Paths
from mediaserver.steps import cleanuparr, introskipper, moonbase, postimport_settings, unpackerr
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
            moonbase.patch_web_app(d, str(hls))
        index = (d / "index.html").read_text()
        self.assertIn('src="vendor/hls/hls.min.js"', index)
        self.assertEqual(index.count('id="media-server-open"'), 1)
        self.assertEqual((d / "flutter_bootstrap.js").read_text().count("canvasKitBaseUrl"), 1)
        self.assertEqual((d / "vendor/hls/hls.min.js").read_text(), "hls")

    def test_intro_skipper_first_and_enabled(self):
        want = introskipper.with_intro_skipper({"MediaSegmentProviderOrder": ["Other", "Intro Skipper"],
                                                "DisabledMediaSegmentProviders": ["Intro Skipper", "X"], "Keep": 1})
        self.assertEqual(want["MediaSegmentProviderOrder"], ["Intro Skipper", "Other"])
        self.assertEqual(want["DisabledMediaSegmentProviders"], ["X"])
        self.assertEqual(want["Keep"], 1)
