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

import re
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


def minutes_of(item: dict) -> int:
    return int((item.get("RunTimeTicks") or 0) // 600_000_000)


def length_text(minutes: int) -> str:
    return "" if not minutes else f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


def tonight(limit: int = 300) -> list:
    """Watch tonight: what's in the library and not started yet, films you
    haven't watched and series you haven't begun (Continue watching has the
    rest), newest first. Each says film, series or anime (from its folder),
    how long it is, and for a series how much of it is here. All of them
    (up to 300): the page filters, then shows the first few."""
    users = jellyfin("Users")
    if not users:
        return []
    uid = users[0]["Id"]   # one viewer for now
    anime_dir = str(STATE.parent / "anime") + "/"
    common = (f"userId={uid}&Recursive=true&IsPlayed=false&EnableImageTypes=Primary&SortBy=DateCreated&SortOrder=Descending"
              "&Fields=Path,UserData,RecursiveItemCount,ProviderIds,CommunityRating&Limit=300")
    films = jellyfin(f"Items?IncludeItemTypes=Movie&{common}").get("Items", [])
    shows = jellyfin(f"Items?IncludeItemTypes=Series&{common}").get("Items", [])
    # Begun = in Continue watching: an episode in progress (none finished
    # still counts every episode as unplayed) or a next one up
    begun = {i.get("SeriesId") for path in (f"UserItems/Resume?userId={uid}&Limit=100&MediaTypes=Video",
                                            f"Shows/NextUp?userId={uid}&Limit=100")
             for i in jellyfin(path).get("Items", [])}
    sonarr_series = {str(s.get("tvdbId")): s for s in sonarr("series")}
    out = []
    for item in films:
        data = item.get("UserData") or {}
        if data.get("PlaybackPositionTicks"):
            continue   # started: it's in Continue watching
        minutes = minutes_of(item)
        out.append({**card(item), "kind": "anime" if (item.get("Path") or "").startswith(anime_dir) else "film",
                    "minutes": minutes, "added": item.get("DateCreated", ""),
                    "detail": " · ".join(x for x in (str(item.get("ProductionYear") or ""), length_text(minutes)) if x)})
    for item in shows:
        data = item.get("UserData") or {}
        have = item.get("RecursiveItemCount") or 0
        if not have or (data.get("UnplayedItemCount") or 0) < have or item["Id"] in begun:
            continue   # nothing to play, or already begun (Continue watching)
        minutes = minutes_of(item)
        stats = (sonarr_series.get(str((item.get("ProviderIds") or {}).get("Tvdb"))) or {}).get("statistics") or {}
        aired = stats.get("episodeCount") or 0
        count = f"{have} of {aired} episodes" if aired > have else f"{have} episode{'s' if have != 1 else ''}"
        out.append({**card(item), "kind": "anime" if (item.get("Path") or "").startswith(anime_dir) else "series",
                    "minutes": minutes, "added": item.get("DateCreated", ""), "partial": aired > have,
                    "detail": " · ".join(x for x in (count, f"{length_text(minutes)} each" if minutes else "") if x)})
    out.sort(key=lambda x: x["added"], reverse=True)
    return out[:limit]


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
    # Why it isn't moving, when it isn't: no one sharing it, no metadata yet
    why = " ".join(q.get("errorMessage") or "" for q in queue).lower()
    if "stalled" in why or "no connections" in why:
        return "downloading", f"{count}{pct}% · stalled: nobody is sharing it right now; Cleanuparr replaces it if it stays that way"
    if "metadata" in why:
        return "downloading", f"{count}{pct}% · getting the torrent's details from other peers (can take a while with few seeders)"
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
        # A partly there series: the episodes it has went through every step
        if reached > k or (reached == -1 and has_file and not checking):
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


def unique_requests(reqs: list, limit: int = 10) -> list:
    """One per title (newest first, as Seerr lists them): asking twice for
    The Odyssey is one row, dated from the first ask, with how many times"""
    by_title: dict = {}
    for r in reqs:
        media = r.get("media") or {}
        key = (r.get("type"), media.get("tmdbId") or media.get("id") or r.get("id"))
        if key in by_title:
            first = by_title[key]
            first["times"] = first.get("times", 1) + 1
            first["firstAsked"] = min(first.get("firstAsked") or first.get("createdAt") or "", r.get("createdAt") or "") or None
            continue
        by_title[key] = dict(r, times=1, firstAsked=r.get("createdAt"))
    return list(by_title.values())[:limit]


def requests():
    reqs = unique_requests(seerr("request?take=30&sort=added").get("results", []))
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
                "by": (r.get("requestedBy") or {}).get("displayName", ""), "date": r.get("firstAsked") or r.get("createdAt"),
                "times": r.get("times", 1), "watch": media.get("jellyfinMediaId")}
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


