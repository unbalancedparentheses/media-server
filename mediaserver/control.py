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

    # ─── Which torrents can go ───────────────────────────────────
    # Deleted with its data only with proof: every video in the torrent is a
    # file Sonarr/Radarr imported for exactly what's being deleted (their
    # import history records each file's original path). Anything else,
    # including what's still downloading, is kept and named.

    VIDEO = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".wmv", ".mov")

    def torrent_files(self, h: str) -> list | None:
        r = c.request(f"{local('qbittorrent')}/api/v2/torrents/files?hash={h}", timeout=20)
        files = r.json(None) if r.ok else None
        return files if isinstance(files, list) else None

    def torrents(self, title: dict, season: int | None) -> tuple[list, list]:
        """(hashes to delete, names of torrents kept)"""
        import os
        app = "radarr" if title["kind"] == "movie" else "sonarr"
        history = (f"history/movie?movieId={title['id']}" if title["kind"] == "movie"
                   else f"history/series?seriesId={title['id']}")
        grabs = self.arr(app, "GET", f"{history}&eventType=1") or []
        imports = self.arr(app, "GET", f"{history}&eventType=3") or []
        episodes_of = ({} if title["kind"] == "movie" else
                       {e["id"]: e.get("seasonNumber") for e in self.arr("sonarr", "GET", f"episode?seriesId={title['id']}") or []})

        def in_scope(record: dict) -> bool:
            return season is None or episodes_of.get(record.get("episodeId")) == season
        # File names imported for what's being deleted, per download
        ours: dict = {}
        for r in imports:
            dropped = (r.get("data") or {}).get("droppedPath") or ""
            if r.get("downloadId") and dropped and in_scope(r):
                ours.setdefault(r["downloadId"].lower(), set()).add(os.path.basename(dropped))
        names = {}
        for g in grabs:
            if g.get("downloadId") and in_scope(g):
                names.setdefault(g["downloadId"].lower(), g.get("sourceTitle") or g["downloadId"])
        delete, kept = [], []
        for h, name in names.items():
            files = self.torrent_files(h)
            if files is None:
                if self.torrent_known(h):
                    kept.append(name)   # can't see inside it: kept
                continue                # (or qBittorrent no longer has it)
            videos = [os.path.basename(f.get("name") or "") for f in files
                      if (f.get("name") or "").lower().endswith(self.VIDEO) and "sample" not in (f.get("name") or "").lower()]
            if videos and set(videos) <= ours.get(h, set()):
                delete.append(h)
            else:
                kept.append(name)
        return delete, kept

    def torrent_known(self, h: str) -> bool:
        r = c.request(f"{local('qbittorrent')}/api/v2/torrents/info?hashes={h}", timeout=20)
        return bool(r.json([])) if r.ok else True

    # ─── Deleting, from a saved plan ─────────────────────────────
    # The plan (ids, torrents, what's left to do) is saved before anything
    # is deleted, and each step crossed off when done: a failure halfway is
    # finished by trying again, even once Radarr/Sonarr no longer have it.

    @property
    def plans_file(self) -> Path:
        return self.state / "deletions.json"

    def plans(self) -> dict:
        """Unfinished deletions; raises PlanTrouble when the record can't be
        read (it may hold cleanup still to do: never replaced by an empty one)"""
        if not self.plans_file.exists():
            return {}
        data = c.read_json(self.plans_file)
        if not isinstance(data, dict):
            raise PlanTrouble(f"{self.plans_file} can't be read; it may list cleanup still to do. "
                              "Fix or remove it, then try again")
        return data

    def save_plan(self, item_id: str, plan: dict | None) -> None:
        plans = self.plans()
        if plan is None:
            plans.pop(item_id, None)
        else:
            plans[item_id] = plan
        c.write_json(self.plans_file, plans, mode=0o600)

    def plan(self, item_id: str, season: int | None, exclude: bool) -> dict:
        title = self.resolve(item_id)
        hashes, kept = self.torrents(title, season)
        plan = {"title": title["title"], "kind": title["kind"], "id": title["id"], "tmdb": title.get("tmdb"),
                "season": season, "exclude": exclude, "hashes": hashes, "kept": kept, "files": [], "bytes": title["size"],
                "warnings": [], "started": int(__import__("time").time())}
        if title["kind"] == "movie":
            plan["steps"] = ["queue", "arr", "torrents", "seerr", "refresh"]
        elif season is None:
            plan["steps"] = ["queue", "arr", "torrents", "seerr", "refresh"]
        else:
            files = [f for f in self.arr("sonarr", "GET", f"episodefile?seriesId={title['id']}") or [] if f.get("seasonNumber") == season]
            plan.update(title=f"{title['title']} season {season}", files=[f["id"] for f in files],
                        bytes=sum(f.get("size") or 0 for f in files), steps=["unmonitor", "files", "torrents", "refresh"])
        return plan

    def step(self, plan: dict, name: str) -> None:
        app = "radarr" if plan["kind"] == "movie" else "sonarr"
        if name == "queue":
            ids = "movieIds" if plan["kind"] == "movie" else "seriesIds"
            # Out of Sonarr/Radarr's queue, but left in qBittorrent: what a
            # download that isn't finished holds can't be proven ours
            for q in (self.arr(app, "GET", f"queue?{ids}={plan['id']}") or {}).get("records") or []:
                self.arr(app, "DELETE", f"queue/{q['id']}?removeFromClient=false&blocklist=false")
                plan["kept"].append(f"{q.get('title') or 'a download'} (still downloading; remove it in qBittorrent if unwanted)")
        elif name == "arr":
            exclude = "true" if plan["exclude"] else "false"
            path = (f"movie/{plan['id']}?deleteFiles=true&addImportExclusion={exclude}" if plan["kind"] == "movie"
                    else f"series/{plan['id']}?deleteFiles=true&addImportListExclusion={exclude}")
            r = c.request(f"{local(app)}/api/v3/{path}", "DELETE", {"X-Api-Key": c.arr_key(self.config, app)}, timeout=60)
            if not (r.ok or r.status == 404):   # 404: gone already (a resumed plan)
                raise ApiError(f"{app} didn't delete it (HTTP {r.status or 'no answer'})")
        elif name == "unmonitor":
            series = self.arr("sonarr", "GET", f"series/{plan['id']}")
            series["seasons"] = [dict(x, monitored=False) if x.get("seasonNumber") == plan["season"] else x
                                 for x in series.get("seasons") or []]
            self.arr("sonarr", "PUT", f"series/{plan['id']}", series)
        elif name == "files":
            for fid in list(plan["files"]):
                r = c.request(f"{local('sonarr')}/api/v3/episodefile/{fid}", "DELETE", {"X-Api-Key": c.arr_key(self.config, "sonarr")},
                              timeout=60)
                if not (r.ok or r.status == 404):
                    raise ApiError(f"Sonarr didn't delete a file (HTTP {r.status or 'no answer'})")
                plan["files"].remove(fid)
        elif name == "torrents" and plan["hashes"]:
            r = c.request(f"{local('qbittorrent')}/api/v2/torrents/delete", "POST",
                          form={"hashes": "|".join(sorted(plan["hashes"])), "deleteFiles": "true"}, timeout=20)
            if not r.ok:
                raise ApiError("qBittorrent didn't remove its torrents")
        elif name == "seerr" and plan.get("tmdb"):
            kind = "movie" if plan["kind"] == "movie" else "tv"
            seerr = {"X-Api-Key": c.seerr_key(self.config)}
            r = c.request(f"{local('seerr')}/api/v1/{kind}/{plan['tmdb']}", headers=seerr, timeout=20)
            if not r.ok:   # a failed read isn't "nothing to clear": the step stays
                raise ApiError(f"Seerr didn't answer (HTTP {r.status or 'no answer'})")
            media = (r.json({}) or {}).get("mediaInfo") or {}
            if media.get("id"):
                r = c.request(f"{local('seerr')}/api/v1/media/{media['id']}", "DELETE", seerr, timeout=20)
                if not (r.ok or r.status == 404):
                    raise ApiError("Seerr didn't clear it")
        elif name == "refresh":
            c.request(f"{local('jellyfin')}/Library/Refresh", "POST", c.jellyfin_auth(self.state), body=b"", timeout=20)

    def pending(self, item_id: str) -> dict | None:
        return self.plans().get(item_id)

    def delete(self, item_id: str, season: int | None, exclude: bool) -> dict:
        import shutil
        plan = self.pending(item_id)
        resumed = plan is not None
        if plan is None:
            plan = self.plan(item_id, season, exclude)
            self.save_plan(item_id, plan)   # before anything is deleted
        free_before = shutil.disk_usage(self.config.parent).free
        while plan["steps"]:
            self.step(plan, plan["steps"][0])   # raises: the plan stays, retrying resumes it
            plan["steps"].pop(0)
            self.save_plan(item_id, plan)
        self.save_plan(item_id, None)
        measured = shutil.disk_usage(self.config.parent).free - free_before
        c.log(f"deleted from the dashboard: {plan['title']} (about {plan['bytes'] // 1024 ** 2} MB of files, "
              f"{len(plan['hashes'])} torrents{', kept ' + str(len(plan['kept'])) if plan['kept'] else ''})")
        return {"deleted": plan["title"], "resumed": resumed,
                # Sonarr/Radarr's sizes: an estimate (hardlinks, torrents)
                "files_removed": plan["bytes"], "disk_freed": max(measured, 0),
                "torrents": len(plan["hashes"]), "torrents_kept": plan["kept"], "warnings": plan["warnings"]}


