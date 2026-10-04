"""Prowlarr: the indexers from config.toml, synced to Sonarr (TV and anime)
and Radarr; Byparr as its Cloudflare proxy for indexers that need it."""
from __future__ import annotations

from typing import Any

from mediaserver import api
from mediaserver.api import ApiError
from mediaserver.arr import set_login, sync_fields
from mediaserver.config import Config, Keys
from mediaserver.ui import info, ok, warn

INTERNAL = "http://localhost:9696"
SONARR_CATEGORIES = [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5090]
RADARR_CATEGORIES = [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090]


class Prowlarr:
    def __init__(self, cfg: Config, key: str):
        self.cfg, self.url, self.key = cfg, cfg.urls.prowlarr, key

    def call(self, method: str, path: str, body: Any = None) -> Any:
        return api.call(method, f"{self.url}/api/v1/{path}", {"X-Api-Key": self.key}, body=body)


def updated_indexer(existing: dict, entry: dict, tag: int | None) -> dict:
    """An existing indexer in line with its config.toml entry: enabled or
    not, its fields, and whether it goes through Byparr"""
    fields = entry.get("fields") or {}
    want: dict[str, Any] = {**existing, "enable": entry.get("enable") is True}
    want["fields"] = [dict(f, value=fields[f["name"]]) if fields.get(f.get("name")) is not None else f
                      for f in existing.get("fields") or []]
    if tag is not None:
        tags = list(existing.get("tags") or [])
        if entry.get("flaresolverr") is True:
            want["tags"] = sorted(set(tags + [tag]))
        else:
            want["tags"] = [t for t in tags if t != tag]
    return want


def new_indexer(schema: dict, entry: dict, tag: int | None) -> dict:
    fields = entry.get("fields") or {}
    body = {k: v for k, v in schema.items() if k != "id"}
    body["fields"] = [dict(f, value=fields[f["name"]]) if fields.get(f.get("name")) else f for f in schema.get("fields") or []]
    body.update(name=entry["name"], enable=True, appProfileId=1,
                tags=[tag] if entry.get("flaresolverr") is True and tag is not None else [])
    return body


def run(cfg: Config) -> None:
    keys = Keys.read(cfg.paths.config)
    if not keys.prowlarr:
        return
    info("Configuring Prowlarr...")
    p = Prowlarr(cfg, keys.prowlarr)
    connect_apps(p, keys)
    byparr(p)
    qbittorrent(p)
    tag = flaresolverr_tag(p)
    indexers(p, tag)
    set_login(cfg, "Prowlarr", p.url, keys.prowlarr, "v1", "prowlarr")


def connect_apps(p: Prowlarr, keys: Keys) -> None:
    try:
        apps = p.call("GET", "applications") or []
    except ApiError:
        apps = None
    # One Sonarr handles TV and anime; every indexer syncs to it and Radarr
    for name, url, key, categories in (("Sonarr", "http://localhost:8989", keys.sonarr, SONARR_CATEGORIES),
                                       ("Radarr", "http://localhost:7878", keys.radarr, RADARR_CATEGORIES)):
        if not key:
            continue
        existing = next((a for a in apps or [] if a.get("name") == name), None)
        if existing:
            # Keep the URLs and API key current (e.g. after a Sonarr reinstall)
            sync_fields(f"Prowlarr {name} app", f"{p.url}/api/v1/applications", existing["id"],
                        {"prowlarrUrl": INTERNAL, "baseUrl": url, "apiKey": key}, p.key)
            ok(f"{name} connected")
        elif apps is None:
            warn(f"Couldn't read Prowlarr's apps; {name} not connected (retried next run)")
        else:
            body = {"name": name, "implementation": name, "configContract": f"{name}Settings", "syncLevel": "fullSync", "tags": [],
                    "fields": [{"name": "prowlarrUrl", "value": INTERNAL}, {"name": "baseUrl", "value": url},
                               {"name": "apiKey", "value": key}, {"name": "syncCategories", "value": categories}]}
            try:
                p.call("POST", "applications", body)
                ok(f"{name} connected")
            except ApiError:
                warn(f"Could not connect {name}")