def playback_results() -> list:
    """postimport's playback checks, newest first"""
    return (c.read_json(STATE / "postimport/status.json", {}) or {}).get("playback") or []


def with_playback(stages: list, folder: str, results: list) -> list:
    """The Ready stage says whether playback was verified (imported isn't
    the same as playable): the latest check of each file in the folder;
    any that failed make it a problem, with the reason"""
    if not folder:
        return stages
    latest: dict = {}
    for r in results:
        if str(r.get("path", "")).startswith(folder.rstrip("/") + "/"):
            latest.setdefault(r["path"], r)
    if not latest:
        return stages
    failed = [r for r in latest.values() if r.get("status") == "failed"]
    for st in stages:
        if st["name"] != "ready":
            continue
        if failed:
            st["state"] = "problem"
            st["detail"] = "; ".join(f"{r.get('title')}: {r.get('detail')}" for r in failed[:2])
        elif st["state"] == "done":
            st["detail"] = (st["detail"] + " · " if st["detail"] else "") + "stream check passed"
    return stages


def stuck_items() -> dict:
    """What the postimport service found out about titles that aren't arriving"""
    return (c.read_json(STATE / "postimport/stuck.json", {}) or {}).get("items") or {}


def with_diagnosis(stages: list, items: list) -> list:
    """On the active Search stage: why nothing is arriving and what's done
    about it (the stuck items of this film, or of this series' seasons)"""
    from mediaserver import stuck
    texts = [(f"season {i['season']}: " if "season" in i and len(items) > 1 else "") + t
             for i, t in ((i, stuck.explain(i)) for i in items) if t]
    if texts:
        for st in stages:
            if st["name"] == "searching" and st["state"] == "active" and not st["detail"].startswith("not out yet"):
                st["detail"] = "; ".join(texts[:2])
                st["diagnosed"] = True
    return stages


def movie_pipeline(movie: dict, requested, queue_m: dict, current: dict, queued: list, subs: dict, today: str) -> list:
    upcoming = ""
    if movie and not movie.get("hasFile") and not movie.get("isAvailable"):
        nxt = next_release(movie, today)
        upcoming = f"not out yet · {nxt[1]} {short_day(nxt[0])}" if nxt else "not out yet"
    stages = with_progress(pipeline(requested, bool(movie.get("hasFile")), "", queue_m.get(movie.get("id"), []), upcoming,
                                    checking_for(current, queued, movie.get("path", ""), "Radarr", movie.get("id")),
                                    subs.get(movie.get("id"), 0)), current, movie.get("path", ""))
    item = stuck_items().get(f"radarr:{movie.get('id')}")
    return with_playback(with_diagnosis(stages, [item] if item else []), movie.get("path", ""), playback_results())


def series_pipeline(show: dict, requested, queue_s: dict, current: dict, queued: list, subs: dict) -> list:
    stats = show.get("statistics") or {}
    have, aired = stats.get("episodeFileCount") or 0, stats.get("episodeCount") or 0
    partial = f"{have} of {aired} episodes" if have and aired and have < aired else ""
    stages = with_progress(pipeline(requested, have > 0, partial, queue_s.get(show.get("id"), []), "",
                                    checking_for(current, queued, show.get("path", ""), "Sonarr", show.get("id")),
                                    subs.get(show.get("id"), 0)), current, show.get("path", ""))
    seasons = [i for k, i in sorted(stuck_items().items()) if k.startswith(f"sonarr:{show.get('id')}:")]
    return with_playback(with_diagnosis(stages, seasons), show.get("path", ""), playback_results())


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


# ─── Worth watching ──────────────────────────────────────────────
# Films and series released in the last four months and this week's trending
# ones (already out), ranked by Rotten Tomatoes critics and IMDb viewers
# (averaged when both are known), else TMDB's users with enough votes: all through Seerr, which already looks these up (IMDb and
# Rotten Tomatoes have no public API; Letterboxd's is partners only).
RECENT_DAYS = 120
GOOD_SCORE = 75          # out of 100
# "Because you watched" is about relevance, so good is enough there
GOOD_ENOUGH_SCORE = 65
RATINGS_KEEP = 86400     # a title's ratings are asked for once a day
RATINGS_RETRY = 6 * 3600  # and after a failed lookup, not again for 6 hours
# TMDB's own score only counts with this many votes; anime gets far fewer
# votes (and Rotten Tomatoes and IMDb rarely have it)
TMDB_MIN_VOTES = {"movies": 100, "series": 100, "anime": 20}
ROWS = ("anime", "series", "movies")


