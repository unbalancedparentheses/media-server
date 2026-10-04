"""control: the changes the dashboard can make, served by dashstatus on
127.0.0.1 and reached through nginx (/api/control/…) only from this Mac,
the home network and Tailscale: download and upload speed limits, and
deleting a title (which also needs the Jellyfin password; see Library).

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
from mediaserver.api import ApiError
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
                return {"error": "qBittorrent took the new limits but its mode couldn't be checked; they may be active"
                        if limited else "qBittorrent isn't answering; nothing changed"}
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
        # Only a limit actually read back counts: unknown isn't "no limit"
        want_sab = down if limited else 0
        if c.sabnzbd_key(self.config) and (sab_answer is None or state.get("sabnzbd_limit") is None
                                           or state["sabnzbd_limit"] != want_sab):
            problems.append("SABnzbd")
        if problems == ["qBittorrent"] or problems == ["qBittorrent", "SABnzbd"]:
            return {"error": f"{' and '.join(problems)} didn't take it", **state}
        if problems:
            state["warning"] = "Applied to qBittorrent, but SABnzbd didn't take it (Usenet downloads keep their old limit)"
        return state


# ─── Deleting a title ────────────────────────────────────────────

class Library:
    """Delete a film, a series or one season the way that sticks: through
    Radarr/Sonarr with its files (deleting it only in Jellyfin would make
    them download it again), its torrents in qBittorrent with their data
    (the seeding copy would keep the space), its Seerr entry for a whole
    title (so it can be requested again), then a Jellyfin rescan. Asking
    needs the Jellyfin password: this can't be undone."""

    def __init__(self, config: Path):
        self.config = config

    @property
    def state(self) -> Path:
        return self.config.parent / ".state"

    def arr(self, name: str, method: str, path: str, body=None):
        from mediaserver import api
        return api.call(method, f"{local(name)}/api/v3/{path}", {"X-Api-Key": c.arr_key(self.config, name)}, body=body)

    def jellyfin(self, path: str):
        return c.get_json(f"{local('jellyfin')}/{path}", c.jellyfin_auth(self.state))

    def password_ok(self, password: str) -> bool:
        from mediaserver.config import Config, Paths
        try:
            user = Config.load(Paths(self.config.parent)).jellyfin_user
        except (OSError, ValueError):
            return False
        r = c.request(f"{local('jellyfin')}/Users/AuthenticateByName", "POST",
                      {"Authorization": 'MediaBrowser Client="dashboard", Device="dashboard", DeviceId="dashboard-delete", Version="1"'},
                      body={"Username": user, "Pw": password}, timeout=20)
        return r.ok and bool(r.json({}).get("AccessToken"))

    def resolve(self, item_id: str) -> dict:
        """A Jellyfin item (a film, series or episode) → the title in Radarr/Sonarr"""
        items = self.jellyfin(f"Items?Ids={item_id}&Fields=ProviderIds").get("Items") or []
        if not items:
            raise LookupError("it's not in the library any more")
        item = items[0]
        if item.get("Type") in ("Episode", "Season") and item.get("SeriesId"):
            item = (self.jellyfin(f"Items?Ids={item['SeriesId']}&Fields=ProviderIds").get("Items") or [item])[0]
        ids = item.get("ProviderIds") or {}
        if item.get("Type") == "Movie":
            found = self.arr("radarr", "GET", f"movie?tmdbId={ids.get('Tmdb')}") if ids.get("Tmdb") else []
            if not found:
                raise LookupError("Radarr doesn't have it")
            m = found[0]
            return {"kind": "movie", "title": f"{m.get('title')} ({m.get('year')})", "id": m["id"], "tmdb": m.get("tmdbId"),
                    "size": m.get("sizeOnDisk") or 0, "seasons": []}
        found = self.arr("sonarr", "GET", f"series?tvdbId={ids.get('Tvdb')}") if ids.get("Tvdb") else []
        if not found:
            raise LookupError("Sonarr doesn't have it")
        s = found[0]
        return {"kind": "series", "title": s.get("title"), "id": s["id"], "tmdb": int(ids["Tmdb"]) if ids.get("Tmdb") else None,
                "size": (s.get("statistics") or {}).get("sizeOnDisk") or 0,
                "seasons": [{"number": x["seasonNumber"], "size": (x.get("statistics") or {}).get("sizeOnDisk") or 0}
                            for x in s.get("seasons") or [] if (x.get("statistics") or {}).get("sizeOnDisk")]}

    def downloads(self, title: dict, season: int | None) -> set:
        """qBittorrent hashes of what was grabbed for it (or for that season)"""
        if title["kind"] == "movie":
            grabs = self.arr("radarr", "GET", f"history/movie?movieId={title['id']}&eventType=1") or []
            return {g["downloadId"].lower() for g in grabs if g.get("downloadId")}
        grabs = self.arr("sonarr", "GET", f"history/series?seriesId={title['id']}&eventType=1") or []
        if season is not None:
            seasons = {e["id"]: e.get("seasonNumber") for e in self.arr("sonarr", "GET", f"episode?seriesId={title['id']}") or []}
            grabs = [g for g in grabs if seasons.get(g.get("episodeId")) == season]
        return {g["downloadId"].lower() for g in grabs if g.get("downloadId")}

    def delete(self, item_id: str, season: int | None, exclude: bool) -> dict:
        title = self.resolve(item_id)
        hashes = self.downloads(title, season)
        warnings = []
        if title["kind"] == "movie":
            for q in (self.arr("radarr", "GET", f"queue?movieIds={title['id']}") or {}).get("records") or []:
                self.arr("radarr", "DELETE", f"queue/{q['id']}?removeFromClient=true&blocklist=false")
            self.arr("radarr", "DELETE", f"movie/{title['id']}?deleteFiles=true&addImportExclusion={'true' if exclude else 'false'}")
            freed = title["size"]
        elif season is None:
            for q in (self.arr("sonarr", "GET", f"queue?seriesIds={title['id']}") or {}).get("records") or []:
                self.arr("sonarr", "DELETE", f"queue/{q['id']}?removeFromClient=true&blocklist=false")
            self.arr("sonarr", "DELETE", f"series/{title['id']}?deleteFiles=true&addImportListExclusion={'true' if exclude else 'false'}")
            freed = title["size"]
        else:
            # One season: not wanted any more (or it'd be downloaded again),
            # then its files
            series = self.arr("sonarr", "GET", f"series/{title['id']}")
            series["seasons"] = [dict(x, monitored=False) if x.get("seasonNumber") == season else x for x in series.get("seasons") or []]
            self.arr("sonarr", "PUT", f"series/{title['id']}", series)
            files = [f for f in self.arr("sonarr", "GET", f"episodefile?seriesId={title['id']}") or [] if f.get("seasonNumber") == season]
            for f in files:
                self.arr("sonarr", "DELETE", f"episodefile/{f['id']}")
            freed = sum(f.get("size") or 0 for f in files)
            title["title"] = f"{title['title']} season {season}"
        # The seeding copies
        if hashes:
            r = c.request(f"{local('qbittorrent')}/api/v2/torrents/delete", "POST",
                          form={"hashes": "|".join(sorted(hashes)), "deleteFiles": "true"}, timeout=20)
            if not r.ok:
                warnings.append("its torrents couldn't be removed from qBittorrent")
        # Requestable again (a whole title only)
        if season is None and title.get("tmdb"):
            kind = "movie" if title["kind"] == "movie" else "tv"
            seerr = {"X-Api-Key": c.seerr_key(self.config)}
            media = (c.try_json(f"{local('seerr')}/api/v1/{kind}/{title['tmdb']}", seerr) or {}).get("mediaInfo") or {}
            if media.get("id") and not c.request(f"{local('seerr')}/api/v1/media/{media['id']}", "DELETE", seerr, timeout=20).ok:
                warnings.append("Seerr still lists it as available until its next sync")
        c.request(f"{local('jellyfin')}/Library/Refresh", "POST", c.jellyfin_auth(self.state), body=b"", timeout=20)
        c.log(f"deleted from the dashboard: {title['title']} ({freed // 1024 ** 2} MB, {len(hashes)} torrents)")
        return {"deleted": title["title"], "freed": freed, "torrents": len(hashes), "warnings": warnings}


