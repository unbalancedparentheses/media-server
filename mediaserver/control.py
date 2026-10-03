"""control: the dashboard's one change it can make, download and upload
speed limits, served by dashstatus on 127.0.0.1 and reached through nginx
(/api/control/speed) only from this Mac, the home network and Tailscale.

- qBittorrent: its alternative speed limits (the turtle in its UI), so the
  normal limits setup manages (downloads.upload_limit_kib) stay as they
  are and an install doesn't undo a limit set here.
- SABnzbd: its speed limit, set to the download limit (Usenet has no upload).

GET /speed: {"limited", "down", "up"} in KiB/s (0 = no limit that way).
POST /speed: {"limited": true, "down": 5120, "up": 512} or {"limited": false};
JSON only, with an X-Requested-With header, so a web page elsewhere can't
make your browser change them (that needs a CORS preflight nginx won't pass).
That isn't a login: anyone on the allowed networks can still change them.

Changes are made one at a time (a lock), then read back: the answer says
what actually took, and names a client that didn't.
"""
from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

from mediaserver import common as c
from mediaserver.config import local

MAX_KIB = 10_000_000   # about 10 GB/s: anything above is a typo
# One change at a time: switching qBittorrent's mode is read-then-toggle, so
# two devices applying at once could otherwise both toggle
LOCK = threading.Lock()


class Speed:
    def __init__(self, config: Path):
        self.config = config

    def qbit(self, path: str, form: dict | None = None):
        url = f"{local('qbittorrent')}/api/v2/{path}"
        return c.request(url, "POST" if form is not None else "GET", form=form, timeout=10)

    def sab(self, **params):
        key = c.sabnzbd_key(self.config)
        if not key:
            return None
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return c.try_json(f"{local('sabnzbd')}/api?{query}&output=json&apikey={key}")

    def state(self) -> dict:
        """What's in effect: limited (the dashboard's limits) or the normal
        limits setup manages (normal_down/normal_up, 0 = none), in KiB/s"""
        mode = self.qbit("transfer/speedLimitsMode")
        prefs = self.qbit("app/preferences").json({}) or {}
        if not mode.ok or not prefs:
            return {"answering": False}
        queue = ((self.sab(mode="queue") or {}).get("queue")) or {}
        return {"answering": True, "limited": mode.body.strip() == b"1",
                "down": int(prefs.get("alt_dl_limit") or 0) // 1024, "up": int(prefs.get("alt_up_limit") or 0) // 1024,
                "normal_down": int(prefs.get("dl_limit") or 0) // 1024, "normal_up": int(prefs.get("up_limit") or 0) // 1024,
                "sabnzbd_limit": int(float(queue.get("speedlimit_abs") or 0)) // 1024 if queue else None}

    def set(self, limited: bool, down: int = 0, up: int = 0) -> dict:
        """Apply, one change at a time, then read back. The state, with
        "warning" naming a client that didn't take it (partly applied), or
        {"error"} when qBittorrent didn't"""
        with LOCK:
            if limited:
                r = self.qbit("app/setPreferences", {"json": json.dumps({"alt_dl_limit": down * 1024, "alt_up_limit": up * 1024})})
                if not r.ok:
                    return {"error": "qBittorrent didn't accept the limits; nothing changed"}
            mode = self.qbit("transfer/speedLimitsMode")
            if not mode.ok:
                return {"error": "qBittorrent isn't answering; nothing changed"}
            if (mode.body.strip() == b"1") != limited and not self.qbit("transfer/toggleSpeedLimitsMode", {}).ok:
                return {"error": "qBittorrent didn't switch its limits"}
            # SABnzbd: the same download limit, or none ("0")
            sab_answer = self.sab(mode="config", name="speedlimit", value=f"{down}K" if limited and down else "0")
            state = self.state()
        problems = []
        if not state.get("answering"):
            return {"error": "qBittorrent stopped answering; check its state in Manage"}
        if state["limited"] != limited or (limited and (state["down"], state["up"]) != (down, up)):
            problems.append("qBittorrent")
        want_sab = down if limited else 0
        if c.sabnzbd_key(self.config) and (sab_answer is None or (state.get("sabnzbd_limit") or 0) != want_sab):
            problems.append("SABnzbd")
        if problems == ["qBittorrent"] or problems == ["qBittorrent", "SABnzbd"]:
            return {"error": f"{' and '.join(problems)} didn't take it", **state}
        if problems:
            state["warning"] = "Applied to qBittorrent, but SABnzbd didn't take it (Usenet downloads keep their old limit)"
        return state


def parse(body: bytes) -> tuple[bool, int, int] | str:
    """(limited, down, up) from a request, or what's wrong with it"""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return "not JSON"
    if not isinstance(data, dict) or not isinstance(data.get("limited"), bool):
        return '"limited" must be true or false'
    if not data["limited"]:
        return False, 0, 0
    values = []
    for k in ("down", "up"):
        v = data.get(k, 0)
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= MAX_KIB:
            return f'"{k}" must be KiB/s from 0 (no limit) to {MAX_KIB}'
        values.append(v)
    return True, values[0], values[1]


def handler(speed: Speed):
    class Handler(http.server.BaseHTTPRequestHandler):
        def answer(self, status: int, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/speed":
                return self.answer(404, {"error": "not found"})
            self.answer(200, speed.state())

        def do_POST(self):
            if self.path != "/speed":
                return self.answer(404, {"error": "not found"})
            # Only the dashboard's own requests: JSON with this header
            if not self.headers.get("X-Requested-With") or "application/json" not in self.headers.get("Content-Type", ""):
                return self.answer(403, {"error": "the dashboard's requests only"})
            length = int(self.headers.get("Content-Length") or 0)
            parsed = parse(self.rfile.read(min(length, 10_000)))
            if isinstance(parsed, str):
                return self.answer(400, {"error": parsed})
            result = speed.set(*parsed)
            self.answer(502 if "error" in result else 200, result)

        def log_message(self, format, *args):  # noqa: A002 (the base class's name)
            pass
    return Handler


def serve(config: Path, port: int) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler(Speed(config)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