class PlanTrouble(Exception):
    """The record of unfinished deletions can't be read"""


# Wrong passwords: at most WRONG_ALLOWED in WRONG_WINDOW, counted before
# Jellyfin is asked, one check at a time (parallel guesses wait their turn)
WRONG_ALLOWED, WRONG_WINDOW = 5, 600
_wrong: list[float] = []
_auth = threading.Lock()


def check_password(lib: "Library", password: str) -> tuple[int, dict] | None:
    """None when it's right; else (status, answer)"""
    import time
    with _auth:
        now = time.time()
        _wrong[:] = [t for t in _wrong if now - t < WRONG_WINDOW]
        if len(_wrong) >= WRONG_ALLOWED:
            wait = int((WRONG_WINDOW - (now - _wrong[0])) // 60) + 1
            return 429, {"error": f"Too many wrong passwords; try again in {wait} min"}
        if lib.password_ok(password):
            return None
        _wrong.append(now)
        time.sleep(1)
        return 403, {"error": "That's not the Jellyfin password"}


def parse_delete(body: bytes) -> tuple[str, int | None, bool, str] | str:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return "not JSON"
    if not isinstance(data, dict):
        return "the request must be a JSON object"
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
                    unfinished = lib.pending(item)
                except PlanTrouble as e:
                    return self.answer(500, {"error": str(e)})
                if unfinished:
                    return self.answer(200, {"kind": unfinished["kind"], "title": unfinished["title"], "size": unfinished["bytes"],
                                             "seasons": [], "unfinished": unfinished["steps"]})
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
            refused = check_password(lib, password)
            if refused:
                return self.answer(*refused)
            from mediaserver import lock
            from mediaserver.ui import SetupError
            with LOCK:
                # The same lock as installs, restores and the e2e test (the
                # background workers wait while it's held)
                try:
                    lock.acquire(lib.state, wait_workers=0)   # a web request doesn't wait
                except SetupError as e:
                    busy = "post-import" in e.message
                    return self.answer(409, {"error": e.message if busy else "An install or other operation is running; try again when it's done"})
                try:
                    result = lib.delete(item, season, exclude)
                except LookupError as e:
                    return self.answer(404, {"error": f"Can't delete it here: {e}"})
                except PlanTrouble as e:
                    return self.answer(500, {"error": str(e)})
                except (ApiError, *c.HTTP_ERRORS) as e:
                    return self.answer(502, {"error": f"Partly deleted ({e}). Delete it again to finish: what's left was saved"})
                finally:
                    lock.release(lib.state)
            self.answer(200, result)

        def log_message(self, format, *args):  # noqa: A002 (the base class's name)
            pass
    return Handler


def serve(config: Path, port: int) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler(Speed(config)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
