"""The host steps (mediaserver/steps/host.py and the agents in launchd.py):
folders, config files written before first start, nginx, the launchd
agents and which services get restarted, waiting, the dashboard's proxy;
and the credentials record.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import json
import os
import plistlib
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mediaserver import creds, launchd
from mediaserver.steps import host
from mediaserver.ui import SetupError
from tests.test_integration import example_config, quiet


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.cfg = example_config(self.root)
        self.manifest = self.root / "services.json"
        self.manifest.write_text(json.dumps({
            "nginxMimeTypes": "/nix/store/x-nginx/conf/mime.types",
            "services": {"sonarr": {"args": ["/nix/store/sonarr/bin/Sonarr", "-data=@CONFIG@/sonarr"], "env": {"DISK": "@DISK_MIN_GB@"}},
                         "nginx": {"args": ["/nix/store/nginx/bin/nginx", "-p", "@CONFIG@/nginx", "-g", "bind @ADMIN_BIND@;"]}}}))
        patcher = mock.patch.dict(os.environ, {"MEDIA_SERVICES_JSON": str(self.manifest)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def with_config(self, **network):
        from mediaserver.config import Config
        data = json.loads(json.dumps(self.cfg.data))
        data.setdefault("network", {}).update(network)
        self.cfg = Config(data, self.cfg.paths)


class Credentials(Base):
    def test_older_shared_record_is_upgraded(self):
        state = self.cfg.paths.state
        state.mkdir(parents=True)
        creds.path(state).write_text(json.dumps({"jellyfin": {"username": "admin", "password": "pw"},
                                                 "qbittorrent": {"username": "q", "password": "qp"}}))
        self.assertEqual(creds.get(state, "sonarr", "password"), "pw")
        self.assertEqual(creds.get(state, "bazarr", "username"), "admin")
        self.assertEqual(creds.get(state, "qbittorrent", "password"), "qp")
        # Cleanuparr's password may never have been applied: not assumed
        self.assertEqual(creds.get(state, "cleanuparr", "password"), "")

    def test_unreadable_record_means_reapply_everything(self):
        state = self.cfg.paths.state
        state.mkdir(parents=True)
        creds.path(state).write_text("{not json")
        services, out = quiet(lambda: creds.load(state)["services"])
        self.assertEqual(services, {})
        self.assertIn("every service's login will be re-applied", out)

    def test_record_is_private_and_matched(self):
        state = self.cfg.paths.state
        creds.record(state, "sonarr", "admin", "pw")
        self.assertEqual(stat.S_IMODE(creds.path(state).stat().st_mode), 0o600)
        self.assertTrue(creds.match(state, "sonarr", "admin", "pw"))
        self.assertFalse(creds.match(state, "sonarr", "admin", "other"))
        self.assertFalse(creds.match(state, "radarr", "admin", "pw"))


class Folders(Base):
    def test_tree(self):
        _, out = quiet(host.directories, self.cfg)
        p = self.cfg.paths
        for path in (p.movies / ".jellyfin-watch", p.anime / ".jellyfin-watch", p.downloads / "torrents/complete/radarr",
                     p.downloads / "usenet/incomplete", p.config / "nginx/temp", p.config / "dashstatus", p.backups, p.logs):
            self.assertTrue(path.exists(), path)
        self.assertIn("directory tree ready", out)
        quiet(host.directories, self.cfg)   # again: nothing breaks


class ConfigFiles(Base):
    def config_xml(self, name="sonarr"):
        return (self.cfg.paths.config / name / "config.xml").read_text()

    def changed(self):
        return host.changed_file(self.cfg).read_text().split() if host.changed_file(self.cfg).exists() else []

    def test_arr_config_written_once_then_follows_config(self):
        quiet(host.seed_arr, self.cfg, "sonarr", 8989)
        first = self.config_xml()
        self.assertIn("<Port>8989</Port>", first)
        self.assertIn("<BindAddress>*</BindAddress>", first)
        self.assertRegex(first, r"<ApiKey>[0-9a-f]{32}</ApiKey>")
        self.assertEqual(stat.S_IMODE((self.cfg.paths.config / "sonarr/config.xml").stat().st_mode), 0o600)
        # Again: same key, nothing to restart
        quiet(host.seed_arr, self.cfg, "sonarr", 8989)
        self.assertEqual(self.config_xml(), first)
        self.assertEqual(self.changed(), [])
        # A new bind address: the key stays, and Sonarr restarts to read it
        self.with_config(admin_bind="127.0.0.1")
        quiet(host.seed_arr, self.cfg, "sonarr", 8989)
        self.assertIn("<BindAddress>127.0.0.1</BindAddress>", self.config_xml())
        self.assertEqual(self.config_xml().split("<ApiKey>")[1], first.split("<ApiKey>")[1])
        self.assertEqual(self.changed(), ["sonarr"])

    def test_sabnzbd_ini_written_once(self):
        _, out = quiet(host.seed_sabnzbd, self.cfg)
        ini = self.cfg.paths.config / "sabnzbd/sabnzbd.ini"
        text = ini.read_text()
        self.assertIn(f"complete_dir = {self.cfg.paths.downloads}/usenet/complete", text)
        self.assertIn("wizard skipped", out)
        ini.write_text(text + "host = x\n")
        quiet(host.seed_sabnzbd, self.cfg)
        self.assertIn("host = x", ini.read_text())

    def test_nginx_and_the_dashboard_pages(self):
        quiet(host.service_configs, self.cfg)
        conf = (self.cfg.paths.config / "nginx/nginx.conf").read_text()
        self.assertIn("include /nix/store/x-nginx/conf/mime.types;", conf)
        self.assertIn("listen 80 default_server;", conf)
        self.assertIn("allow 127.0.0.1;\n        allow ::1;\n        deny all;", conf)   # this Mac only
        self.assertIn("if ($dashboard_foreign_host) {\n            return 403;", conf)   # not as a *.ts.net name
        self.assertIn("if ($dashboard_proxied) {\n            return 403;", conf)   # nor relayed by a proxy
        self.assertNotIn("allow 192.168", (self.cfg.paths.config / "nginx/api-proxy.conf").read_text())
        www = self.cfg.paths.config / "nginx/www"
        self.assertEqual((www / "settings.js").read_text(), 'window.MEDIA_SETTINGS={"adminLocalOnly":false};\n')
        self.assertEqual((www / "index.html").read_text(), (host.REPO / "landing.html").read_text())
        self.assertTrue((self.cfg.paths.config / "nginx/api-proxy.conf").exists())
        # A first install has nothing to restart
        self.assertNotIn("nginx", self.changed())
        # A new dashboard port: nginx restarts
        self.with_config(dashboard_port=8088, admin_bind="127.0.0.1")
        quiet(host.service_configs, self.cfg)
        self.assertIn("listen 8088 default_server;", (self.cfg.paths.config / "nginx/nginx.conf").read_text())
        self.assertEqual((www / "settings.js").read_text(), 'window.MEDIA_SETTINGS={"adminLocalOnly":true};\n')
        self.assertEqual(self.changed(), ["nginx", "prowlarr", "radarr", "sonarr"])

    def test_render_escapes_for_nginx_strings(self):
        tpl = self.root / "t.tpl"
        tpl.write_text('key "{{KEY}}" {{MISSING}}end')
        self.assertEqual(host.render(tpl, {"KEY": 'a"b\\c'}), 'key "a\\"b\\\\c" end')

    def test_needs_the_nix_manifest(self):
        with mock.patch.dict(os.environ, {"MEDIA_SERVICES_JSON": str(self.root / "missing.json")}):
            with self.assertRaises(SetupError):
                quiet(host.manifest)


class Agents(Base):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.agents = self.home / "Library/LaunchAgents"
        self.running: set[str] = set()
        self.calls: list = []
        for name, fn in (("loaded", lambda n: n in self.running), ("bootstrap", self.bootstrap), ("stop", self.stop)):
            patcher = mock.patch.object(launchd, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (mock.patch("pathlib.Path.home", return_value=self.home),
                        mock.patch("mediaserver.steps.host.cleanuparr.run_require_login"),
                        mock.patch("mediaserver.steps.host.subprocess.run", return_value=mock.Mock(returncode=0))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def bootstrap(self, name):
        self.calls.append(("start", name))
        self.running.add(name)
        return True

    def stop(self, config, name):
        self.calls.append(("stop", name))
        self.running.discard(name)
        return True

    def test_first_start_then_nothing_then_only_what_changed(self):
        _, out = quiet(host.services, self.cfg)
        self.assertIn("sonarr (started)", out)
        self.assertEqual(len(self.running), len(launchd.SERVICE_NAMES))
        plist = plistlib.loads((self.agents / "org.media-server.sonarr.plist").read_bytes())
        self.assertEqual(plist["ProgramArguments"], ["/nix/store/sonarr/bin/Sonarr", f"-data={self.cfg.paths.config}/sonarr"])
        self.assertEqual(plist["EnvironmentVariables"]["DISK"], "10")
        self.assertEqual(plist["EnvironmentVariables"]["TZ"], self.cfg.timezone)
        self.assertEqual(plist["StandardOutPath"], str(self.cfg.paths.logs / "sonarr.log"))
        self.assertTrue((self.cfg.paths.config / "sonarr").is_dir())
        # Again: nothing restarts
        self.calls.clear()
        _, out = quiet(host.services, self.cfg)
        self.assertEqual(self.calls, [])
        self.assertIn("sonarr (running)", out)
        # A new bind address changes nginx's command line, and Sonarr's
        # config file changed: both restart, nothing else
        self.with_config(admin_bind="127.0.0.1")
        host.mark_changed(self.cfg, "sonarr")
        _, out = quiet(host.services, self.cfg)
        self.assertEqual(sorted(self.calls), [("start", "nginx"), ("start", "sonarr"), ("stop", "nginx"), ("stop", "sonarr")])
        self.assertIn("nginx (restarted with new settings)", out)
        self.assertFalse(host.changed_file(self.cfg).exists())

    def test_service_that_wont_stop_stops_setup(self):
        quiet(host.services, self.cfg)
        host.mark_changed(self.cfg, "radarr")
        with mock.patch.object(launchd, "stop", return_value=False):
            with self.assertRaises(SetupError):
                quiet(host.services, self.cfg)
        # Still recorded: the next run restarts it
        self.assertEqual(host.changed_file(self.cfg).read_text().split(), ["radarr"])

    def test_interrupted_restart_is_finished_next_time(self):
        quiet(host.services, self.cfg)
        # nginx's command line changes; the install stops before restarting it
        self.with_config(admin_bind="127.0.0.1")
        with mock.patch.object(launchd, "start_all", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                quiet(host.services, self.cfg)
        self.assertIn("nginx", host.changed_file(self.cfg).read_text().split())
        # Its agent is already rewritten, but the next run still restarts it
        self.calls.clear()
        _, out = quiet(host.services, self.cfg)
        self.assertIn(("stop", "nginx"), self.calls)
        self.assertIn("nginx (restarted with new settings)", out)
        self.assertFalse(host.changed_file(self.cfg).exists())

    def test_interrupted_while_writing_agents(self):
        """Stopped right after replacing nginx's agent, before the rest"""
        quiet(host.services, self.cfg)
        self.with_config(admin_bind="127.0.0.1")
        real_replace = os.replace

        def replace(src, dst):
            real_replace(src, dst)
            if str(dst).endswith(".nginx.plist"):
                raise KeyboardInterrupt
        with mock.patch.object(launchd.os, "replace", replace), self.assertRaises(KeyboardInterrupt):
            quiet(host.services, self.cfg)
        self.assertEqual(host.changed_file(self.cfg).read_text().split(), ["nginx"])
        self.calls.clear()
        _, out = quiet(host.services, self.cfg)
        self.assertIn("nginx (restarted with new settings)", out)

    def test_retired_services_removed(self):
        self.agents.mkdir(parents=True)
        for name in ("sonarr-anime", "sonarr"):
            (self.agents / f"org.media-server.{name}.plist").write_bytes(b"x")
        (self.agents / "com.other.thing.plist").write_bytes(b"x")
        _, out = quiet(host.remove_retired, self.cfg)
        self.assertFalse((self.agents / "org.media-server.sonarr-anime.plist").exists())
        self.assertTrue((self.agents / "org.media-server.sonarr.plist").exists())
        self.assertTrue((self.agents / "com.other.thing.plist").exists())
        self.assertIn("sonarr-anime: no longer used", out)

    def test_gc_root_failure_is_a_warning(self):
        with mock.patch("mediaserver.steps.host.subprocess.run", return_value=mock.Mock(returncode=1)):
            _, out = quiet(host.services, self.cfg)
        self.assertIn("Could not register a Nix GC root", out)


