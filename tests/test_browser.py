"""The dashboard in a real browser engine: landing.html loaded in WebKit
(tests/webcheck.swift, the engine of Safari and the Media Server app)
against a stand-in server with fixed data, clicked and typed into like a
person would. Catches what the backend tests can't: a list that never
renders, a filter that throws, a dialog that sends the wrong request.

Needs the Swift compiler (Xcode or its Command Line Tools; skipped without).

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
from __future__ import annotations

import http.server
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mediaserver.steps import macapp

REPO = Path(__file__).resolve().parent.parent
NOW = int(time.time())


def card(i, title, kind="film", **extra):
    return dict({"id": i, "title": title, "detail": "2010", "image": i, "tag": "t", "kind": kind}, **extra)


STATUS = {
    "updated": NOW, "media_updated": NOW, "slow_updated": NOW, "media_ages": {}, "media_failed": [],
    "playing": [], "attention": [], "connection": "online", "jellyfin_up": True,
    "downloads": {"answering": {"qbittorrent": True, "sabnzbd": True}, "dl_speed": 0, "up_speed": 0, "downloading": 0, "stalled": 0, "seeding": 0},
    "speed_limit": {"answering": True, "limited": False, "down": 0, "up": 0},
    "system": {}, "usage": {}, "sonarr": {"health": []}, "radarr": {"health": []}, "prowlarr": {"off": []},
    "library": {"movies": 2, "series": 1, "episodes": 13}, "subtitles": {}, "indexer_stats": {}, "tailscale": {}, "uptime": [],
    "requests": {}, "disk": {"total_gb": 1000, "free_gb": 900, "warn_gb": 50, "min_gb": 10},
    "continue": [card("c1", "Sintel", user="you", kind="resume", progress=40)],
    "tonight": [card("t1", "Tears of Steel", kind="film", minutes=12), card("t2", "Spring", kind="film", minutes=8),
                card("t3", "Lain", kind="anime", minutes=24)],
    "because": [], "latest": [], "requests_live": [], "upcoming": [],
    "health": {"fixed": [], "looking": [], "kept": [], "playback_failed": [], "dubs": []},
    "recommended": {"anime": [], "series": [], "movies": []},
    "library_titles": [{"id": "jf1", "title": "Sintel", "year": 2010, "kind": "film", "size": 1_000_000_000, "managed": True},
                       {"id": "jf2", "title": "Spring", "year": 2019, "kind": "film", "size": 500_000_000, "managed": True},
                       {"id": "jf3", "title": "Lain", "year": 1998, "kind": "series", "size": 9_000_000_000, "managed": True}],
    "abandoned": [{"title": "Kaiji season 2", "app": "Sonarr", "days": 30, "searches": 9, "why": "No releases found",
                   "item": "sonarr13", "season": 2, "episodes": 26, "admin": "sonarr:/wanted/missing"}],
    "unfinished": [{"what": "Repackaging Film as MP4", "left": "Sonarr/Radarr rescan", "error": "", "retry": "in the next round"}],
    "paused": {},
}
PREVIEWS = {"jf1": {"kind": "movie", "title": "Sintel (2010)", "id": 1, "size": 1_000_000_000, "seasons": []},
            "sonarr13": {"kind": "series", "title": "Kaiji", "id": 13, "size": 10_000_000_000,
                         "seasons": [{"number": 1, "size": 10_000_000_000}, {"number": 2, "size": 0}]}}


class Stand(http.server.BaseHTTPRequestHandler):
    """landing.html, its status.json and the endpoints it calls; POSTs recorded"""
    posts: list = []
    page = ""

    def send(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self.send(200, self.page.encode(), "text/html")
        if path == "/status.json":
            return self.send(200, STATUS)
        if path == "/api/control/delete":
            return self.send(200, PREVIEWS.get(self.path.split("item=")[1], {}))
        if path == "/api/jellyfin/Items":
            return self.send(200, {"Items": [{"Id": "jf1", "Name": "Sintel", "Type": "Movie", "ProductionYear": 2010,
                                              "ProviderIds": {"Tmdb": "45745"}}]})
        if path == "/api/seerr/search":
            return self.send(200, {"results": [{"id": 45745, "mediaType": "movie", "title": "Sintel", "releaseDate": "2010-09-27"},
                                               {"id": 10378, "mediaType": "movie", "title": "Big Buck Bunny", "releaseDate": "2008-04-10"}]})
        if path.startswith("/api/"):
            return self.send(200, [] if "qbt" in path else {})
        return self.send(404, b"", "text/plain")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        Stand.posts.append((self.path, body))
        if self.path == "/api/control/pause":
            return self.send(200, {"paused": {body["what"]: NOW + 86400}})
        if self.path == "/api/control/delete":
            return self.send(200, {"stopped": "Kaiji season 2"} if body.get("stop") else {"deleted": "Sintel (2010)", "files_removed": 1})
        return self.send(200, {})

    def log_message(self, format, *args):  # noqa: A002 (the base class's name)
        pass


@unittest.skipUnless(macapp.swiftc(), "needs the Swift compiler (Xcode or its Command Line Tools)")
class Dashboard(unittest.TestCase):
    runner: Path

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.runner = cls.tmp / "webcheck"
        compiler = macapp.swiftc()
        assert compiler
        r = subprocess.run([*compiler, "-O", "-o", str(cls.runner), str(REPO / "tests/webcheck.swift")],
                           capture_output=True, text=True, env=macapp.apple_env(), check=False)
        if r.returncode:
            raise unittest.SkipTest(f"couldn't build the WebKit runner: {r.stderr[-300:]}")
        Stand.page = (REPO / "landing.html").read_text()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stand)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_steps(self, steps: list, hash_: str = "") -> list:
        Stand.posts = []
        file = self.tmp / "steps.json"
        file.write_text(json.dumps(steps))
        r = subprocess.run([str(self.runner), f"http://127.0.0.1:{self.server.server_address[1]}/{hash_}", str(file)],
                           capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_home_renders_and_filters_work(self):
        out = self.run_steps([
            {"js": "return document.querySelectorAll('#continue .poster').length"},
            {"js": "return document.querySelectorAll('#tonight .poster').length"},
            {"js": "return document.querySelectorAll('#library-list .lib-row').length"},   # once empty until you typed
            # Watch tonight: Anime only
            {"js": "var b=[...document.querySelectorAll('.chips button')].find(x=>x.textContent.trim()==='Anime'); b.click(); return true"},
            {"js": "return [...document.querySelectorAll('#tonight .poster')].map(p=>p.textContent.trim().slice(0,4))"},
            # Library filter
            {"js": "var f=document.getElementById('library-filter'); f.value='spr'; f.dispatchEvent(new Event('input')); return true"},
            {"js": "return [...document.querySelectorAll('#library-list .lib-row')].map(r=>r.textContent.trim().slice(0,6))"},
            {"js": "return window.__errors || []"},
        ])
        self.assertEqual(out[0], 1)
        self.assertEqual(out[1], 3)
        self.assertEqual(out[2], 3)
        self.assertEqual(out[4], ["Lain"])
        self.assertEqual(out[6], ["Spring"])

    def test_search_merges_library_and_requests(self):
        out = self.run_steps([
            {"js": "var s=document.getElementById('search'); s.value='sintel'; s.dispatchEvent(new Event('input')); return true", "wait": 900},
            {"js": "return [...document.querySelectorAll('.sr-group')].map(g=>g.textContent)"},
            {"js": "return [...document.querySelectorAll('.sr-item')].map(i=>i.textContent.includes('Play')+':'+i.textContent.slice(0,8))"},
        ])
        self.assertEqual(out[1], ["In your library", "Not in your library"])
        self.assertEqual(out[2][0], "true:Sintel 2")   # one row for Sintel, with Play (matched by TMDB id)

    def test_delete_and_stop_looking_dialogs_send_the_right_request(self):
        out = self.run_steps([
            {"js": "document.querySelector('[data-delete=\"jf1\"]').click(); return true", "wait": 600},
            {"js": "return [document.getElementById('del-title').textContent, document.getElementById('del-go').textContent,"
                   " !document.getElementById('del-hint').classList.contains('hidden')]"},
            {"js": "document.getElementById('del-cancel').click(); location.hash='#manage'; return true", "wait": 600},
            {"js": "document.querySelector('[data-stop]').click(); return true", "wait": 600},
            {"js": "return [document.getElementById('del-title').textContent, document.getElementById('del-go').textContent,"
                   " document.getElementById('del-hint').classList.contains('hidden')]"},
            {"js": "document.getElementById('del-go').click(); return true", "wait": 600},
        ])
        self.assertEqual(out[1], ["Delete Sintel (2010)?", "Delete", True])
        self.assertEqual(out[4], ["Stop looking for Kaiji season 2?", "Stop looking", True])
        self.assertIn(("/api/control/delete", {"item": "sonarr13", "season": 2, "stop": True}), Stand.posts)

    def test_automation_panel(self):
        out = self.run_steps([
            {"js": "return document.getElementById('auto-sub').textContent"},
            {"js": "return document.querySelectorAll('#unfinished .lib-row').length"},
            {"js": "document.querySelector('[data-pause=\"searches\"][data-for=\"24h\"]').click(); return true", "wait": 600},
            {"js": "return document.getElementById('pause-searches').textContent"},
        ], "#manage")
        self.assertEqual(out[0], "1 thing unfinished")
        self.assertEqual(out[1], 1)
        self.assertIn("paused until", out[3])
        self.assertIn(("/api/control/pause", {"what": "searches", "for": "24h"}), Stand.posts)


if __name__ == "__main__":
    unittest.main()
