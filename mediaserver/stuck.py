"""Requests that aren't arriving: why, and bounded recovery.

Sonarr and Radarr search once when something is added, then only take what
shows up in their RSS feeds; they never search again for what's still
missing. So, run from the postimport service:

- Recovery: films and seasons still missing (released, monitored) are
  searched again through Sonarr/Radarr, so your quality profiles, language
  and size rules and release filters all apply as always. Each one waits
  longer after every search (1 h, 2 h, 4 h, … up to a day). At most
  SEARCHES_PER_RUN search commands an hour (a season search is one command,
  though Sonarr may ask the indexers several times for it), never while
  offline or during an install. Indexers switched off after failures are
  re-tested every 6 hours. A title that's downloading keeps its history,
  so a failed download doesn't restart its schedule.
- Diagnosis: for one missing for over a day, a release search that grabs
  nothing (Sonarr/Radarr's interactive search) shows what's out there and
  why each release was turned down: nothing found, indexers down, quality,
  language, size, the release filters, no seeders, blocklisted. That's what
  the dashboard shows, with what to do.
- Lower resolution only if you ask for it: with quality.fallback_resolution
  ("720p" or "480p"), a title stuck for a week whose releases are turned
  down only for their quality gets a copy of its own profile that also
  allows that resolution; everything else in the profile (custom formats,
  language, cutoff, minimum score) stays as it was. Sonarr sets profiles
  per series, so for a series the copy applies to all of it.
- Dubbed anime already in the library (Japanese anime whose file has no
  Japanese audio, as Sonarr read the file): nothing is deleted here. Setup
  lets Sonarr replace files scored below 0 (its Dubs Only format) once a
  better release is out, which it does safely: the new release is
  downloaded and imported, matched to the same episodes, before the old
  file goes, and postimport checks it (within max_replacements). This asks
  Sonarr to search such a season when a look-only search finds a Japanese
  or Dual Audio release it would take, with seeders; every 12 hours, one
  season an hour. Dubs Sonarr doesn't score as dubs (their release names
  don't say so) are only reported: pick a release by hand.
  quality.anime_block_dubs = false turns it off.
- library.search_missing = false turns all of it off: nothing here asks
  the indexers anything.

State: stuck.json next to postimport's own record.
"""
from __future__ import annotations

import re
import time
from typing import Any

from mediaserver import common as c
from mediaserver.pins import fallback_name, profile_origin

RUN_EVERY = 3600
SEARCHES_PER_RUN = 3
DIAGNOSES_PER_RUN = 2           # look-only release searches an hour
DIAGNOSE_AFTER = 86400          # missing this long before it's looked into
DIAGNOSE_EVERY = 12 * 3600      # and not again sooner (it queries the indexers)
MAX_WAIT = 86400                # the longest pause between searches
RETEST_INDEXERS_EVERY = 6 * 3600
# A release search asks every indexer: it can take a minute or more
RELEASE_SEARCH_TIMEOUT = 180
FALLBACK_AFTER = 7 * 86400

# Words in Sonarr/Radarr's rejection reasons → what kind of reason it is
REASONS = [
    ("queued", ("in queue",)),
    ("blocklisted", ("blocklist",)),
    ("size", ("size", "larger than", "smaller than")),
    ("language", ("language",)),
    ("filters", ("custom format", "score")),
    ("peers", ("seeder", "peers")),
    ("quality", ("quality", "not wanted in profile")),
    ("mismatch", ("unknown", "wasn't requested", "not monitored", "match", "parse")),
]
EXPLAIN = {
    "no_results": ("No releases found on your indexers",
                   "Searched again regularly; Interactive Search later, or add indexers in Prowlarr"),
    "indexers_down": ("Your indexers aren't answering",
                      "Being re-tested every 6 hours; Prowlarr → Indexers → Test All to try now"),
    "quality": ("Releases found, but none in a quality your profile takes",
                "Interactive Search to pick one, or set quality.fallback_resolution to accept lower"),
    "language": ("Releases found, but none in your language",
                 "Interactive Search to pick one; dual-audio releases count"),
    "size": ("Releases found, but all outside the size limits", "Interactive Search to pick one"),
    "filters": ("Releases found, but all blocked by the release filters (junk, low score)",
                "Interactive Search to see them; the filters are in [quality]"),
    "peers": ("Releases found, but nobody is sharing them (no seeders)", "Searched again regularly; new uploads appear"),
    "blocklisted": ("The releases found already failed once (blocklisted)", "Searched again regularly for new ones"),
    "mismatch": ("Releases found, but they don't match this title", "Interactive Search to check"),
    "usable": ("A usable release is out there", "It's grabbed with the next search"),
    "queued": ("A release is already downloading", "See its progress under Download"),
}


