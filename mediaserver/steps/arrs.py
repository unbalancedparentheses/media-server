"""Sonarr (TV and anime) and Radarr (movies): root folders, download
clients, the Jellyfin connection, logins, file naming, minimum free space,
the release filters, and moving an older separate anime Sonarr's series
into Sonarr."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from mediaserver import api
from mediaserver import common as c
from mediaserver.api import ApiError
from mediaserver.arr import set_login, sync_fields
from mediaserver.config import Config, Keys
from mediaserver.pins import ANIME_PROFILE
from mediaserver.ui import info, ok, warn

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
    if sonarr:
        migrate_anime_sonarr(cfg, sonarr)
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
    is_anime = profile.get("name") == ANIME_PROFILE
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
            sonarr.call("POST", "qualityprofile", dict({k: v for k, v in base.items() if k != "id"}, name=ANIME_PROFILE))
            ok(f"Sonarr: '{ANIME_PROFILE}' profile created (from {cfg.sonarr_anime_profile})")
        else:
            synced = dict(anime, items=base.get("items"), cutoff=base.get("cutoff"), upgradeAllowed=base.get("upgradeAllowed"))
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


def run_junk_filters(cfg: Config) -> None:
    info("Blocking junk releases...")
    sonarr, radarr = apps(cfg)
    if sonarr:
        ensure_anime_profile(cfg, sonarr)
        apply_junk_filters(cfg, sonarr, "sonarr")
    if radarr:
        apply_junk_filters(cfg, radarr, "radarr")


# ─── The old anime Sonarr ────────────────────────────────────────

def migrate_anime_sonarr(cfg: Config, sonarr: App) -> None:
    """Older versions ran a second Sonarr for anime. Add its series to
    Sonarr (as anime, keeping their folders, no search), copy its series,
    season and episode monitoring exactly, and rescan so the existing files
    are picked up. Its data folder is left in place.

    Progress is kept per series in sonarr-anime-migration.json: "adding"
    just before asking Sonarr, "added" (id in Sonarr) once it accepted,
    "done" once its monitoring is copied and checked, so an interrupted run
    finishes the series it started, even one interrupted between Sonarr
    adding it and the progress being saved. Series that were already in
    Sonarr aren't touched."""
    db = cfg.paths.config / "sonarr-anime/sonarr.db"
    marker = cfg.paths.state / "sonarr-anime-migrated"
    progress = cfg.paths.state / "sonarr-anime-migration.json"
    if not db.exists() or marker.exists():
        return
    info("Moving the old anime Sonarr's series into Sonarr...")
    try:
        rows = query(db, "SELECT Id, TvdbId, Path, Monitored, Seasons FROM Series")
    except sqlite3.Error:
        warn(f"Could not read {db}; its series were not moved (setup retries next run)")
        return
    try:
        have = sonarr.call("GET", "series") or []
    except ApiError:
        warn("Could not list Sonarr series")
        return
    state: dict = {"adding": [], "added": {}, "done": []}
    if progress.exists():
        saved = c.read_json(progress)
        if not isinstance(saved, dict):
            warn(f"Can't read {progress}; the migration waits until it's fixed or removed")
            return
        state = {"adding": saved.get("adding") or [], "added": saved.get("added") or {}, "done": saved.get("done") or []}

    def save() -> None:
        c.write_json(progress, state, mode=0o600)
    try:
        profiles = sonarr.call("GET", "qualityprofile") or []
    except ApiError:
        profiles = []
    profile_id = next((p["id"] for p in profiles if p.get("name") == ANIME_PROFILE), None) \
        or next((p["id"] for p in profiles if p.get("name") == cfg.sonarr_anime_profile), None) \
        or (profiles[0]["id"] if profiles else None)
    failed = added = 0
    for row in rows:
        old_id, tvdb, path = row["Id"], str(row["TvdbId"]), row["Path"]
        if tvdb in state["done"]:
            continue
        new_id = state["added"].get(tvdb)
        # Added by an earlier, interrupted run: still there?
        if new_id is not None and not any(s.get("id") == new_id for s in have):
            new_id = None
        # Sonarr has it, and this migration was adding it: it's ours to finish
        if new_id is None and tvdb in state["adding"]:
            new_id = next((s["id"] for s in have if str(s.get("tvdbId")) == tvdb), None)
            if new_id is not None:
                state["added"][tvdb] = new_id
                save()
        if new_id is None:
            if any(str(s.get("tvdbId")) == tvdb for s in have):
                # Already in Sonarr before the migration: leave it as it is
                state["done"].append(tvdb)
                save()
                continue
            try:
                lookup = next(iter(sonarr.call("GET", f"series/lookup?term=tvdb:{tvdb}") or []), None)
            except ApiError:
                lookup = None
            # Recorded before asking Sonarr, so an interruption right after
            # it adds the series doesn't make it look like one you already had
            state["adding"] = sorted(set(state["adding"]) | {tvdb})
            save()
            # Added unmonitored with "skip" (Sonarr leaves episode monitoring
            # alone); the old choices are applied below
            try:
                new = sonarr.call("POST", "series", dict(lookup, path=path, qualityProfileId=profile_id, monitored=False, seriesType="anime",
                                                         seasonFolder=True, addOptions={"searchForMissingEpisodes": False, "monitor": "skip"})) if lookup else None
            except ApiError:
                new = None
            new_id = (new or {}).get("id") if isinstance(new, dict) else None
            if new_id is None:
                warn(f"Could not add the series at {path} (TVDB {tvdb}); retried next run")
                failed = 1
                continue
            state["added"][tvdb] = new_id
            save()
            added += 1
        if migrate_monitoring(sonarr, db, old_id, new_id, row):
            state["done"].append(tvdb)
            save()
            ok(f"Moved: {Path(path).name}")
        else:
            warn(f"Series at {path} is in Sonarr, but its monitoring wasn't copied yet; retried next run")
            failed = 1
    if added:
        try:
            sonarr.call("POST", "command", {"name": "RescanSeries"})
        except ApiError:
            pass
    if not failed:
        c.write_atomic(marker, "")
        ok(f"Old anime Sonarr's series are in Sonarr; you can delete {cfg.paths.config / 'sonarr-anime'}")


