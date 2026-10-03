"""Seerr: requests. Signed in with the Jellyfin admin, connected to Sonarr
(TV, and anime into the anime folder with the anime profile) and Radarr
(movies), with search on and config.toml's quality profiles."""
from __future__ import annotations

import re
import time
from typing import Any

from mediaserver import api
from mediaserver import common as c
from mediaserver.api import ApiError
from mediaserver.config import Config, Keys
from mediaserver.pins import ANIME_PROFILE
from mediaserver.ui import info, ok, warn

# Seerr permission bits
ADMIN, AUTO_APPROVE = 2, 128


class Seerr:
    def __init__(self, cfg: Config):
        self.cfg, self.url = cfg, cfg.urls.seerr
        self.cookie = ""

    def call(self, method: str, path: str, body: Any = None) -> Any:
        return api.call(method, f"{self.url}/api/v1/{path}", {"Cookie": f"connect.sid={self.cookie}"}, body=body)

    def sign_in(self) -> bool:
        """Sign in with the Jellyfin admin. The very first sign-in also tells
        Seerr where Jellyfin is (serverType 2 = Jellyfin) and makes that user
        Seerr's admin; after that Seerr rejects a hostname, so try without
        one first."""
        body = {"username": self.cfg.jellyfin_user, "password": self.cfg.jellyfin_pass, "email": "admin@media.local"}
        for attempt in (body, dict(body, hostname="localhost", port=8096, useSsl=False, urlBase="", serverType=2)):
            for _ in range(3):
                r = c.request(f"{self.url}/api/v1/auth/jellyfin", "POST", body=attempt)
                cookie = next((m.group(1) for k, v in r.headers.items() if k.lower() == "set-cookie"
                               for m in [re.match(r"connect\.sid=([^;]+)", v)] if m), "")
                if cookie:
                    self.cookie = cookie
                    return True
                if r.status:  # answered, just not with a session: try the other body
                    break
                time.sleep(2)
        return False


def arr_profile(url: str, key: str, name: str) -> dict | None:
    """Quality profile {id, name} called <name> in the *arr at <url>; None if
    there's none (never a substitute: a wrong profile means wrong downloads)"""
    try:
        profiles = api.get(f"{url}/api/v3/qualityprofile", {"X-Api-Key": key}) or []
    except ApiError:
        return None
    return next(({"id": p["id"], "name": p["name"]} for p in profiles if p.get("name") == name), None)


def sonarr_connection(cfg: Config, key: str, profile: dict, anime: dict) -> dict:
    p = cfg.paths
    # Anime requests go to the same Sonarr, into the anime folder with the
    # anime profile and Sonarr's anime numbering
    return {"name": "Sonarr", "hostname": "localhost", "port": 8989, "useSsl": False, "apiKey": key, "baseUrl": "",
            "activeProfileId": profile["id"], "activeProfileName": profile["name"], "activeDirectory": str(p.tv),
            "activeAnimeProfileId": anime["id"], "activeAnimeProfileName": anime["name"], "activeAnimeDirectory": str(p.anime),
            "seriesType": "standard", "animeSeriesType": "anime", "is4k": False, "enableSeasonFolders": True,
            "isDefault": True, "externalUrl": "http://localhost:8989", "enableSearch": True}


def radarr_connection(cfg: Config, key: str, profile: dict) -> dict:
    return {"name": "Radarr", "hostname": "localhost", "port": 7878, "useSsl": False, "apiKey": key, "baseUrl": "",
            "activeProfileId": profile["id"], "activeProfileName": profile["name"], "activeDirectory": str(cfg.paths.movies),
            "is4k": False, "isDefault": True, "externalUrl": "http://localhost:7878", "minimumAvailability": "released",
            "enableSearch": True}


def synced(conn: dict, kind: str, cfg: Config, key: str, profile: dict | None, anime: dict | None) -> dict:
    """An existing connection with address, API key, folder, configured
    profile and search as config.toml says"""
    p = cfg.paths
    port = 8989 if kind == "sonarr" else 7878
    want = dict(conn, enableSearch=True, hostname="localhost", port=port, useSsl=False, apiKey=key,
                activeDirectory=str(p.tv if kind == "sonarr" else p.movies))
    if profile:
        want.update(activeProfileId=profile["id"], activeProfileName=profile["name"])
    if kind == "sonarr":
        want.update(activeAnimeDirectory=str(p.anime), seriesType="standard", animeSeriesType="anime")
        if anime:
            want.update(activeAnimeProfileId=anime["id"], activeAnimeProfileName=anime["name"])
    return want


def approval_permissions(perms: int, on: bool) -> int:
    return perms | AUTO_APPROVE if on else perms & ~AUTO_APPROVE


