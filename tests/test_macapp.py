"""The Mac app (mediaserver/steps/macapp.py): the bundle setup writes, its
icon, and the Dock tile, against a scratch home and a stand-in Dock (the
real Dock and ~/Applications are never touched).

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
from __future__ import annotations

import json
import plistlib
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from mediaserver.config import Config, Paths
from mediaserver.steps import macapp
from tests.test_integration import quiet


class FakeDock:
    """defaults export/import com.apple.dock and killall Dock"""

    def __init__(self, prefs=None, fail=False):
        self.prefs, self.fail, self.restarts = prefs if prefs is not None else {"persistent-apps": []}, fail, 0

    def __call__(self, args, input=None, **_):   # noqa: A002 (subprocess's name)
        if args[:2] == ["defaults", "export"]:
            return mock.Mock(returncode=1 if self.fail else 0, stdout=plistlib.dumps(self.prefs))
        if args[:2] == ["defaults", "import"]:
            self.prefs = plistlib.loads(input or b"")
            return mock.Mock(returncode=0)
        if args == ["killall", "Dock"]:
            self.restarts += 1
        return mock.Mock(returncode=0, stdout=b"")


class MacApp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        media = Path(tmp.name) / "media"
        (media / ".state").mkdir(parents=True)
        (media / "config.toml").write_text("")
        self.cfg = Config.load(Paths(media))
        self.enterContext(mock.patch("pathlib.Path.home", return_value=self.home))
        self.enterContext(mock.patch.object(macapp, "icns", return_value=b"icns"))
        self.enterContext(mock.patch.object(macapp, "LSREGISTER", "/nonexistent"))
        self.compiler = self.enterContext(mock.patch.object(macapp, "swiftc", return_value=None))
        self.dock = FakeDock()
        self.enterContext(mock.patch.object(macapp.subprocess, "run", self.dock))
        self.app = self.home / "Applications/Media Server.app"

    def setup(self, config=""):
        self.cfg.paths.config_file.write_text(config)
        self.cfg = Config.load(self.cfg.paths)
        return quiet(macapp.run, self.cfg)[1]

    def tiles(self):
        return [t["tile-data"]["file-data"]["_CFURLString"] for t in self.dock.prefs.get("persistent-apps", [])]

    def test_app_written_and_added_to_the_dock_once(self):
        out = self.setup()
        exe = self.app / "Contents/MacOS/media-server"
        self.assertIn("URL='http://localhost'", exe.read_text())
        self.assertTrue(exe.stat().st_mode & 0o111)
        info = plistlib.loads((self.app / "Contents/Info.plist").read_bytes())
        self.assertEqual((info["CFBundleExecutable"], info["CFBundleIconFile"]), ("media-server", "icon"))
        self.assertEqual((self.app / "Contents/Resources/icon.icns").read_bytes(), b"icns")
        self.assertEqual(self.tiles(), [self.app.resolve().as_uri() + "/"])
        self.assertIn("added to the Dock", out)
        # A re-run: nothing rewritten, not added twice
        out = self.setup()
        self.assertNotIn("written", out)
        self.assertEqual(len(self.tiles()), 1)
        self.assertEqual(self.dock.restarts, 1)

    def test_taken_out_of_the_dock_stays_out(self):
        self.setup()
        self.dock.prefs["persistent-apps"] = []
        self.setup()
        self.assertEqual(self.tiles(), [])

    def test_rewritten_when_the_port_changes(self):
        self.setup()
        out = self.setup("[network]\ndashboard_port = 8088\n")
        self.assertIn("written", out)
        self.assertIn("URL='http://localhost:8088'", (self.app / "Contents/MacOS/media-server").read_text())

    def test_dock_off(self):
        self.setup("[app]\ndock = false\n")
        self.assertTrue(self.app.exists())
        self.assertEqual(self.tiles(), [])

    def test_dock_unreadable(self):
        self.dock.fail = True
        out = self.setup()
        self.assertIn("drag it there", out)
        self.assertFalse((self.cfg.paths.state / "app-in-dock").exists())   # tried again next time

    def test_switched_off_removes_app_and_tile(self):
        other = {"tile-data": {"file-data": {"_CFURLString": "file:///Applications/Safari.app/"}}}
        self.dock.prefs["persistent-apps"].append(other)
        self.setup()
        out = self.setup("[app]\nenabled = false\n")
        self.assertFalse(self.app.exists())
        self.assertEqual(self.tiles(), ["file:///Applications/Safari.app/"])
        self.assertIn("removed", out)

    def test_remove_leaves_an_app_setup_didnt_make(self):
        (self.app / "Contents").mkdir(parents=True)
        quiet(macapp.remove, self.cfg)
        self.assertTrue(self.app.exists())

    def test_native_app_with_every_page(self):
        def compile_app(compiler, out):
            out.write_bytes(b"\xcf\xfa\xed\xfe program")
            return ""
        self.compiler.return_value = ["swiftc"]
        with mock.patch.object(macapp, "compile_app", side_effect=compile_app) as built:
            out = self.setup()
            self.assertIn("in one window", out)
            info = plistlib.loads((self.app / "Contents/Info.plist").read_bytes())
            self.assertFalse(info["LSUIElement"])   # a real app, with its Dock icon
            self.assertTrue(info["NSAppTransportSecurity"]["NSAllowsLocalNetworking"])   # reads http://localhost/status.json
            pages = json.loads((self.app / "Contents/Resources/services.json").read_text())
            self.assertEqual([p["name"] for p in pages][:3], ["Home", "Watch", "Requests"])
            self.assertEqual(pages[0]["url"], "http://localhost")
            self.assertEqual(pages[1]["url"], "http://localhost:8096/Moonfin/Web/")
            self.assertEqual([p["name"] for p in pages][-2:], ["qBittorrent", "Bazarr"])   # no Usenet provider: no SABnzbd
            self.assertEqual(len(pages), 8)
            self.setup()   # unchanged: not rebuilt
            self.assertEqual(built.call_count, 1)

    def test_sabnzbd_page_with_a_usenet_provider(self):
        self.setup('[[usenet_providers]]\nname = "news"\nenable = true\n')
        names = [p["name"] for p in macapp.pages(self.cfg)]
        self.assertEqual(names[-2:], ["SABnzbd", "Bazarr"])
        self.setup('[[usenet_providers]]\nname = "news"\nenable = false\n')
        self.assertNotIn("SABnzbd", [p["name"] for p in macapp.pages(self.cfg)])

    def test_failed_build_falls_back_and_tries_again(self):
        self.compiler.return_value = ["swiftc"]
        with mock.patch.object(macapp, "compile_app", return_value="error: no SDK") as built:
            out = self.setup()
            self.assertIn("Couldn't build the native app (error: no SDK)", out)
            self.assertIn("exec open", (self.app / "Contents/MacOS/media-server").read_text())   # the browser launcher
            self.setup()
            self.assertEqual(built.call_count, 2)

    def test_launcher_prefers_an_app_mode_browser(self):
        script = macapp.launcher("http://localhost")
        self.assertIn('exec open -na "$d/$b.app" --args --app="$URL"', script)
        self.assertTrue(script.rstrip().endswith('exec open "$URL"'))
        self.assertEqual(subprocess.run(["sh", "-n"], input=script, text=True, check=False).returncode, 0)


class Compiler(unittest.TestCase):
    def test_not_there(self):
        with mock.patch.object(macapp.subprocess, "run", return_value=mock.Mock(returncode=2, stdout="")):
            self.assertIsNone(macapp.swiftc())
        with mock.patch.object(macapp.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="/nonexistent/dev\n")):
            self.assertIsNone(macapp.swiftc())

    @unittest.skipUnless(macapp.swiftc(), "Xcode or its Command Line Tools")
    def test_the_app_source_compiles(self):
        compiler = macapp.swiftc()
        assert compiler
        r = subprocess.run([*compiler, "-typecheck", str(macapp.SOURCE)], capture_output=True, text=True, env=macapp.apple_env(),
                           check=False)
        self.assertEqual(r.returncode, 0, r.stderr)


class Icon(unittest.TestCase):
    @unittest.skipUnless(macapp.shutil.which("sips") and macapp.shutil.which("iconutil"), "macOS's sips and iconutil")
    def test_icns(self):
        data = macapp.icns(macapp.icon_png(64))
        assert data is not None
        self.assertEqual(data[:4], b"icns")

    def test_a_valid_png_with_a_transparent_corner_and_a_gold_centre(self):
        png = macapp.icon_png(64)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        w, h = struct.unpack(">II", png[16:24])
        self.assertEqual((w, h), (64, 64))
        idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
        raw = zlib.decompress(idat)
        row = 1 + 64 * 4

        def pixel(x, y):
            i = y * row + 1 + x * 4
            return tuple(raw[i:i + 4])
        self.assertEqual(pixel(0, 0)[3], 0)   # outside the rounded square
        r, g, b, a = pixel(33, 32)
        self.assertEqual(a, 255)
        self.assertGreater(r, 200)   # the play button
        self.assertLess(b, 120)


if __name__ == "__main__":
    unittest.main()
