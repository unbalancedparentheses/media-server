"""dashmedia: the media half of the dashboard's data, which dashstatus
gathers every slow round (5 minutes) into status.json, so the API keys
stay on the server.

  continue   what each user is in the middle of, and the next episode of
             what they're watching (Jellyfin's resume and next up)
  latest     what arrived lately; episodes grouped into their series
  requests   Seerr's latest requests, and anything else on its way (new
             episodes Sonarr grabbed), each with where it is in the
             pipeline: requested → searching → downloading → importing →
             checking & fixing (postimport) → subtitles (Bazarr) → ready
  upcoming   episodes airing in the next two weeks and movies whose next
             release (cinema, digital, disc) is coming, with which one
  health     what the post-import checks fixed and rejected, and anime made
             in Japanese whose files have no Japanese audio

Each part is independent: one that fails is left out (and listed in
media_failed, so dashstatus retries soon) and the page shows what it has.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mediaserver import common as c
from mediaserver.config import local

CONFIG = c.MEDIA / "config"
STATE = c.MEDIA / ".state"


def configure(config: Path, state: Path) -> None:
    global CONFIG, STATE
    CONFIG, STATE = config, state


def jellyfin(path):
    return c.get_json(f"{local('jellyfin')}/{path}", c.jellyfin_auth(STATE))


def sonarr(path):
    return c.get_json(f"{local('sonarr')}/api/v3/{path}", {"X-Api-Key": c.arr_key(CONFIG, "sonarr")})


def radarr(path):
    return c.get_json(f"{local('radarr')}/api/v3/{path}", {"X-Api-Key": c.arr_key(CONFIG, "radarr")})


def seerr(path):
    return c.get_json(f"{local('seerr')}/api/v1/{path}", {"X-Api-Key": c.seerr_key(CONFIG)})


def poster(item):
    """(item id, image tag) of the poster to show: the series' for an episode"""
    if item.get("Type") == "Episode" and item.get("SeriesPrimaryImageTag"):
        return item["SeriesId"], item["SeriesPrimaryImageTag"]
    tag = (item.get("ImageTags") or {}).get("Primary")
    return (item["Id"], tag) if tag else (None, None)


def episode_label(item):
    return f"S{item.get('ParentIndexNumber') or 0}E{item.get('IndexNumber') or 0} · {item.get('Name', '')}"


def card(item, **extra):
    image, tag = poster(item)
    episode = item.get("Type") == "Episode"
    return {"id": item["Id"], "title": item.get("SeriesName") if episode else item.get("Name"),
            "detail": episode_label(item) if episode else str(item.get("ProductionYear") or ""),
            "image": image, "tag": tag, **extra}


# ─── Parts ───────────────────────────────────────────────────────

def continue_watching():
    users = jellyfin("Users")
    fields = "Fields=UserData&EnableImageTypes=Primary"
    items, seen = [], set()
    for user in users:
        uid, name = user["Id"], user["Name"]
        resume = jellyfin(f"UserItems/Resume?userId={uid}&Limit=12&MediaTypes=Video&{fields}").get("Items", [])
        next_up = jellyfin(f"Shows/NextUp?userId={uid}&Limit=12&{fields}").get("Items", [])
        for kind, group in (("resume", resume), ("next", next_up)):
            for item in group:
                # One card per series (the in-progress episode wins)
                key = (uid, item.get("SeriesId") or item["Id"])
                if key in seen:
                    continue
                seen.add(key)
                data = item.get("UserData") or {}
                items.append(card(item, user=name, kind=kind,
                                  progress=int(data.get("PlayedPercentage") or 0),
                                  date=data.get("LastPlayedDate") or ""))
    # Most recently played first; next-up episodes (no date) after
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[:16]


def latest():
    data = jellyfin("Items?Recursive=true&SortBy=DateCreated&SortOrder=Descending&Limit=80"
                    "&IncludeItemTypes=Movie,Episode&Fields=DateCreated&EnableImageTypes=Primary")
    cards, by_series = [], {}
    for item in data.get("Items", []):
        series = item.get("SeriesId")
        if series:
            if series in by_series:
                by_series[series]["new"] += 1
                continue
            c = card(item, date=item.get("DateCreated", ""), new=1)
            by_series[series] = c
        else:
            c = card(item, date=item.get("DateCreated", ""))
        cards.append(c)
        if len(cards) >= 16:
            break
    for c in by_series.values():
        if c["new"] > 1:
            c["detail"] = f"{c['new']} new episodes"
    return cards


