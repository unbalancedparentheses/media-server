"""Tests for setup's Python steps (mediaserver/steps/), against a scratch
~/media with launchd replaced by fakes.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mediaserver import launchd
from mediaserver.config import Config, Paths
from mediaserver.steps import postimport_settings, unpackerr


def scratch(data=None) -> Config:
    root = Path(tempfile.mkdtemp())
    return Config(data or {}, Paths(root))


def run(fn, *args):
    with redirect_stdout(io.StringIO()) as out:
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
