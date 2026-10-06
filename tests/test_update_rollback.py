"""Updates that roll themselves back (maintenance.update / roll_back, and
the end of cli.install): real git repositories in a scratch folder, the
backup, restore and re-run of setup stood in for.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mediaserver import cli, maintenance
from tests.test_integration import example_config, quiet


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


class Exec(Exception):
    """os.execvpe stood in for: the re-run of setup"""


class Rollback(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.enterContext(mock.patch.dict(os.environ, env))
        # The published repo, and the checkout one version behind it
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "flake.nix").write_text("v1\n")
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-qm", "v1")
        self.repo = self.root / "media-server"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True)
        self.v1 = git(self.repo, "rev-parse", "HEAD")
        (self.origin / "flake.nix").write_text("v2\n")
        git(self.origin, "commit", "-qam", "v2")
        self.v2 = git(self.origin, "rev-parse", "HEAD")
        self.cfg = example_config(self.root / "media")
        self.cfg.paths.config.mkdir(parents=True)
        self.cfg.paths.state.mkdir(parents=True)
        self.backup = self.root / "backup.tar.gz"
        self.execs: list = []

        def execvpe(cmd, args, env):
            self.execs.append(env)
            raise Exec
        for patcher in (mock.patch.object(maintenance, "backup", return_value=self.backup),
                        mock.patch.object(maintenance.os, "execvpe", execvpe),
                        mock.patch.dict(os.environ, {"MEDIA_SERVER_REPO": str(self.repo)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def update(self):
        with self.assertRaises(Exec):
            quiet(maintenance.update, self.cfg, True)
        return self.execs[-1]

    def test_update_records_what_to_go_back_to(self):
        env = self.update()
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.v2)
        self.assertEqual(env["MEDIA_UPDATE_ROLLBACK"], "1")
        record = json.loads(maintenance.rollback_file(self.cfg).read_text())
        self.assertEqual((record["from"], record["to"], record["backup"]), (self.v1, self.v2, str(self.backup)))

    def test_no_rollback_when_nothing_changed_or_local_changes(self):
        git(self.repo, "pull", "-q", "--ff-only")
        env = self.update()   # already up to date
        self.assertNotIn("MEDIA_UPDATE_ROLLBACK", env)
        (self.repo / "flake.nix").write_text("my own change\n")
        env = self.update()
        self.assertNotIn("MEDIA_UPDATE_ROLLBACK", env)
        self.assertFalse(maintenance.rollback_file(self.cfg).exists())

    def test_failed_checks_go_back_to_the_previous_version(self):
        env = self.update()
        restored = []
        with mock.patch.dict(os.environ, env), mock.patch.object(maintenance, "restore", lambda cfg, f, yes: restored.append(f)), \
                self.assertRaises(Exec):
            quiet(maintenance.roll_back, self.cfg, 3)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.v1)
        self.assertEqual(restored, [str(self.backup)])
        again = self.execs[-1]
        self.assertNotIn("MEDIA_UPDATE_ROLLBACK", again)   # the re-run doesn't roll back again
        self.assertIn("failed 3 check(s)", again["MEDIA_ROLLED_BACK"])
        self.assertFalse(maintenance.rollback_file(self.cfg).exists())

    def test_not_after_the_checkout_changed(self):
        env = self.update()
        (self.repo / "flake.nix").write_text("edited after the update\n")
        with mock.patch.dict(os.environ, env), mock.patch.object(maintenance, "restore") as restore:
            self.assertFalse(quiet(maintenance.roll_back, self.cfg, 1)[0])
        restore.assert_not_called()
        self.assertEqual((self.repo / "flake.nix").read_text(), "edited after the update\n")

    def test_not_outside_an_update(self):
        maintenance.rollback_file(self.cfg).write_text(json.dumps({"repo": str(self.repo), "from": self.v1, "to": self.v2}))
        with mock.patch.dict(os.environ, {"MEDIA_UPDATE_ROLLBACK": ""}):
            self.assertFalse(quiet(maintenance.roll_back, self.cfg, 1)[0])


class InstallEnd(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.cfg = example_config(self.root)
        self.cfg.paths.state.mkdir(parents=True)

    def run_install(self, failed, env):
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "check_platform", return_value=""), \
                mock.patch.object(cli, "ensure_config", return_value=self.cfg), mock.patch.object(cli, "run_step"), \
                mock.patch.object(cli.arrs, "require_no_unmerged_anime_sonarr"), mock.patch.object(cli.tailscale, "configure", return_value=""), \
                mock.patch("mediaserver.cli.verify.main", return_value=failed), mock.patch.object(cli, "lan_ip", return_value=""), \
                mock.patch.object(cli, "open_dashboard_once"), mock.patch.object(cli.c, "notify"), \
                mock.patch.object(maintenance, "roll_back", return_value=False) as roll_back:
            result, out = quiet(cli.install, cli.Options(yes=True))
        return result, out, roll_back

    def test_failed_checks_after_an_update_roll_back(self):
        result, _, roll_back = self.run_install(2, {"MEDIA_UPDATE_ROLLBACK": "1"})
        self.assertEqual(result, 1)
        roll_back.assert_called_once_with(self.cfg, 2)

    def test_a_good_update_clears_its_record(self):
        maintenance.rollback_file(self.cfg).write_text("{}")
        result, _, roll_back = self.run_install(0, {"MEDIA_UPDATE_ROLLBACK": "1"})
        self.assertEqual(result, 0)
        roll_back.assert_not_called()
        self.assertFalse(maintenance.rollback_file(self.cfg).exists())

    def test_after_a_rollback_it_says_so(self):
        _, out, _ = self.run_install(0, {"MEDIA_ROLLED_BACK": "abc1234 failed 3 check(s); back on def5678"})
        self.assertIn("The update was rolled back: abc1234 failed 3 check(s); back on def5678", out)


if __name__ == "__main__":
    unittest.main()
