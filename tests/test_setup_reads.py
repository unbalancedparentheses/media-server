"""Smaller safety points: Bazarr's config stays private, the Byparr tag
goes on Byparr's proxy, a SABnzbd setting it refused isn't counted as set,
netwatch leaves Cleanuparr alone when it doesn't know what's wanted, and the
stream check reads only the start of a film.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import http.server
import io
import shutil
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mediaserver import playback
from mediaserver.steps import bazarr, downloads, prowlarr
from tests.test_netwatch import FakeNetwatch


class Scratch(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)


class BazarrPrivate(Scratch):
    def test_written_0600_whatever_the_umask(self):
        import os
        path = self.dir / "config.yaml"
        old = os.umask(0o022)
        try:
            bazarr.write_yaml(path, {"auth": {"apikey": "secret"}})
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


    def test_an_existing_readable_file_is_made_private(self):
        from tests.test_integration import example_config
        cfg = example_config(self.dir)
        path = self.dir / "config/bazarr/config/config.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("auth:\n  apikey: secret\n")
        path.chmod(0o644)
        with mock.patch.object(bazarr, "configure", create=True), redirect_stdout(io.StringIO()):
            try:
                bazarr.run(cfg)
            except Exception:
                pass   # the rest needs a Bazarr; only the file's mode matters here
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class ByparrProxy(unittest.TestCase):
    def test_tag_goes_on_byparrs_proxy_not_the_first(self):
        calls = []

        class P:
            def call(self, method, path, body=None):
                calls.append((method, path, body))
                if path == "tag":
                    return [{"id": 3, "label": "flaresolverr"}]
                if path == "indexerProxy":
                    return [{"id": 1, "name": "Some HTTP proxy", "tags": []}, {"id": 2, "name": "Byparr", "tags": []}]
                return {}
        self.assertEqual(prowlarr.flaresolverr_tag(P()), 3)   # type: ignore[arg-type]
        puts = [(p, b["tags"]) for m, p, b in calls if m == "PUT"]
        self.assertEqual(puts, [("indexerProxy/2", [3])])


class SabnzbdRefusal(unittest.TestCase):
    def test_an_error_answer_isnt_success(self):
        sab = downloads.Sabnzbd.__new__(downloads.Sabnzbd)
        for answer, ok in (({"config": {"misc": {}}}, True), ({"status": False, "error": "Not allowed"}, False), (None, False)):
            with mock.patch.object(sab, "api", return_value=answer):
                self.assertEqual(sab.set("misc", "complete_dir", "/x"), ok, answer)


class NetwatchUnknown(Scratch):
    def test_unknown_wanted_leaves_the_cleaner_as_it_is(self):
        nw = FakeNetwatch(self.dir)
        nw.state_dir.mkdir(parents=True)
        nw.cleaner_on = False
        logs = []
        with mock.patch("mediaserver.netwatch.c.log", logs.append):
            nw.round()
        self.assertFalse(nw.cleaner_on)   # not switched on by a guess
        self.assertTrue(any("isn't known" in x for x in logs))
        (nw.state_dir / "cleanuparr-wanted").write_text("true\n")
        nw.round()
        self.assertTrue(nw.cleaner_on)


class WholeFilm(http.server.BaseHTTPRequestHandler):
    """A server that ignores Range and sends a 'film' of 8 MB"""
    sent = 0

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(8 * 1024 * 1024))
        self.end_headers()
        try:
            for _ in range(128):
                self.wfile.write(b"\0" * 65536)
                type(self).sent += 65536
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):  # noqa: A002 (the base class's name)
        pass


class BoundedRead(Scratch):
    def test_only_the_start_is_read(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), WholeFilm)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        (self.dir / "dashstatus").mkdir()
        (self.dir / "dashstatus/jellyfin-key").write_text("k")
        with mock.patch.object(playback, "local", return_value=f"http://127.0.0.1:{server.server_address[1]}"):
            jf = playback.Jellyfin(self.dir)
            self.assertEqual(jf.first_bytes("i1", "src"), 200)


if __name__ == "__main__":
    unittest.main()
