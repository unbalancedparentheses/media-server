"""The Byparr launcher and the e2e test's film download.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import http.server
import io
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mediaserver import byparr, e2e


class Scratch(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)


class Byparr(Scratch):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.failing = set()

        def run(cmd, cwd=None, env=None, check=False):
            self.calls.append(cmd[1:3])
            return mock.Mock(returncode=1 if cmd[1] in self.failing or "fetch" in cmd and "fetch" in self.failing else 0)
        patcher = mock.patch.object(byparr.subprocess, "run", run)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(byparr.c, "log")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = byparr.environment(self.dir / "state")

    def test_browser_fetched_once(self):
        self.assertTrue(byparr.prepare("uv", self.dir, self.dir / "state", self.env))
        self.assertTrue(byparr.prepare("uv", self.dir, self.dir / "state", self.env))
        self.assertEqual(self.calls, [["sync", "--frozen"], ["run", "--frozen"], ["sync", "--frozen"]])

    def test_failed_fetch_tried_again_next_start(self):
        self.failing = {"fetch"}
        self.assertFalse(byparr.prepare("uv", self.dir, self.dir / "state", self.env))
        self.assertFalse((self.dir / "state/browser-fetched").exists())

    def test_failed_sync_doesnt_start(self):
        self.failing = {"sync"}
        self.assertFalse(byparr.prepare("uv", self.dir, self.dir / "state", self.env))
        self.assertEqual(self.calls, [["sync", "--frozen"]])

    def test_environment_and_start(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/ours", "BYPARR_SRC": str(self.dir), "BYPARR_STATE": str(self.dir / "state"),
                                          "BYPARR_UV": "/bin/uv"}), \
                mock.patch.object(byparr.os, "execvpe") as execvpe, mock.patch.object(byparr.os, "chdir"):
            byparr.main()
        uv, argv, env = execvpe.call_args[0]
        self.assertEqual((uv, argv[-2:]), ("/bin/uv", ["python", "main.py"]))
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], str(self.dir / "state/venv"))


class RangeHandler(http.server.BaseHTTPRequestHandler):
    data = b""
    honour_range = True
    cut_after = None   # bytes sent before the connection drops

    def do_GET(self):
        # Like Cloudflare in front of download.blender.org
        if "Python-urllib" in self.headers.get("User-Agent", ""):
            self.send_response(403)
            self.end_headers()
            return
        start = 0
        rng = self.headers.get("Range")
        if rng and self.honour_range:
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
        else:
            self.send_response(200)
        body = self.data[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body[:self.cut_after] if self.cut_after else body)

    def log_message(self, format, *args):  # noqa: A002 (the base class's name)
        pass


class Download(Scratch):
    def serve(self, data, honour_range=True, cut_after=None):
        handler = type("H", (RangeHandler,), {"data": data, "honour_range": honour_range, "cut_after": cut_after})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/film.mov"

    def get(self, url, size):
        with redirect_stdout(io.StringIO()):
            return e2e.download(url, self.dir / "film.mov", size, timeout=5)

    def test_whole_file(self):
        data = os.urandom(3 << 20)
        self.assertTrue(self.get(self.serve(data), len(data)))
        self.assertEqual((self.dir / "film.mov").read_bytes(), data)
        self.assertFalse((self.dir / "film.mov.part").exists())

    def test_interrupted_then_continued(self):
        data = os.urandom(3 << 20)
        self.assertFalse(self.get(self.serve(data, cut_after=1 << 20), len(data)))
        self.assertFalse((self.dir / "film.mov").exists())   # never under the final name
        self.assertEqual((self.dir / "film.mov.part").stat().st_size, 1 << 20)
        # The next try asks for the rest only
        self.assertTrue(self.get(self.serve(data), len(data)))
        self.assertEqual((self.dir / "film.mov").read_bytes(), data)

    def test_server_ignoring_the_range_starts_over(self):
        data = os.urandom(2 << 20)
        (self.dir / "film.mov.part").write_bytes(data[:1000])
        self.assertTrue(self.get(self.serve(data, honour_range=False), len(data)))
        self.assertEqual((self.dir / "film.mov").read_bytes(), data)

    def test_bigger_than_expected_isnt_kept(self):
        data = os.urandom(1 << 20)
        self.assertFalse(self.get(self.serve(data), len(data) - 5))
        self.assertFalse((self.dir / "film.mov").exists())
        self.assertFalse((self.dir / "film.mov.part").exists())

    def test_nothing_answering(self):
        self.assertFalse(self.get("http://127.0.0.1:9/film.mov", 10))


if __name__ == "__main__":
    unittest.main()