JAPANESE_TITLE = re.compile(r"dual[ ._-]?audio|japanese|\bjpn\b|\bjap\b", re.I)
# Rejections that only say the dub on disk is as good (not why a release
# is bad): a Japanese release turned down only for these is still takeable
REPLACEABLE = ("existing file", "not an upgrade", "upgrade for existing")


def japanese_release(r: dict) -> bool:
    langs = {(x.get("name") or "").lower() for x in r.get("languages") or []}
    return "japanese" in langs or bool(JAPANESE_TITLE.search(r.get("title") or ""))


def takeable(r: dict) -> bool:
    """Sonarr would take it but for the file already there, and it can
    actually be downloaded"""
    if r.get("protocol") == "torrent" and not (r.get("seeders") or 0):
        return False
    if r.get("approved"):
        return True
    rejections = [x.lower() for x in r.get("rejections") or []]
    return bool(rejections) and all(any(w in x for w in REPLACEABLE) for x in rejections)


def has_japanese_audio(f: dict) -> bool | None:
    """From Sonarr's reading of the file; None when it doesn't know"""
    langs = ((f.get("mediaInfo") or {}).get("audioLanguages") or "").lower()
    if not langs.strip():
        return None
    return any(x in langs for x in ("jpn", "japanese", "jap"))


def kind_of(reason: str) -> str:
    low = reason.lower()
    return next((k for k, words in REASONS if any(w in low for w in words)), "other")


def classify(releases: list, indexers_down: bool) -> dict:
    """{"code", "text", "action", "found", …} from an interactive search's results"""
    if not releases:
        code = "indexers_down" if indexers_down else "no_results"
        text, action = EXPLAIN[code]
        return {"code": code, "text": text, "action": action, "found": 0}
    approved = [r for r in releases if r.get("approved")]
    if approved:
        alive = [r for r in approved if r.get("protocol") != "torrent" or (r.get("seeders") or 0) > 0]
        code = "usable" if alive else "peers"
        text, action = EXPLAIN[code]
        return {"code": code, "text": text, "action": action, "found": len(releases)}
    counts: dict = {}
    for r in releases:
        kinds = {kind_of(x) for x in r.get("rejections") or []} or {"other"}
        for k in kinds:
            counts[k] = counts.get(k, 0) + 1
    code = max(counts, key=lambda k: (counts[k], k != "other"))
    out = {"found": len(releases), "kinds": counts}
    if code == "other":
        reason = next((x for r in releases for x in r.get("rejections") or []), "turned down")
        out.update(code="other", text=f"{len(releases)} releases found, none suitable: {reason}",
                   action="Interactive Search to see them")
        return out
    text, action = EXPLAIN[code]
    if code == "quality":
        qualities = sorted({((r.get("quality") or {}).get("quality") or {}).get("name", "") for r in releases} - {""})
        if qualities:
            text += f" (found: {', '.join(qualities[:4])})"
    out.update(code=code, text=f"{text} ({len(releases)} found)" if code != "quality" else text, action=action,
               only_quality=set(counts) == {"quality"})
    return out


def timestamp(iso: str | None) -> float | None:
    """Sonarr/Radarr's "2026-09-28T12:00:00Z" as seconds"""
    if not iso:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def wait_after(searches: int) -> int:
    return min(3600 * 2 ** max(searches - 1, 0), MAX_WAIT)


