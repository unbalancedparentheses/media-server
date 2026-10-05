"""Fake services for the tests: small HTTP servers that keep their state in
memory and answer the API calls setup makes, like the real ones (Sonarr,
Radarr, Prowlarr, Jellyfin, Seerr, qBittorrent, SABnzbd, Bazarr,
Cleanuparr). Every write is recorded, so a test can check that a second
setup run changes nothing.

    with FakeStack(root) as stack:     # servers up, MEDIASERVER_URL_* set,
        ...run steps...                # key files written under root/config
        stack.sonarr.writes            # [(method, path), ...]
"""
from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


class Request:
    def __init__(self, method: str, path: str, query: dict, headers: dict, body: bytes):
        self.method, self.path, self.query, self.headers, self.raw = method, path, query, headers, body

    def json(self) -> Any:
        return json.loads(self.raw) if self.raw else None

    def form(self) -> dict:
        return {k: v[-1] for k, v in parse_qs(self.raw.decode(), keep_blank_values=True).items()}

    def form_all(self) -> dict:
        return parse_qs(self.raw.decode())

    def arg(self, name: str, default: str = "") -> str:
        return (self.query.get(name) or [default])[-1]


Response = tuple  # (status, body (json-able, bytes or None), headers dict)


class FakeService:
    """Routes: (method, regex) → handler(request, *groups) returning a body
    (status 200), or (status, body[, headers])"""

    def __init__(self, name: str):
        self.name = name
        self.routes: list[tuple[str, re.Pattern, Callable]] = []
        self.writes: list[tuple[str, str]] = []
        self.requests: list[tuple[str, str]] = []
        self.down = False

    def route(self, method: str, pattern: str):
        def register(fn):
            self.routes.append((method, re.compile(pattern + r"/?$"), fn))
            return fn
        return register

    def dispatch(self, req: Request) -> Response:
        self.requests.append((req.method, req.path))
        if req.method != "GET":
            self.writes.append((req.method, req.path))
        for method, pattern, fn in self.routes:
            m = pattern.match(req.path)
            if m and method == req.method:
                result = fn(req, *m.groups())
                if isinstance(result, tuple):
                    return result if len(result) == 3 else (result[0], result[1], {})
                return 200, result, {}
        return 404, {"error": f"{self.name}: no route for {req.method} {req.path}"}, {}

    def start(self) -> None:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self):
                url = urlparse(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if service.down:
                    self.send_response(503)
                    self.end_headers()
                    return
                status, payload, headers = service.dispatch(Request(self.command, url.path, parse_qs(url.query),
                                                                    dict(self.headers), body))
                data = payload if isinstance(payload, bytes) else b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if data and "Content-Type" not in headers:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any

            def log_message(self, format, *args):  # noqa: A002 (the base class's name)
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        # A short poll interval: stopping waits for the next poll
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Collection:
    """A list of JSON objects with ids, like an *arr resource"""

    def __init__(self, items: list | None = None):
        self.items: list[dict] = []
        self.next_id = 1
        for item in items or []:
            self.add(item)

    def add(self, item: dict) -> dict:
        item = dict(item, id=item.get("id") or self.next_id)
        self.next_id = max(self.next_id, item["id"]) + 1
        self.items.append(item)
        return item

    def get(self, item_id) -> dict | None:
        return next((x for x in self.items if str(x["id"]) == str(item_id)), None)

    def put(self, item_id, item: dict) -> dict | None:
        for i, x in enumerate(self.items):
            if str(x["id"]) == str(item_id):
                self.items[i] = dict(item, id=x["id"])
                return self.items[i]
        return None

    def delete(self, item_id) -> bool:
        before = len(self.items)
        self.items = [x for x in self.items if str(x["id"]) != str(item_id)]
        return len(self.items) < before

    def rest(self, service: FakeService, base: str) -> None:
        """GET/POST base, GET/PUT/DELETE base/<id>"""
        @service.route("GET", base)
        def listing(req):
            return copy.deepcopy(self.items)

        @service.route("POST", base)
        def create(req):
            return 201, self.add(req.json() or {})

        @service.route("GET", base + r"/(\d+)")
        def one(req, item_id):
            item = self.get(item_id)
            return (200, copy.deepcopy(item)) if item else (404, None)

        @service.route("PUT", base + r"/(\d+)")
        def update(req, item_id):
            item = self.put(item_id, req.json() or {})
            return (202, item) if item else (404, None)

        @service.route("DELETE", base + r"/(\d+)")
        def remove(req, item_id):
            return (200, None) if self.delete(item_id) else (404, None)


def form_login(service: FakeService, check: Callable[[str, str], bool], path: str = "/login"):
    """*arr form login: a redirect to the app on success, back to the login page on failure"""
    @service.route("POST", path)
    def login(req):
        f = req.form()
        location = "/" if check(f.get("username", ""), f.get("password", "")) else "/login?loginFailed=true"
        return 302, None, {"Location": location}


# ─── The *arr apps ───────────────────────────────────────────────

BUILTIN_PROFILES = ["Any", "SD", "HD-720p", "HD-1080p", "Ultra-HD", "HD - 720p/1080p"]


class FakeArr(FakeService):
    """Sonarr / Radarr (v3) or Prowlarr (v1)"""

    def __init__(self, name: str, key: str):
        super().__init__(name)
        self.key = key
        self.version = "v1" if name == "Prowlarr" else "v3"
        v = f"/api/{self.version}"
        self.host = {"id": 1, "username": "", "password": "", "authenticationMethod": "none", "authenticationRequired": "disabledForLocalAddresses"}
        if name != "Sonarr":   # newer versions (Radarr, Prowlarr here) have it
            self.host["allowedHosts"] = ""
        self.naming = {"id": 1, "renameEpisodes": False, "renameMovies": False}
        self.mediamanagement = {"id": 1, "minimumFreeSpaceWhenImporting": 100}
        self.commands: list[dict] = []
        self.resources = {name: Collection() for name in ("rootfolder", "downloadclient", "notification", "customformat",
                                                          "series", "movie", "tag", "applications", "indexerProxy", "indexer")}
        self.resources["qualityprofile"] = Collection([
            {"name": n, "minFormatScore": 0, "cutoff": 1, "upgradeAllowed": False, "formatItems": [],
             "items": [{"quality": {"id": 0, "name": "Unknown"}, "allowed": False}, {"quality": {"id": 1, "name": "SDTV"}, "allowed": True}]}
            for n in BUILTIN_PROFILES])
        self.schemas: list[dict] = []
        for res, coll in self.resources.items():
            coll.rest(self, f"{v}/{res}")
        for single in ("host", "naming", "mediamanagement"):
            self.singleton(f"{v}/config/{single}", single)

        self.health: list = []
        self.queue: list = []
        self.history: list = []
        self.episodes: list = []
        self.statuses: list = []
        self.deleted_files: list = []
        self.failed: list = []

        @self.route("GET", f"{v}/health")
        def health(req):
            return copy.deepcopy(self.health)

        @self.route("GET", "/ping")
        def ping(req):
            return {"status": "OK"}

        @self.route("GET", f"{v}/queue/status")
        def queue_status(req):
            return {"totalCount": len(self.queue)}

        @self.route("GET", f"{v}/queue")
        def queue(req):
            return {"records": copy.deepcopy(self.queue)}

        @self.route("GET", f"{v}/wanted/missing")
        def missing(req):
            items = [x for x in self.resources["movie"].items if not x.get("hasFile")] if name == "Radarr" else \
                [e for e in self.episodes if not e.get("hasFile")]
            return {"totalRecords": len(items), "records": items}

        @self.route("GET", f"{v}/history")
        def history(req):
            rows = sorted(self.history, key=lambda r: r["id"], reverse=True)
            if req.arg("eventType"):
                rows = [r for r in rows if str(r.get("eventTypeId")) == req.arg("eventType")]
            if req.arg("downloadId"):
                rows = [r for r in rows if r.get("downloadId") == req.arg("downloadId")]
            size, page = int(req.arg("pageSize", "50")), int(req.arg("page", "1"))
            return {"records": copy.deepcopy(rows[(page - 1) * size:page * size]), "totalRecords": len(rows)}

        @self.route("POST", fr"{v}/history/failed/(\d+)")
        def mark_failed(req, hid):
            self.failed.append(int(hid))
            return 200, None

        @self.route("DELETE", fr"{v}/(episodefile|moviefile)/(\d+)")
        def delete_file(req, kind, fid):
            self.deleted_files.append((kind, int(fid)))
            return 200, None

        @self.route("GET", f"{v}/indexerstatus")
        def statuses(req):
            return copy.deepcopy(self.statuses)

        @self.route("GET", f"{v}/indexerstats")
        def stats(req):
            return {"indexers": [{"numberOfQueries": 10, "numberOfGrabs": 2, "numberOfFailedQueries": 1}]}

        @self.route("GET", f"{v}/calendar")
        def calendar(req):
            return [x for x in (self.resources["movie"].items if name == "Radarr" else self.episodes) if x.get("calendar")]

        @self.route("GET", fr"{v}/episode/(\d+)")
        def episode(req, eid):
            return next((e for e in self.episodes if str(e["id"]) == eid), (404, None))

        self.tested = 0

        @self.route("POST", f"{v}/indexer/testall")
        def testall(req):
            self.tested += 1
            return 200, []

        @self.route("GET", f"{v}/search")
        def search(req):
            return [{"title": "Big Buck Bunny"}]

        @self.route("POST", f"{v}/command")
        def command(req):
            self.commands.append(req.json() or {})
            return 201, {"id": len(self.commands), "status": "queued"}

        @self.route("GET", f"{v}/command")
        def commands(req):
            return []

        @self.route("GET", f"{v}/indexer/schema")
        def schema(req):
            return copy.deepcopy(self.schemas)

        @self.route("GET", f"{v}/episode")
        def episodes(req):
            sid = req.arg("seriesId")
            return [e for e in self.episodes if not sid or str(e.get("seriesId")) == sid]

        form_login(self, lambda u, p: self.host["authenticationMethod"] == "forms" and u == self.host["username"] and p == self.host["password"])

    def singleton(self, path: str, attr: str) -> None:
        @self.route("GET", path)
        def read(req):
            return copy.deepcopy(getattr(self, attr))

        @self.route("PUT", path + r"/(\d+)")
        def write_id(req, _id):
            new = req.json() or {}
            # Newer *arr versions: no login without the host names it answers to
            if attr == "host" and "allowedHosts" in new and new.get("authenticationRequired") != "enabled" and not new["allowedHosts"]:
                return 400, [{"propertyName": "AllowedHosts", "errorMessage": "Allowed Hosts is required"}]
            setattr(self, attr, dict(new, id=1))
            return 202, getattr(self, attr)

        @self.route("PUT", path)
        def write(req):
            setattr(self, attr, dict(req.json() or {}, id=1))
            return 202, getattr(self, attr)


# ─── Jellyfin ────────────────────────────────────────────────────

class FakeJellyfin(FakeService):
    def __init__(self, plugins: list | None = None):
        super().__init__("Jellyfin")
        self.wizard_done = False
        self.users: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}
        self.folders: list[dict] = []
        self.keys: list[dict] = []
        self.system = {"LibraryMonitorDelay": 60}
        self.encoding = {"HardwareAccelerationType": "none"}
        self.plugins = plugins or []
        self.repositories: list[dict] = []
        self.refreshes = 0

        def me(req) -> dict | None:
            auth = req.headers.get("Authorization", "")
            m = re.search(r'Token="([^"]+)"', auth)
            return self.users.get(self.tokens.get(m.group(1), "")) if m else None

        @self.route("GET", "/health")
        def health(req):
            return 200, b"Healthy", {"Content-Type": "text/plain"}

        @self.route("GET", "/Startup/Configuration")
        def startup(req):
            return (404, None) if self.wizard_done else {"UICulture": "en-US"}

        @self.route("POST", "/Startup/Configuration")
        def startup_conf(req):
            return 204, None

        @self.route("GET", "/Startup/User")
        def first_user(req):
            return {"Name": "root"}

        @self.route("POST", "/Startup/User")
        def set_user(req):
            body = req.json()
            self.add_user(body["Name"], body["Password"])
            return 204, None

        @self.route("POST", "/Startup/Complete")
        def complete(req):
            self.wizard_done = True
            return 204, None

        @self.route("POST", "/Users/AuthenticateByName")
        def login(req):
            body = req.json() or {}
            user = self.users.get(body.get("Username") or "")
            if not user or user["password"] != body.get("Pw"):
                return 401, None
            token = f"tok-{len(self.tokens) + 1}"
            self.tokens[token] = user["Name"]
            return {"AccessToken": token, "User": {"Id": user["Id"]}}

        @self.route("GET", "/Users/Me")
        def users_me(req):
            user = me(req)
            return (200, self.public(user)) if user else (401, None)

        @self.route("GET", "/Users")
        def users(req):
            return [self.public(u) for u in self.users.values()]

        @self.route("GET", r"/Users/(\w+)")
        def user(req, uid):
            u = self.by_id(uid)
            return (200, self.public(u)) if u else (404, None)

        @self.route("POST", r"/Users/(\w+)/Password")
        def password(req, uid):
            u, body = self.by_id(uid), req.json()
            if not u or body.get("CurrentPw") != u["password"]:
                return 403, None
            u["password"] = body["NewPw"]
            return 204, None

        @self.route("POST", r"/Users/(\w+)/Policy")
        def policy(req, uid):
            self.by_id(uid)["Policy"] = req.json()
            return 204, None

        @self.route("POST", "/Users/Configuration")
        def configuration(req):
            self.by_id(req.arg("userId"))["Configuration"] = req.json()
            return 204, None

        @self.route("GET", "/Library/VirtualFolders")
        def folders(req):
            return copy.deepcopy(self.folders)

        @self.route("POST", "/Library/VirtualFolders")
        def add_folder(req):
            self.folders.append({"Name": req.arg("name"), "CollectionType": req.arg("collectionType"), "Locations": [],
                                 "ItemId": f"lib{len(self.folders) + 1}", "LibraryOptions": {"EnableRealtimeMonitor": False}})
            return 204, None

        @self.route("POST", "/Library/VirtualFolders/Paths")
        def add_path(req):
            body = req.json()
            next(f for f in self.folders if f["Name"] == body["Name"])["Locations"].append(body["PathInfo"]["Path"])
            return 204, None

        @self.route("POST", "/Library/VirtualFolders/LibraryOptions")
        def options(req):
            body = req.json()
            next(f for f in self.folders if f["ItemId"] == body["Id"])["LibraryOptions"] = body["LibraryOptions"]
            return 204, None

        @self.route("POST", "/Library/Refresh")
        def refresh(req):
            self.refreshes += 1
            return 204, None

        @self.route("GET", "/Auth/Keys")
        def keys(req):
            return {"Items": copy.deepcopy(self.keys)}

        @self.route("POST", "/Auth/Keys")
        def new_key(req):
            self.keys.append({"AppName": req.arg("app"), "AccessToken": f"key-{len(self.keys) + 1}"})
            return 204, None

        for path, attr in (("/System/Configuration", "system"), ("/System/Configuration/encoding", "encoding")):
            self._config(path, attr)

        @self.route("GET", "/Plugins")
        def plugin_list(req):
            return copy.deepcopy(self.plugins)

        @self.route("GET", r"/Plugins/([\w-]+)/Configuration")
        def plugin_conf(req, pid):
            return next((p.get("config", {}) for p in self.plugins if p["Id"] == pid), {})

        @self.route("POST", r"/Plugins/([\w-]+)/Configuration")
        def set_plugin_conf(req, pid):
            next(p for p in self.plugins if p["Id"] == pid)["config"] = req.json()
            return 204, None

        @self.route("GET", "/Repositories")
        def repos(req):
            return copy.deepcopy(self.repositories)

        @self.route("POST", "/Repositories")
        def set_repos(req):
            self.repositories = req.json()
            return 204, None

        self.install_dir: Path | None = None   # where an installed plugin's folder appears
        self.pending_plugins: list = []      # installed, loaded after a restart

        @self.route("POST", r"/Packages/Installed/([^/]+)")
        def install(req, package):
            from urllib.parse import unquote
            name = unquote(package)
            self.pending_plugins.append({"Id": req.arg("assemblyGuid").replace("-", ""), "Name": name, "Version": req.arg("version"), "Status": "Active"})
            if self.install_dir:
                (self.install_dir / f"{name}_{req.arg('version')}").mkdir(parents=True, exist_ok=True)
            return 204, None

        @self.route("GET", "/ScheduledTasks")
        def tasks(req):
            return [{"Id": "t1", "Name": "Moonfin Startup", "Key": "MoonfinStartup"}]

        @self.route("POST", r"/ScheduledTasks/Running/(\w+)")
        def run_task(req, tid):
            return 204, None

        @self.route("GET", "/Moonfin/Web")
        def moonfin(req):
            return 200, b"<html></html>", {"Content-Type": "text/html"}

        @self.route("GET", "/Moonfin/Web/flutter_bootstrap.js")
        def bootstrap(req):
            return 200, b'_flutter.loader.load({\n  config: { canvasKitBaseUrl: "canvaskit/" },', {"Content-Type": "text/javascript"}

        self.items: list = []
        self.sessions: list = []

        @self.route("GET", "/Items")
        def items(req):
            rows = self.items
            if req.arg("ParentId"):
                rows = [i for i in rows if i.get("ParentId") == req.arg("ParentId")]
            if req.arg("IncludeItemTypes"):
                kinds = req.arg("IncludeItemTypes").split(",")
                rows = [i for i in rows if i.get("Type") in kinds]
            return {"Items": copy.deepcopy(rows), "TotalRecordCount": len(rows)}

        self.playback: dict = {}     # item id → PlaybackInfo answer
        self.streams: dict = {}      # item id → HTTP status of its stream

        @self.route("POST", r"/Items/(\w+)/PlaybackInfo")
        def playback_info(req, item):
            if item not in self.playback:
                return 404, {"error": "no such item"}
            return copy.deepcopy(self.playback[item])

        @self.route("GET", r"/Videos/(\w+)/stream")
        def stream(req, item):
            status = self.streams.get(item, 206)
            return status, (b"\x1aE\xdf\xa3" * 1024 if status in (200, 206) else None), {"Content-Type": "video/x-matroska"}

        @self.route("GET", "/Items/Counts")
        def counts(req):
            return {"MovieCount": sum(i["Type"] == "Movie" for i in self.items), "SeriesCount": 1, "EpisodeCount": sum(i["Type"] == "Episode" for i in self.items)}

        @self.route("GET", "/Sessions")
        def sessions(req):
            return copy.deepcopy(self.sessions)

        @self.route("GET", "/UserItems/Resume")
        def resume(req):
            return {"Items": [i for i in self.items if (i.get("UserData") or {}).get("PlaybackPositionTicks")]}

        @self.route("GET", "/Shows/NextUp")
        def next_up(req):
            return {"Items": [i for i in self.items if i.get("next_up")]}

    def _config(self, path: str, attr: str) -> None:
        @self.route("GET", path)
        def read(req):
            return copy.deepcopy(getattr(self, attr))

        @self.route("POST", path)
        def write(req):
            setattr(self, attr, req.json())
            return 204, None

    def restart(self) -> None:
        """What a restart does: plugins installed since are loaded"""
        self.plugins += self.pending_plugins
        self.pending_plugins = []

    def add_user(self, name: str, password: str) -> dict:
        user = {"Name": name, "Id": f"u{len(self.users) + 1}", "password": password,
                "Configuration": {"SubtitleMode": "Default", "PlayDefaultAudioTrack": True},
                "Policy": {"EnablePlaybackRemuxing": True, "EnableContentDownloading": True}}
        self.users[name] = user
        return user

    def by_id(self, uid: str) -> dict:
        return next((u for u in self.users.values() if u["Id"] == uid), {})

    @staticmethod
    def public(user: dict) -> dict:
        return {k: copy.deepcopy(v) for k, v in user.items() if k != "password"}