def ratings(kind: str, tmdb: int, cache: dict) -> dict:
    """{"rt": critics %, "rt_audience": %, "imdb": 0-10} (what's known),
    cached for a day so Rotten Tomatoes isn't asked every few minutes"""
    key, now = f"{kind}:{tmdb}", time.time()
    hit = cache.get(key)
    if hit and now - hit.get("at", 0) < RATINGS_KEEP:
        return hit["r"]
    if hit and now - hit.get("failed", 0) < RATINGS_RETRY:
        return hit["r"]
    try:
        raw = seerr(f"movie/{tmdb}/ratingscombined" if kind == "movie" else f"tv/{tmdb}/ratings") or {}
    except c.HTTP_ERRORS:
        raw = None
    if raw is None:
        # Not answering: what's cached, even if old, and no new try for a while
        cache[key] = {**(hit or {"at": 0, "r": {}}), "failed": now}
        return cache[key]["r"]
    rt = raw.get("rt") if kind == "movie" else raw
    imdb = raw.get("imdb") if kind == "movie" else None
    r = {k: v for k, v in (("rt", (rt or {}).get("criticsScore")), ("rt_audience", (rt or {}).get("audienceScore")),
                           ("imdb", (imdb or {}).get("criticsScore"))) if v is not None}
    cache[key] = {"at": now, "r": r}
    return r


def score(r: dict, tmdb_vote: float | None, tmdb_votes: int = 0, min_votes: int = 100) -> float | None:
    """A ranking out of 100 (our choice, not a measure the sources share):
    the average of Rotten Tomatoes' critics % and IMDb's viewer score x10
    where both are known (critics alone overrate some), either one alone,
    else TMDB's users when enough of them voted. The page shows the
    original scores, not this."""
    known = [float(r["rt"])] if r.get("rt") is not None else []
    known += [float(r["imdb"]) * 10] if r.get("imdb") is not None else []
    if known:
        return sum(known) / len(known)
    return float(tmdb_vote) * 10 if tmdb_vote and tmdb_votes >= min_votes else None


def row_of(kind: str, r: dict) -> str:
    """anime (Japanese animation, films or series), series or movies"""
    if 16 in (r.get("genreIds") or []) and r.get("originalLanguage") == "ja":
        return "anime"
    return "movies" if kind == "movie" else "series"


def pick(kind: str, tmdb: int, r: dict, cache: dict, row: str, bar: int = GOOD_SCORE) -> dict | None:
    """A Seerr result as a poster, if it's rated at least <bar> (else None)"""
    rating = ratings(kind, tmdb, cache)
    value = score(rating, r.get("voteAverage"), r.get("voteCount") or 0, TMDB_MIN_VOTES[row])
    if value is None or value < bar:
        return None
    media = r.get("mediaInfo") or {}
    return {"title": r.get("title") or r.get("name"), "type": kind, "tmdb": tmdb,
            "year": (r.get("releaseDate") or r.get("firstAirDate") or "")[:4], "poster": r.get("posterPath"),
            "score": round(value), **rating, "tmdb_vote": r.get("voteAverage"),
            # Seerr's status: 5 available, 4 partly, 2/3 requested or on its way
            "status": media.get("status"), "watch": media.get("jellyfinMediaId")}


