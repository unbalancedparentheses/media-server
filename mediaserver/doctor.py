"""nix run .#doctor: a read-only look at whether things are *working*, not
just configured (that's `test`). Each finding says what was observed and
suggests what to do; it changes nothing. Findings are facts about files and
services; whether they're a problem can depend on your preferences.
Exit status 1 when something needs attention."""
from __future__ import annotations

import re
import shutil
import time
from datetime import datetime

from mediaserver import common as c
from mediaserver import launchd
from mediaserver.config import Config, Keys
from mediaserver.ui import info

JF_CLIENT = 'MediaBrowser Client="doctor", Device="script", DeviceId="doctor", Version="1.0"'
HEALTH_SOURCES = re.compile(r"Indexer|DownloadClient|RootFolder|ImportMechanism")


def iso_time(s: str) -> float:
    return datetime.fromisoformat(re.sub(r"\.\d+Z$", "Z", s).replace("Z", "+00:00")).timestamp()


class Doctor:
    def __init__(self, cfg: Config):
        self.cfg, self.urls, self.paths = cfg, cfg.urls, cfg.paths
        self.keys = Keys.read(self.paths.config)
        self.attention: list[tuple[str, str]] = []
        self.notes: list[tuple[str, str]] = []
        self.fine: list[str] = []

    def need(self, finding: str, suggestion: str = "") -> None:
        self.attention.append((finding, suggestion))

    def note(self, finding: str, suggestion: str = "") -> None:
        self.notes.append((finding, suggestion))

    def good(self, finding: str) -> None:
        self.fine.append(finding)

    def run(self) -> int:
        info("Doctor (read-only)")
        for part in (self.services, self.connection, self.indexers, self.downloads, self.subtitles,
                     self.library, self.postimport, self.disk, self.records):
            try:
                part()
            except (KeyError, IndexError, TypeError, ValueError, AttributeError, *c.HTTP_ERRORS) as e:
                self.note(f"Couldn't check {part.__name__}: {e}")
        print()
        for title, color, items, mark in (("Needs attention", "31", self.attention, "✗"), ("Worth knowing", "33", self.notes, "!")):
            if items:
                print(f"\033[1;{color}m  {title}\033[0m")
                for finding, suggestion in items:
                    print(f"   {mark} {finding}")
                    if suggestion:
                        print(f"     → {suggestion}")
                print()
        print("\033[1;32m  Looks fine\033[0m")
        for finding in self.fine:
            print(f"   ✓ {finding}")
        print()
        return 1 if self.attention else 0

    # ─── Parts ───────────────────────────────────────────────────

    def services(self) -> None:
        down = [f"{n} ({s})" for n in launchd.SERVICE_NAMES if not (s := launchd.state(n)).startswith("running")]
        if down:
            self.need("Not running: " + " ".join(down), "nix run .#logs -- <service> to see why, then nix run .#restart -- <service>")
        else:
            self.good("All services running")

    def connection(self) -> None:
        conn = c.read_text(self.paths.state / "netwatch/connection") or "unknown"
        if conn == "offline":
            self.note("netwatch sees the Mac offline: Cleanuparr is paused and nothing can download", "It resumes on its own when the connection is back")
        elif conn == "online":
            self.good("Online (netwatch)")
        else:
            self.note("netwatch hasn't determined the connection yet")

    def indexers(self) -> None:
        h = {"X-Api-Key": self.keys.prowlarr}
        statuses = c.try_json(f"{self.urls.prowlarr}/api/v1/indexerstatus", h, None, timeout=60)
        if statuses is None:
            self.need("Couldn't read Prowlarr's indexer status", "Is Prowlarr running? nix run .#status")
            return
        indexers = c.try_json(f"{self.urls.prowlarr}/api/v1/indexer", h, [], timeout=60) or []
        names = {i["id"]: i["name"] for i in indexers}
        on = {i["id"] for i in indexers if i.get("enable")}
        now = time.time()
        # Switched-off indexers keep their last failure; only enabled ones count
        off = [f"{names.get(s['indexerId'], 'indexer %s' % s['indexerId'])} (until {datetime.fromtimestamp(iso_time(s['disabledTill'])).strftime('%a %H:%M')})"
               for s in statuses if s.get("indexerId") in on and s.get("disabledTill") and iso_time(s["disabledTill"]) > now]
        if off:
            self.need("Prowlarr switched off indexers after failures: " + ", ".join(off),
                      "Usually temporary (a site down, blocked, or you were offline). Prowlarr → Indexers → Test All clears the ones that work again")
        else:
            self.good("Indexers: none switched off")
        for app, url, key in (("Sonarr", self.urls.sonarr, self.keys.sonarr), ("Radarr", self.urls.radarr, self.keys.radarr)):
            health = c.try_json(f"{url}/api/v3/health", {"X-Api-Key": key}, [], timeout=60) or []
            problems = [x for x in health if x.get("type") == "error" or HEALTH_SOURCES.search(x.get("source") or "")]
            if not problems:
                self.good(f"{app}: no health problems")
            # Errors stop things working; warnings (e.g. an indexer that was
            # rate-limited or unreachable recently) usually clear on their own
            for x in problems:
                if x.get("type") == "error":
                    self.need(f"{app}: {x.get('message')}", f"{app} → System → Status explains it")
                else:
                    self.note(f"{app}: {x.get('message')}", "Often rate limits or a site that was briefly down; clears after the next successful search")

    def downloads(self) -> None:
        torrents = c.try_json(f"{self.urls.qbittorrent}/api/v2/torrents/info", default=None, timeout=60)
        if torrents is None:
            self.need("Couldn't read qBittorrent's downloads", "Is qBittorrent running? nix run .#status")
            return
        now = time.time()
        stuck = [t for t in torrents if t["progress"] < 1 and t["state"] in ("stalledDL", "metaDL") and now - t["added_on"] > 86400]
        for t in stuck:
            self.need(f"Download not moving for over a day: {t['name'][:60]} ({int(t['progress'] * 100)}%, {t['num_seeds']} seeders, "
                      f"added {int((now - t['added_on']) / 86400)} days ago)",
                      "Sonarr/Radarr → Activity → remove it with \"Blocklist release\" so another one is tried (Cleanuparr does this for public torrents once they count as stalled)")
        if not stuck:
            self.good("Downloads: none stuck for more than a day")
        for app, url, key in (("Sonarr", self.urls.sonarr, self.keys.sonarr), ("Radarr", self.urls.radarr, self.keys.radarr)):
            queue = c.try_json(f"{url}/api/v3/queue?pageSize=200", {"X-Api-Key": key}, {}, timeout=60) or {}
            for r in queue.get("records") or []:
                if r.get("trackedDownloadStatus") not in ("ok", None):
                    messages = [m for s in r.get("statusMessages") or [] for m in s.get("messages") or []]
                    self.need(f"{app} can't finish: {r.get('title', '')[:60]}: {messages[0] if messages else r.get('trackedDownloadState')}",
                              f"{app} → Activity → Queue shows the details (often a failed import: manual import or blocklist)")

    def subtitles(self) -> None:
        key = c.bazarr_key(self.paths.config)
        if not key:
            self.note("Couldn't read Bazarr's API key")
            return
        episodes = (c.try_json(f"{self.urls.bazarr}/api/episodes/wanted?apikey={key}&length=500", default={}, timeout=60) or {}).get("data") or []
        movies = (c.try_json(f"{self.urls.bazarr}/api/movies/wanted?apikey={key}&length=500", default={}, timeout=60) or {}).get("data") or []
        counts: dict[str, int] = {}
        for e in episodes:
            counts[e["seriesTitle"]] = counts.get(e["seriesTitle"], 0) + 1
        series = ", ".join(f"{t} ({n})" for t, n in sorted(counts.items()))
        films = ", ".join(m["title"] for m in movies)
        if series or films:
            parts = ([f"episodes of {series}"] if series else []) + ([f"films: {films}"] if films else [])
            self.note("Still without subtitles in your language: " + "; ".join(parts),
                      "The free providers don't have them (yet). An OpenSubtitles.com account (in Bazarr, then config.toml) finds most")
        else:
            self.good("Subtitles: nothing missing")

    def jellyfin_token(self) -> str:
        r = c.request(f"{self.urls.jellyfin}/Users/AuthenticateByName", "POST", {"Authorization": JF_CLIENT},
                      body={"Username": self.cfg.jellyfin_user, "Pw": self.cfg.jellyfin_pass})
        return (r.json({}) or {}).get("AccessToken") or ""

    def library(self) -> None:
        """Facts about the files in Jellyfin's libraries"""
        token = self.jellyfin_token()
        if not token:
            self.note("Couldn't log in to Jellyfin to look at the library")
            return
        auth = {"Authorization": f'MediaBrowser Token="{token}"'}
        libs = c.try_json(f"{self.urls.jellyfin}/Library/VirtualFolders", auth, [], timeout=60) or []
        # Anime made in Japanese (Sonarr's original language) whose files have
        # no Japanese audio track, i.e. dubs; shows made in English aren't counted
        anime = next((lib["ItemId"] for lib in libs if lib.get("Name") == "Anime"), None)
        if anime:
            items = (c.try_json(f"{self.urls.jellyfin}/Items?Recursive=true&IncludeItemTypes=Episode&Fields=MediaStreams&ParentId={anime}",
                                auth, {}, timeout=60) or {}).get("Items") or []
            series = c.try_json(f"{self.urls.sonarr}/api/v3/series", {"X-Api-Key": self.keys.sonarr}, [], timeout=60) or []
            japanese = {s["title"] for s in series if s.get("seriesType") == "anime" and (s.get("originalLanguage") or {}).get("name") == "Japanese"}
            dubs: dict[str, int] = {}
            for item in items:
                langs = [s.get("Language") for s in item.get("MediaStreams") or [] if s.get("Type") == "Audio"]
                if item.get("SeriesName") in japanese and "jpn" not in langs:
                    dubs[item["SeriesName"]] = dubs.get(item["SeriesName"], 0) + 1
            if dubs:
                self.note("Anime with no Japanese audio track: " + ", ".join(f"{t} ({n} episodes)" for t, n in sorted(dubs.items())),
                          "If you want Japanese audio, Sonarr → the series → Interactive Search for a Dual Audio or Japanese release (delete the current files first)")
            else:
                self.good("Anime made in Japanese: every episode has Japanese audio")
        # Preferred-language subtitles only as pictures: browsers can't show
        # them, so Jellyfin burns them in (re-encoding the video) if selected
        lang = self.cfg.get("playback.subtitle_language", "eng")
        items = (c.try_json(f"{self.urls.jellyfin}/Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=MediaStreams",
                            auth, {}, timeout=60) or {}).get("Items") or []
        pictures = set()
        for item in items:
            subs = [s for s in item.get("MediaStreams") or [] if s.get("Type") == "Subtitle" and s.get("Language") == lang]
            if subs and not any(s.get("IsTextSubtitleStream") for s in subs):
                pictures.add(item.get("SeriesName") or item.get("Name"))
        if pictures:
            self.note(f'Subtitles in "{lang}" only as pictures (Blu-ray/DVD) in: ' + ", ".join(sorted(pictures)),
                      "In a browser, Jellyfin has to burn those into the video (heavy on the CPU). Bazarr looks for a text version; until then, pick another track or turn subtitles off")
        else:
            self.good(f'Subtitles in "{lang}": all available as text where present')

    def postimport(self) -> None:
        """What the checks after each download found and did"""
        status = c.read_json(self.paths.state / "postimport/status.json")
        if not status:
            self.note("The checks after each download haven't run yet", "They start with the postimport service; nix run .#install sets it up")
            return
        age = time.time() - status.get("updated", 0)
        # A round can take a while (rewriting a large file), hence the margin
        if age > 3600:
            self.note(f"The checks after each download last ran {int(age / 60)} minutes ago", "nix run .#logs postimport shows why")
        looking = "; ".join(f"{r['title']} ({r['reason']})" for r in status.get("looking") or [])
        kept = "; ".join(f"{r['title']} ({r['reason']})" for r in status.get("kept") or [])
        if looking:
            self.note(f"Downloads rejected, another release is being looked for: {looking}",
                      "Nothing to do; you get a notification if nothing better turns up within 6 hours")
        if kept:
            self.note(f"Kept although no better release was found: {kept}", "Sonarr/Radarr → the title → Interactive Search, to pick one by hand")
        recent = sum(1 for r in status.get("recent") or [] if r.get("time", 0) > time.time() - 86400)
        self.good(f"Checks after each download: running ({recent} fixes in the last day)")

    def disk(self) -> None:
        free = shutil.disk_usage(self.paths.media).free // 1024 ** 3
        min_gb, warn_gb = self.cfg.disk_min_gb, self.cfg.disk_warn_gb
        if free < min_gb:
            self.need(f"Only {free} GB free: Sonarr and Radarr stop importing below {min_gb} GB", "Delete something in Sonarr or Radarr (with \"Delete files\")")
        elif free < warn_gb:
            self.note(f"{free} GB free on the media disk", f"Imports stop below {min_gb} GB")
        else:
            self.good(f"{free} GB free on the media disk")

    def records(self) -> None:
        """Work that an interrupted operation left unfinished"""
        state = self.paths.state
        if (state / "e2e/paused-indexers.json").exists():
            self.need("An end-to-end test left Radarr indexers paused", "Run nix run .#install (it restores them), or re-enable automatic search in Radarr → Settings → Indexers")
        if (state / "e2e/owned.json").exists():
            self.note(f"An end-to-end test didn't finish cleaning up (record: {state / 'e2e/owned.json'})", "The next nix run .#e2e removes its leftovers")
        if (self.paths.config / "sonarr-anime/sonarr.db").exists() and not (state / "sonarr-anime-migrated").exists():
            self.need("The old anime Sonarr's series were never merged into Sonarr",
                      "nix run .#install explains how (with the last version that merges them)")
        owner = c.read_text(state / "lock/pid")
        if owner:
            if c.operation_running(state / "lock"):
                self.note(f"An operation is running right now (PID {owner})")
            else:
                self.note("A previous operation stopped before finishing (stale lock)", "Harmless; the next one takes it over")


def main(cfg: Config | None = None) -> int:
    return Doctor(cfg or Config.load()).run()