def parse_delete(body: bytes) -> tuple[str, int | None, bool, str] | str:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return "not JSON"
    item, season, exclude, password = data.get("item"), data.get("season"), data.get("exclude", False), data.get("password")
    if not isinstance(item, str) or not item.isalnum() or len(item) > 64:
        return '"item" must be a library item id'
    if season is not None and (not isinstance(season, int) or isinstance(season, bool) or not 0 <= season <= 1000):
        return '"season" must be a season number'
    if not isinstance(exclude, bool):
        return '"exclude" must be true or false'
    if not isinstance(password, str) or not password:
        return "the Jellyfin password is needed to delete"
    return item, season, exclude, password


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


def handler(speed: Speed, library: "Library | None" = None):
    lib: Library = library or Library(speed.config)

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
            if self.path == "/speed":
                return self.answer(200, speed.state())
            if self.path.startswith("/delete?item="):
                # What deleting would remove, for the confirmation
                item = self.path.split("=", 1)[1]
                if not item.isalnum():
                    return self.answer(400, {"error": "not a library item"})
                try:
                    return self.answer(200, lib.resolve(item))
                except LookupError as e:
                    return self.answer(404, {"error": f"Can't delete it here: {e}"})
                except (ApiError, *c.HTTP_ERRORS):
                    return self.answer(502, {"error": "Radarr, Sonarr or Jellyfin isn't answering"})
            self.answer(404, {"error": "not found"})

        def do_POST(self):
            if self.path not in ("/speed", "/delete"):
                return self.answer(404, {"error": "not found"})
            # Only the dashboard's own requests: JSON with this header
            if not self.headers.get("X-Requested-With") or "application/json" not in self.headers.get("Content-Type", ""):
                return self.answer(403, {"error": "the dashboard's requests only"})
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, 10_000))
            if self.path == "/delete":
                return self.delete(body)
            parsed = parse(body)
            if isinstance(parsed, str):
                return self.answer(400, {"error": parsed})
            result = speed.set(*parsed)
            self.answer(502 if "error" in result else 200, result)

        def delete(self, body: bytes) -> None:
            parsed = parse_delete(body)
            if isinstance(parsed, str):
                return self.answer(400, {"error": parsed})
            item, season, exclude, password = parsed
            if not lib.password_ok(password):
                import time
                time.sleep(1)   # guessing is slow
                return self.answer(403, {"error": "That's not the Jellyfin password"})
            try:
                with LOCK:
                    result = lib.delete(item, season, exclude)
            except LookupError as e:
                return self.answer(404, {"error": f"Can't delete it here: {e}"})
            except (ApiError, *c.HTTP_ERRORS) as e:
                return self.answer(502, {"error": f"It couldn't be deleted completely ({e}); check Radarr/Sonarr"})
            self.answer(200, result)

        def log_message(self, format, *args):  # noqa: A002 (the base class's name)
            pass
    return Handler


def serve(config: Path, port: int) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler(Speed(config)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