def byparr(p: Prowlarr) -> None:
    """Byparr speaks the FlareSolverr API, so it's added as a FlareSolverr proxy"""
    try:
        proxies = p.call("GET", "indexerProxy") or []
    except ApiError:
        warn("Couldn't read Prowlarr's proxies; Byparr not checked (retried next run)")
        return
    if any("Byparr" in (x.get("name") or "") for x in proxies):
        ok("Byparr connected")
        return
    body = {"name": "Byparr", "implementation": "FlareSolverr", "configContract": "FlareSolverrSettings",
            "fields": [{"name": "host", "value": p.cfg.urls.byparr}, {"name": "requestTimeout", "value": 60}]}
    try:
        p.call("POST", "indexerProxy", body)
        ok("Byparr connected")
    except ApiError:
        warn("Could not add Byparr")


def qbittorrent(p: Prowlarr) -> None:
    cfg = p.cfg
    try:
        clients = p.call("GET", "downloadclient") or []
    except ApiError:
        return
    existing = next((x for x in clients if x.get("implementation") == "QBittorrent"), None)
    if existing:
        sync_fields("Prowlarr qBittorrent client", f"{p.url}/api/v1/downloadclient", existing["id"],
                    {"username": cfg.qbit_user, "password": cfg.qbit_pass}, p.key)
        return
    body = {"name": "qBittorrent", "implementation": "QBittorrent", "configContract": "QBittorrentSettings", "enable": True,
            "protocol": "torrent", "priority": 1,
            "fields": [{"name": "host", "value": "localhost"}, {"name": "port", "value": 8081},
                       {"name": "username", "value": cfg.qbit_user}, {"name": "password", "value": cfg.qbit_pass},
                       {"name": "category", "value": "prowlarr"}]}
    try:
        p.call("POST", "downloadclient", body)
        ok("qBittorrent connected to Prowlarr")
    except ApiError:
        pass


def flaresolverr_tag(p: Prowlarr) -> int | None:
    """The "flaresolverr" tag: indexers with it go through Byparr"""
    try:
        tag = next((t["id"] for t in p.call("GET", "tag") or [] if t.get("label") == "flaresolverr"), None)
        if tag is None:
            tag = (p.call("POST", "tag", {"label": "flaresolverr"}) or {}).get("id")
            if tag is not None:
                ok(f"Created FlareSolverr tag (id: {tag})")
        if tag is None:
            return None
        # Byparr's proxy, not whichever proxy is listed first
        proxy = next((x for x in p.call("GET", "indexerProxy") or [] if "Byparr" in (x.get("name") or "")), None)
        if proxy and tag not in (proxy.get("tags") or []):
            p.call("PUT", f"indexerProxy/{proxy['id']}", dict(proxy, tags=(proxy.get("tags") or []) + [tag]))
        return tag
    except ApiError:
        return None


def indexers(p: Prowlarr, tag: int | None) -> None:
    entries = p.cfg.get("indexers", []) or []
    if not entries:
        return
    # Unreadable is not "none": every indexer would look new and be added twice
    try:
        existing = p.call("GET", "indexer") or []
    except ApiError:
        warn("Couldn't read Prowlarr's indexers; indexers skipped (retried next run)")
        return
    info("Adding indexers from config...")
    schemas: list = []
    fetched = False
    for entry in entries:
        name, on = entry["name"], entry.get("enable") is True
        current = next((x for x in existing if x.get("name") == name), None)
        if current:
            want = updated_indexer(current, entry, tag)
            if want == current:
                if on:
                    ok(f"{name} already added")
                continue
            try:
                p.call("PUT", f"indexer/{current['id']}?forceSave=true", want)
                ok(f"{name} updated ({'enabled' if on else 'disabled'}{', through Byparr' if entry.get('flaresolverr') is True else ''})")
            except ApiError:
                warn(f"{name}: could not update")
            continue
        if not on:
            continue
        if not fetched:
            fetched = True
            try:
                schemas = p.call("GET", "indexer/schema") or []
            except ApiError:
                schemas = []
        schema = next((x for x in schemas if x.get("definitionName") == entry.get("definitionName")), None)
        if schema is None:
            warn(f"{name}: indexer '{entry.get('definitionName')}' not found in Prowlarr schemas")
            continue
        try:
            p.call("POST", "indexer", new_indexer(schema, entry, tag))
            ok(f"{name} added")
        except ApiError:
            warn(f"Could not add {name}")
