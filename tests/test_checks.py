"""Tests for mediaserver/verify.py and mediaserver/doctor.py with fake
service answers: what counts as passing, and what doctor reports.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import io
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mediaserver import common as c
from mediaserver import doctor, verify
from mediaserver.config import Config, Paths


def config(data=None) -> Config:
    root = Path(tempfile.mkdtemp())
    base = {"jellyfin": {"username": "admin", "password": "pw"}, "qbittorrent": {"username": "admin", "password": "pw"},
            "quality": {"sonarr_profile": "HD-1080p", "sonarr_anime_profile": "HD-1080p", "radarr_profile": "HD-1080p"}}
    return Config({**base, **(data or {})}, Paths(root))


def quiet(fn, *args):
    with redirect_stdout(io.StringIO()) as out:
        result = fn(*args)
    return result, out.getvalue()


class ConfigTests(unittest.TestCase):
    def test_flags_and_defaults(self):
        cfg = config({"quality": {"prefer_h265": False}, "network": {"admin_bind": "127.0.0.1", "dashboard_port": 8088}})
        self.assertFalse(cfg.flag("quality.prefer_h265", True))     # explicit false wins
        self.assertTrue(cfg.flag("quality.rename_files", True))     # missing → default
        self.assertEqual(cfg.get("playback.subtitle_mode", "Always"), "Always")
        self.assertEqual(cfg.urls.sonarr, "http://127.0.0.1:8989")
        self.assertEqual(cfg.urls.dashboard, "http://localhost:8088")
        self.assertEqual(config().urls.dashboard, "http://localhost")

    def test_byparr_needed(self):
        self.assertFalse(config().byparr_needed())
        self.assertTrue(config({"indexers": [{"name": "x", "enable": True, "flaresolverr": True}]}).byparr_needed())
        self.assertFalse(config({"indexers": [{"name": "x", "enable": False, "flaresolverr": True}]}).byparr_needed())


class VerifyTests(unittest.TestCase):
    def test_bazarr_auth_section(self):
        auth = verify.bazarr_auth_section("general:\n  apikey: no\nauth:\n  apikey: 'k1'\n  type: form\n  username: ''\nnext:\n  x: 1\n")
        self.assertEqual(auth, {"apikey": "k1", "type": "form", "username": "''"})

    def test_junk_filters(self):
        """BR-DISK must score -10000 or less in a profile whose minimum
        score is 0 or more; HEVC must score above 0"""
        cfg = config()
        v = verify.Verifier(cfg)
        v.keys.sonarr, v.keys.radarr = "", "k"
        cfs = [{"id": 1, "name": "BR-DISK"}, {"id": 2, "name": "Foreign Subtitles"}, {"id": 3, "name": "Prefer HEVC"},
               {"id": 4, "name": "Prefer English Audio"}]
        profiles = [{"name": "HD-1080p", "minFormatScore": 0,
                     "formatItems": [{"format": 1, "score": -10000}, {"format": 2, "score": -5}, {"format": 3, "score": 10},
                                     {"format": 4, "score": 50}]}]
        answers = {"customformat": cfs, "qualityprofile": profiles, "config/naming": {"renameMovies": True}}
        with mock.patch.object(verify.Verifier, "arr", lambda self, url, key, path, default=None, ver="v3": answers.get(path, default)):
            _, out = quiet(v.junk_filters)
        self.assertIn("✓ Radarr → BR-DISK blocked in HD-1080p", out)
        self.assertIn("✗ Radarr → Foreign Subtitles blocked in HD-1080p", out)
        self.assertIn("✓ Radarr → HEVC preferred in HD-1080p", out)
        self.assertIn("✓ Radarr → English audio preferred (HD-1080p)", out)
        self.assertEqual((v.t.passed, v.t.failed), (4, 1))

    def test_byparr_skipped_when_unused(self):
        v = verify.Verifier(config())
        with mock.patch.object(c, "status_code", side_effect=lambda url, timeout=5: 0 if "8191" in url else 200), \
                mock.patch.object(verify.time, "sleep"):
            _, out = quiet(v.health)
        self.assertIn("Byparr responds (000; no enabled indexer needs it) (skipped)", out)
        self.assertEqual(v.t.failed, 0)


class DoctorTests(unittest.TestCase):
    def test_stuck_downloads_and_switched_off_indexers(self):
        cfg = config()
        d = doctor.Doctor(cfg)
        now = time.time()
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3600))
        answers = {
            "/api/v2/torrents/info": [{"name": "Old Show", "progress": 0.1, "state": "stalledDL", "added_on": now - 3 * 86400, "num_seeds": 0},
                                      {"name": "New Show", "progress": 0.1, "state": "stalledDL", "added_on": now, "num_seeds": 0}],
            "/api/v1/indexerstatus": [{"indexerId": 1, "disabledTill": future}, {"indexerId": 2, "disabledTill": future}],
            "/api/v1/indexer": [{"id": 1, "name": "On", "enable": True}, {"id": 2, "name": "Off", "enable": False}],
        }

        def fake(url, headers=None, default=None, timeout=10):
            return next((v for k, v in answers.items() if k in url.split("?")[0]), default)
        with mock.patch.object(c, "try_json", fake):
            d.downloads()
            d.indexers()
        texts = [a for a, _ in d.attention]
        self.assertEqual(sum("Old Show" in t for t in texts), 1)
        self.assertFalse(any("New Show" in t for t in texts))
        off = next(t for t in texts if "switched off" in t)
        self.assertIn("On (until", off)
        self.assertNotIn("Off", off.replace("switched off", ""))

    def test_exit_status_follows_attention(self):
        d = doctor.Doctor(config())
        for part in ("services", "connection", "indexers", "downloads", "subtitles", "library", "postimport", "disk", "records"):
            setattr(d, part, lambda: None)
        self.assertEqual(quiet(d.run)[0], 0)
        d.need("broken")
        self.assertEqual(quiet(d.run)[0], 1)


if __name__ == "__main__":
    unittest.main()
