"""Sonarr (TV and anime) and Radarr (movies): root folders, download
clients, the Jellyfin connection, logins, file naming, minimum free space,
and the release filters."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from mediaserver import api
from mediaserver import common as c
from mediaserver.api import ApiError
from mediaserver.arr import set_login, sync_fields
from mediaserver.config import Config, Keys
from mediaserver.pins import ANIME_PROFILE, profile_origin
from mediaserver.ui import err, info, ok, warn

JUNK_SCORE = -10000
REPO = Path(__file__).resolve().parent.parent.parent


class App:
    def __init__(self, label: str, url: str, key: str, version: str = "v3"):
        self.label, self.url, self.key, self.version = label, url, key, version

    def call(self, method: str, path: str, body: Any = None) -> Any:
        return api.call(method, f"{self.url}/api/{self.version}/{path}", {"X-Api-Key": self.key}, body=body)


def apps(cfg: Config) -> tuple[App | None, App | None]:
    keys = Keys.read(cfg.paths.config)
    return (App("Sonarr", cfg.urls.sonarr, keys.sonarr) if keys.sonarr else None,
            App("Radarr", cfg.urls.radarr, keys.radarr) if keys.radarr else None)


# ─── Each app ────────────────────────────────────────────────────

def configure_app(cfg: Config, app: App, roots: list[str], category_field: str, service: str) -> None:
    info(f"Configuring {service}...")
    keys = Keys.read(cfg.paths.config)
    # Root folders: exactly these; others, like a stale /downloads, are removed
    try:
        existing = app.call("GET", "rootfolder") or []
    except ApiError:
        warn(f"{service}: couldn't read its root folders; skipped (retried next run)")
        existing, roots = [], []
    if roots:
        for stale in (r for r in existing if r.get("path") not in roots):
            try:
                app.call("DELETE", f"rootfolder/{stale['id']}")
                ok(f"Removed stale root folder (id: {stale['id']})")
            except ApiError:
                pass
    for root in roots:
        if any(r.get("path") == root for r in existing):
            ok(f"Root folder: {root}")
            continue
        try:
            app.call("POST", "rootfolder", {"path": root})
            ok(f"Root folder: {root}")
        except ApiError:
            warn(f"Could not add root folder {root}")

    # Unreadable is not "none": qBittorrent/SABnzbd would be added twice
    try:
        clients = app.call("GET", "downloadclient") or []
    except ApiError:
        warn(f"{service}: couldn't read its download clients; skipped (retried next run)")
        set_login(cfg, service, app.url, app.key, app.version, service)
        return
    qbit = next((x for x in clients if x.get("implementation") == "QBittorrent"), None)
    sab = next((x for x in clients if x.get("implementation") == "Sabnzbd"), None)
    resource = f"{app.url}/api/{app.version}/downloadclient"
    if qbit:
        # Keep the login in step with config.toml
        sync_fields(f"{service} qBittorrent client", resource, qbit["id"], {"username": cfg.qbit_user, "password": cfg.qbit_pass}, app.key)
        ok("qBittorrent connected")
    else:
        body = {"name": "qBittorrent", "implementation": "QBittorrent", "configContract": "QBittorrentSettings", "enable": True,
                "protocol": "torrent", "priority": 1,
                "fields": [{"name": "host", "value": "localhost"}, {"name": "port", "value": 8081},
                           {"name": "username", "value": cfg.qbit_user}, {"name": "password", "value": cfg.qbit_pass},
                           {"name": category_field, "value": service}]}
        try:
            app.call("POST", "downloadclient", body)
            ok(f"qBittorrent connected (category: {service})")
        except ApiError:
            warn("Could not add qBittorrent")
    if keys.sabnzbd and sab:
        sync_fields(f"{service} SABnzbd client", resource, sab["id"], {"apiKey": keys.sabnzbd}, app.key)
        ok("SABnzbd connected")
    elif keys.sabnzbd:
        body = {"name": "SABnzbd", "implementation": "Sabnzbd", "configContract": "SabnzbdSettings", "enable": True,
                "protocol": "usenet", "priority": 2,
                "fields": [{"name": "host", "value": "localhost"}, {"name": "port", "value": 8080},
                           {"name": "apiKey", "value": keys.sabnzbd}, {"name": category_field, "value": service}]}
        try:
            app.call("POST", "downloadclient", body)
            ok(f"SABnzbd connected (category: {service})")
        except ApiError:
            warn("Could not add SABnzbd")

    # The Jellyfin connection: a library scan on import and upgrade
    jellyfin_key = c.read_text(cfg.paths.state / "dashstatus/jellyfin-key")
    if jellyfin_key:
        try:
            notification = next((n for n in app.call("GET", "notification") or [] if n.get("implementation") == "MediaBrowser"), None)
        except ApiError:
            notification = None
        if notification:
            # Setup's own Jellyfin key (older versions could store another app's)
            sync_fields(f"{service} Jellyfin notification", f"{app.url}/api/{app.version}/notification", notification["id"],
                        {"apiKey": jellyfin_key}, app.key)
            ok("Jellyfin notification connected")
        else:
            body = {"name": "Jellyfin", "implementation": "MediaBrowser", "configContract": "MediaBrowserSettings", "enable": True,
                    "onDownload": True, "onUpgrade": True, "onRename": True,
                    "fields": [{"name": "host", "value": "localhost"}, {"name": "port", "value": 8096},
                               {"name": "useSsl", "value": False}, {"name": "apiKey", "value": jellyfin_key},
                               {"name": "updateLibrary", "value": True}]}
            try:
                app.call("POST", "notification", body)
                ok("Jellyfin notification connected")
            except ApiError:
                warn("Could not add Jellyfin notification")
    set_login(cfg, service, app.url, app.key, app.version, service)


def enable_unknown_quality(app: App) -> None:
    try:
        profile = app.call("GET", "qualityprofile/1")
    except ApiError:
        return
    if not profile:
        return
    unknown = [i for i in profile.get("items") or [] if (i.get("quality") or {}).get("id") == 0]
    if unknown and unknown[0].get("allowed") is False:
        profile["items"] = [dict(i, allowed=True) if (i.get("quality") or {}).get("id") == 0 else i for i in profile["items"]]
        try:
            app.call("PUT", "qualityprofile/1", profile)
            ok("Quality: enabled Unknown quality")
        except ApiError:
            pass


def set_min_free_space(cfg: Config, app: App) -> None:
    """Refuse imports that would leave less than disk.min_free_gb free"""
    want = cfg.disk_min_gb * 1024
    try:
        mm = app.call("GET", "config/mediamanagement")
    except ApiError:
        warn(f"{app.label}: could not read media management settings")
        return
    if mm.get("minimumFreeSpaceWhenImporting") == want:
        return
    try:
        app.call("PUT", "config/mediamanagement", dict(mm, minimumFreeSpaceWhenImporting=want))
        ok(f"{app.label}: imports stop below {cfg.disk_min_gb} GB free")
    except ApiError:
        warn(f"{app.label}: could not set minimum free space")


def set_renaming(cfg: Config, app: App, kind: str) -> None:
    """File names for imports ([quality] rename_files): with it off, files
    keep their release names, and ones without a season number ("Show E01
    …") leave Jellyfin without season/episode numbers. Turning it on also
    renames what's already in the library, once (recorded only after the
    app accepted the request, so an interrupted run does it next time).
    Library files are hard links, so seeding is unaffected."""
    marker = cfg.paths.state / f"renamed-{app.label.lower()}"
    field = "renameEpisodes" if kind == "series" else "renameMovies"
    want = cfg.flag("quality.rename_files", True)
    try:
        naming = app.call("GET", "config/naming")
    except ApiError:
        warn(f"{app.label}: could not read its naming settings")
        return
    if naming.get(field) != want:
        try:
            app.call("PUT", "config/naming", dict(naming, **{field: want}))
        except ApiError:
            warn(f"{app.label}: could not change its file naming")
            return
    ok(f"{app.label}: rename files {str(want).lower()}")
    if not want:
        marker.unlink(missing_ok=True)
        return
    if marker.exists():
        return
    try:
        ids = [x["id"] for x in app.call("GET", "series" if kind == "series" else "movie") or []]
    except ApiError:
        return
    try:
        if ids:
            app.call("POST", "command", {"name": "RenameSeries", "seriesIds": ids} if kind == "series"
                     else {"name": "RenameMovie", "movieIds": ids})
            ok(f"{app.label}: renaming the files already in the library")
        c.write_atomic(marker, "")
    except ApiError:
        warn(f"{app.label}: could not start renaming the existing files (retried next run)")


def run(cfg: Config) -> None:
    require_no_unmerged_anime_sonarr(cfg)
    sonarr, radarr = apps(cfg)
    p = cfg.paths
    # One Sonarr for TV and anime: Seerr sends anime to the anime folder
    if sonarr:
        configure_app(cfg, sonarr, [str(p.tv), str(p.anime)], "tvCategory", "sonarr")
    if radarr:
        configure_app(cfg, radarr, [str(p.movies)], "movieCategory", "radarr")
    if sonarr:
        set_min_free_space(cfg, sonarr)
        set_renaming(cfg, sonarr, "series")
    if radarr:
        set_renaming(cfg, radarr, "movie")
    if radarr:
        set_min_free_space(cfg, radarr)
    for app in (sonarr, radarr):
        if app:
            enable_unknown_quality(app)


# ─── Release filters ─────────────────────────────────────────────

def custom_format(path: Path) -> tuple[dict, int, str]:
    """A file in custom-formats/<app>: the API payload (TRaSH's format →
    fields as a list of name/value pairs), its score and which profiles it
    applies to ("all", "anime" or "standard")"""
    data = json.loads(path.read_text())
    payload = {"name": data["name"], "includeCustomFormatWhenRenaming": False,
               "specifications": [{"name": s["name"], "implementation": s["implementation"], "negate": s["negate"],
                                   "required": s["required"], "fields": [{"name": k, "value": v} for k, v in (s.get("fields") or {}).items()]}
                                  for s in data["specifications"]]}
    return payload, data.get("mediaServerScore", JUNK_SCORE), data.get("mediaServerProfiles", "all")


def comparable(cf: dict) -> dict:
    """A custom format as far as setup sets it (the apps add their own
    fields to what they read back: ids, labels, help texts)"""
    return {"name": cf.get("name"), "includeCustomFormatWhenRenaming": bool(cf.get("includeCustomFormatWhenRenaming")),
            "specifications": sorted(
                (json.dumps({"name": sp.get("name"), "implementation": sp.get("implementation"), "negate": bool(sp.get("negate")),
                             "required": bool(sp.get("required")),
                             "fields": {f.get("name"): f.get("value") for f in sp.get("fields") or []}}, sort_keys=True)
                 for sp in cf.get("specifications") or []))}


def preference_score(cfg: Config, name: str, score: int) -> int:
    """Preferences config.toml can turn off score 0"""
    if name == "Prefer HEVC" and not cfg.flag("quality.prefer_h265", True):
        return 0
    if name == "Prefer English Audio" and not cfg.flag("quality.prefer_english_audio", True):
        return 0
    if name == "Dubs Only" and not cfg.flag("quality.anime_block_dubs", True):
        return 0
    if name.startswith(("Anime BD Tier", "Anime Web Tier")) and not cfg.flag("quality.anime_release_groups", True):
        return 0
    return score


def scored_profile(profile: dict, formats: list[dict]) -> dict:
    """The profile with each format's score (0 outside its scope) and a
    minimum score of at least 0, which rejection relies on"""
    # The Anime profile and its fallback copies (Anime (+720p))
    is_anime = profile_origin(profile.get("name") or "") == ANIME_PROFILE
    scores = {f["id"]: (f["score"] if f["scope"] == "all" or (f["scope"] == "anime") == is_anime else 0) for f in formats}
    items = [dict(i, score=scores[i["format"]]) if i.get("format") in scores else i for i in profile.get("formatItems") or []]
    have = {i.get("format") for i in profile.get("formatItems") or []}
    items += [{"format": fid, "score": s} for fid, s in scores.items() if fid not in have]
    return dict(profile, minFormatScore=max(profile.get("minFormatScore") or 0, 0), formatItems=items)


def apply_junk_filters(cfg: Config, app: App, folder: str) -> None:
    """Create the custom formats in custom-formats/<app> (updating ones
    that already exist) and score them in the quality profiles. Filters
    score -10000 (never grabbed); a file can set its own score and limit
    itself to the Anime profile or the others."""
    try:
        existing = app.call("GET", "customformat") or []
    except ApiError:
        warn(f"{app.label}: couldn't read its custom formats; filters skipped (retried next run)")
        return
    formats = []
    for path in sorted((REPO / "custom-formats" / folder).glob("*.json")):
        payload, score, scope = custom_format(path)
        name = payload["name"]
        score = preference_score(cfg, name, score)
        current = next((x for x in existing if x.get("name") == name), None)
        try:
            if current:
                if comparable(current) != comparable(payload):
                    app.call("PUT", f"customformat/{current['id']}", dict(payload, id=current["id"]))
                fid = current["id"]
            else:
                fid = (app.call("POST", "customformat", payload) or {}).get("id")
                if fid is None:
                    raise ApiError("no id")
        except ApiError:
            warn(f"{app.label}: could not {'update' if current else 'create'} custom format {name}")
            continue
        formats.append({"id": fid, "score": score, "scope": scope})
    if not formats:
        return
    try:
        profiles = app.call("GET", "qualityprofile") or []
    except ApiError:
        profiles = []
    for profile in profiles:
        want = scored_profile(profile, formats)
        if want != profile:
            try:
                app.call("PUT", f"qualityprofile/{profile['id']}", want)
            except ApiError:
                warn(f"{app.label}: could not update profile {profile.get('name')}")
    extra = ", 3D" if folder == "radarr" else ""
    ok(f"{app.label}: release filters on (BR-DISK, LQ, Upscaled, Extras, Foreign Subtitles{extra})")
    flags = f"prefer HEVC {str(cfg.flag('quality.prefer_h265', True)).lower()}, English audio {str(cfg.flag('quality.prefer_english_audio', True)).lower()}"
    if folder == "sonarr":
        flags += (f"; anime: block dub-only releases {str(cfg.flag('quality.anime_block_dubs', True)).lower()}, "
                  f"rank release groups {str(cfg.flag('quality.anime_release_groups', True)).lower()}")
    ok(f"{app.label}: {flags}")


def ensure_anime_profile(cfg: Config, sonarr: App) -> None:
    """Sonarr's "Anime" profile: a copy of quality.sonarr_anime_profile,
    where anime-only filters apply (dub-only releases blocked) and TV
    preferences don't. Seerr's anime requests use it, and anime series
    still on the base profile move to it. Kept in step with the base
    profile's qualities."""
    try:
        profiles = sonarr.call("GET", "qualityprofile") or []
    except ApiError:
        warn("Sonarr: could not read quality profiles")
        return
    base = next((p for p in profiles if p.get("name") == cfg.sonarr_anime_profile), None)
    if not base:
        warn(f"Sonarr: profile '{cfg.sonarr_anime_profile}' not found; no Anime profile")
        return
    anime = next((p for p in profiles if p.get("name") == ANIME_PROFILE), None)
    try:
        if anime is None:
            sonarr.call("POST", "qualityprofile", dict({k: v for k, v in base.items() if k != "id"}, name=ANIME_PROFILE,
                                                       **anime_upgrades(cfg, base)))
            ok(f"Sonarr: '{ANIME_PROFILE}' profile created (from {cfg.sonarr_anime_profile})")
        else:
            synced = dict(anime, items=base.get("items"), **anime_upgrades(cfg, base))
            if synced != anime:
                sonarr.call("PUT", f"qualityprofile/{anime['id']}", synced)
    except ApiError:
        warn(f"Sonarr: could not {'create' if anime is None else 'update'} the Anime profile")
        if anime is None:
            return
    try:
        anime_id = next((p["id"] for p in sonarr.call("GET", "qualityprofile") or [] if p.get("name") == ANIME_PROFILE), None)
        moved = [x["id"] for x in sonarr.call("GET", "series") or []
                 if x.get("seriesType") == "anime" and x.get("qualityProfileId") == base["id"]]
        if anime_id is not None and moved:
            sonarr.call("PUT", "series/editor", {"seriesIds": moved, "qualityProfileId": anime_id})
            ok(f"Sonarr: {len(moved)} anime series moved to the '{ANIME_PROFILE}' profile")
    except ApiError:
        warn("Sonarr: could not move anime series to the Anime profile")