def run(cfg: Config) -> None:
    info("Configuring Seerr...")
    s = Seerr(cfg)
    if not s.sign_in():
        warn("Could not sign in to Seerr with the Jellyfin credentials")
        warn(f"Could not authenticate — complete wizard manually at {s.url}")
        return
    ok(f"Signed in as {cfg.jellyfin_user} (Jellyfin at localhost:8096)")
    keys, urls = Keys.read(cfg.paths.config), cfg.urls

    # A sign-in can succeed without Seerr knowing where Jellyfin is (e.g.
    # an earlier run was interrupted after creating the admin); set it if missing
    try:
        if not (s.call("GET", "settings/jellyfin") or {}).get("ip"):
            try:
                s.call("POST", "settings/jellyfin", {"ip": "localhost", "port": 8096, "useSsl": False, "urlBase": ""})
                ok("Jellyfin address set (localhost:8096)")
            except ApiError:
                warn("Could not set Seerr's Jellyfin address")
    except ApiError:
        pass

    # Sync and enable the Jellyfin libraries (?sync=true fetches, ?enable=ids saves)
    try:
        libraries = s.call("GET", "settings/jellyfin/library?sync=true") or []
        if libraries:
            s.call("GET", "settings/jellyfin/library?enable=" + ",".join(str(lib["id"]) for lib in libraries))
            ok("Libraries synced")
    except ApiError:
        pass

    connect_sonarr(s, cfg, keys)
    connect_radarr(s, cfg, keys)
    # Keep existing connections in line with config.toml: search enabled and
    # the configured quality profile (older setups picked the first profile)
    for kind, name, url, key, profile_name in (("sonarr", "Sonarr", urls.sonarr, keys.sonarr, cfg.sonarr_profile),
                                                ("radarr", "Radarr", urls.radarr, keys.radarr, cfg.radarr_profile)):
        sync_connection(s, cfg, kind, name, url, key, profile_name)
    set_auto_approve(s, cfg)
    try:
        s.call("POST", "settings/initialize")
    except ApiError:
        pass
    ok("Setup finalized")


def connect_sonarr(s: Seerr, cfg: Config, keys: Keys) -> None:
    if not keys.sonarr:
        return
    url = cfg.urls.sonarr
    try:
        existing = s.call("GET", "settings/sonarr") or []
    except ApiError:
        existing = []
    if any(x.get("name") == "Sonarr" for x in existing):
        ok("Sonarr already connected")
    else:
        profile = arr_profile(url, keys.sonarr, cfg.sonarr_profile)
        if not profile:
            warn(f"Sonarr: quality profile '{cfg.sonarr_profile}' doesn't exist there; not connecting (fix [quality] in {cfg.paths.config_file})")
        else:
            anime = arr_profile(url, keys.sonarr, ANIME_PROFILE) or arr_profile(url, keys.sonarr, cfg.sonarr_anime_profile)
            if not anime:
                warn(f"Sonarr: no '{ANIME_PROFILE}' or '{cfg.sonarr_anime_profile}' profile; using '{profile['name']}' for anime")
                anime = profile
            try:
                s.call("POST", "settings/sonarr", sonarr_connection(cfg, keys.sonarr, profile, anime))
                ok(f"Sonarr connected (profile: {profile['name']})")
            except ApiError:
                warn("Could not add Sonarr")


def connect_radarr(s: Seerr, cfg: Config, keys: Keys) -> None:
    try:
        existing = s.call("GET", "settings/radarr") or []
    except ApiError:
        existing = []
    if existing:
        ok("Radarr already connected")
        return
    if not keys.radarr:
        return
    profile = arr_profile(cfg.urls.radarr, keys.radarr, cfg.radarr_profile)
    if not profile:
        warn(f"Radarr: quality profile '{cfg.radarr_profile}' doesn't exist there; not connecting (fix [quality] in {cfg.paths.config_file})")
        return
    try:
        s.call("POST", "settings/radarr", radarr_connection(cfg, keys.radarr, profile))
        ok(f"Radarr connected (profile: {profile['name']})")
    except ApiError:
        warn("Could not add Radarr")


def sync_connection(s: Seerr, cfg: Config, kind: str, name: str, url: str, key: str, profile_name: str) -> None:
    if not key:
        return
    try:
        conn = next((x for x in s.call("GET", f"settings/{kind}") or [] if x.get("name") == name), None)
    except ApiError:
        return
    if conn is None:
        return
    profile = arr_profile(url, key, profile_name)
    if not profile:
        warn(f"{name}: quality profile '{profile_name}' doesn't exist there; keeping the connection's current profile")
    anime = arr_profile(url, key, ANIME_PROFILE) if kind == "sonarr" else None
    want = synced(conn, kind, cfg, key, profile, anime)
    if want == conn:
        return
    body = {k: v for k, v in want.items() if k != "id"}  # id is read-only in the request body
    try:
        s.call("PUT", f"settings/{kind}/{conn['id']}", body)
        ok(f"{name}: search on, profile {want.get('activeProfileName')}, folder {want.get('activeDirectory')}")
    except ApiError:
        warn(f"Could not update {name} connection")


def set_auto_approve(s: Seerr, cfg: Config) -> None:
    """[requests] auto_approve: whether requests from other users (family
    members signing in with their Jellyfin account) download right away or
    wait for an admin's approval. Admins' requests are always approved."""
    on = cfg.flag("requests.auto_approve", True)
    try:
        main = s.call("GET", "settings/main")
    except ApiError:
        warn("Could not read Seerr's settings")
        return
    want = approval_permissions(main.get("defaultPermissions") or 32, on)
    if main.get("defaultPermissions") != want:
        body = {k: v for k, v in main.items() if k != "apiKey"}  # read-only; Seerr rejects it
        body["defaultPermissions"] = want
        try:
            s.call("POST", "settings/main", body)
        except ApiError:
            warn("Could not set Seerr's default permissions")
            return
    # Existing users who aren't admins follow the setting too
    try:
        users = (s.call("GET", "user?take=500") or {}).get("results") or []
    except ApiError:
        users = []
    for user in users:
        perms = user.get("permissions") or 0
        if perms & ADMIN:
            continue
        new = approval_permissions(perms, on)
        if new != perms:
            try:
                s.call("POST", f"user/{user['id']}/settings/permissions", {"permissions": new})
            except ApiError:
                warn(f"Could not update {user.get('displayName') or user.get('jellyfinUsername') or 'a user'}'s Seerr permissions")
    ok("Requests from other users: " + ("approved automatically" if on else "wait for an admin's approval"))