class Waiting(Base):
    def test_byparr_may_be_late_others_may_not(self):
        with mock.patch("mediaserver.steps.host.api.wait_for", lambda name, url, s: name != "Byparr"):
            _, out = quiet(host.wait, self.cfg)
        self.assertIn("Byparr didn't start", out)
        with mock.patch("mediaserver.steps.host.api.wait_for", lambda name, url, s: name != "Seerr"):
            with self.assertRaises(SetupError) as raised:
                quiet(host.wait, self.cfg)
        self.assertIn("Seerr didn't start within 120s", raised.exception.message)


class Keys(Base):
    def test_required_and_optional_keys(self):
        quiet(host.service_configs, self.cfg)
        _, out = quiet(host.api_keys, self.cfg)
        self.assertIn("Seerr key not found (will read after setup)", out)
        self.assertIn("qBittorrent:  user admin", out)
        (self.cfg.paths.config / "radarr/config.xml").unlink()
        with self.assertRaises(SetupError) as raised:
            quiet(host.api_keys, self.cfg)
        self.assertEqual(raised.exception.message, "Radarr key not found")

    def test_api_proxy(self):
        quiet(host.service_configs, self.cfg)
        (self.cfg.paths.state / "dashstatus").mkdir(parents=True)
        (self.cfg.paths.state / "dashstatus/jellyfin-key").write_text("jfkey\n")
        (self.cfg.paths.config / "seerr").mkdir(parents=True)
        (self.cfg.paths.config / "seerr/settings.json").write_text(json.dumps({"main": {"apiKey": "seerrkey"}}))
        with mock.patch.object(launchd, "restart", return_value=True) as restart:
            _, out = quiet(host.api_proxy, self.cfg)
        proxy = self.cfg.paths.config / "nginx/api-proxy.conf"
        text = proxy.read_text()
        self.assertIn("MediaBrowser Token=\"jfkey\"", text.replace("\\\"", '"'))
        self.assertIn("seerrkey", text)
        self.assertIn("http://127.0.0.1:8081/api/v2/torrents/info", text)
        self.assertNotIn("{{", text)
        self.assertEqual(stat.S_IMODE(proxy.stat().st_mode), 0o600)
        restart.assert_called_once_with(self.cfg.paths.config, "nginx")
        self.assertIn("nginx reloaded", out)
        with mock.patch.object(launchd, "restart", return_value=False):
            _, out = quiet(host.api_proxy, self.cfg)
        self.assertIn("Could not restart nginx", out)


if __name__ == "__main__":
    unittest.main()