def lowest_allowed(items: list):
    """The id of a profile's lowest allowed quality (or group): items run
    from lowest to highest"""
    for i in items or []:
        if i.get("allowed"):
            return i.get("id") if i.get("items") else (i.get("quality") or {}).get("id")
    return None


def anime_upgrades(cfg: Config, base: dict) -> dict:
    """With anime_block_dubs: Sonarr replaces a file scored below 0 (a dub,
    a low-quality group) once a better-scored release is out, itself: it
    downloads and imports the new one, matched to the same episodes, before
    the old file goes, and postimport checks it. Upgrades by score only:
    the quality cutoff is the lowest allowed quality, so a file is never
    replaced just for a higher resolution."""
    if not cfg.flag("quality.anime_block_dubs", True):
        return {"cutoff": base.get("cutoff"), "upgradeAllowed": base.get("upgradeAllowed")}
    return {"upgradeAllowed": True, "cutoff": lowest_allowed(base.get("items") or []) or base.get("cutoff"),
            "cutoffFormatScore": 0}


def run_junk_filters(cfg: Config) -> None:
    info("Blocking junk releases...")
    sonarr, radarr = apps(cfg)
    if sonarr:
        ensure_anime_profile(cfg, sonarr)
        apply_junk_filters(cfg, sonarr, "sonarr")
    if radarr:
        apply_junk_filters(cfg, radarr, "radarr")