def short_day(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %-d")


def next_release(movie, today):
    """(date, which) of a movie's next release on or after today, or None"""
    dates = [(movie.get(k) or "")[:10] for k in ("inCinemas", "digitalRelease", "physicalRelease")]
    upcoming = sorted((d, w) for d, w in zip(dates, ("in cinemas", "digital", "on disc")) if d and d >= today)
    return upcoming[0] if upcoming else None


STAGES = ("requested", "searching", "downloading", "importing", "checking", "subtitles", "ready")
IMPORTING = {"importPending", "importing"}
STUCK = {"importBlocked", "failedPending", "importFailed"}


def stage(name: str, state: str = "todo", detail: str = "") -> dict:
    return {"name": name, "state": state, "detail": detail}


def queue_progress(queue: list) -> tuple[str, str]:
    """(stage, detail) for items in Sonarr/Radarr's queue: downloading
    (with % and time left), importing, or stuck at the import"""
    if any((q.get("trackedDownloadState") or "") in STUCK or q.get("trackedDownloadStatus") == "error" for q in queue):
        return "stuck", "the import needs a look (Sonarr/Radarr → Activity)"
    if all((q.get("trackedDownloadState") or "") in IMPORTING for q in queue):
        return "importing", "moving into the library"
    size = sum(q.get("size") or 0 for q in queue)
    left = sum(q.get("sizeleft") or 0 for q in queue)
    pct = int((size - left) * 100 / size) if size else 0
    eta = next((q.get("timeleft") for q in queue if q.get("timeleft")), "")
    count = f"{len(queue)} episodes · " if len(queue) > 1 else ""
    return "downloading", f"{count}{pct}%" + (f" · {eta} left" if eta and eta != "00:00:00" else "")


def pipeline(requested: dict | None, has_file: bool, partial: str, queue: list, upcoming: str,
             checking: str, subtitles_missing: int) -> list:
    """The stages of one title. requested: {"status", "by"} or None (not
    requested through Seerr); partial: "12 of 24 episodes" when some are
    there; upcoming: "not out yet · …" when it isn't released; checking:
    what postimport is doing with it ("" if nothing)."""
    out = []
    if requested is not None:
        if requested.get("status") == 1:
            return [stage("requested", "active", "waiting for approval")] + [stage(n) for n in STAGES[1:]]
        if requested.get("status") == 3:
            return [stage("requested", "problem", "declined")]
        out.append(stage("requested", "done", requested.get("by", "")))
    where, detail = queue_progress(queue) if queue else ("", "")
    in_progress = bool(queue) or bool(checking)
    if in_progress or (has_file and not partial):
        out.append(stage("searching", "done"))
    elif upcoming:
        out.append(stage("searching", "active", upcoming))
    else:
        out.append(stage("searching", "active", (partial + " · " if partial else "") + "looking for a good release"))
    order = ["downloading", "importing", "checking"]
    reached = {"downloading": 0, "importing": 1, "stuck": 1}.get(where, -1)
    if checking:
        reached = 2
    for k, name in enumerate(order):
        if reached > k or (reached == -1 and has_file and not partial and not checking):
            out.append(stage(name, "done"))
        elif reached == k:
            state = "problem" if where == "stuck" and name == "importing" else "active"
            out.append(stage(name, state, checking if name == "checking" else detail))
        else:
            out.append(stage(name))
    ready = has_file and not in_progress and not partial
    if subtitles_missing:
        out.append(stage("subtitles", "active" if has_file else "todo",
                         f"looking for {subtitles_missing} episode{'s' if subtitles_missing != 1 else ''}" if subtitles_missing > 1 or partial
                         else "looking for subtitles"))
    else:
        out.append(stage("subtitles", "done" if has_file else "todo"))
    out.append(stage("ready", "done" if ready or (has_file and partial) else "todo", partial if partial and has_file else ""))
    return out


def postimport_activity(state: Path) -> tuple[dict, list]:
    """What postimport is doing: (current task or {}, queued imports)"""
    status = c.read_json(state / "postimport/status.json", {}) or {}
    return status.get("current") or {}, status.get("queued") or []


def fixing_text(current: dict) -> str:
    """What's being done, how far, how long left: "adding stereo audio · 42% · about 3 min left"."""
    text = current.get("what") or "fixing the file"
    if current.get("progress") is not None:
        text += f" · {current['progress']}%"
        if current.get("eta") is not None:
            minutes = round(current["eta"] / 60)
            text += " · less than a minute left" if minutes < 1 else f" · about {minutes} min left"
    return text


def fixing_now(state: Path) -> dict | None:
    """What postimport is rewriting right now, with how far it is; read
    every dashboard round (the rest of the media part is slower), so the
    progress moves. None when idle or when postimport stopped reporting."""
    status = c.read_json(state / "postimport/status.json", {}) or {}
    current = status.get("current")
    if not current or time.time() - status.get("updated", 0) > 600:
        return None
    return {"title": current.get("title", ""), "path": current.get("path", ""), "progress": current.get("progress"),
            "eta": current.get("eta"), "since": current.get("since"), "text": fixing_text(current)}


def working_on(current: dict, folder: str) -> bool:
    return bool(current and folder and str(current.get("path", "")).startswith(folder.rstrip("/") + "/"))


def with_progress(stages: list, current: dict, folder: str) -> list:
    """The percentage on the check & fix stage while its file is rewritten"""
    if working_on(current, folder) and current.get("progress") is not None:
        for st in stages:
            if st["name"] == "checking" and st["state"] == "active":
                st["progress"] = current["progress"]
    return stages


def checking_for(current: dict, queued: list, folder: str, app: str, item_id: Any) -> str:
    if working_on(current, folder):
        return fixing_text(current)
    key = "movieId" if app == "Radarr" else "seriesId"
    for q in queued:
        if q.get("app") == app and q.get(key) == item_id:
            return f"checking again: {q['suspect']}" if q.get("suspect") else "waiting to be checked"
    return ""


def bazarr_wanted(kind: str) -> dict:
    """Missing subtitles per Radarr movie / Sonarr series id"""
    key = c.bazarr_key(CONFIG)
    if not key:
        return {}
    data = c.try_json(f"{local('bazarr')}/api/{kind}/wanted?start=0&length=-1&apikey={key}", default={}) or {}
    counts: dict = {}
    for row in data.get("data") or []:
        item = row.get("radarrId") if kind == "movies" else row.get("sonarrSeriesId")
        counts[item] = counts.get(item, 0) + 1
    return counts


def poster_url(item: dict) -> str | None:
    return next((i.get("remoteUrl") for i in item.get("images") or [] if i.get("coverType") == "poster"), None)


def requests():
    reqs = seerr("request?take=10&sort=added").get("results", [])
    movies = {m.get("tmdbId"): m for m in radarr("movie")}
    series = {s.get("tvdbId"): s for s in sonarr("series")}
    queue_m, queue_s = {}, {}
    for q in radarr("queue?pageSize=200").get("records", []):
        queue_m.setdefault(q.get("movieId"), []).append(q)
    for q in sonarr("queue?pageSize=500").get("records", []):
        queue_s.setdefault(q.get("seriesId"), []).append(q)
    current, queued = postimport_activity(STATE)
    subs_m, subs_s = bazarr_wanted("movies"), bazarr_wanted("episodes")
    today = date.today().isoformat()
    out, shown = [], set()
    for r in reqs:
        media = r.get("media") or {}
        kind = "tv" if r.get("type") == "tv" else "movie"
        try:
            details = seerr(f"{kind}/{media.get('tmdbId')}")
        except c.HTTP_ERRORS:
            details = {}
        item = {"title": details.get("title") or details.get("name") or f"TMDB {media.get('tmdbId')}",
                "year": (details.get("releaseDate") or details.get("firstAirDate") or "")[:4],
                "type": kind, "tmdb": media.get("tmdbId"), "poster": details.get("posterPath"),
                "by": (r.get("requestedBy") or {}).get("displayName", ""), "date": r.get("createdAt"),
                "watch": media.get("jellyfinMediaId")}
        requested = {"status": r.get("status"), "by": item["by"]}
        if r.get("status") == 1:
            item.update(state="waiting", text="waiting for approval")
        elif r.get("status") == 3:
            item.update(state="declined", text="declined")
        elif kind == "movie":
            item.update(movie_state(movies.get(media.get("tmdbId")), queue_m, today))
        else:
            item.update(series_state(series.get(media.get("tvdbId")), queue_s))
        if media.get("status") == 5:
            item.update(state="available", text="ready to watch")
        if kind == "movie":
            m = movies.get(media.get("tmdbId")) or {}
            shown.add(("movie", m.get("id")))
            item["stages"] = movie_pipeline(m, requested, queue_m, current, queued, subs_m, today)
            item["folder"] = m.get("path", "")
        else:
            show = series.get(media.get("tvdbId")) or {}
            shown.add(("tv", show.get("id")))
            item["stages"] = series_pipeline(show, requested, queue_s, current, queued, subs_s)
            item["folder"] = show.get("path", "")
        out.append(item)
    # Also on its way without a request: what Sonarr/Radarr are downloading
    # (e.g. new episodes of a series you follow)
    for kind, queue, catalog, app in (("movie", queue_m, movies, "Radarr"), ("tv", queue_s, series, "Sonarr")):
        by_id = {x.get("id"): x for x in catalog.values()}
        for item_id in queue:
            if (kind, item_id) in shown or item_id not in by_id:
                continue
            x = by_id[item_id]
            stages = movie_pipeline(x, None, queue_m, current, queued, subs_m, today) if kind == "movie" \
                else series_pipeline(x, None, queue_s, current, queued, subs_s)
            where, detail = queue_progress(queue[item_id])
            out.insert(0, {"title": x.get("title"), "year": str(x.get("year") or ""), "type": kind, "tmdb": x.get("tmdbId"),
                           "poster_url": poster_url(x), "by": "", "date": None, "state": "downloading" if where != "stuck" else "stuck",
                           "text": detail, "stages": stages, "folder": x.get("path", ""),
                           "admin": f"{'radarr:/movie' if kind == 'movie' else 'sonarr:/series'}/{x.get('titleSlug')}"})
    return out


def movie_pipeline(movie: dict, requested, queue_m: dict, current: dict, queued: list, subs: dict, today: str) -> list:
    upcoming = ""
    if movie and not movie.get("hasFile") and not movie.get("isAvailable"):
        nxt = next_release(movie, today)
        upcoming = f"not out yet · {nxt[1]} {short_day(nxt[0])}" if nxt else "not out yet"
    return with_progress(pipeline(requested, bool(movie.get("hasFile")), "", queue_m.get(movie.get("id"), []), upcoming,
                                  checking_for(current, queued, movie.get("path", ""), "Radarr", movie.get("id")),
                                  subs.get(movie.get("id"), 0)), current, movie.get("path", ""))


def series_pipeline(show: dict, requested, queue_s: dict, current: dict, queued: list, subs: dict) -> list:
    stats = show.get("statistics") or {}
    have, aired = stats.get("episodeFileCount") or 0, stats.get("episodeCount") or 0
    partial = f"{have} of {aired} episodes" if have and aired and have < aired else ""
    return with_progress(pipeline(requested, have > 0, partial, queue_s.get(show.get("id"), []), "",
                                  checking_for(current, queued, show.get("path", ""), "Sonarr", show.get("id")),
                                  subs.get(show.get("id"), 0)), current, show.get("path", ""))


def downloading(queue):
    size = sum(q.get("size") or 0 for q in queue)
    left = sum(q.get("sizeleft") or 0 for q in queue)
    pct = int((size - left) * 100 / size) if size else 0
    stuck = all((q.get("trackedDownloadState") or "") in ("importPending", "importBlocked", "failedPending")
                or q.get("status") == "warning" for q in queue)
    return pct, stuck


def movie_state(movie, queue, today):
    if not movie:
        return {"state": "searching", "text": "being added"}
    link = {"admin": f"radarr:/movie/{movie.get('titleSlug')}"}
    if movie.get("hasFile"):
        return {"state": "available", "text": "ready to watch", **link}
    if movie.get("id") in queue:
        pct, stuck = downloading(queue[movie["id"]])
        return {"state": "stuck" if stuck else "downloading",
                "text": "downloaded; import needs a look" if stuck else f"downloading · {pct}%", **link}
    if not movie.get("isAvailable"):
        nxt = next_release(movie, today)
        return {"state": "upcoming", "text": f"not out yet · {nxt[1]} {short_day(nxt[0])}" if nxt else "not out yet", **link}
    return {"state": "searching", "text": "no good release found yet · still looking", **link}


def series_state(show, queue):
    if not show:
        return {"state": "searching", "text": "being added"}
    link = {"admin": f"sonarr:/series/{show.get('titleSlug')}"}
    stats = show.get("statistics") or {}
    have, aired = stats.get("episodeFileCount") or 0, stats.get("episodeCount") or 0
    if show.get("id") in queue:
        pct, stuck = downloading(queue[show["id"]])
        n = len(queue[show["id"]])
        return {"state": "stuck" if stuck else "downloading",
                "text": "downloaded; import needs a look" if stuck else
                f"downloading {n} episode{'s' if n != 1 else ''} · {pct}%", **link}
    if aired and have >= aired:
        return {"state": "available", "text": "ready to watch", **link}
    if have:
        return {"state": "partial", "text": f"{have} of {aired} episodes · looking for the rest", **link}
    return {"state": "searching", "text": "no good release found yet · still looking", **link}


def upcoming():
    today = date.today()
    start, soon, later = today.isoformat(), (today + timedelta(days=14)).isoformat(), (today + timedelta(days=120)).isoformat()
    out = []
    for ep in sonarr(f"calendar?includeSeries=true&start={start}&end={soon}"):
        if ep.get("hasFile"):
            continue
        out.append({"date": (ep.get("airDateUtc") or ep.get("airDate") or "")[:10], "type": "episode",
                    "title": (ep.get("series") or {}).get("title", ""),
                    "detail": f"S{ep.get('seasonNumber', 0)}E{ep.get('episodeNumber', 0)} · {ep.get('title') or ''}"})
    for movie in radarr(f"calendar?start={start}&end={later}"):
        nxt = next_release(movie, start)
        if movie.get("hasFile") or not nxt:
            continue
        out.append({"date": nxt[0], "type": "movie", "title": movie.get("title", ""),
                    "detail": f"{movie.get('year', '')} · {nxt[1]}"})
    return sorted(out, key=lambda x: x["date"])[:20]


def health():
    status = c.read_json(STATE / "postimport/status.json", {}) or {}
    week = time.time() - 7 * 86400
    out = {"fixed": [r for r in status.get("recent", []) if r.get("time", 0) > week][:6],
           "looking": status.get("looking", []), "kept": status.get("kept", []),
           "checks_running": bool(status) and time.time() - status.get("updated", 0) < 3600}
    # Anime made in Japanese whose files have no Japanese audio (dubs);
    # untagged audio may be Japanese, so only tagged tracks count
    japanese = {s["title"]: s.get("titleSlug") for s in sonarr("series")
                if s.get("seriesType") == "anime" and (s.get("originalLanguage") or {}).get("name") == "Japanese"}
    dubs = {}
    if japanese:
        anime = [v["ItemId"] for v in jellyfin("Library/VirtualFolders") if v.get("Name") == "Anime"]
        for lib in anime:
            items = jellyfin(f"Items?Recursive=true&IncludeItemTypes=Episode&Fields=MediaStreams&ParentId={lib}")
            for item in items.get("Items", []):
                if item.get("SeriesName") not in japanese:
                    continue
                langs = [s.get("Language") or "" for s in item.get("MediaStreams", []) if s.get("Type") == "Audio"]
                if langs and all(langs) and "jpn" not in langs:
                    d = dubs.setdefault(item["SeriesName"], {"title": item["SeriesName"], "episodes": 0,
                                                             "admin": f"sonarr:/series/{japanese[item['SeriesName']]}"})
                    d["episodes"] += 1
    out["dubs"] = sorted(dubs.values(), key=lambda d: d["title"])
    return out


def collect() -> dict:
    result, failed = {}, []
    for name, part in (("continue", continue_watching), ("latest", latest), ("requests_live", requests),
                       ("upcoming", upcoming), ("health", health)):
        try:
            result[name] = part()
        except Exception as e:  # one broken source mustn't blank the others
            c.log(f"dashmedia: {name}: {e}")
            failed.append(name)
    # dashstatus asks again next round instead of in 5 minutes (e.g. right
    # after the services restart)
    result["media_failed"] = failed
    result["media_updated"] = int(datetime.now(timezone.utc).timestamp())
    return result
