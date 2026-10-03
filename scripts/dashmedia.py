#!/usr/bin/env python3
"""dashmedia: the media half of the dashboard's data. dashstatus.sh runs it
every slow round (5 minutes) and merges the JSON it prints into
status.json, so the API keys stay on the server.

  continue   what each user is in the middle of, and the next episode of
             what they're watching (Jellyfin's resume and next up)
  latest     what arrived lately; episodes grouped into their series
  requests   Seerr's latest requests, with what's really happening to each
             (downloading 40%, not released until ..., no release found
             yet, 12 of 24 episodes), from Sonarr/Radarr
  upcoming   episodes airing in the next two weeks and movies whose next
             release (cinema, digital, disc) is coming, with which one
  health     what the post-import checks fixed and rejected, and anime made
             in Japanese whose files have no Japanese audio

Each part is independent: one that fails is left out and the page shows
what it has. Environment: DASH_CONFIG, DASH_STATE (as dashstatus.sh).
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CONFIG = Path(os.environ.get("DASH_CONFIG", Path.home() / "media/config"))
STATE = Path(os.environ.get("DASH_STATE", Path.home() / "media/.state"))
JELLYFIN = "http://127.0.0.1:8096"
SONARR = "http://127.0.0.1:8989/api/v3"
RADARR = "http://127.0.0.1:7878/api/v3"
SEERR = "http://127.0.0.1:5055/api/v1"


def get(url, headers):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as resp:
        return json.loads(resp.read() or b"null")


def arr_key(name):
    try:
        m = re.search(r"<ApiKey>(.*?)</ApiKey>", (CONFIG / name / "config.xml").read_text())
        return m.group(1) if m else ""
    except OSError:
        return ""


def jellyfin(path):
    key = (STATE / "dashstatus/jellyfin-key").read_text().strip()
    return get(f"{JELLYFIN}/{path}", {"Authorization": f'MediaBrowser Token="{key}"'})


def sonarr(path):
    return get(f"{SONARR}/{path}", {"X-Api-Key": arr_key("sonarr")})


def radarr(path):
    return get(f"{RADARR}/{path}", {"X-Api-Key": arr_key("radarr")})


def seerr(path):
    key = json.loads((CONFIG / "seerr/settings.json").read_text())["main"]["apiKey"]
    return get(f"{SEERR}/{path}", {"X-Api-Key": key})


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


def requests():
    reqs = seerr("request?take=10&sort=added").get("results", [])
    movies = {m.get("tmdbId"): m for m in radarr("movie")}
    series = {s.get("tvdbId"): s for s in sonarr("series")}
    queue_m, queue_s = {}, {}
    for q in radarr("queue?pageSize=200").get("records", []):
        queue_m.setdefault(q.get("movieId"), []).append(q)
    for q in sonarr("queue?pageSize=500").get("records", []):
        queue_s.setdefault(q.get("seriesId"), []).append(q)
    today = date.today().isoformat()
    out = []
    for r in reqs:
        media = r.get("media") or {}
        kind = "tv" if r.get("type") == "tv" else "movie"
        try:
            details = seerr(f"{kind}/{media.get('tmdbId')}")
        except OSError:
            details = {}
        item = {"title": details.get("title") or details.get("name") or f"TMDB {media.get('tmdbId')}",
                "year": (details.get("releaseDate") or details.get("firstAirDate") or "")[:4],
                "type": kind, "tmdb": media.get("tmdbId"), "poster": details.get("posterPath"),
                "by": (r.get("requestedBy") or {}).get("displayName", ""), "date": r.get("createdAt"),
                "watch": media.get("jellyfinMediaId")}
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
        out.append(item)
    return out


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
    status = {}
    try:
        status = json.loads((STATE / "postimport/status.json").read_text())
    except (OSError, ValueError):
        pass
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


def main():
    result = {}
    for name, part in (("continue", continue_watching), ("latest", latest), ("requests_live", requests),
                       ("upcoming", upcoming), ("health", health)):
        try:
            result[name] = part()
        except Exception as e:  # one broken source mustn't blank the others
            print(f"dashmedia: {name}: {e}", file=sys.stderr)
    result["media_updated"] = int(datetime.now(timezone.utc).timestamp())
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
