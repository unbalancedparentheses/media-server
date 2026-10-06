"""Updates that roll themselves back (maintenance.update / roll_back): real
git repositories in a scratch folder; setup (a child process the update
supervises), the backup and restore stood in for.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
from __future__ import annotations

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


class Rollback(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.enterContext(mock.patch.dict(os.environ, env))
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
        self.setups: list = []   # (commit checked out, env) for each run of setup
        self.results: list = []  # what each run of setup exits with
        self.restored: list = []

        def run_setup(repo, yes, env):
            self.setups.append((git(repo, "rev-parse", "HEAD"), env))
            return self.results.pop(0)
        for patcher in (mock.patch.object(maintenance, "backup", return_value=self.backup),
                        mock.patch.object(maintenance, "run_setup", run_setup),
                        mock.patch.object(maintenance, "restore", lambda cfg, f, yes: self.restored.append(f)),
                        mock.patch.dict(os.environ, {"MEDIA_SERVER_REPO": str(self.repo)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def update(self, *results):
        self.results = list(results)
        return quiet(maintenance.update, self.cfg, True)

    def test_a_good_update_keeps_the_new_version(self):
        code, _ = self.update(0)
        self.assertEqual(code, 0)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.v2)
        self.assertFalse(maintenance.rollback_file(self.cfg).exists())

    def test_any_failure_goes_back(self):
        for failure in (1, 2):   # failed checks, a build failure or a crash: any non-zero exit
            with self.subTest(failure=failure):
                git(self.repo, "reset", "-q", "--hard", self.v1)
                self.setups.clear()
                self.restored.clear()
                code, out = self.update(failure, 0)
                self.assertNotEqual(code, 0)   # the update itself failed
                self.assertEqual([s[0] for s in self.setups], [self.v2, self.v1])   # tried the new, then back on the old
                self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.v1)
                self.assertEqual(self.restored, [str(self.backup)])
                self.assertIn(f"failed (setup exited with status {failure})", self.setups[1][1]["MEDIA_ROLLED_BACK"])
                self.assertFalse(maintenance.rollback_file(self.cfg).exists())

    def test_nothing_to_go_back_to(self):
        git(self.repo, "pull", "-q", "--ff-only")
        code, _ = self.update(1)   # already up to date: the failure is just reported
        self.assertEqual(code, 1)
        self.assertEqual(len(self.setups), 1)
        self.assertEqual(self.restored, [])

    def test_local_changes_are_never_reset(self):
        (self.repo / "flake.nix").write_text("my own change\n")
        self.update(1)
        self.assertEqual((self.repo / "flake.nix").read_text(), "my own change\n")
        self.assertEqual(self.restored, [])

    def test_interrupted_leaves_it_to_you(self):
        code, out = self.update(130)
        self.assertEqual(code, 130)
        self.assertIn("git reset --hard", out)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.v2)


class InstallEnd(unittest.TestCase):
    def test_after_a_rollback_it_says_so(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        cfg = example_config(root)
        cfg.paths.state.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"MEDIA_ROLLED_BACK": "abc1234 failed (x); back on def5678"}), \
                mock.patch.object(cli, "check_platform", return_value=""), mock.patch.object(cli, "ensure_config", return_value=cfg), \
                mock.patch.object(cli, "run_step"), mock.patch.object(cli.arrs, "require_no_unmerged_anime_sonarr"), \
                mock.patch.object(cli.tailscale, "configure", return_value=""), mock.patch("mediaserver.cli.verify.main", return_value=0), \
                mock.patch.object(cli, "lan_ip", return_value=""), mock.patch.object(cli, "open_dashboard_once"), \
                mock.patch.object(cli.c, "notify"):
            _, out = quiet(cli.install, cli.Options(yes=True))
        self.assertIn("The update was rolled back: abc1234 failed (x); back on def5678", out)


if __name__ == "__main__":
    unittest.main()