class Stuck:
    def __init__(self, apps: list, settings: dict, state: dict, now: float | None = None):
        self.apps = {a.name: a for a in apps}
        self.settings, self.state = settings, state
        self.now = time.time() if now is None else now
        state.setdefault("items", {})

    # ─── What's missing ──────────────────────────────────────────

    def downloading(self) -> set:
        """Keys of the films and seasons with something in the download queue"""
        out: set = set()
        radarr, sonarr = self.apps.get("Radarr"), self.apps.get("Sonarr")
        if radarr:
            out |= {f"radarr:{q.get('movieId')}" for q in (radarr.call("GET", "queue?pageSize=500") or {}).get("records") or []}
        if sonarr:
            records = (sonarr.call("GET", "queue?pageSize=500&includeEpisode=true") or {}).get("records") or []
            out |= {f"sonarr:{q.get('seriesId')}:{(q.get('episode') or {}).get('seasonNumber', q.get('seasonNumber'))}" for q in records}
        return out

    def missing(self) -> dict:
        """key → {"app", "title", "query", "search", "series"/"movie"}:
        released, monitored films without a file; aired, monitored episodes
        grouped by season (one season search finds packs and episodes)"""
        out: dict = {}
        radarr, sonarr = self.apps.get("Radarr"), self.apps.get("Sonarr")
        if radarr:
            # Already downloading isn't missing: not searched or diagnosed
            downloading = {q.get("movieId") for q in (radarr.call("GET", "queue?pageSize=500") or {}).get("records") or []}
            for m in radarr.call("GET", "movie") or []:
                if m.get("monitored") and not m.get("hasFile") and m.get("isAvailable") and m["id"] not in downloading:
                    out[f"radarr:{m['id']}"] = {"app": "Radarr", "title": f"{m.get('title')} ({m.get('year')})", "movie": m["id"],
                                                "added": timestamp(m.get("added")),
                                                "query": f"release?movieId={m['id']}",
                                                "search": {"name": "MoviesSearch", "movieIds": [m["id"]]}}
        if sonarr:
            downloading = {q.get("episodeId") for q in (sonarr.call("GET", "queue?pageSize=500") or {}).get("records") or []}
            page = sonarr.call("GET", "wanted/missing?page=1&pageSize=500&monitored=true&includeSeries=true") or {}
            for e in page.get("records") or []:
                if e.get("id") in downloading:
                    continue
                sid, season = e.get("seriesId"), e.get("seasonNumber")
                key = f"sonarr:{sid}:{season}"
                item = out.setdefault(key, {"app": "Sonarr", "series": sid, "season": season, "episodes": 0,
                                            "title": f"{(e.get('series') or {}).get('title', 'Series')} season {season}",
                                            "query": f"release?seriesId={sid}&seasonNumber={season}",
                                            "search": {"name": "SeasonSearch", "seriesId": sid, "seasonNumber": season}})
                item["episodes"] += 1
                aired = timestamp(e.get("airDateUtc"))
                if aired and (item.get("added") is None or aired < item["added"]):
                    item["added"] = aired
        return out

    def indexers_down(self, app) -> bool:
        """Every enabled indexer switched off after failures"""
        try:
            enabled = [i for i in app.call("GET", "indexer") or [] if i.get("enableAutomaticSearch") or i.get("enableRss")]
            statuses = app.call("GET", "indexerstatus") or []
        except c.HTTP_ERRORS:
            return False
        off = {s.get("indexerId") for s in statuses if (s.get("disabledTill") or "") > time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.now))}
        return bool(enabled) and all(i["id"] in off for i in enabled)

    # ─── A round ─────────────────────────────────────────────────

    def run(self) -> None:
        if not self.settings.get("search_missing", True):
            # Off: nothing asks the indexers, and no old schedule is shown
            self.state.clear()
            self.state["items"] = {}
            return
        if "last_run" in self.state and self.now - self.state["last_run"] < RUN_EVERY:
            return
        self.state["last_run"] = self.now
        missing, downloading = self.missing(), self.downloading()
        items = self.state["items"]
        for key, item in list(items.items()):
            if key in downloading and key not in missing:
                item["downloading"] = True   # kept: if it fails, the schedule goes on
            elif key not in missing:
                items.pop(key)   # arrived, unmonitored or deleted
            else:
                item.pop("downloading", None)
        for key, m in missing.items():
            # Missing since it was added (or aired), not since we first looked
            added = m.get("added")
            since = min(self.now, added if added is not None else self.now)
            item = items.setdefault(key, {"since": since, "searches": 0, "next_search": self.now})
            item.update(title=m["title"], app=m["app"], **{k: m[k] for k in ("movie", "series", "season", "episodes") if k in m})
        self.search(missing)
        self.diagnose(missing)
        if self.settings.get("block_dubs", True):
            self.replace_dubs(downloading)
        self.fallback(missing)
        self.retest_indexers()

    def search(self, missing: dict) -> None:
        items = self.state["items"]
        due = sorted((k for k in missing if items[k]["next_search"] <= self.now), key=lambda k: items[k]["next_search"])
        for key in due[:SEARCHES_PER_RUN]:
            m, item = missing[key], items[key]
            try:
                self.apps[m["app"]].call("POST", "command", m["search"])
            except c.HTTP_ERRORS:
                continue   # tried again next round
            item["searches"] += 1
            item["last_search"] = self.now
            item["next_search"] = self.now + wait_after(item["searches"])
            c.log(f"searching again for {m['title']} (search {item['searches']}; next in {wait_after(item['searches']) // 3600} h)")

    def diagnose(self, missing: dict) -> None:
        items = self.state["items"]
        due = [k for k in missing if self.now - items[k]["since"] >= DIAGNOSE_AFTER
               and self.now - items[k].get("diagnosed_at", 0) >= DIAGNOSE_EVERY]
        due.sort(key=lambda k: items[k].get("diagnosed_at", 0))
        for key in due[:DIAGNOSES_PER_RUN]:
            m, item = missing[key], items[key]
            app = self.apps[m["app"]]
            try:
                releases = app.call("GET", m["query"], timeout=RELEASE_SEARCH_TIMEOUT) or []
            except c.HTTP_ERRORS as e:
                c.log(f"{m['title']}: couldn't look at the releases ({e})")
                continue
            item["diagnosis"] = classify(releases if isinstance(releases, list) else [], self.indexers_down(app))
            item["diagnosed_at"] = self.now
            if item["diagnosis"]["code"] == "usable":
                item["next_search"] = self.now   # it'd be grabbed: search now rather than wait
            c.log(f"{m['title']}: {item['diagnosis']['text']}")

    def fallback(self, missing: dict) -> None:
        """quality.fallback_resolution: only when set, after a week of
        releases turned down for nothing but their quality"""
        resolution = self.settings.get("fallback_resolution") or ""
        if not resolution:
            return
        for key, m in missing.items():
            item = self.state["items"][key]
            d = item.get("diagnosis") or {}
            if item.get("fallback") or d.get("code") != "quality" or not d.get("only_quality") \
                    or self.now - item["since"] < FALLBACK_AFTER:
                continue
            app = self.apps[m["app"]]
            path = f"movie/{m['movie']}" if "movie" in m else f"series/{m['series']}"
            try:
                current = app.call("GET", path)
                profiles = app.call("GET", "qualityprofile") or []
                own = next((p for p in profiles if p.get("id") == current.get("qualityProfileId")), None)
                if not own:
                    continue
                wider = widened(own, resolution)
                name = wider["name"]
                existing = next((p for p in profiles if p.get("name") == name), None)
                if existing:
                    target = existing["id"]
                    # In step with the original in everything it keeps
                    # (language, scores, minimum score, cutoff), not only
                    # the qualities
                    if {k: v for k, v in existing.items() if k != "id"} != wider:
                        app.call("PUT", f"qualityprofile/{target}", dict(wider, id=target))
                else:
                    target = (app.call("POST", "qualityprofile", wider) or {}).get("id")
                if target and current.get("qualityProfileId") != target:
                    app.call("PUT", path, dict(current, qualityProfileId=target))
            except c.HTTP_ERRORS:
                continue
            item["fallback"] = name
            item["next_search"] = self.now
            c.log(f"{m['title']}: now also accepts {resolution} ({name}) after a week of releases only in other qualities")

    def dubbed_seasons(self, sonarr) -> dict:
        """(series id, season) → {"title", "files": [episode files]} for
        Japanese anime whose files have audio but none in Japanese"""
        out: dict = {}
        for series in sonarr.call("GET", "series") or []:
            if series.get("seriesType") != "anime" or (series.get("originalLanguage") or {}).get("name") != "Japanese":
                continue
            for f in sonarr.call("GET", f"episodefile?seriesId={series['id']}") or []:
                if has_japanese_audio(f) is False:
                    entry = out.setdefault((series["id"], f.get("seasonNumber")),
                                           {"title": f"{series.get('title')} season {f.get('seasonNumber')}", "files": []})
                    entry["files"].append(f)
        return out

    def replace_dubs(self, downloading: set) -> None:
        sonarr = self.apps.get("Sonarr")
        if not sonarr:
            return
        dubs = self.state.setdefault("dubs", {})
        try:
            seasons = self.dubbed_seasons(sonarr)
        except c.HTTP_ERRORS:
            return
        # Forget seasons that are no longer dubbed (replaced, or deleted)
        for key in [k for k in dubs if tuple(int(x) for x in k.split(":")) not in seasons]:
            dubs.pop(key)

        def looked_lately(k) -> bool:
            entry = dubs.get(f"{k[0]}:{k[1]}") or {}
            return "looked_at" in entry and self.now - entry["looked_at"] < DIAGNOSE_EVERY
        due = [k for k in seasons if f"sonarr:{k[0]}:{k[1]}" not in downloading and not looked_lately(k)]
        due.sort(key=lambda k: dubs.get(f"{k[0]}:{k[1]}", {}).get("looked_at", 0))
        for sid, season in due[:1]:
            entry = dubs.setdefault(f"{sid}:{season}", {"looks": 0})
            files = seasons[(sid, season)]["files"]
            entry.update(title=seasons[(sid, season)]["title"], episodes=len(files))
            try:
                self.look_for_japanese(sonarr, sid, season, files, entry)
            except c.HTTP_ERRORS as e:
                # Said, not swallowed: tried again in 12 hours
                entry["status"] = "error"
                entry["error"] = str(e)[:200]
                c.log(f"{entry['title']}: couldn't look for a Japanese release ({e}); trying again in 12 h")

    def look_for_japanese(self, sonarr, sid: int, season: int, files: list, entry: dict) -> None:
        """Nothing is deleted: Sonarr upgrades files it scores below 0 (it
        downloads the new one first); this only asks it to search when
        there's something to find"""
        entry["looks"] += 1
        entry["looked_at"] = self.now
        if all((f.get("customFormatScore") or 0) >= 0 for f in files):
            # Sonarr doesn't see them as dubs, so it won't replace them
            entry["status"] = "manual"
            return
        releases = sonarr.call("GET", f"release?seriesId={sid}&seasonNumber={season}", timeout=RELEASE_SEARCH_TIMEOUT) or []
        if not any(japanese_release(r) and takeable(r) for r in releases):
            entry["status"] = "waiting"
            return
        sonarr.call("POST", "command", {"name": "SeasonSearch", "seriesId": sid, "seasonNumber": season})
        entry["status"] = "upgrading"
        entry["searched_at"] = self.now
        c.log(f"{entry['title']}: a Japanese release is out; Sonarr is asked to upgrade the dub ({entry['episodes']} episodes)")

    def retest_indexers(self) -> None:
        """Ask Prowlarr to test the indexers again when some are switched off
        (it otherwise waits up to a day)"""
        if "indexers_retested" in self.state and self.now - self.state["indexers_retested"] < RETEST_INDEXERS_EVERY:
            return
        prowlarr = self.apps.get("Prowlarr")
        if not prowlarr:
            return
        try:
            if not (prowlarr.call("GET", "indexerstatus") or []):
                return
            prowlarr.call("POST", "indexer/testall")
        except c.HTTP_ERRORS:
            pass   # a 400 means some still fail: the test ran
        self.state["indexers_retested"] = self.now