# ─── Seerr ───────────────────────────────────────────────────────

class FakeSeerr(FakeService):
    def __init__(self, jellyfin: FakeJellyfin):
        super().__init__("Seerr")
        self.jellyfin_settings = {"ip": "", "libraries": []}
        self.main = {"apiKey": "seerr-key", "defaultPermissions": 32}
        self.users = [{"id": 1, "permissions": 2, "displayName": "admin"}, {"id": 2, "permissions": 32, "displayName": "kid"}]
        self.sonarr, self.radarr = Collection(), Collection()
        self.initialized = False
        self.sonarr.rest(self, "/api/v1/settings/sonarr")
        self.radarr.rest(self, "/api/v1/settings/radarr")

        @self.route("POST", "/api/v1/auth/jellyfin")
        def sign_in(req):
            body = req.json() or {}
            user = jellyfin.users.get(body.get("username") or "")
            if not user or user["password"] != body.get("password"):
                return 401, None
            if body.get("hostname"):
                self.jellyfin_settings["ip"] = body["hostname"]
            return 200, {"id": 1}, {"Set-Cookie": "connect.sid=s3ss10n; Path=/; HttpOnly"}

        @self.route("GET", "/api/v1/settings/jellyfin")
        def jf(req):
            return copy.deepcopy(self.jellyfin_settings)

        @self.route("POST", "/api/v1/settings/jellyfin")
        def set_jf(req):
            self.jellyfin_settings.update(req.json())
            return self.jellyfin_settings

        @self.route("GET", "/api/v1/settings/jellyfin/library")
        def libraries(req):
            if req.arg("sync"):
                self.jellyfin_settings["libraries"] = [{"id": f["ItemId"], "name": f["Name"], "enabled": False} for f in jellyfin.folders]
            if req.arg("enable"):
                ids = req.arg("enable").split(",")
                for lib in self.jellyfin_settings["libraries"]:
                    lib["enabled"] = lib["id"] in ids
            return copy.deepcopy(self.jellyfin_settings["libraries"])

        @self.route("GET", "/api/v1/settings/main")
        def main(req):
            return copy.deepcopy(self.main)

        @self.route("POST", "/api/v1/settings/main")
        def set_main(req):
            body = req.json()
            if "apiKey" in body:
                return 400, {"message": "apiKey is read-only"}
            self.main.update(body)
            return self.main

        @self.route("GET", "/api/v1/settings/public")
        def public(req):
            return {"initialized": self.initialized}

        @self.route("POST", "/api/v1/settings/initialize")
        def initialize(req):
            self.initialized = True
            return {"initialized": True}

        self.requests_list: list = []
        self.details: dict = {}

        @self.route("GET", "/api/v1/status")
        def status(req):
            return {"version": "3"}

        @self.route("GET", "/api/v1/request")
        def requests(req):
            return {"results": copy.deepcopy(self.requests_list), "pageInfo": {"results": len(self.requests_list)}}

        @self.route("GET", "/api/v1/request/count")
        def request_count(req):
            return {"total": len(self.requests_list), "pending": 0, "processing": 1, "available": 1}

        self.discover: dict = {"movies": [], "tv": [], "trending": []}
        self.ratings: dict = {}

        @self.route("GET", r"/api/v1/discover/(movies|tv|trending)")
        def discover(req, kind):
            return {"page": 1, "results": copy.deepcopy(self.discover[kind])}

        self.recommendations: dict = {}

        @self.route("GET", r"/api/v1/(movie|tv)/(\d+)/recommendations")
        def recommendations(req, kind, tmdb):
            return {"page": 1, "results": copy.deepcopy(self.recommendations.get((kind, int(tmdb)), []))}

        @self.route("GET", r"/api/v1/(movie|tv)/(\d+)/(ratingscombined|ratings)")
        def rating(req, kind, tmdb, _):
            return self.ratings.get((kind, int(tmdb)), {})

        @self.route("GET", r"/api/v1/(movie|tv)/(\d+)")
        def details(req, kind, tmdb):
            return self.details.get((kind, int(tmdb)), {"title": f"TMDB {tmdb}"})

        @self.route("GET", "/api/v1/user")
        def users(req):
            return {"results": copy.deepcopy(self.users)}

        @self.route("POST", r"/api/v1/user/(\d+)/settings/permissions")
        def permissions(req, uid):
            next(u for u in self.users if str(u["id"]) == uid)["permissions"] = req.json()["permissions"]
            return {}


