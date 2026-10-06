"""More failure paths and less common paths: launchd, the command line,
installing the Jellyfin plugins, Seerr's first sign-in, and api waits.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mediaserver import __main__ as cli
from mediaserver import api, launchd
from mediaserver import common as c
from mediaserver.pins import INTRO_SKIPPER
from mediaserver.steps import arrs, introskipper, moonbase, seerr
from tests.test_integration import Stack, example_config, quiet


class Launchd(unittest.TestCase):
    def run_with(self, answers):
        """subprocess.run answering by the command's first two words"""
        calls = []

        def run(cmd, **kw):
            calls.append(cmd[:2])
            out = answers.get(tuple(cmd[:2]), (0, ""))
            return mock.Mock(returncode=out[0], stdout=out[1])
        return mock.patch.object(launchd.subprocess, "run", run), calls

    def test_state(self):
        patcher, _ = self.run_with({("launchctl", "print"): (0, "\tpid = 123\n")})
        with patcher:
            self.assertEqual(launchd.state("sonarr"), "running (pid 123)")
        patcher, _ = self.run_with({("launchctl", "print"): (0, "\tlast exit code = 1\n")})
        with patcher:
            self.assertEqual(launchd.state("sonarr"), "stopped (last exit 1)")
        patcher, _ = self.run_with({("launchctl", "print"): (113, "")})
        with patcher:
            self.assertEqual(launchd.state("sonarr"), "not installed")

    def test_stop_kills_leftovers_and_reports_survivors(self):
        loaded = iter([True, False, False])
        patcher, calls = self.run_with({("pkill", "-f"): (0, ""), ("pgrep", "-f"): (1, "")})
        with patcher, mock.patch.object(launchd, "loaded", lambda name: next(loaded)), mock.patch.object(launchd.time, "sleep"):
            self.assertTrue(launchd.stop(Path("/c"), "bazarr"))
        self.assertIn(["launchctl", "bootout"], calls)
        self.assertIn(["pkill", "-f"], calls)
        # Still running afterwards: not stopped
        patcher, _ = self.run_with({("pkill", "-f"): (0, ""), ("pgrep", "-f"): (0, "")})
        with patcher, mock.patch.object(launchd, "loaded", return_value=False), mock.patch.object(launchd.time, "sleep"), \
                redirect_stdout(io.StringIO()):
            self.assertFalse(launchd.stop(Path("/c"), "bazarr"))

    def test_restart_needs_it_loaded(self):
        with mock.patch.object(launchd, "loaded", return_value=False):
            self.assertFalse(launchd.restart(Path("/c"), "x"))
        with mock.patch.object(launchd, "loaded", return_value=True), mock.patch.object(launchd, "stop", return_value=True), \
                mock.patch.object(launchd, "bootstrap", return_value=True) as boot:
            self.assertTrue(launchd.restart(Path("/c"), "x"))
        boot.assert_called_once_with("x")


class CommandLine(unittest.TestCase):
    def test_usage_and_unknown_steps(self):
        out = io.StringIO()
        with redirect_stderr(out), redirect_stdout(out):
            self.assertEqual(cli.main([]), 2)
            self.assertEqual(cli.main(["nope"]), 2)
            self.assertEqual(cli.main(["step", "nope"]), 2)
        self.assertIn("unknown: nope", out.getvalue())

    def test_runs_a_step_and_passes_setup_errors_on(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        cfg = example_config(root)
        with mock.patch("mediaserver.steps.Config.load", return_value=cfg), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["step", "postimport"]), 0)
        self.assertIn("After each download", out.getvalue())
        from mediaserver.ui import err

        def failing(argv):
            raise err("broken")
        with mock.patch("mediaserver.steps.main", failing), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["step", "x"]), 1)