def recently_watched(limit: int = 3) -> list[dict]:
    """The titles watched most recently, finished or not ({"title", "kind",
    "tmdb"}; an episode counts as its series), newest first"""
    users = jellyfin("Users")
    if not users:
        return []
    uid = users[0]["Id"]   # one viewer for now
    common = (f"userId={uid}&Recursive=true&IncludeItemTypes=Movie,Episode&SortBy=DatePlayed&SortOrder=Descending"
              "&Limit=40&Fields=ProviderIds")
    items = [i for f in ("IsResumable", "IsPlayed") for i in jellyfin(f"Items?{common}&Filters={f}").get("Items", [])]
    items.sort(key=lambda i: (i.get("UserData") or {}).get("LastPlayedDate") or "", reverse=True)
    series_ids = sorted({i["SeriesId"] for i in items if i.get("SeriesId")})
    series = {s["Id"]: s for s in (jellyfin(f"Items?userId={uid}&Ids={','.join(series_ids)}&Fields=ProviderIds")
                                   .get("Items", []) if series_ids else [])}
    out, seen = [], set()
    for i in items:
        source = series.get(i.get("SeriesId")) if i.get("SeriesId") else i
        if not source:
            continue   # the series' details didn't come back
        tmdb = (source.get("ProviderIds") or {}).get("Tmdb")
        kind = "tv" if i.get("SeriesId") else "movie"
        if not tmdb or (kind, str(tmdb)) in seen:
            continue
        seen.add((kind, str(tmdb)))
        out.append({"title": source.get("Name"), "kind": kind, "tmdb": int(tmdb)})
    return out[:limit]


def because(per_row: int = 12) -> list:
    """[{"because": "Skyfall", "items": [...]}]: TMDB's recommendations for
    what you watched last (through Seerr), rated at least GOOD_ENOUGH_SCORE,
    in TMDB's order of relevance; nothing you've watched, nothing twice"""
    watched = recently_watched(limit=10)
    skip = {(w["kind"], w["tmdb"]) for w in watched}
    cache_file = STATE / "dashstatus/ratings.json"
    cache = c.read_json(cache_file, {}) or {}
    rows = []
    for seed in watched[:3]:
        items = []
        # TMDB's second page only when the first gave few well-rated ones
        for page in (1, 2):
            if page == 2 and len(items) >= 6:
                break
            results = (seerr(f"{seed['kind']}/{seed['tmdb']}/recommendations?page={page}") or {}).get("results") or []
            add_picks(results, seed, skip, cache, items, per_row)
        if items:
            rows.append({"because": seed["title"], "items": items})
    c.write_json(cache_file, cache, compact=True)
    return rows


def add_picks(results: list, seed: dict, skip: set, cache: dict, items: list, per_row: int) -> None:
    """The well-rated ones of a page of recommendations, in order, into items"""
    for r in results[:20]:
        kind = r.get("mediaType") or seed["kind"]
        if kind not in ("movie", "tv") or not r.get("posterPath") or (kind, r.get("id")) in skip:
            continue
        picked = pick(kind, r["id"], r, cache, row_of(kind, r), GOOD_ENOUGH_SCORE)
        if picked:
            items.append(picked)
            skip.add((kind, r["id"]))   # not again in another row
        if len(items) >= per_row:
            return