# ─── Download clients ────────────────────────────────────────────

class FakeQbittorrent(FakeService):
    def __init__(self, user: str, password: str, config_dir: Path | None = None):
        super().__init__("qBittorrent")
        self.login = (user, password)
        self.config_dir = config_dir
        self.prefs: dict = {"web_ui_address": "*"}
        self.categories: dict = {}

        @self.route("POST", "/api/v2/auth/login")
        def login(req):
            f = req.form()
            if (f.get("username"), f.get("password")) != self.login:
                return 200, b"Fails.", {"Content-Type": "text/plain"}
            return 200, b"Ok.", {"Set-Cookie": "SID=abc; HttpOnly; path=/", "Content-Type": "text/plain"}

        @self.route("GET", "/api/v2/app/preferences")
        def prefs(req):
            return copy.deepcopy(self.prefs)

        @self.route("POST", "/api/v2/app/setPreferences")
        def set_prefs(req):
            new = json.loads(req.form()["json"])
            if "web_ui_password" in new:
                if len(new["web_ui_password"]) < 6:
                    return 400, None
                self.login = (self.login[0], new.pop("web_ui_password"))
                if self.config_dir:  # like qBittorrent: the hash goes into qBittorrent.ini
                    from mediaserver.steps.downloads import ini_set_login
                    ini_set_login(self.config_dir, *self.login)
            self.prefs.update(new)
            return 200, None

        @self.route("GET", "/api/v2/torrents/categories")
        def cats(req):
            return copy.deepcopy(self.categories)

        @self.route("POST", "/api/v2/torrents/(createCategory|editCategory)")
        def set_cat(req, _what):
            f = req.form()
            self.categories[f["category"]] = {"name": f["category"], "savePath": f["savePath"]}
            return 200, None

        self.torrents: list = []

        @self.route("GET", "/")
        def index(req):
            return 200, b"<html>qBittorrent</html>", {"Content-Type": "text/html"}

        @self.route("GET", "/api/v2/torrents/info")
        def info(req):
            return copy.deepcopy(self.torrents)

        @self.route("GET", "/api/v2/transfer/info")
        def transfer(req):
            return {"dl_info_speed": 0, "up_info_speed": 0}