# ─── The old anime Sonarr ────────────────────────────────────────
# Versions up to the tag below ran a second Sonarr for anime and merged it
# into Sonarr on install; this one no longer does.
LAST_WITH_MIGRATION = "last-with-anime-sonarr-migration"


def require_no_unmerged_anime_sonarr(cfg: Config) -> None:
    """Stop rather than leave the old anime Sonarr's series behind unnoticed"""
    old = cfg.paths.config / "sonarr-anime"
    if (old / "sonarr.db").exists() and not (cfg.paths.state / "sonarr-anime-migrated").exists():
        raise err(f"{old} holds the separate anime Sonarr of an older version, and its series were never merged into Sonarr. "
                  f"This version no longer merges them. Merge them with the last version that does: "
                  f"git checkout {LAST_WITH_MIGRATION} && nix run .#install, then git checkout main and install again. "
                  f"If you don't need its series, delete {old} instead.")


def series_settled(sonarr: App, series_id: int, seconds: int = 180) -> bool:
    """Sonarr has finished adding a series: its episodes are listed and no
    refresh or scan is pending. Only then are the add options (such as
    monitor "none") applied, so monitoring changed earlier gets overwritten."""
    start = time.time()
    while time.time() - start < seconds:
        try:
            episodes = sonarr.call("GET", f"episode?seriesId={series_id}") or []
            commands = sonarr.call("GET", "command") or []
            busy = any(x.get("name") in ("RefreshSeries", "RescanSeries") and x.get("status") in ("queued", "started") for x in commands)
            if episodes and not busy:
                return True
        except ApiError:
            pass
        time.sleep(2)
    return False