def query(db: Path, sql: str) -> list[dict]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql)]


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


def migrate_monitoring(sonarr: App, db: Path, old_id: int, new_id: int, row: dict) -> bool:
    """Copy one series' monitoring from the old database: episodes, seasons
    and the series flag, once Sonarr has finished adding it (its refresh and
    scan would otherwise overwrite them), then check it took"""
    if not series_settled(sonarr, new_id):
        warn(f"Sonarr is still adding series {new_id}")
        return False
    try:
        old = {f"{e['s']}x{e['e']}": e["m"] == 1 for e in
               query(db, f"SELECT SeasonNumber AS s, EpisodeNumber AS e, Monitored AS m FROM Episodes WHERE SeriesId = {int(old_id)}")}
        episodes = sonarr.call("GET", f"episode?seriesId={new_id}") or []
        for monitored in (True, False):
            ids = [e["id"] for e in episodes if old.get(f"{e['seasonNumber']}x{e['episodeNumber']}") is monitored]
            if ids:
                sonarr.call("PUT", "episode/monitor", {"episodeIds": ids, "monitored": monitored})
        series = sonarr.call("GET", f"series/{new_id}")
        seasons = {str(s["seasonNumber"]): s["monitored"] for s in json.loads(row.get("Seasons") or "[]")}
        series["monitored"] = row.get("Monitored") == 1
        series["seasons"] = [dict(s, monitored=seasons[str(s["seasonNumber"])]) if str(s["seasonNumber"]) in seasons else s
                             for s in series.get("seasons") or []]
        sonarr.call("PUT", f"series/{new_id}", series)
        # Every episode both databases know must now match
        episodes = sonarr.call("GET", f"episode?seriesId={new_id}") or []
    except (ApiError, sqlite3.Error, ValueError, KeyError, TypeError):
        return False
    if all(old.get(f"{e['seasonNumber']}x{e['episodeNumber']}") in (None, e.get("monitored")) for e in episodes):
        return True
    warn(f"Episode monitoring of series {new_id} didn't match the old Sonarr's")
    return False