class FakeSabnzbd(FakeService):
    def __init__(self, key: str):
        super().__init__("SABnzbd")
        self.key = key
        self.misc: dict = {}
        self.servers: dict[str, dict] = {}
        self.categories: list[str] = ["*"]

        @self.route("POST", "/api")
        def api(req):
            f = req.form()
            if f.get("apikey") != self.key:
                return 403, {"error": "API Key Incorrect"}
            mode, section = f.get("mode"), f.get("section")
            if mode == "get_cats":
                return {"categories": list(self.categories)}
            if mode == "get_config" and section == "servers":
                return {"config": {"servers": [dict(s, name=n) for n, s in self.servers.items()]}}
            if mode == "set_config" and section == "misc":
                self.misc[f["keyword"]] = f["value"]
                return {"config": {"misc": self.misc}}
            if mode == "set_config" and section == "categories":
                self.categories.append(f["keyword"])
                return {"config": {"categories": self.categories}}
            if mode == "set_config" and section == "servers":
                server = self.servers.setdefault(f["keyword"], {"enable": 1})
                for k, v in f.items():
                    if k not in ("mode", "section", "keyword", "apikey", "output"):
                        server[k] = int(v) if k == "enable" else v
                return {"config": {"servers": [dict(s, name=n) for n, s in self.servers.items()]}}
            return {"status": False, "error": "not implemented"}

        @self.route("GET", "/api")
        def queue(req):
            return {"queue": {"slots": [], "noofslots": 0, "kbpersec": "0"}}

        @self.route("GET", "/")
        def index(req):
            # Its login page while it has a login, else the app
            return (303, None, {"Location": "/login/"}) if self.misc.get("username") else (200, b"sabnzbd", {"Content-Type": "text/html"})

        @self.route("POST", "/login")
        def login(req):
            f = req.form()
            ok = (f.get("username"), f.get("password")) == (self.misc.get("username"), self.misc.get("password"))
            return (303, None, {"Location": "/"}) if ok else (200, b"login", {"Content-Type": "text/html"})


