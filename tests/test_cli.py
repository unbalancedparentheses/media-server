"""setup's entry point and maintenance modes (mediaserver/cli.py,
maintenance.py, lock.py, tailscale.py): arguments, config.toml creation,
backup and restore with setup's records, the operation lock, Tailscale
routes, uninstall; some through setup.sh itself.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mediaserver import cli, launchd, lock, maintenance, tailscale
from mediaserver.common import operation_running as c_operation_running
from mediaserver.config import Config, Paths
from mediaserver.ui import SetupError
from tests.test_integration import REPO, example_config, quiet


class Scratch(unittest.TestCase):
    """A scratch ~/media (MEDIA_DIR) with nothing running"""

    def setUp(self):
        self.media = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.media)
        self.paths = Paths(self.media)
        for d in (self.paths.config, self.paths.state, self.paths.logs):
            d.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"MEDIA_DIR": str(self.media)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.running: set[str] = set()
        for name, fn in (("loaded", lambda n: n in self.running), ("stop", lambda c, n: self.running.discard(n) or True),
                         ("bootstrap", lambda n: self.running.add(n) or True)):
            patcher = mock.patch.object(launchd, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    @property
    def cfg(self):
        return cli.config_or_defaults()

    def records(self):
        state = self.paths.state
        (state / "credentials.json").write_text('{"version":2,"services":{"jellyfin":{"username":"admin","password":"current"}}}')
        (state / "renamed-sonarr").touch()
        (state / "e2e").mkdir(exist_ok=True)
        (state / "e2e/owned.json").write_text('{"movie_id":"42"}')


class Arguments(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(cli.parse([]).mode, "setup")
        o = cli.parse(["--yes", "--restore", "f.tar.gz"])
        self.assertEqual((o.mode, o.arg, o.yes), ("restore", "f.tar.gz", True))
        self.assertEqual(cli.parse(["--restart"]).arg, "")
        self.assertEqual(cli.parse(["--restart", "sonarr", "--yes"]).arg, "sonarr")
        self.assertEqual(cli.parse(["--restart", "--yes"]).arg, "")
        self.assertEqual(cli.parse(["--check-config"]).mode, "check-config")
        o = cli.parse(["--e2e", "--keep"])
        self.assertTrue(o.keep)
        self.assertTrue(cli.parse(["--uninstall", "--purge"]).purge)

    def test_mistakes(self):
        for argv, message in ((["--status", "--test"], "Only one mode"), (["--purge"], "--purge only goes with --uninstall"),
                              (["--logs"], "Usage"), (["--nope"], "Usage")):
            with self.assertRaises(SetupError) as raised, redirect_stderr(io.StringIO()):
                cli.parse(argv)
            self.assertIn(message, raised.exception.message)
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()) as out:
            cli.parse(["--help"])
        self.assertIn("Usage", out.getvalue())


class ConfigFile(Scratch):
    def test_created_with_generated_passwords(self):
        cfg, out = quiet(cli.ensure_config, True)
        text = self.paths.config_file.read_text()
        self.assertNotIn("changeme", text)
        self.assertEqual(oct(self.paths.config_file.stat().st_mode & 0o777), "0o600")
        self.assertEqual(len(cfg.jellyfin_pass), 24)
        self.assertIn(f"Jellyfin password: {cfg.jellyfin_pass}", out)
        self.assertNotEqual(cfg.jellyfin_pass, cfg.qbit_pass)
        # The rest of the file (comments too) is the example's
        self.assertEqual(len(text.splitlines()), len((REPO / "config.toml.example").read_text().splitlines()))

    def test_asked_for(self):
        answers = iter(["", "", "jfpass", "nope", "jfpass", "jfpass", "qb", "qbpass", "qbpass"])
        with mock.patch("builtins.input", lambda p: next(answers)), mock.patch("getpass.getpass", lambda p: next(answers)):
            cfg, out = quiet(cli.ensure_config, False)
        self.assertEqual((cfg.jellyfin_user, cfg.jellyfin_pass, cfg.qbit_user, cfg.qbit_pass), ("admin", "jfpass", "qb", "qbpass"))
        self.assertIn("Password cannot be empty", out)
        self.assertIn("Passwords don't match", out)

    def test_no_terminal_explains_what_to_do(self):
        def eof(prompt):
            raise EOFError
        with mock.patch("builtins.input", eof), self.assertRaises(SetupError) as raised:
            quiet(cli.ensure_config, False)
        self.assertIn("run with --yes", raised.exception.message)

    def test_quotes_and_backslashes_kept(self):
        path = self.media / "c.toml"
        path.write_text('[jellyfin]\nusername = "a"\npassword = "b"\n# note\n[qbittorrent]\nusername = "c"\npassword = "d"\n[other]\npassword = "keep"\n')
        cli.write_credentials(path, "u", 'p"\\q', "qu", "qp")
        from mediaserver.config import load_toml
        data = load_toml(path)
        self.assertEqual(data["jellyfin"]["password"], 'p"\\q')
        self.assertEqual(data["other"]["password"], "keep")
        self.assertIn("# note", path.read_text())

    def test_typo_rejected_with_suggestion(self):
        text = (REPO / "config.toml.example").read_text().replace('"changeme"', '"real-password"')
        self.paths.config_file.write_text(text.replace("prefer_h265 = true", "prefer_h256 = true"))
        with redirect_stderr(io.StringIO()) as out, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--check-config"]), 1)
        self.assertIn('quality.prefer_h256: unknown setting (did you mean "prefer_h265"?)', out.getvalue())
        self.paths.config_file.write_text(text)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["--check-config"]), 0)
        self.assertIn("Config validation passed", out.getvalue())

    def test_modes_that_need_config(self):
        with redirect_stderr(io.StringIO()) as out:
            self.assertEqual(cli.main(["--status"]), 1)
        self.assertIn("run 'nix run .#install' first", out.getvalue())


class BackupRestore(Scratch):
    def test_records_travel_with_the_databases(self):
        self.paths.config_file.write_text('timezone = "UTC"\n')
        (self.paths.config / "sonarr.db").write_text("db")
        (self.paths.config / "sonarr/logs").mkdir(parents=True)
        (self.paths.config / "sonarr/logs/big.txt").write_text("log")
        self.records()
        self.running = {"sonarr", "radarr"}
        backup, out = quiet(maintenance.backup, self.cfg)
        self.assertEqual(self.running, {"sonarr", "radarr"})   # put back
        self.assertIn("Services restarted: sonarr radarr", out)
        names = tarfile.open(backup).getnames()
        for record in ("credentials.json", "renamed-sonarr", "e2e/owned.json"):
            self.assertIn(f".state/{record}", names)
        self.assertIn("config/sonarr.db", names)
        self.assertNotIn("config/sonarr/logs/big.txt", names)
        self.assertEqual(oct(backup.stat().st_mode & 0o777), "0o600")

        (self.paths.state / "renamed-sonarr").unlink()
        (self.paths.state / "credentials.json").write_text('{"version":2,"services":{}}')
        (self.paths.config / "sonarr.db").write_text("newer")
        _, out = quiet(maintenance.restore, self.cfg, str(backup), True)
        self.assertTrue((self.paths.state / "renamed-sonarr").exists())
        self.assertEqual(json.loads((self.paths.state / "credentials.json").read_text())["services"]["jellyfin"]["password"], "current")
        self.assertEqual((self.paths.config / "sonarr.db").read_text(), "db")
        self.assertEqual(len(list(self.media.glob("config.pre-restore-*"))), 1)
        self.assertEqual(list(self.media.glob(".restore-*")), [])

    def test_old_backup_sets_records_aside(self):
        (self.paths.config / "sonarr.db").write_text("old-db")
        self.paths.config_file.write_text('timezone = "UTC"\n')
        old = self.media / "old.tar.gz"
        with tarfile.open(old, "w:gz") as tar:
            tar.add(self.paths.config, "config")
            tar.add(self.paths.config_file, "config.toml")
        (self.paths.config / "sonarr.db").write_text("new-db")
        self.records()
        _, out = quiet(maintenance.restore, self.cfg, str(old), True)
        self.assertEqual((self.paths.config / "sonarr.db").read_text(), "old-db")
        for record in ("renamed-sonarr", "e2e/owned.json", "credentials.json"):
            self.assertFalse((self.paths.state / record).exists(), record)
        self.assertEqual(len(list(self.paths.state.glob("pre-restore-*/credentials.json"))), 1)
        self.assertIn("predates setup's records", out)

    def test_not_a_backup(self):
        bad = self.media / "bad.tar.gz"
        with tarfile.open(bad, "w:gz") as tar:
            tar.add(self.paths.logs, "logs")
        with self.assertRaises(SetupError) as raised:
            quiet(maintenance.restore, self.cfg, str(bad), True)
        self.assertIn("no config/ inside", raised.exception.message)
        self.assertEqual(list(self.media.glob(".restore-*")), [])

    def test_restore_stops_when_a_service_wont(self):
        backup = quiet(maintenance.backup, self.cfg)[0]
        (self.paths.config / "marker").write_text("current")
        with mock.patch.object(launchd, "stop", lambda c, n: n != "jellyfin"), self.assertRaises(SetupError):
            quiet(maintenance.restore, self.cfg, str(backup), True)
        self.assertEqual((self.paths.config / "marker").read_text(), "current")

    def test_restore_asks_first(self):
        backup = quiet(maintenance.backup, self.cfg)[0]
        with mock.patch("builtins.input", lambda p: "n"):
            _, out = quiet(maintenance.restore, self.cfg, str(backup), False)
        self.assertIn("Aborted", out)
        self.assertEqual(list(self.media.glob("config.pre-restore-*")), [])

    def test_backup_stops_when_a_service_wont_and_restarts_the_others(self):
        self.running = {"sonarr", "radarr"}
        with mock.patch.object(launchd, "stop", lambda c, n: n != "radarr" and (self.running.discard(n) or True)), \
                self.assertRaises(SetupError):
            quiet(maintenance.backup, self.cfg)
        self.assertEqual(self.running, {"sonarr", "radarr"})
        self.assertEqual(list(self.paths.backups.glob("*.tar.gz")), [])

    def test_old_backups_pruned(self):
        self.paths.backups.mkdir()
        for i in range(12):
            f = self.paths.backups / f"media-server_2020010{i:02d}.tar.gz"
            f.write_text("x")
            os.utime(f, (1000 + i, 1000 + i))
        _, out = quiet(maintenance.backup, self.cfg)
        self.assertEqual(len(list(self.paths.backups.glob("*.tar.gz"))), 10)
        self.assertIn("Pruned: media-server_202001000.tar.gz", out)


class Lock(Scratch):
    def hold(self, seconds=30):
        """Another process holding the lock, as an operation would"""
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from mediaserver import lock; "
                "lock.acquire(Path(sys.argv[2])); print('held', flush=True); time.sleep(float(sys.argv[3]))")
        proc = subprocess.Popen([sys.executable, "-c", code, str(REPO), str(self.paths.state), str(seconds)],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        assert proc.stdout is not None
        self.assertEqual(proc.stdout.readline().strip(), "held")
        return proc

    def test_second_operation_refused(self):
        owner = self.hold()
        with self.assertRaises(SetupError) as raised, redirect_stderr(io.StringIO()):
            lock.acquire(self.paths.state)
        self.assertIn("Another media-server operation is running", raised.exception.message)
        self.assertIn(f"PID {owner.pid}", raised.exception.message)
        # Through the entry point: nothing is backed up
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--backup"]), 1)
        self.assertFalse(self.paths.backups.exists())
        self.assertEqual((self.paths.state / "lock/pid").read_text().strip(), str(owner.pid))

    def test_released_when_the_holder_dies(self):
        owner = self.hold()
        owner.kill()
        owner.wait()
        lock.acquire(self.paths.state)
        self.addCleanup(lock.release, self.paths.state)
        self.assertEqual((self.paths.state / "lock/pid").read_text().strip(), str(os.getpid()))

    def test_only_one_of_many_at_the_same_moment(self):
        """No gap between taking the lock and recording who has it"""
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from mediaserver import lock; "
                "from mediaserver.ui import SetupError\n"
                "while time.time() < float(sys.argv[3]): pass\n"
                "try:\n    lock.acquire(Path(sys.argv[2])); print('won', flush=True); time.sleep(1)\n"
                "except SetupError:\n    print('lost', flush=True)")
        start = __import__("time").time() + 1.5
        procs = [subprocess.Popen([sys.executable, "-c", code, str(REPO), str(self.paths.state), str(start)],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) for _ in range(12)]
        results = [p.communicate()[0].strip() for p in procs]
        self.assertEqual(results.count("won"), 1, results)

    def test_left_behind_record_is_replaced_and_released(self):
        (self.paths.state / "lock").mkdir()
        (self.paths.state / "lock/pid").write_text("999999\n")
        lock.acquire(self.paths.state)
        self.assertEqual((self.paths.state / "lock/pid").read_text().strip(), str(os.getpid()))
        self.assertTrue(c_operation_running(self.paths.state / "lock"))
        lock.release(self.paths.state)
        self.assertFalse((self.paths.state / "lock").exists())

    def test_someone_elses_lock_not_released(self):
        owner = self.hold()
        lock.release(self.paths.state)
        self.assertEqual((self.paths.state / "lock/pid").read_text().strip(), str(owner.pid))

    def test_read_only_modes_dont_lock(self):
        held = []
        with mock.patch.object(lock, "acquire", lambda s: held.append(s)), mock.patch.object(cli, "run", return_value=0):
            cli.main(["--status"])
            cli.main(["--backup"])
            cli.main(["--uninstall", "--dry-run"])
        self.assertEqual(len(held), 1)


class FakeTailscale:
    """The tailscale command: serve status as given; which removals work"""

    def __init__(self, serve, fail_off=False, status_fails=False):
        self.serve, self.fail_off, self.status_fails, self.calls = serve, fail_off, status_fails, []

    def __call__(self, ts, *args, timeout=10):
        self.calls.append(args)
        if args == ("serve", "status", "--json"):
            return None if self.status_fails else mock.Mock(returncode=0, stdout=json.dumps(self.serve))
        if args[-1:] == ("off",):
            return mock.Mock(returncode=1 if self.fail_off else 0, stdout="")
        return mock.Mock(returncode=0, stdout="")


class Tailscale(Scratch):
    def routes(self, data):
        (self.paths.state / "tailscale-routes.json").write_text(json.dumps(data))

    def test_route_for_an_old_dashboard_port_removed(self):
        self.paths.config_file.write_text('timezone = "UTC"\n[network]\ndashboard_port = 80\n')
        self.routes({"443": "http://127.0.0.1:8080"})
        fake = FakeTailscale({"Web": {"mac.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}},
                                      "mac.ts.net:9000": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}}}}})
        with mock.patch.object(tailscale, "run", fake):
            ok, _ = quiet(tailscale.remove, self.cfg, "ts")
        self.assertTrue(ok)
        self.assertIn(("serve", "--https=443", "off"), fake.calls)
        self.assertNotIn(("serve", "--https=9000", "off"), fake.calls)   # not ours
        self.assertFalse((self.paths.state / "tailscale-routes.json").exists())

    def test_failed_removal_keeps_record(self):
        self.routes({"8096": "http://127.0.0.1:8096"})
        fake = FakeTailscale({"Web": {"mac.ts.net:8096": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8096"}}}}}, fail_off=True)
        with mock.patch.object(tailscale, "run", fake):
            ok, out = quiet(tailscale.remove, self.cfg, "ts")
        self.assertFalse(ok)
        self.assertIn("tailscale serve --https=8096 off", out)
        self.assertTrue((self.paths.state / "tailscale-routes.json").exists())

    def test_unreadable_status_keeps_record(self):
        self.routes({"8096": "http://127.0.0.1:8096"})
        with mock.patch.object(tailscale, "run", FakeTailscale({}, status_fails=True)):
            ok, out = quiet(tailscale.remove, self.cfg, "ts")
        self.assertFalse(ok)
        self.assertIn("nothing removed", out)
        self.assertTrue((self.paths.state / "tailscale-routes.json").exists())

    def test_publish_records_and_skips_whats_there(self):
        cfg = example_config(self.media)
        serve = {"Web": {"mac.ts.net:8096": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8096"}}}}}
        fake = FakeTailscale(serve)
        original = fake.__call__

        def answer(ts, *args, timeout=10):
            if args == ("status", "--json"):
                return mock.Mock(returncode=0, stdout=json.dumps({"Self": {"DNSName": "mac.ts.net."}}))
            if args == ("ip", "-4"):
                return mock.Mock(returncode=0, stdout="100.64.0.1\n")
            return original(ts, *args, timeout=timeout)
        with mock.patch.object(tailscale, "run", answer):
            hostname, out = quiet(tailscale.configure, cfg, "ts")
        self.assertEqual(hostname, "mac.ts.net")
        self.assertIn("HTTPS :8096 → Jellyfin\n", out.replace("\x1b[0m", ""))
        self.assertIn("HTTPS :443 → dashboard (published)", out)
        published = [a for a in fake.calls if "--bg" in a]
        self.assertEqual([a[3] for a in published], ["--https=443", "--https=5055"])
        self.assertEqual(json.loads((self.paths.state / "tailscale-routes.json").read_text()),
                         {"443": "http://127.0.0.1:80", "8096": "http://127.0.0.1:8096", "5055": "http://127.0.0.1:5055"})

    def test_switched_off_takes_routes_down(self):
        data = json.loads(json.dumps(example_config(self.media).data))
        data.setdefault("network", {})["tailscale_https"] = False
        fake = FakeTailscale({"Web": {"mac.ts.net:5055": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5055"}}}}})
        with mock.patch.object(tailscale, "run", fake):
            hostname, out = quiet(tailscale.configure, Config(data, self.paths), "ts")
        self.assertEqual(hostname, "")
        self.assertIn(("serve", "--https=5055", "off"), fake.calls)
        self.assertIn("Tailscale HTTPS disabled", out)

    def test_not_connected(self):
        with mock.patch.object(tailscale, "run", lambda ts, *a, timeout=10: mock.Mock(returncode=1, stdout="")):
            hostname, out = quiet(tailscale.configure, example_config(self.media), "ts")
        self.assertEqual(hostname, "")
        self.assertIn("not connected", out)


class Uninstall(Scratch):
    def setUp(self):
        super().setUp()
        self.home = self.media / "home"
        self.agents = self.home / "Library/LaunchAgents"
        self.agents.mkdir(parents=True)
        patcher = mock.patch("pathlib.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(tailscale, "cli", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("sonarr", "radarr"):
            (self.agents / f"{launchd.label(name)}.plist").write_bytes(b"x")
        self.paths.config_file.write_text("x")
        self.paths.movies.mkdir()

    def test_keeps_configs_and_library(self):
        _, out = quiet(maintenance.uninstall, self.cfg, False, True)
        self.assertEqual(list(self.agents.iterdir()), [])
        self.assertTrue(self.paths.config_file.exists())
        self.assertIn("Configs (reinstall picks them up)", out)

    def test_purge_deletes_configs_never_the_library(self):
        _, out = quiet(maintenance.uninstall, self.cfg, True, True)
        self.assertFalse(self.paths.config.exists())
        self.assertFalse(self.paths.config_file.exists())
        self.assertTrue(self.paths.movies.exists())
        self.assertIn("Configs, logs and state deleted", out)

    def test_purge_asks_first(self):
        with mock.patch("builtins.input", lambda p: ""):
            _, out = quiet(maintenance.uninstall, self.cfg, True, False)
        self.assertTrue(self.paths.config.exists())
        self.assertIn("Kept configs", out)

    def test_purge_stops_while_tailscale_routes_are_still_published(self):
        with mock.patch.object(tailscale, "remove", return_value=False), self.assertRaises(SetupError) as raised:
            quiet(maintenance.uninstall, self.cfg, True, True)
        self.assertIn("Not purging", raised.exception.message)
        self.assertTrue(self.paths.config.exists())
        self.assertTrue(self.paths.state.exists())
        self.assertEqual(list(self.agents.iterdir()), [])   # the services are still removed

    def test_service_that_wont_stop(self):
        with mock.patch.object(launchd, "stop", lambda c, n: n != "radarr"), self.assertRaises(SetupError) as raised:
            quiet(maintenance.uninstall, self.cfg, True, True)
        self.assertIn("Could not stop: radarr", raised.exception.message)
        self.assertTrue(self.paths.config.exists())


class Modes(Scratch):
    def test_restart_and_logs_check_the_name(self):
        with self.assertRaises(SetupError) as raised, redirect_stderr(io.StringIO()):
            maintenance.restart(self.cfg, "nope")
        self.assertIn("One of: jellyfin sonarr", raised.exception.message)
        with mock.patch.object(launchd, "restart", lambda c, n: n == "sonarr"):
            _, out = quiet(maintenance.restart, self.cfg, "")
        self.assertIn("sonarr restarted", out)
        self.assertIn("radarr is not installed", out)
        with self.assertRaises(SetupError) as raised, redirect_stderr(io.StringIO()):
            maintenance.logs(self.cfg, "sonarr")
        self.assertIn("No log yet", raised.exception.message)

    def test_status(self):
        with mock.patch.object(launchd, "state", lambda n: "running (pid 1)" if n == "sonarr" else "not installed"), \
                mock.patch("mediaserver.common.request", return_value=mock.Mock(status=0)):
            _, out = quiet(maintenance.status, example_config(self.media))
        self.assertIn("sonarr        running (pid 1)", out)
        self.assertIn("(HTTP none)", out)
        self.assertIn("GB free on the media disk", out)

    def test_preflight(self):
        with mock.patch.dict(os.environ, {"MEDIA_SERVICES_JSON": "/nonexistent"}):
            result, out = quiet(maintenance.preflight, self.cfg)
        self.assertEqual(result, 1)
        self.assertIn("not running through Nix", out)
        self.assertIn(f"no {self.paths.config_file} yet", out)

    def test_dry_run(self):
        _, out = quiet(cli.main, ["--uninstall", "--purge", "--dry-run"])
        self.assertIn("would stop and remove the launchd agents, then delete configs and state", out)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--status", "--dry-run"]), 1)


class Install(Scratch):
    """The order of the install and what happens after it"""

    def test_runs_every_step_then_verifies(self):
        quiet(cli.ensure_config, True)
        ran = []
        with mock.patch.object(cli, "check_platform", return_value=""), \
                mock.patch.object(cli, "run_step", lambda cfg, name: ran.append(name)), \
                mock.patch("mediaserver.cli.verify.main", return_value=0), \
                mock.patch.object(cli, "lan_ip", return_value="192.168.1.5"):
            result, out = quiet(cli.install, cli.Options(yes=True))
        self.assertEqual(result, 0)
        self.assertEqual(ran, cli.INSTALL)
        self.assertIn("Setup Complete!", out)
        self.assertIn("replace localhost with 192.168.1.5", out)
        self.assertTrue((self.paths.state / "dashboard-opened").exists())
        # Failed checks: not complete
        with mock.patch.object(cli, "check_platform", return_value=""), mock.patch.object(cli, "run_step"), \
                mock.patch("mediaserver.cli.verify.main", return_value=2):
            result, out = quiet(cli.install, cli.Options(yes=True))
        self.assertEqual(result, 1)
        self.assertIn("2 verification check(s) failed", out)

    def test_every_install_step_exists(self):
        from mediaserver.steps import STEPS
        self.assertEqual([s for s in cli.INSTALL if s not in STEPS and s != "resume-indexers"], [])

    def test_opens_the_dashboard_only_the_first_time_in_a_terminal(self):
        cfg = example_config(self.media)
        with mock.patch("mediaserver.cli.subprocess.run") as run, mock.patch.object(cli, "interactive", return_value=True), \
                mock.patch.dict(os.environ, {"CI": ""}):
            quiet(cli.open_dashboard_once, cfg, False)
            quiet(cli.open_dashboard_once, cfg, False)
        self.assertEqual(run.call_count, 1)


class EntryPoint(Scratch):
    """setup.sh itself, as `nix run` starts it"""

    def run_setup(self, *args):
        manifest = self.media / "services.json"
        manifest.write_text("{}")
        # The Python running the tests (Nix's), not macOS's older one
        env = {**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
               "MEDIA_DIR": str(self.media), "MEDIA_SERVICES_JSON": str(manifest),
               "MEDIA_LABEL_PREFIX": f"test.media-server.{os.getpid()}"}
        return subprocess.run(["bash", str(REPO / "setup.sh"), *args], env=env, capture_output=True, text=True, check=False)

    def test_backup_and_restore(self):
        self.paths.config_file.write_text('timezone = "UTC"\n')
        (self.paths.config / "sonarr.db").write_text("db")
        self.records()
        r = self.run_setup("--backup")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        backup = next(self.paths.backups.glob("media-server_*.tar.gz"))
        names = tarfile.open(backup).getnames()
        for record in ("credentials.json", "renamed-sonarr", "e2e/owned.json"):
            self.assertIn(f".state/{record}", names)
        (self.paths.state / "renamed-sonarr").unlink()
        r = self.run_setup("--restore", str(backup), "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.paths.state / "renamed-sonarr").exists())
        self.assertFalse((self.paths.state / "lock").exists())

    def test_usage(self):
        r = self.run_setup("--nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage: setup.sh", r.stderr)


if __name__ == "__main__":
    unittest.main()