class PluginInstalls(Stack):
    """Installing Moonbase and Intro Skipper on a Jellyfin that has neither"""

    def setUp(self):
        super().setUp()
        j = self.stack.jellyfin
        j.plugins = []
        j.install_dir = self.cfg.paths.config / "jellyfin/data/plugins"
        j.add_user("admin", "admin-pass")
        j.wizard_done = True
        j.folders = [{"Name": "TV Shows", "CollectionType": "tvshows", "ItemId": "lib1", "Locations": [],
                      "LibraryOptions": {"MediaSegmentProviderOrder": [], "DisabledMediaSegmentProviders": ["Intro Skipper"]}}]
        # A lambda, not self.restart: patched onto the class, it must take the Jellyfin instance
        patcher = mock.patch("mediaserver.jellyfin.Jellyfin.restart_ready", lambda jf: self.restart(jf))  # noqa: PLW0108
        patcher.start()
        self.addCleanup(patcher.stop)

    def restart(self, jf):
        self.stack.jellyfin.restart()
        return jf.login()

    def test_intro_skipper_installed_and_enabled(self):
        _, out = quiet(introskipper.run, self.cfg)
        self.assertIn("Intro Skipper installed; restarting Jellyfin", out)
        self.assertIn(f"Intro Skipper {INTRO_SKIPPER.version} loaded", out)
        options = self.stack.jellyfin.folders[0]["LibraryOptions"]
        self.assertEqual((options["MediaSegmentProviderOrder"], options["DisabledMediaSegmentProviders"]), (["Intro Skipper"], []))
        self.assertEqual(self.stack.jellyfin.repositories[0]["Url"], INTRO_SKIPPER.manifest)

    def test_moonbase_installed_and_connected_to_seerr(self):
        _, out = quiet(moonbase.run, self.cfg)
        self.assertIn("Moonbase installed; restarting Jellyfin", out)
        plugin = next(p for p in self.stack.jellyfin.plugins if p["Name"] == "Moonbase")
        self.assertEqual(plugin["config"], {"SeerrEnabled": True, "SeerrUrl": self.cfg.urls.seerr})

    def test_plugin_that_never_loads_is_reported(self):
        with mock.patch.object(self.stack.jellyfin, "restart", lambda: None), mock.patch("mediaserver.jellyfin.Jellyfin.wait_plugin", return_value=None):
            _, out = quiet(introskipper.run, self.cfg)
        self.assertIn("Intro Skipper didn't load after restart", out)

    def test_download_that_never_finishes(self):
        self.stack.jellyfin.install_dir = None
        with mock.patch("mediaserver.steps.introskipper.time.sleep"):
            _, out = quiet(introskipper.run, self.cfg)
        self.assertIn("download didn't finish", out)


class SeerrFirstTime(Stack):
    def setUp(self):
        super().setUp()
        self.stack.jellyfin.add_user("admin", "admin-pass")
        self.stack.jellyfin.wizard_done = True

    def test_jellyfin_address_set_when_missing(self):
        quiet(arrs.run_junk_filters, self.cfg)
        _, out = quiet(seerr.run, self.cfg)
        self.assertIn("Jellyfin address set (localhost:8096)", out)
        self.assertEqual([x["name"] for x in self.stack.seerr.sonarr.items], ["Sonarr"])

    def test_wrong_password_stops_here(self):
        self.stack.jellyfin.users["admin"]["password"] = "other"
        _, out = quiet(seerr.run, self.cfg)
        self.assertIn("Could not sign in to Seerr", out)
        self.assertEqual(self.stack.seerr.sonarr.items, [])

    def test_missing_profile_isnt_replaced_by_another(self):
        data = json.loads(json.dumps(self.cfg.data))
        data["quality"]["radarr_profile"] = "HD-1080p"
        self.stack.radarr.resources["qualityprofile"].items = []
        from mediaserver.config import Config
        _, out = quiet(seerr.run, Config(data, self.cfg.paths))
        self.assertIn("quality profile 'HD-1080p' doesn't exist there; not connecting", out)
        self.assertEqual(self.stack.seerr.radarr.items, [])


class Waiting(unittest.TestCase):
    def test_wait_for(self):
        answers = iter([c.Response(0, {}, b""), c.Response(503, {}, b""), c.Response(200, {}, b"")])
        with mock.patch.object(c, "request", lambda *a, **k: next(answers)), mock.patch.object(api.time, "sleep"), \
                redirect_stdout(io.StringIO()) as out:
            self.assertTrue(api.wait_for("Sonarr", "http://x"))
        self.assertIn("up", out.getvalue())
        clock = iter([0, 0, 200])
        with mock.patch.object(c, "request", return_value=c.Response(0, {}, b"")), mock.patch.object(api.time, "sleep"), \
                mock.patch.object(api.time, "time", lambda: next(clock)), redirect_stdout(io.StringIO()) as out:
            self.assertFalse(api.wait_for("Sonarr", "http://x", 120))
        self.assertIn("timeout", out.getvalue())

    def test_api_errors(self):
        with mock.patch.object(c, "request", return_value=c.Response(500, {}, b"")):
            with self.assertRaises(api.ApiError):
                api.get("http://x")
        with mock.patch.object(c, "request", return_value=c.Response(200, {}, b"<html>Bad gateway</html>")):
            with self.assertRaises(api.ApiError) as raised:
                api.get("http://x")
            self.assertIn("not JSON", str(raised.exception))
        with mock.patch.object(c, "request", return_value=c.Response(204, {}, b"")):
            self.assertIsNone(api.call("POST", "http://x"))


if __name__ == "__main__":
    unittest.main()