# ─── Bazarr and Cleanuparr ───────────────────────────────────────

class FakeBazarr(FakeService):
    def __init__(self, config_file: Path):
        super().__init__("Bazarr")
        self.config_file = config_file
        self.profiles: list = []
        self.settings_posts: list = []

        def auth() -> dict:
            import yaml
            return (yaml.safe_load(self.config_file.read_text()) or {}).get("auth") or {}

        @self.route("GET", "/api/system/languages/profiles")
        def profiles(req):
            return copy.deepcopy(self.profiles)

        @self.route("POST", "/api/system/settings")
        def settings(req):
            form = req.form_all()
            self.settings_posts.append(form)
            if "languages-profiles" in form:
                self.profiles = json.loads(form["languages-profiles"][-1])
            return 204, None

        @self.route("GET", "/api/system/settings")
        def get_settings(req):
            return {"general": {"enabled_providers": ["opensubtitlescom"]}}

        @self.route("POST", "/api/system/tasks")
        def tasks(req):
            return 204, None

        self.wanted_movies: list = []
        self.wanted_episodes: list = []

        @self.route("GET", "/api/movies/wanted")
        def wanted_movies(req):
            return {"data": copy.deepcopy(self.wanted_movies)}

        @self.route("GET", "/api/episodes/wanted")
        def wanted_episodes(req):
            return {"data": copy.deepcopy(self.wanted_episodes)}

        @self.route("GET", "/api/badges")
        def badges(req):
            return {"episodes": len(self.wanted_episodes), "movies": len(self.wanted_movies)}

        @self.route("POST", "/api/providers")
        def providers(req):
            return 204, None

        @self.route("POST", "/api/system/account")
        def login(req):
            import hashlib
            f, a = req.form(), auth()
            ok = a.get("type") in ("form", "basic") and f.get("username") == a.get("username") \
                and hashlib.md5(f.get("password", "").encode()).hexdigest() == a.get("password")
            return (204, None) if ok else (401, None)

        @self.route("GET", "/")
        def index(req):
            return 200, b"<html>Bazarr</html>", {"Content-Type": "text/html"}


