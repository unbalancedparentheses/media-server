"""The dashboard's speed-limit control (mediaserver/control.py): what it
accepts, what it changes in qBittorrent and SABnzbd, and who can ask.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import http.client
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mediaserver import common as c
from mediaserver import control


class Parse(unittest.TestCase):
    def test_requests(self):
        self.assertEqual(control.parse(b'{"limited": false}'), (False, 0, 0))
        self.assertEqual(control.parse(b'{"limited": true, "down": 5120, "up": 512}'), (True, 5120, 512))
        self.assertEqual(control.parse(b'{"limited": true}'), (True, 0, 0))
        for bad in (b"nope", b"[]", b'{"limited": "yes"}', b'{"limited": true, "down": -1}', b'{"limited": true, "up": 1.5}',
                    b'{"limited": true, "down": true}', b'{"limited": true, "down": 99999999999}'):
            self.assertIsInstance(control.parse(bad), str, bad)


class FakeClients:
    """qBittorrent (alternative limits, the turtle mode) and SABnzbd's limit"""

    def __init__(self):
        self.mode, self.sab_limit, self.qbit_up = "0", "0", True
        self.prefs = {"alt_dl_limit": 10240, "alt_up_limit": 10240, "dl_limit": 0, "up_limit": 102400}
        self.sab_refuses = self.toggle_fails = self.sab_unreadable = self.mode_unreadable = False

    def request(self, url, method="GET", headers=None, body=None, form=None, timeout=15, follow=True):
        if not self.qbit_up:
            return c.Response(0, {}, b"")
        if url.endswith("transfer/speedLimitsMode"):
            if self.mode_unreadable:
                return c.Response(500, {}, b"")
            return c.Response(200, {}, self.mode.encode())
        if url.endswith("transfer/toggleSpeedLimitsMode"):
            if self.toggle_fails:
                return c.Response(500, {}, b"")
            import time
            time.sleep(0.01)   # long enough for a second request to read the old mode
            self.mode = "0" if self.mode == "1" else "1"
            return c.Response(200, {}, b"")
        if url.endswith("app/preferences"):
            return c.Response(200, {}, json.dumps(self.prefs).encode())
        if url.endswith("app/setPreferences"):
            self.prefs.update(json.loads((form or {})["json"]))
            return c.Response(200, {}, b"")
        return c.Response(404, {}, b"")

    def try_json(self, url, headers=None, default=None, timeout=10):
        if "name=speedlimit" in url:
            if self.sab_refuses:
                return None
            self.sab_limit = url.split("value=")[1].split("&")[0]
            return {"status": True}
        if "mode=queue" in url and self.sab_unreadable:
            return None
        if "mode=queue" in url:
            kib = int(self.sab_limit[:-1]) if self.sab_limit.endswith("K") else 0
            return {"queue": {"speedlimit_abs": str(kib * 1024)}}
        return default


class Apply(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "sabnzbd").mkdir()
        (self.root / "sabnzbd/sabnzbd.ini").write_text("api_key = k\n")
        self.fake = FakeClients()
        for name in ("request", "try_json"):
            patcher = mock.patch.object(c, name, getattr(self.fake, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.speed = control.Speed(self.root)

    def test_limit_then_full_speed(self):
        state = self.speed.set(True, 5120, 512)
        self.assertEqual(state, {"answering": True, "limited": True, "down": 5120, "up": 512, "normal_down": 0, "normal_up": 100,
                                 "sabnzbd_limit": 5120})
        self.assertEqual((self.fake.mode, self.fake.sab_limit), ("1", "5120K"))
        state = self.speed.set(True, 2048, 0)   # already limited: not toggled off
        self.assertEqual((state["limited"], state["down"], state["up"]), (True, 2048, 0))
        state = self.speed.set(False)
        self.assertEqual((state["limited"], self.fake.mode, self.fake.sab_limit), (False, "0", "0"))
        # Full speed leaves the alternative limits for next time
        self.assertEqual(self.fake.prefs["alt_dl_limit"], 2048 * 1024)

    def test_upload_only_leaves_usenet_alone(self):
        self.speed.set(True, 0, 100)
        self.assertEqual(self.fake.sab_limit, "0")

    def test_partly_applied_says_which(self):
        self.fake.sab_refuses = True
        state = self.speed.set(True, 5120, 512)
        self.assertTrue(state["limited"])
        self.assertIn("SABnzbd didn't take it", state["warning"])

    def test_unreadable_sabnzbd_limit_isnt_taken_as_cleared(self):
        self.speed.set(True, 5120, 512)
        self.fake.sab_unreadable = True
        state = self.speed.set(False)
        self.assertIn("SABnzbd didn't take it", state.get("warning", ""))

    def test_limits_written_but_mode_unknown_says_so(self):
        self.fake.mode_unreadable = True
        result = self.speed.set(True, 5120, 512)
        self.assertIn("took the new limits", result["error"])
        self.assertEqual(self.fake.prefs["alt_dl_limit"], 5120 * 1024)   # they were written

    def test_mode_switch_failing_is_an_error(self):
        self.fake.toggle_fails = True
        self.assertIn("error", self.speed.set(True, 5120, 512))

    def test_two_devices_at_once_dont_undo_each_other(self):
        import threading
        threads = [threading.Thread(target=self.speed.set, args=(True, 1024, 100)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.fake.mode, "1")   # limited, not toggled back off

    def test_qbittorrent_down(self):
        self.fake.qbit_up = False
        self.assertEqual(self.speed.state(), {"answering": False})
        self.assertIn("error", self.speed.set(True, 100, 100))


class Endpoint(unittest.TestCase):
    """Only the dashboard's own JSON requests change anything"""

    def setUp(self):
        self.speed = mock.Mock()
        self.speed.state.return_value = {"answering": True, "limited": False, "down": 0, "up": 0}
        self.speed.set.return_value = {"answering": True, "limited": True, "down": 100, "up": 10}
        with mock.patch.object(control, "Speed", return_value=self.speed):
            self.server = control.serve(Path("/x"), 0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def ask(self, method, path="/speed", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = json.loads(r.read() or b"{}")
        conn.close()
        return r.status, data

    def test_reading(self):
        self.assertEqual(self.ask("GET")[0], 200)
        self.assertEqual(self.ask("GET", "/other")[0], 404)

    def test_changing_needs_the_dashboards_request(self):
        body = json.dumps({"limited": True, "down": 100, "up": 10})
        # A form post from another website: no custom header, not JSON
        self.assertEqual(self.ask("POST", body=body, headers={"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.ask("POST", body=body, headers={"Content-Type": "application/json"})[0], 403)
        self.speed.set.assert_not_called()
        status, data = self.ask("POST", body=body, headers={"Content-Type": "application/json", "X-Requested-With": "media-server"})
        self.assertEqual((status, data["limited"]), (200, True))
        self.speed.set.assert_called_once_with(True, 100, 10)
        status, data = self.ask("POST", body='{"limited": true, "down": -5}',
                                headers={"Content-Type": "application/json", "X-Requested-With": "media-server"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
