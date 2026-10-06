"""nix run .#test (and the end of every install): about 125 checks that the
stack is configured as config.toml says, each service is up and the
services are connected to each other. Prints one line per check; the exit
status is the number of failed checks (at most 100)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from mediaserver import common as c
from mediaserver import launchd, logins
from mediaserver.config import Config, Keys
from mediaserver.pins import ANIME_PROFILE, INTRO_SKIPPER, MOONBASE
from mediaserver.ui import info

JF_CLIENT = 'MediaBrowser Client="verify", Device="script", DeviceId="verify", Version="1.0"'


class Checks:
    def __init__(self):
        self.passed = self.failed = self.skipped = 0

    def ok(self, desc: str) -> None:
        print(f"\033[1;32m   ✓ {desc}\033[0m", flush=True)
        self.passed += 1

    def fail(self, desc: str) -> None:
        print(f"\033[1;31m   ✗ {desc}\033[0m", flush=True)
        self.failed += 1

    def skip(self, desc: str) -> None:
        print(f"\033[1;33m   - {desc} (skipped)\033[0m", flush=True)
        self.skipped += 1

    def check(self, desc: str, result: Any) -> None:
        (self.ok if result is True else self.fail)(desc)


def safe(fn, default: Any = False) -> Any:
    """Run a check's expression; anything unexpected (missing field, bad
    answer) counts as a failed check rather than a crash"""
    try:
        return fn()
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, *c.HTTP_ERRORS):
        return default


class Verifier:
    def __init__(self, cfg: Config):
        self.cfg, self.urls, self.paths = cfg, cfg.urls, cfg.paths
        self.keys = Keys.read(self.paths.config)
        self.t = Checks()
        self.jf_token = ""

    # API helpers: the decoded answer, or default when it fails
    def arr(self, url: str, key: str, path: str, default: Any = None, ver: str = "v3") -> Any:
        return c.try_json(f"{url}/api/{ver}/{path}", {"X-Api-Key": key}, default, timeout=60)

    def jf(self, path: str, default: Any = None) -> Any:
        return c.try_json(f"{self.urls.jellyfin}/{path}", {"Authorization": f'MediaBrowser Token="{self.jf_token}"'}, default, timeout=60)

    def run(self) -> int:
        info("Running end-to-end verification...")
        for part in (self.health, self.api_keys, self.download_clients, self.root_folders, self.prowlarr,
                     self.jellyfin, self.jellyfin_sync, self.seerr, self.quality_profiles, self.authentication,
                     self.cleanuparr, self.health_checks, self.landing_page, self.access_control,
                     self.junk_filters, self.disk, self.moonfin, self.services, self.mac_app, self.tailscale):
            part()
        t = self.t
        total = t.passed + t.failed
        skipped = f" ({t.skipped} skipped)" if t.skipped else ""
        print("\n  ────────────────────────────────────────────────────────────")
        if t.failed == 0:
            print(f"\033[1;32m   All {total} checks passed!{skipped}\033[0m")
        else:
            print(f"\033[1;31m   {t.failed}/{total} checks failed{skipped}\033[0m")
        print()
        return min(t.failed, 100)

    # ─── Parts ───────────────────────────────────────────────────

    def health(self) -> None:
        info("Service health...")
        for name, url in self.urls.health_endpoints():
            # Generous: Byparr's /health starts a browser to answer, which
            # takes a while after a restart (a restore, on a slow Mac)
            code = c.status_code(url, timeout=30)
            # Up to 2 minutes for Byparr, when an enabled indexer needs it
            for _ in range(24 if name == "Byparr" and self.cfg.byparr_needed() else 1):
                if code != 0:
                    break
                time.sleep(5)
                code = c.status_code(url, timeout=30)
            shown = f"{code:03d}"
            if name == "Byparr" and not 200 <= code < 400 and not self.cfg.byparr_needed():
                self.t.skip(f"Byparr responds ({shown}; no enabled indexer needs it)")
                continue
            # Only 2xx/3xx count: a 404 or 500 means the service is up but broken
            self.t.check(f"{name} responds ({shown})", 200 <= code < 400)

    def api_keys(self) -> None:
        # Every later check needs these; a missing key must fail, not skip checks
        info("API keys...")
        for name in ("sonarr", "radarr", "prowlarr", "sabnzbd", "seerr"):
            self.t.check(f"{name.upper()}_KEY present", bool(getattr(self.keys, name)))

    def download_clients(self) -> None:
        info("Download clients...")
        cookie = logins.qbittorrent_cookie(self.urls.qbittorrent, self.cfg.qbit_user, self.cfg.qbit_pass)
        self.t.check("qBittorrent API reachable", bool(cookie))
        # Requests from this Mac skip the password, so check the stored hash
        self.t.check("qBittorrent → password is config.toml's", logins.qbittorrent_password_is(self.paths.config, self.cfg.qbit_pass))
        if cookie:
            cats = c.try_json(f"{self.urls.qbittorrent}/api/v2/torrents/categories", {"Cookie": cookie}, {}) or {}
            self.t.check("qBittorrent category: sonarr", "sonarr" in cats)
            self.t.check("qBittorrent category: radarr", "radarr" in cats)
        for name, url, key in (("Sonarr", self.urls.sonarr, self.keys.sonarr), ("Radarr", self.urls.radarr, self.keys.radarr)):
            if key:
                clients = self.arr(url, key, "downloadclient", []) or []
                self.t.check(f"{name} → qBittorrent", any(d.get("name") == "qBittorrent" and d.get("enable") for d in clients))

    def root_folders(self) -> None:
        info("Root folders...")
        p = self.paths
        for name, url, key, folder in (("Sonarr", self.urls.sonarr, self.keys.sonarr, p.tv), ("Sonarr", self.urls.sonarr, self.keys.sonarr, p.anime),
                                       ("Radarr", self.urls.radarr, self.keys.radarr, p.movies)):
            if key:
                roots = self.arr(url, key, "rootfolder", []) or []
                self.t.check(f"{name} → {folder}", any(r.get("path") == str(folder) for r in roots))
        if self.keys.radarr:
            roots = self.arr(self.urls.radarr, self.keys.radarr, "rootfolder")
            self.t.check("Radarr → no stale root folders", roots is not None and all(r.get("path") == str(p.movies) for r in roots))

    def prowlarr(self) -> None:
        info("Prowlarr...")
        key = self.keys.prowlarr
        if not key:
            return
        apps = self.arr(self.urls.prowlarr, key, "applications", [], "v1") or []
        names = [a.get("name") for a in apps]
        self.t.check("Prowlarr → Sonarr connected", "Sonarr" in names)
        self.t.check("Prowlarr → Radarr connected", "Radarr" in names)
        indexers = self.arr(self.urls.prowlarr, key, "indexer", [], "v1") or []
        enabled = sum(1 for i in indexers if i.get("enable"))
        self.t.check(f"Prowlarr → indexers enabled ({enabled})", enabled > 0)
        results = c.try_json(f"{self.urls.prowlarr}/api/v1/search?query=test&type=movie&limit=3", {"X-Api-Key": key}, [], timeout=30) or []
        if results:
            self.t.ok(f"Prowlarr → search works ({len(results)} results)")
        elif enabled:
            self.t.skip("Prowlarr → search (indexers may be rate-limited)")
        else:
            self.t.fail("Prowlarr → search works")

    def jellyfin(self) -> None:
        info("Jellyfin...")
        r = c.request(f"{self.urls.jellyfin}/Users/AuthenticateByName", "POST", {"Authorization": JF_CLIENT},
                      body={"Username": self.cfg.jellyfin_user, "Pw": self.cfg.jellyfin_pass})
        self.jf_token = (r.json({}) or {}).get("AccessToken") or ""
        self.t.check("Jellyfin → login", bool(self.jf_token))
        if not self.jf_token:
            return
        mode, lang = self.cfg.get("playback.subtitle_mode", "Always"), self.cfg.get("playback.subtitle_language", "eng")
        me = self.jf("Users/Me", {}) or {}
        self.t.check(f"Jellyfin → playback: subtitles {mode} ({lang})",
                     safe(lambda: me["Configuration"]["SubtitleMode"] == mode and me["Configuration"]["SubtitleLanguagePreference"] == lang))
        folders = self.jf("Library/VirtualFolders", []) or []
        p = self.paths
        for name, path in (("Movies", p.movies), ("TV Shows", p.tv), ("Anime", p.anime)):
            self.t.check(f"Jellyfin → library: {name}", any(f.get("Name") == name and str(path) in (f.get("Locations") or []) for f in folders))
        self.t.check("Jellyfin → real-time monitoring", all((f.get("LibraryOptions") or {}).get("EnableRealtimeMonitor") is True for f in folders))
        self.t.check("Jellyfin → daily scan", all((f.get("LibraryOptions") or {}).get("AutomaticRefreshIntervalDays") == 1 for f in folders))

    def jellyfin_sync(self) -> None:
        info("Jellyfin sync...")
        for name, url, key in (("Sonarr", self.urls.sonarr, self.keys.sonarr), ("Radarr", self.urls.radarr, self.keys.radarr)):
            if key:
                notes = self.arr(url, key, "notification", []) or []
                self.t.check(f"{name} → Jellyfin notification", any(n.get("name") == "Jellyfin" for n in notes))

    def seerr(self) -> None:
        info("Seerr...")
        public = c.try_json(f"{self.urls.seerr}/api/v1/settings/public", default={}) or {}
        self.t.check("Seerr → initialized", public.get("initialized") is True)
        key = self.keys.seerr
        if not key:
            return
        h = {"X-Api-Key": key}
        sonarrs = c.try_json(f"{self.urls.seerr}/api/v1/settings/sonarr", h, []) or []
        radarrs = c.try_json(f"{self.urls.seerr}/api/v1/settings/radarr", h, []) or []
        p, cfg = self.paths, self.cfg

        def conn(label: str, conns: list, name: str, port: int, folder: Path, profile: str, extra=lambda x: True) -> None:
            mine = [x for x in conns if x.get("name") == name]
            self.t.check(f"Seerr → {label} (port {port}, {folder.name}, {profile})", safe(lambda: len(mine) == 1 and (
                mine[0]["port"] == port and mine[0]["activeDirectory"] == str(folder) and mine[0]["activeProfileName"] == profile
                and mine[0]["enableSearch"] is True and extra(mine[0]))))
        # Anime requests go to the same Sonarr, into the anime folder
        conn("Sonarr", sonarrs, "Sonarr", 8989, p.tv, cfg.sonarr_profile,
             lambda x: (x.get("seriesType") or "standard") != "anime" and x.get("animeSeriesType") == "anime"
             and x.get("activeAnimeDirectory") == str(p.anime) and x.get("activeAnimeProfileName") == ANIME_PROFILE)
        auto = cfg.flag("requests.auto_approve", True)
        main = c.try_json(f"{self.urls.seerr}/api/v1/settings/main", h, {}) or {}
        self.t.check(f"Seerr → requests from other users {'approved automatically' if auto else 'need approval'}",
                     safe(lambda: ((main["defaultPermissions"] // 128) % 2 == 1) == auto))
        self.t.check("Seerr → only one Sonarr connection", len(sonarrs) == 1)
        conn("Radarr", radarrs, "Radarr", 7878, p.movies, cfg.radarr_profile)
        libs = (c.try_json(f"{self.urls.seerr}/api/v1/settings/jellyfin", h, {}) or {}).get("libraries") or []
        enabled = sum(1 for lib in libs if lib.get("enabled"))
        self.t.check(f"Seerr → libraries enabled ({enabled}/{len(libs)})", enabled > 0)

    def quality_profiles(self) -> None:
        info("Quality profiles...")
        for name, url, key in (("Sonarr", self.urls.sonarr, self.keys.sonarr), ("Radarr", self.urls.radarr, self.keys.radarr)):
            if not key:
                continue
            profile = self.arr(url, key, "qualityprofile/1")
            if not profile:
                self.t.skip(f"{name} → quality profile")
                continue
            unknown = [i.get("allowed") for i in profile.get("items", []) if (i.get("quality") or {}).get("id") == 0]
            self.t.check(f"{name} → Unknown quality allowed", bool(unknown) and unknown[0] is True)

    def authentication(self) -> None:
        info("Authentication...")
        user, pw = self.cfg.jellyfin_user, self.cfg.jellyfin_pass
        # On this Mac only (network.admin_bind): the admin pages open without a login
        local = self.cfg.admin_local_only
        # Log in with config.toml's password, not just check a username is set
        for name, url, key, ver in (("Sonarr", self.urls.sonarr, self.keys.sonarr, "v3"), ("Radarr", self.urls.radarr, self.keys.radarr, "v3"),
                                    ("Prowlarr", self.urls.prowlarr, self.keys.prowlarr, "v1")):
            if not key:
                continue
            host = self.arr(url, key, "config/host", None, ver)
            if not host:
                self.t.skip(f"{name} → auth")
                continue
            if local:
                self.t.check(f"{name} → opens without a login on this Mac",
                             host.get("authenticationRequired") == "disabledForLocalAddresses" and logins.opens_without_login(url))
                continue
            self.t.check(f"{name} → login required", host.get("authenticationMethod") == "forms" and host.get("authenticationRequired") == "enabled")
            self.t.check(f"{name} → config.toml login works", logins.arr(url, user, pw))
        if self.keys.sabnzbd:
            if local:
                self.t.check("SABnzbd → opens without a login on this Mac", logins.opens_without_login(self.urls.sabnzbd))
            else:
                self.t.check("SABnzbd → config.toml login works", logins.sabnzbd(self.urls.sabnzbd, user, pw))
        yaml = next((f for f in (self.paths.config / "bazarr/config/config/config.yaml", self.paths.config / "bazarr/config/config.yaml") if f.exists()), None)
        if yaml:
            auth = bazarr_auth_section(yaml.read_text())
            kind, name = auth.get("type", ""), auth.get("username", "")
            # type null means no login at all, even with a username set
            if local:
                self.t.check(f"Bazarr → opens without a login on this Mac ({kind or 'null'})", kind in ("", "null", "~"))
            else:
                self.t.check(f"Bazarr → login required ({kind})", bool(name) and name != "''" and kind in ("form", "basic"))
                self.t.check("Bazarr → config.toml login works", logins.bazarr(self.urls.bazarr, user, pw))
            settings = c.try_json(f"{self.urls.bazarr}/api/system/settings", {"X-API-KEY": auth.get("apikey", "")}, {}) or {}
            providers = len(((settings.get("general") or {}).get("enabled_providers")) or [])
            self.t.check(f"Bazarr → subtitle providers enabled ({providers})", providers > 0)

    def cleanuparr(self) -> None:
        info("Cleanuparr...")
        url, key = self.urls.cleanuparr, c.cleanuparr_key(self.paths.config)
        status = c.try_json(f"{url}/api/auth/status", default={}) or {}
        self.t.check("Cleanuparr → login required", status.get("setupCompleted") is True and status.get("authBypassActive") is False)
        self.t.check("Cleanuparr → config.toml login works", logins.cleanuparr(url, self.cfg.jellyfin_user, self.cfg.jellyfin_pass))
        if not key:
            self.t.check("Cleanuparr → API key readable", False)
            return
        h = {"X-Api-Key": key}
        for app in ("sonarr", "radarr"):
            conf = c.try_json(f"{url}/api/configuration/{app}", h, {}) or {}
            self.t.check(f"Cleanuparr → {app.capitalize()} connected", any(i.get("enabled") for i in conf.get("instances") or []))
        clients = (c.try_json(f"{url}/api/configuration/download_client", h, {}) or {}).get("clients") or []
        self.t.check("Cleanuparr → qBittorrent connected", any(x.get("typeName") == "qBittorrent" and x.get("enabled") for x in clients))
        if self.cfg.flag("cleanuparr.enabled", True):
            cleaner = c.try_json(f"{url}/api/configuration/queue_cleaner", h, {}) or {}
            # Paused by netwatch while offline
            if c.read_text(self.paths.state / "netwatch/connection") == "offline":
                self.t.check("Cleanuparr → queue cleaner paused (offline)", cleaner.get("enabled") is False)
            else:
                self.t.check("Cleanuparr → queue cleaner on", cleaner.get("enabled") is True)
            strikes = self.cfg.get("cleanuparr.stalled_strikes", 6)
            rules = c.try_json(f"{url}/api/queue-rules/stall", h, []) or []
            self.t.check(f"Cleanuparr → stalled-download rule ({strikes} strikes)",
                         any(r.get("name") == "Stalled" and r.get("enabled") and r.get("maxStrikes") == strikes for r in rules))

    def health_checks(self) -> None:
        info("Health checks...")
        # The *arr apps cache health results; ask for a fresh check (it runs async)
        for name, url, key in (("Sonarr", self.urls.sonarr, self.keys.sonarr), ("Radarr", self.urls.radarr, self.keys.radarr)):
            if not key:
                continue
            c.request(f"{url}/api/v3/command", "POST", {"X-Api-Key": key}, body={"name": "CheckHealth"})
            errors = None
            for _ in range(16):
                health = self.arr(url, key, "health")
                errors = None if health is None else sum(1 for x in health if x.get("type") == "error")
                if errors == 0:
                    break
                time.sleep(1)
            self.t.check(f"{name} → no health errors", errors == 0)

    def landing_page(self) -> None:
        info("Landing page...")
        page = c.request(self.urls.dashboard)
        html = page.body.decode(errors="replace") if page.ok else ""
        ctype = next((v for k, v in page.headers.items() if k.lower() == "content-type"), "")
        self.t.check("Landing page → serves HTML", bool(re.search(r"Media.*Server", html)))
        self.t.check("Landing page → Content-Type text/html", "text/html" in ctype.lower())
        self.t.check("Landing page → watch link", "Moonfin/Web" in html)
        self.t.check("Landing page → downloads widget", "qbt/torrents" in html)

        def is_json(path: str) -> bool:
            r = c.request(self.urls.dashboard + path)
            return r.ok and r.json(None) is not None
        self.t.check("Landing page → qBittorrent proxy", is_json("/api/qbt/torrents/info"))
        self.t.check("Proxy → Jellyfin latest", is_json("/api/jellyfin/Items?SortBy=DateCreated&SortOrder=Descending&Limit=3&Recursive=true&IncludeItemTypes=Movie,Series"))
        self.t.check("Proxy → SABnzbd queue", is_json("/api/sabnzbd/?mode=queue&output=json"))

    def access_control(self) -> None:
        info("Access control...")
        d = self.urls.dashboard
        post = c.request(f"{d}/api/jellyfin/Items", "POST", body=b"").status
        self.t.check(f"Proxy → rejects writes (POST Jellyfin items: {post})", post == 403)
        cal = c.status_code(f"{d}/api/sonarr/calendar")
        self.t.check(f"Proxy → no Sonarr access (calendar: {cal})", cal == 404)
        keys = c.status_code(f"{d}/api/jellyfin/Auth/Keys")
        self.t.check(f"Proxy → hides other endpoints (Jellyfin Auth/Keys: {keys})", keys == 404)
        self.t.check("Proxy → SABnzbd limited to queue/history", c.status_code(f"{d}/api/sabnzbd/?mode=get_config") == 403)
        from mediaserver import leaks
        r = leaks.find(self.cfg)
        detail = (f": {', '.join(r.found)}" if r.found else f": couldn't fetch {', '.join(r.unverified)}" if r.unverified
                  else f" ({r.paths} paths" + (f"; too short to look for: {', '.join(r.unchecked)}" if r.unchecked else "") + ")")
        self.t.check(f"Dashboard → serves no API key or password{detail}", r.clean)
        # The dashboard is this Mac's only: a request through a proxy (an old
        # Tailscale route) or for another host name is refused
        via_proxy = c.request(f"{d}/", headers={"X-Forwarded-For": "100.64.0.9"}).status
        self.t.check(f"Dashboard → refuses requests through a proxy ({via_proxy})", via_proxy == 403)
        other_host = c.request(f"{d}/api/control/speed", headers={"Host": "mac.tailnet.ts.net"}).status
        self.t.check(f"Dashboard → refuses other host names ({other_host})", other_host == 403)
        # From this Mac qBittorrent skips its login (for the dashboard); from
        # the network it must not
        lan = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, check=False).stdout.strip()
        if lan and self.cfg.get("network.admin_bind", "0.0.0.0") == "0.0.0.0":
            self.t.check("qBittorrent → requires login from the network", c.status_code(f"http://{lan}:8081/api/v2/app/version") == 403)
        else:
            self.t.skip("qBittorrent → login from the network (not reachable from here)")

    def junk_filters(self) -> None:
        info("Junk-release filters...")
        cfg = self.cfg
        cache: dict[str, tuple[list, list]] = {}

        def formats(url: str, key: str) -> tuple[list, list]:
            if url not in cache:
                cache[url] = (self.arr(url, key, "customformat", []) or [], self.arr(url, key, "qualityprofile", []) or [])
            return cache[url]

        def score(url: str, key: str, profile: str, cf: str):
            """Score of custom format <cf> in <profile> (None if unset)"""
            cfs, profiles = formats(url, key)
            ids = [x["id"] for x in cfs if x.get("name") == cf]
            prof = next((p for p in profiles if p.get("name") == profile), None)
            if not ids or not prof:
                return None
            return next((i.get("score") for i in prof.get("formatItems", []) if i.get("format") == ids[0]), None)

        def junk(name: str, url: str, key: str, profile: str) -> None:
            cfs, profiles = formats(url, key)
            prof = next((p for p in profiles if p.get("name") == profile), {})
            if cfg.flag("quality.prefer_h265", True):
                s = score(url, key, profile, "Prefer HEVC")
                self.t.check(f"{name} → HEVC preferred in {profile}", s is not None and s > 0)
            for cf in ("BR-DISK", "Foreign Subtitles"):
                s = score(url, key, profile, cf)
                self.t.check(f"{name} → {cf} blocked in {profile}", (prof.get("minFormatScore", -1) >= 0) and s is not None and s <= -10000)
        s_url, s_key, r_url, r_key = self.urls.sonarr, self.keys.sonarr, self.urls.radarr, self.keys.radarr
        if s_key:
            junk("Sonarr", s_url, s_key, cfg.sonarr_profile)
            junk("Sonarr", s_url, s_key, ANIME_PROFILE)
        if r_key:
            junk("Radarr", r_url, r_key, cfg.radarr_profile)
        if s_key:
            if cfg.flag("quality.anime_block_dubs", True):
                self.t.check(f"Sonarr → dub-only releases blocked for anime ({ANIME_PROFILE})", score(s_url, s_key, ANIME_PROFILE, "Dubs Only") == -10000)
            self.t.check(f"Sonarr → dub-only releases allowed for TV ({cfg.sonarr_profile})", score(s_url, s_key, cfg.sonarr_profile, "Dubs Only") == 0)
            # Setup moves anime series off the base profile (others were chosen by hand)
            profiles = formats(s_url, s_key)[1]
            base = next((p["id"] for p in profiles if p.get("name") == cfg.sonarr_anime_profile), None)
            series = self.arr(s_url, s_key, "series")
            self.t.check(f"Sonarr → anime series use the {ANIME_PROFILE} profile",
                         base is not None and series is not None and all(x.get("qualityProfileId") != base for x in series if x.get("seriesType") == "anime"))
            self.t.check(f"Sonarr → raw anime (no subtitles) blocked ({ANIME_PROFILE})", score(s_url, s_key, ANIME_PROFILE, "Anime Raws") == -10000)
            if cfg.flag("quality.anime_release_groups", True):
                self.t.check(f"Sonarr → anime release groups ranked ({ANIME_PROFILE})", score(s_url, s_key, ANIME_PROFILE, "Anime BD Tier 01") == 1400)
            self.t.check(f"Sonarr → anime rankings off for TV ({cfg.sonarr_profile})", score(s_url, s_key, cfg.sonarr_profile, "Anime BD Tier 01") == 0)
        if cfg.flag("quality.prefer_english_audio", True):
            if s_key:
                self.t.check(f"Sonarr → English audio preferred for TV ({cfg.sonarr_profile})", score(s_url, s_key, cfg.sonarr_profile, "Prefer English Audio") == 50)
                self.t.check("Sonarr → no English-audio preference for anime", score(s_url, s_key, ANIME_PROFILE, "Prefer English Audio") == 0)
            if r_key:
                self.t.check(f"Radarr → English audio preferred ({cfg.radarr_profile})", score(r_url, r_key, cfg.radarr_profile, "Prefer English Audio") == 50)
        if cfg.flag("quality.rename_files", True):
            if s_key:
                self.t.check("Sonarr → renames files", (self.arr(s_url, s_key, "config/naming", {}) or {}).get("renameEpisodes") is True)
            if r_key:
                self.t.check("Radarr → renames files", (self.arr(r_url, r_key, "config/naming", {}) or {}).get("renameMovies") is True)

    def disk(self) -> None:
        info("Disk space...")
        free = shutil.disk_usage(self.paths.media).free // 1024 ** 3
        min_gb = self.cfg.disk_min_gb
        self.t.check(f"Media disk: {free} GB free (imports stop below {min_gb} GB)", free >= min_gb)
        for name, url, key in (("Sonarr", self.urls.sonarr, self.keys.sonarr), ("Radarr", self.urls.radarr, self.keys.radarr)):
            mm = self.arr(url, key, "config/mediamanagement", {}) or {}
            self.t.check(f"{name} → minimum free space {min_gb} GB", mm.get("minimumFreeSpaceWhenImporting") == min_gb * 1024)

    def moonfin(self) -> None:
        info("Moonfin...")
        if self.jf_token:
            repos = self.jf("Repositories", []) or []
            plugins = self.jf("Plugins", []) or []
            version = next((p.get("Version") for p in plugins if p.get("Name") == "Moonbase"), None)
            moonfin_repos = [r for r in repos if "Moonfin-Client/Plugin" in (r.get("Url") or "")]
            self.t.check(f"Moonbase pinned ({MOONBASE.version}, manifest at {MOONBASE.commit[:12]})",
                         bool(moonfin_repos) and all(r.get("Url") == MOONBASE.manifest for r in moonfin_repos) and version == MOONBASE.version)
            guid = INTRO_SKIPPER.guid.replace("-", "")
            skipper = next((f"{p.get('Version')} {p.get('Status')}" for p in plugins if (p.get("Id") or "").lower().replace("-", "") == guid), "")
            self.t.check(f"Intro Skipper {INTRO_SKIPPER.version} loaded", skipper == f"{INTRO_SKIPPER.version} Active")
            folders = self.jf("Library/VirtualFolders", []) or []
            tv = [f for f in folders if f.get("CollectionType") == "tvshows"]
            self.t.check("Intro Skipper on for the TV and Anime libraries",
                         bool(tv) and all("Intro Skipper" in ((f.get("LibraryOptions") or {}).get("MediaSegmentProviderOrder") or []) for f in tv))
            hw = self.cfg.flag("playback.hardware_acceleration", True)
            enc = self.jf("System/Configuration/encoding", {}) or {}
            self.t.check(f"Jellyfin → hardware transcoding ({str(hw).lower()})", (enc.get("HardwareAccelerationType") == "videotoolbox") == hw)
        index = c.request(f"{self.urls.jellyfin}/Moonfin/Web/")
        boot = c.request(f"{self.urls.jellyfin}/Moonfin/Web/flutter_bootstrap.js")
        self.t.check("Moonfin web app references nothing on the internet (page and loader files; not a browser test)",
                     not re.search(rb'<script[^>]*src="https?://', index.body) and b'canvasKitBaseUrl: "canvaskit/"' in boot.body)
        status = c.request(f"{self.urls.dashboard}/status.json").json({}) or {}
        self.t.check("Dashboard live data is fresh (status.json, under 2 minutes old)", time.time() - status.get("updated", 0) < 120)
        # Right after a first start, dashstatus may still be on its first
        # media round (recommendations, ratings): wait for it a while
        media = ("requests_live", "upcoming", "health")
        deadline = time.time() + 120
        while not all(k in status for k in media) and time.time() < deadline:
            time.sleep(5)
            status = c.request(f"{self.urls.dashboard}/status.json").json({}) or {}
        self.t.check("Dashboard media data (requests, coming up, library health)", all(k in status for k in media))
        self.t.check("Moonfin web app (/Moonfin/Web/)", 200 <= index.status < 400)

    def mac_app(self) -> None:
        info("Mac app...")
        if self.cfg.get("app.enabled", True) is not True:
            self.t.skip("Mac app (app.enabled = false)")
            return
        from mediaserver.steps import macapp
        app = macapp.app_path()
        exe = app / "Contents/MacOS/media-server"
        try:
            first = json.loads((app / "Contents/Resources/services.json").read_text())[0]["url"]
        except (OSError, ValueError, LookupError, TypeError):
            first = ""
        opens = exe.is_file() and bool(exe.stat().st_mode & 0o111) and first == self.urls.dashboard
        self.t.check(f"Mac app opens the dashboard ({app})", opens)

    def services(self) -> None:
        info("Services (launchd)...")
        for name in launchd.SERVICE_NAMES:
            state = launchd.state(name)
            # Byparr only matters when an enabled indexer goes through it
            if name == "byparr" and not state.startswith("running") and not self.cfg.byparr_needed():
                self.t.skip(f"Service: byparr ({state}; no enabled indexer needs it)")
                continue
            self.t.check(f"Service: {name} ({state})", state.startswith("running"))

    def tailscale(self) -> None:
        info("Tailscale...")
        cli = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        if not os.access(cli, os.X_OK):
            self.t.skip("Tailscale not installed")
            return
        self.t.ok("Tailscale installed")
        if subprocess.run([cli, "status"], capture_output=True, check=False).returncode != 0:
            self.t.skip("Tailscale not connected (remote access unavailable)")
            return
        ip = subprocess.run([cli, "ip", "-4"], capture_output=True, text=True, check=False).stdout.strip()
        if ip:
            self.t.ok(f"Tailscale connected ({ip})")
        else:
            self.t.fail("Tailscale connected but no IPv4 address")
        serve = subprocess.run([cli, "serve", "status"], capture_output=True, text=True, check=False).stdout
        if "https" in serve:
            self.t.ok("Tailscale HTTPS configured")
        else:
            self.t.skip("Tailscale HTTPS not configured")
        from mediaserver import tailscale
        try:
            routes = tailscale.published(json.loads(subprocess.run([cli, "serve", "status", "--json"], capture_output=True, text=True,
                                                                   check=False).stdout or "{}"))
        except ValueError:
            routes = None
        exposed = [p for p, targets in (routes or {}).items() if any(tailscale.is_dashboard(t, self.urls.dashboard_port) for t in targets)]
        if routes is None:
            self.t.skip("Tailscale → dashboard not published (routes unreadable)")
        else:
            self.t.check(f"Tailscale → dashboard not published{f' (still on :{exposed[0]}: tailscale serve --https={exposed[0]} off)' if exposed else ''}", not exposed)


def bazarr_auth_section(yaml_text: str) -> dict:
    """The auth: section of Bazarr's config.yaml as {key: value} (quotes
    kept as Bazarr wrote them, except around the value as a whole)"""
    out, inside = {}, False
    for line in yaml_text.splitlines():
        if line.startswith("auth:"):
            inside = True
        elif inside and line and not line.startswith(" "):
            break
        elif inside and line.startswith("  ") and ":" in line:
            k, v = line.strip().split(":", 1)
            v = v.strip()
            out[k] = v if v == "''" else v.strip("'\"")
    return out


def main(cfg: Config | None = None) -> int:
    return Verifier(cfg or Config.load()).run()