def widened(profile: dict, resolution: str) -> dict:
    """A copy of a quality profile that also allows <resolution> ("720p"):
    only those qualities change; custom formats, language, cutoff and
    minimum score stay as they are"""
    def allow(entry: dict) -> dict:
        entry = dict(entry)
        names = [(entry.get("quality") or {}).get("name", ""), entry.get("name", "")]
        if entry.get("items"):
            entry["items"] = [allow(x) for x in entry["items"]]
            if any(x.get("allowed") for x in entry["items"]):
                entry["allowed"] = True
        elif any(resolution.lower() in (n or "").lower() for n in names):
            entry["allowed"] = True
        return entry
    copy = {k: v for k, v in profile.items() if k != "id"}
    copy["name"] = fallback_name(profile_origin(profile.get("name") or ""), resolution)
    copy["items"] = [allow(x) for x in profile.get("items") or []]
    return copy


def explain(item: dict, now: float | None = None) -> str:
    """The dashboard's line for one stuck film or season"""
    now = time.time() if now is None else now
    parts = []
    d = item.get("diagnosis")
    if d:
        parts.append(f"{d['text']} · {d['action']}")
    if item.get("fallback"):
        parts.append(f"now also accepts lower resolutions ({item['fallback']})")
    if item.get("downloading"):
        parts.append("a release is downloading")
    if item.get("searches"):
        left = item.get("next_search", now) - now
        nxt = "now" if left <= 60 else f"in {int(left // 3600)} h" if left >= 3600 else f"in {int(left // 60)} min"
        parts.append(f"searched again {item['searches']}× (next {nxt})")
    return " · ".join(parts)


def run(apps: list, settings: dict, state_file, offline: bool) -> Any:
    """One round from the postimport loop (it times itself: hourly)"""
    if offline:
        return None
    state = c.read_json(state_file, {}) or {}
    if not isinstance(state, dict):
        state = {}
    Stuck(apps, settings, state).run()
    c.write_json(state_file, state)
    return state
