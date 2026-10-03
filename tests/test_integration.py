"""Setup's steps against fake services (tests/fakes.py): a fresh install
configures everything as config.toml says, the checks (nix run .#test)
pass against the result, and a second run changes nothing.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import io
import json
import re
import shutil
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mediaserver import api, launchd, verify
from mediaserver import common as c
from mediaserver.config import Config, Paths
from mediaserver.pins import INTRO_SKIPPER, MOONBASE
from mediaserver.steps import STEPS
from tests.fakes import FakeStack

REPO = Path(__file__).resolve().parent.parent
# Setup's order (setup.sh run_setup)
ORDER = ["seed-qbittorrent", "cleanuparr-require-login", "qbittorrent", "jellyfin", "sabnzbd", "arrs", "junk-filters", "prowlarr",
         "usenet-providers", "bazarr", "sabnzbd-login", "seerr", "moonbase", "intro-skipper", "unpackerr", "cleanuparr", "postimport"]
# Writes a second run makes on purpose: logins (to check them), and
# secrets sent every run because they read back masked
REPEATED = [
    ("Sonarr", "POST", r"/login"), ("Radarr", "POST", r"/login"), ("Prowlarr", "POST", r"/login"),
    ("Sonarr", "PUT", r"/api/v3/(downloadclient|notification)/\d+"), ("Radarr", "PUT", r"/api/v3/(downloadclient|notification)/\d+"),
    ("Prowlarr", "PUT", r"/api/v1/(applications|downloadclient)/\d+"),
    ("Jellyfin", "POST", r"/Users/AuthenticateByName"), ("Seerr", "POST", r"/api/v1/auth/jellyfin"),
    ("Seerr", "POST", r"/api/v1/settings/initialize"), ("qBittorrent", "POST", r"/api/v2/auth/login"),
    ("qBittorrent", "POST", r"/api/v2/app/setPreferences"), ("SABnzbd", "POST", r"/api"), ("SABnzbd", "POST", r"/login/?"),
    ("Bazarr", "POST", r"/api/system/account"), ("Cleanuparr", "POST", r"/api/auth/login"),
    ("Cleanuparr", "PUT", r"/api/configuration/(sonarr|radarr)/instances/\d+"), ("Cleanuparr", "PUT", r"/api/configuration/download_client/\d+"),
]


def example_config(root: Path) -> Config:
    data = tomllib.loads((REPO / "config.toml.example").read_text().replace('"changeme"', '"real-password"'))
    data["jellyfin"]["password"] = "admin-pass"
    data["qbittorrent"]["password"] = "qbit-pass"
    # A few indexers, one through Byparr, one switched off
    data["indexers"] = [{"name": "Nyaa.si", "definitionName": "nyaasi", "enable": True},
                        {"name": "1337x", "definitionName": "1337x", "enable": True, "flaresolverr": True},
                        {"name": "Old", "definitionName": "old", "enable": False}]
    data["usenet_providers"] = [{"name": "news", "enable": True, "host": "news.example", "port": 563, "ssl": True,
                                 "username": "u", "password": "p", "connections": 8}]
    return Config(data, Paths(root))


def quiet(fn, *args):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        result = fn(*args)
    return result, out.getvalue()


class Stack(unittest.TestCase):
    """A scratch ~/media with every service faked, launchd and waits off"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.cfg = example_config(self.root)
        plugins = [{"Id": MOONBASE.guid.replace("-", ""), "Name": "Moonbase", "Version": MOONBASE.version, "Status": "Active"},
                   {"Id": INTRO_SKIPPER.guid.replace("-", ""), "Name": "Intro Skipper", "Version": INTRO_SKIPPER.version, "Status": "Active"}]
        self.stack = FakeStack(self.root, qbit=("admin", "qbit-pass"), plugins=plugins)
        self.stack.prowlarr.schemas = [{"definitionName": d, "fields": [{"name": "baseUrl", "value": ""}]} for d in ("nyaasi", "1337x", "old")]
        web = self.root / f"config/jellyfin/data/plugins/Moonbase_{MOONBASE.version}/frontend"
        (web / "canvaskit").mkdir(parents=True)
        (web / "flutter_bootstrap.js").write_text("_flutter.loader.load({\n});")
        (web / "index.html").write_text("<html><head></head><body></body></html>")
        self.web = web
        self.stack.__enter__()
        self.addCleanup(self.stack.__exit__)
        # No launchd, no waiting
        for target, value in ((launchd, "restart"), (launchd, "stop"), (launchd, "bootstrap")):
            patcher = mock.patch.object(target, value, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (mock.patch.object(launchd, "loaded", return_value=False), mock.patch.object(api, "wait_for", return_value=True),
                        mock.patch("mediaserver.steps.downloads.time.sleep"), mock.patch("mediaserver.jellyfin.time.sleep"),
                        mock.patch("mediaserver.steps.moonbase.time.sleep"), mock.patch("mediaserver.steps.introskipper.time.sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_steps(self) -> str:
        import importlib
        output = []
        for name in ORDER:
            module, _, function = STEPS[name].partition(":")
            fn = getattr(importlib.import_module(f"mediaserver.steps.{module}"), function or "run")
            _, out = quiet(fn, self.cfg)
            output.append(out)
        return "\n".join(output)


class FreshInstall(Stack):
    maxDiff = None

    def test_everything_configured_then_nothing_changes(self):
        out = self.run_steps()
        self.assertNotIn("✗", out)
        s = self.stack
        p = self.cfg.paths
        # Sonarr / Radarr
        self.assertEqual(sorted(r["path"] for r in s.sonarr.resources["rootfolder"].items), sorted([str(p.tv), str(p.anime)]))
        self.assertEqual([r["path"] for r in s.radarr.resources["rootfolder"].items], [str(p.movies)])
        for arr in (s.sonarr, s.radarr):
            self.assertEqual(sorted(x["implementation"] for x in arr.resources["downloadclient"].items), ["QBittorrent", "Sabnzbd"])
            self.assertEqual([n["implementation"] for n in arr.resources["notification"].items], ["MediaBrowser"])
            self.assertEqual((arr.host["authenticationMethod"], arr.host["username"]), ("forms", "admin"))
            self.assertEqual(arr.mediamanagement["minimumFreeSpaceWhenImporting"], 10 * 1024)
        self.assertTrue(s.sonarr.naming["renameEpisodes"] and s.radarr.naming["renameMovies"])
        self.assertIn("Anime", [x["name"] for x in s.sonarr.resources["qualityprofile"].items])
        self.assertTrue(s.radarr.resources["customformat"].items)
        # Prowlarr
        self.assertEqual(sorted(a["name"] for a in s.prowlarr.resources["applications"].items), ["Radarr", "Sonarr"])
        self.assertEqual(sorted(i["name"] for i in s.prowlarr.resources["indexer"].items), ["1337x", "Nyaa.si"])  # "Old" is off
        tag = s.prowlarr.resources["tag"].items[0]["id"]
        by_name = {i["name"]: i for i in s.prowlarr.resources["indexer"].items}
        self.assertEqual((by_name["1337x"]["tags"], by_name["Nyaa.si"]["tags"]), ([tag], []))
        # Jellyfin
        j = s.jellyfin
        self.assertTrue(j.wizard_done)
        self.assertEqual(sorted(f["Name"] for f in j.folders), ["Anime", "Movies", "TV Shows"])
        self.assertTrue(all(f["LibraryOptions"]["EnableRealtimeMonitor"] for f in j.folders))
        self.assertEqual(c.read_text(p.state / "dashstatus/jellyfin-key"), j.keys[0]["AccessToken"])
        user = j.users["admin"]
        self.assertEqual((user["Policy"]["EnablePlaybackRemuxing"], user["Configuration"]["SubtitleMode"]), (False, "Always"))
        self.assertEqual(j.encoding["HardwareAccelerationType"], "videotoolbox")
        self.assertEqual([r["Url"] for r in j.repositories], [MOONBASE.manifest, INTRO_SKIPPER.manifest])
        # Seerr
        self.assertEqual(([x["name"] for x in s.seerr.sonarr.items], [x["name"] for x in s.seerr.radarr.items]), (["Sonarr"], ["Radarr"]))
        self.assertEqual(s.seerr.sonarr.items[0]["activeAnimeProfileName"], "Anime")
        self.assertEqual(s.seerr.main["defaultPermissions"], 160)
        # Downloads
        self.assertEqual(s.qbittorrent.prefs["max_ratio"], self.cfg.get("downloads.seeding_ratio"))
        self.assertEqual(sorted(s.qbittorrent.categories), ["radarr", "sonarr"])
        self.assertEqual(s.sabnzbd.servers["news"]["host"], "news.example")
        self.assertEqual(s.sabnzbd.misc["username"], "admin")
        # Bazarr
        self.assertEqual([i["language"] for i in s.bazarr.profiles[0]["items"]], ["en", "es"])
        import yaml
        bz = yaml.safe_load((p.config / "bazarr/config/config.yaml").read_text())
        self.assertEqual((bz["auth"]["type"], bz["sonarr"]["apikey"], bz["general"]["theme"]), ("form", "sonarr-key", "dark"))
        # Cleanuparr
        cu = s.cleanuparr
        self.assertEqual(([x["name"] for x in cu.instances["sonarr"].items], len(cu.clients.items), cu.stall.items[0]["maxStrikes"]), (["Sonarr"], 1, 6))
        self.assertTrue(cu.queue_cleaner["enabled"])
        # Moonfin's web app: offline renderer, dashboard links, preferences
        index = (self.web / "index.html").read_text()
        self.assertIn('id="media-server-open"', index)
        self.assertIn('"pref_fallback_subtitle_language": "spa"', index)
        self.assertIn('canvasKitBaseUrl: "canvaskit/"', (self.web / "flutter_bootstrap.js").read_text())
        # Files
        self.assertIn('api_key = "sonarr-key"', (p.config / "unpackerr/unpackerr.conf").read_text())
        self.assertTrue(c.read_json(p.state / "postimport/settings.json")["check_downloads"])

        # The checks pass against what setup made
        v = verify.Verifier(self.cfg)
        checks = ""
        for part in (v.api_keys, v.download_clients, v.root_folders, v.jellyfin, v.jellyfin_sync, v.seerr, v.quality_profiles,
                     v.authentication, v.cleanuparr, v.junk_filters):
            checks += quiet(part)[1]
        failed = [line.strip() for line in checks.splitlines() if "✗" in line]
        self.assertEqual(failed, [], "checks failed against the fresh install")
        self.assertGreater(v.t.passed, 60)

        # A second run changes nothing (beyond the writes made on purpose)
        self.stack.clear()
        out2 = self.run_steps()
        unexpected = [(svc, m, path) for svc, writes in self.stack.writes().items() for m, path in writes
                      if not any(svc.lower() == r[0].lower() and m == r[1] and re.fullmatch(r[2], path) for r in REPEATED)]
        self.assertEqual(sorted(set(unexpected)), [], "the second run changed something")
        for marker in ("(created)", "(updated)", "login set:", "restarted with", "(folder fixed)"):
            self.assertNotIn(marker, out2)


class ChangedConfig(Stack):
    """After a fresh install, changing config.toml reaches the services"""

    def test_changes_reach_the_services(self):
        self.run_steps()
        data = json.loads(json.dumps(self.cfg.data))
        data["quality"]["rename_files"] = False
        data["cleanuparr"]["enabled"] = False
        data["cleanuparr"]["stalled_strikes"] = 9
        data["requests"]["auto_approve"] = False
        data["playback"]["hardware_acceleration"] = False
        data["indexers"][0]["enable"] = False
        data["subtitles"]["want"] = "all"
        self.cfg = Config(data, self.cfg.paths)
        self.run_steps()
        s = self.stack
        self.assertFalse(s.radarr.naming["renameMovies"])
        self.assertFalse(s.cleanuparr.queue_cleaner["enabled"])
        self.assertEqual(s.cleanuparr.stall.items[0]["maxStrikes"], 9)
        self.assertEqual(s.seerr.main["defaultPermissions"], 32)
        self.assertEqual(s.seerr.users[1]["permissions"], 32)  # existing non-admin users follow
        self.assertEqual(s.jellyfin.encoding["HardwareAccelerationType"], "none")
        self.assertFalse(next(i for i in s.prowlarr.resources["indexer"].items if i["name"] == "Nyaa.si")["enable"])
        self.assertIsNone(s.bazarr.profiles[0]["cutoff"])
        self.assertEqual(c.read_text(self.cfg.paths.state / "netwatch/cleanuparr-wanted"), "false")

    def test_jellyfin_password_change(self):
        self.run_steps()
        data = json.loads(json.dumps(self.cfg.data))
        data["jellyfin"]["password"] = "new-admin-pass"
        self.cfg = Config(data, self.cfg.paths)
        out = self.run_steps()
        self.assertEqual(self.stack.jellyfin.users["admin"]["password"], "new-admin-pass")
        self.assertEqual(self.stack.sonarr.host["password"], "new-admin-pass")
        self.assertIn("Jellyfin password changed", out)


if __name__ == "__main__":
    unittest.main()