def recommended(per_row: int = 12) -> dict:
    """{"anime": [...], "series": [...], "movies": [...]}, best first"""
    today = datetime.now(timezone.utc)
    since, until = (today - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")
    movies = f"primaryReleaseDateGte={since}&primaryReleaseDateLte={until}&sortBy=popularity.desc"
    shows = f"firstAirDateGte={since}&firstAirDateLte={until}&sortBy=popularity.desc"
    # Released in the last four months, up to today (not upcoming ones),
    # anime asked for on its own (it's rarely among the most popular), and
    # this week's trending (which includes ongoing series)
    sources = [("movie", f"discover/movies?page={p}&{movies}&voteCountGte=50") for p in (1, 2)]
    sources += [("tv", f"discover/tv?page={p}&{shows}&voteCountGte=30") for p in (1, 2)]
    sources += [("tv", f"discover/tv?page={p}&{shows}&genre=16&language=ja&voteCountGte=10") for p in (1, 2)]
    sources += [("movie", f"discover/movies?page=1&{movies}&genre=16&language=ja&voteCountGte=10")]
    sources += [(None, f"discover/trending?page={p}") for p in (1, 2)]
    candidates: dict = {}
    for kind, path in sources:
        for r in (seerr(path) or {}).get("results") or []:
            k = kind or r.get("mediaType")
            released = r.get("releaseDate") or r.get("firstAirDate") or ""
            if k not in ("movie", "tv") or r.get("video") or not r.get("posterPath") or not released or released > until:
                continue
            candidates.setdefault((k, r["id"]), r)
    cache_file = STATE / "dashstatus/ratings.json"
    cache = c.read_json(cache_file, {}) or {}
    rows: dict = {name: [] for name in ROWS}
    by_row: dict = {name: [] for name in ROWS}
    for (kind, tmdb), r in candidates.items():
        by_row[row_of(kind, r)].append((kind, tmdb, r))
    for name in ROWS:
        # The most popular of each row first, so the ratings asked for are
        # the ones that matter, and every row gets its share
        for kind, tmdb, r in sorted(by_row[name], key=lambda x: -(x[2].get("popularity") or 0))[:20]:
            picked = pick(kind, tmdb, r, cache, name)
            if picked:
                rows[name].append(picked)
        rows[name].sort(key=lambda x: (-x["score"], x["title"] or ""))
        rows[name] = rows[name][:per_row]
    # Forget ratings not asked for in a week
    cache = {k: v for k, v in cache.items() if time.time() - max(v.get("at", 0), v.get("failed", 0)) < 7 * 86400}
    c.write_json(cache_file, cache, compact=True)
    return rows


def grouped_fixes(recent: list) -> list:
    """One line per title and fix: the same fix on several episodes of a
    series is "Kaiji · 4 episodes" (newest first, as given)"""
    out: list = []
    by_key: dict = {}
    for r in recent:
        title = c.strip_quality(r.get("title", ""))
        m = re.match(r"(.+?) - S\d+E\d+", title)
        series = m.group(1) if m else None
        key = (series or title, r.get("what"))
        if key in by_key:
            by_key[key]["episodes"] += 1
            continue
        entry = {"title": title, "what": r.get("what", ""), "time": r.get("time", 0), "episodes": 1, "series": series}
        by_key[key] = entry
        out.append(entry)
    for e in out:
        if e["series"] and e["episodes"] > 1:
            e["title"] = e["series"]
    return out


def health():
    status = c.read_json(STATE / "postimport/status.json", {}) or {}
    week = time.time() - 7 * 86400
    out = {"fixed": grouped_fixes([r for r in status.get("recent", []) if r.get("time", 0) > week])[:6],
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
    # Files imported that Jellyfin couldn't play (the latest check of each)
    latest: dict = {}
    for r in status.get("playback") or []:
        latest.setdefault(r.get("path"), r)
    out["playback_failed"] = [{"title": r.get("title"), "detail": r.get("detail"), "time": r.get("at")}
                              for r in latest.values() if r.get("status") == "failed" and (r.get("at") or 0) > week][:6]
    out["playback_verified"] = sum(1 for r in latest.values() if r.get("status") == "verified" and (r.get("at") or 0) > week)
    # What the automatic replacement is doing about each (stuck.py)
    looks = (c.read_json(STATE / "postimport/stuck.json", {}) or {}).get("dubs") or {}
    for d in dubs.values():
        seasons = [e for e in looks.values() if (e.get("title") or "").startswith(d["title"] + " season ")]
        if any(e.get("status") == "upgrading" for e in seasons):
            d["status"] = "A Japanese release is out: Sonarr is replacing the dub (the old files stay until the new ones are in)"
        elif any(e.get("status") == "manual" for e in seasons):
            d["status"] = "Sonarr doesn't see these files as dubs, so it won't replace them: pick a Japanese or Dual Audio release in Sonarr"
        elif any(e.get("status") == "waiting" for e in seasons):
            d["status"] = f"No Japanese or Dual Audio release out yet (looked {max(e.get('looks', 0) for e in seasons)}×; again every 12 h)"
        elif any(e.get("status") == "error" for e in seasons):
            d["status"] = "Couldn't look for a Japanese release last time (Sonarr or the indexers didn't answer); trying again every 12 h"
    out["dubs"] = sorted(dubs.values(), key=lambda d: d["title"])
    return out


MEDIA_PARTS = ("continue", "tonight", "because", "latest", "requests_live", "upcoming", "health", "recommended")


def carry_over(previous: dict | None, new: dict) -> dict:
    """A part that failed this time keeps its last good data (instead of
    disappearing), with when that was in media_ages, so the page can say
    it's from earlier rather than show an empty list"""
    previous = previous or {}
    ages = dict(previous.get("media_ages") or {})
    for name in MEDIA_PARTS:
        if name in new.get("media_failed", []):
            if name in previous:
                new[name] = previous[name]
        else:
            ages[name] = new.get("media_updated")
    new["media_ages"] = ages
    return new


def collect() -> dict:
    result, failed = {}, []
    for name, part in (("continue", continue_watching), ("tonight", tonight), ("because", because), ("latest", latest), ("requests_live", requests),
                       ("upcoming", upcoming), ("health", health), ("recommended", recommended)):
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