class FakeCleanuparr(FakeService):
    def __init__(self, users_db: Path, password_check: Callable[[str, str], bool] | None = None):
        super().__init__("Cleanuparr")
        self.setup_completed = False
        self.instances = {"sonarr": Collection(), "radarr": Collection()}
        self.clients = Collection()
        self.queue_cleaner = {"enabled": False, "downloadingMetadataMaxStrikes": 0,
                              "failedImport": {"maxStrikes": 0, "patterns": [], "ignorePrivate": False, "patternMode": "Include"}}
        self.stall = Collection()
        self.general = {"auth": {"disableAuthForLocalAddresses": True}}
        self.login_ok = password_check or (lambda u, p: True)
        self.users_db = users_db
        for app, coll in self.instances.items():
            @self.route("GET", f"/api/configuration/{app}")
            def listing(req, coll=coll):
                return {"instances": copy.deepcopy(coll.items)}

            @self.route("POST", f"/api/configuration/{app}/instances")
            def add(req, coll=coll):
                return 201, coll.add(req.json())

            @self.route("PUT", f"/api/configuration/{app}/instances/(\\d+)")
            def put(req, iid, coll=coll):
                return coll.put(iid, req.json())

        @self.route("GET", "/health")
        def health(req):
            return 200, b"ok", {"Content-Type": "text/plain"}

        @self.route("GET", "/api/auth/status")
        def status(req):
            return {"setupCompleted": self.setup_completed, "authBypassActive": False}

        @self.route("POST", "/api/auth/setup/account")
        def account(req):
            users_db.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(users_db)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS users (api_key TEXT, username TEXT, password_hash TEXT, failed_login_attempts INT, lockout_end TEXT, updated_at TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS refresh_tokens (token TEXT)")
                db.execute("INSERT INTO users (api_key, username) VALUES ('cu-key', ?)", ((req.json() or {}).get("username"),))
            return 201, None

        @self.route("POST", "/api/auth/setup/complete")
        def complete(req):
            self.setup_completed = True
            return 200, None

        @self.route("POST", "/api/auth/login")
        def login(req):
            body = req.json() or {}
            return {"tokens": {"accessToken": "t"}} if self.login_ok(body.get("username") or "", body.get("password") or "") else (401, None)

        @self.route("GET", "/api/configuration/download_client")
        def clients(req):
            return {"clients": copy.deepcopy(self.clients.items)}

        @self.route("POST", "/api/configuration/download_client")
        def add_client(req):
            return 201, self.clients.add(req.json())

        @self.route("PUT", r"/api/configuration/download_client/(\d+)")
        def put_client(req, cid):
            return self.clients.put(cid, req.json())

        @self.route("GET", "/api/configuration/queue_cleaner")
        def qc(req):
            return copy.deepcopy(self.queue_cleaner)

        @self.route("PUT", "/api/configuration/queue_cleaner")
        def set_qc(req):
            self.queue_cleaner = req.json()
            return 200, None

        @self.route("GET", "/api/configuration/general")
        def general(req):
            return copy.deepcopy(self.general)

        @self.route("PUT", "/api/configuration/general")
        def set_general(req):
            self.general = req.json()
            return 200, None

        self.stall.rest(self, "/api/queue-rules/stall")


# ─── Everything together ─────────────────────────────────────────

class FakeStack:
    """Every service setup configures, with the files setup reads keys
    from, under <root> (a scratch ~/media). Sets MEDIASERVER_URL_* while open."""

    KEYS = {"sonarr": "sonarr-key", "radarr": "radarr-key", "prowlarr": "prowlarr-key"}

    def __init__(self, root: Path, qbit=("admin", "qbit-pass"), plugins: list | None = None):
        self.root = root
        config = root / "config"
        for app, key in self.KEYS.items():
            (config / app).mkdir(parents=True, exist_ok=True)
            (config / app / "config.xml").write_text(f"<Config><ApiKey>{key}</ApiKey></Config>")
        (config / "sabnzbd").mkdir(parents=True, exist_ok=True)
        (config / "sabnzbd/sabnzbd.ini").write_text("api_key = sab-key\n")
        (config / "seerr").mkdir(parents=True, exist_ok=True)
        (config / "seerr/settings.json").write_text(json.dumps({"main": {"apiKey": "seerr-key"}}))
        bazarr_yaml = config / "bazarr/config/config.yaml"
        bazarr_yaml.parent.mkdir(parents=True, exist_ok=True)
        bazarr_yaml.write_text("general:\n  theme: dark\nauth:\n  apikey: bazarr-key\n  type: null\n")
        self.sonarr = FakeArr("Sonarr", self.KEYS["sonarr"])
        self.radarr = FakeArr("Radarr", self.KEYS["radarr"])
        self.prowlarr = FakeArr("Prowlarr", self.KEYS["prowlarr"])
        self.jellyfin = FakeJellyfin(plugins)
        self.seerr = FakeSeerr(self.jellyfin)
        self.qbittorrent = FakeQbittorrent(*qbit, config_dir=config)
        self.sabnzbd = FakeSabnzbd("sab-key")
        self.bazarr = FakeBazarr(bazarr_yaml)
        self.cleanuparr = FakeCleanuparr(config / "cleanuparr/users.db")
        self.services = {"sonarr": self.sonarr, "radarr": self.radarr, "prowlarr": self.prowlarr, "jellyfin": self.jellyfin,
                         "seerr": self.seerr, "qbittorrent": self.qbittorrent, "sabnzbd": self.sabnzbd, "bazarr": self.bazarr,
                         "cleanuparr": self.cleanuparr}
        self.saved_env: dict = {}

    def __enter__(self) -> "FakeStack":
        for name, service in self.services.items():
            service.start()
            var = f"MEDIASERVER_URL_{name.upper()}"
            self.saved_env[var] = os.environ.get(var)
            os.environ[var] = service.url
        return self

    def __exit__(self, *exc) -> None:
        for service in self.services.values():
            service.stop()
        for var, value in self.saved_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value

    def writes(self) -> dict:
        return {name: list(s.writes) for name, s in self.services.items() if s.writes}

    def clear(self) -> None:
        for s in self.services.values():
            s.writes.clear()
            s.requests.clear()
