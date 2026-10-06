"""dashstatus: gathers the dashboard's live data into one file,
~/media/config/nginx/www/status.json, which the page reads. The API keys
stay here; the browser only ever sees the summary. Runs as a launchd agent
(see flake.nix).

Every DASH_INTERVAL seconds (15): what's playing (and why Jellyfin is
converting it, if it is), transfer speeds, the Mac's load and memory, the
CPU and memory of the media server's own services (each with what it
started, e.g. Jellyfin's conversions), the connection. Every DASH_SLOW_EVERY rounds (20, i.e. 5 minutes): library
counts, Sonarr/Radarr queues, missing items and health, Prowlarr's
indexers, Bazarr's missing subtitles, Seerr's request counts, disk space,
Tailscale, an uptime sample per service, and the media side from dashmedia
(continue watching, latest, requests' real state, upcoming, library
health), which is asked again every minute while part of it fails.
"attention" lists what needs you, each with a suggested action.

Environment: DASH_CONFIG (~/media/config), DASH_STATE (~/media/.state),
DASH_MEDIA (~/media), DASH_OUT, DISK_WARN_GB, DISK_MIN_GB, DASH_INTERVAL,
DASH_SLOW_EVERY.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, UTC
from pathlib import Path

from mediaserver import common as c
from mediaserver import control, dashmedia, launchd, pause, unfinished
from mediaserver.config import PORTS, local


def site(indexer: dict) -> str:
    """An indexer's site: its base URL setting, else its definition's first"""
    base = next((f.get("value") for f in indexer.get("fields") or [] if f.get("name") == "baseUrl" and f.get("value")), "")
    return base or next(iter(indexer.get("indexerUrls") or []), "")


def unresolved(url: str) -> str:
    """The host, when the DNS doesn't know it ("" when it does, or there's
    no URL, or the Mac is offline: then nothing resolves)"""
    import socket
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or ""
    if not host:
        return ""
    try:
        socket.getaddrinfo(host, 443)
        return ""
    except socket.gaierror:
        try:
            socket.getaddrinfo("apple.com", 443)   # online, and this one is unknown
        except socket.gaierror:
            return ""
        return host

# 24-hour availability: a sample per service every slow round (5 minutes).
# Byparr is checked on /docs: its /health opens a browser and can take
# longer than the timeout.
def uptime_checks() -> list[tuple[str, str]]:
    return [("Jellyfin", local("jellyfin") + "/health"), ("Seerr", local("seerr") + "/api/v1/status"),
            ("Sonarr", local("sonarr") + "/ping"), ("Radarr", local("radarr") + "/ping"),
            ("Prowlarr", local("prowlarr") + "/ping"), ("Bazarr", local("bazarr")),
            ("qBittorrent", local("qbittorrent")), ("SABnzbd", local("sabnzbd")),
            ("Cleanuparr", local("cleanuparr") + "/health"), ("Byparr", local("byparr") + "/docs")]
UPTIME_KEEP = 288


def sysctl(name: str) -> str:
    result = subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, check=False)
    return result.stdout.strip()


def iso_time(s: str) -> float:
    """Unix time of an ISO date like 2026-10-03T08:00:50.123Z"""
    return datetime.fromisoformat(re.sub(r"\.\d+Z$", "Z", s).replace("Z", "+00:00")).timestamp()


class Collector:
    def __init__(self, config: Path, state: Path, media: Path, warn_gb: int, min_gb: int):
        self.config, self.state, self.media, self.warn_gb, self.min_gb = config, state, media, warn_gb, min_gb
        self.round_no = 0
        self.slow: dict | None = None
        self.slow_at = 0
        self.media_data: dict | None = None

    # ─── Fast: every round ───────────────────────────────────────

    def system(self) -> dict:
        load = list(os.getloadavg())
        cpus = os.cpu_count() or 1
        mem = int(sysctl("hw.memsize") or 0)
        page = int(sysctl("hw.pagesize") or 4096)
        # In use = active + wired + compressed (what Activity Monitor calls "used")
        vm = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, check=False).stdout

        def pages(label: str) -> int:
            m = re.search(rf"{label}:\s+(\d+)", vm)
            return int(m.group(1)) if m else 0
        used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * page
        boot = re.search(r"sec = (\d+)", sysctl("kern.boottime"))
        return {"load": load, "cpus": cpus, "cpu_pct": min(100, int(load[0] / cpus * 100)),
                "mem_total": mem, "mem_used": used, "mem_pct": int(used * 100 / mem) if mem else 0,
                "uptime_s": int(time.time()) - int(boot.group(1)) if boot else 0}

    def services_usage(self, ncpu: int, mem_total: int) -> dict:
        """CPU and memory of the media server's own services (each launchd
        job and everything it started: Jellyfin's ffmpeg conversions,
        Byparr's browser, Bazarr's server), apart from the rest of the Mac.
        CPU is ps's recent average; 100% = the whole Mac."""
        jobs = {}
        listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False).stdout
        prefix = launchd.LABEL_PREFIX + "."
        for line in listing.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[2].startswith(prefix) and parts[0].isdigit():
                jobs[int(parts[0])] = parts[2][len(prefix):]
        procs = subprocess.run(["ps", "-axo", "pid=,ppid=,%cpu=,rss="], capture_output=True, text=True, check=False).stdout
        parent, cpu, rss = {}, {}, {}
        for line in procs.splitlines():
            f = line.split()
            if len(f) == 4:
                pid = int(f[0])
                parent[pid], cpu[pid], rss[pid] = int(f[1]), float(f[2]), int(f[3]) * 1024
        per: dict[str, dict] = {name: {"name": name, "cpu": 0.0, "mem": 0} for name in jobs.values()}
        for pid in parent:
            p, seen = pid, 0
            while p not in jobs and p in parent and p > 1 and seen < 50:  # walk up to the service's job
                p, seen = parent[p], seen + 1
            if p in jobs:
                per[jobs[p]]["cpu"] += cpu[pid]
                per[jobs[p]]["mem"] += rss[pid]
        services = sorted(per.values(), key=lambda x: (x["cpu"], x["mem"]), reverse=True)
        for x in services:
            x["cpu"] = round(x["cpu"] / ncpu, 2)
        total_cpu = sum(x["cpu"] for x in services)
        total_mem = sum(x["mem"] for x in services)
        return {"cpu_pct": round(total_cpu, 2), "mem_bytes": total_mem,
                "mem_pct": round(total_mem * 100 / mem_total, 1) if mem_total else 0, "services": services}

    def playing(self) -> list:
        auth = c.jellyfin_auth(self.state)
        if not auth:
            return []
        out = []
        for s in c.try_json(local("jellyfin") + "/Sessions?ActiveWithinSeconds=120", auth, []) or []:
            item = s.get("NowPlayingItem")
            if not item:
                continue
            play, trans = s.get("PlayState") or {}, s.get("TranscodingInfo") or {}
            runtime = item.get("RunTimeTicks") or 0
            episode = bool(item.get("SeriesName"))
            out.append({
                "user": s.get("UserName"), "client": s.get("Client") or "", "device": s.get("DeviceName") or "",
                "id": item.get("Id"), "image": item.get("SeriesId") if item.get("SeriesPrimaryImageTag") else item.get("Id"),
                "tag": item.get("SeriesPrimaryImageTag") or (item.get("ImageTags") or {}).get("Primary"),
                "title": item.get("SeriesName") if episode else item.get("Name"),
                "detail": (f"S{item.get('ParentIndexNumber') or 0}E{item.get('IndexNumber') or 0} · {item.get('Name')}"
                           if episode else str(item.get("ProductionYear") or "")),
                "progress": int((play.get("PositionTicks") or 0) * 100 / runtime) if runtime else 0,
                "paused": play.get("IsPaused", False), "method": play.get("PlayMethod") or "DirectPlay",
                "video_direct": trans.get("IsVideoDirect", True), "hw": trans.get("HardwareAccelerationType") or "",
                "reasons": trans.get("TranscodeReasons") or []})
        return out

    def downloads(self, torrents: list | None) -> dict:
        """Counts and speeds from the sources that answered; "answering" says
        which did, so a source that's down never reads as "nothing there"
        (None: not set up). torrents is None when qBittorrent didn't answer."""
        transfer = c.try_json(f"{local('qbittorrent')}/api/v2/transfer/info") if torrents is not None else None
        key = c.sabnzbd_key(self.config)
        sab = c.try_json(local("sabnzbd") + f"/api?mode=queue&output=json&apikey={key}") if key else None
        queue = (sab or {}).get("queue") if isinstance(sab, dict) else None
        qbit_ok, sab_ok = torrents is not None and isinstance(transfer, dict), (isinstance(queue, dict) if key else None)
        try:
            sab_speed = float((queue or {}).get("kbpersec") or 0)
        except ValueError:
            sab_speed = 0.0
        active = re.compile(r"downloading|forcedDL|metaDL|stalledDL|queuedDL")
        rows = torrents or []
        known = qbit_ok and sab_ok is not False
        return {"answering": {"qbittorrent": qbit_ok, "sabnzbd": sab_ok},
                "dl_speed": int(((transfer or {}).get("dl_info_speed") or 0) + sab_speed * 1024) if known else None,
                "up_speed": (transfer or {}).get("up_info_speed") or 0 if qbit_ok else None,
                "downloading": sum(1 for t in rows if t["progress"] < 1 and active.search(t["state"]))
                               + int((queue or {}).get("noofslots") or 0) if known else None,
                "stalled": sum(1 for t in rows if t["progress"] < 1 and t["state"] in ("stalledDL", "metaDL")) if qbit_ok else None,
                "seeding": sum(1 for t in rows if t["progress"] >= 1 and re.search(r"uploading|stalledUP|forcedUP", t["state"]))
                           if qbit_ok else None}

    # ─── Slow: every few minutes ─────────────────────────────────

    def arr(self, url: str, key: str) -> dict:
        if not key:
            return {}
        h = {"X-Api-Key": key}
        queue = c.try_json(f"{url}/api/v3/queue/status", h)
        missing = c.try_json(f"{url}/api/v3/wanted/missing?pageSize=1&monitored=true", h)
        health = c.try_json(f"{url}/api/v3/health", h, []) or []
        return {"queue": (queue or {}).get("totalCount") if queue is not None else None,
                "missing": (missing or {}).get("totalRecords") if missing is not None else None,
                "health": [{"type": x.get("type"), "message": x.get("message")} for x in health
                           if x.get("source") != "UpdateCheck"]}

    def tailscale(self) -> dict:
        cli = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        if not os.access(cli, os.X_OK):
            return {"installed": False}
        result = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, check=False, timeout=10)
        try:
            st = json.loads(result.stdout)
        except ValueError:
            return {"installed": False}
        me = st.get("Self") or {}
        return {"installed": True, "state": st.get("BackendState"), "online": me.get("Online", False),
                "name": (me.get("DNSName") or "").rstrip("."), "ip": (me.get("TailscaleIPs") or [""])[0],
                "peers": len(st.get("Peer") or {}), "key_expiry": me.get("KeyExpiry")}

    def uptime(self) -> list:
        f = self.state / "dashstatus/uptime.json"
        sample = {name: 200 <= c.status_code(url) < 400 for name, url in uptime_checks()}
        hist = (c.read_json(f, []) or []) + [{"t": int(time.time()), "up": sample}]
        hist = hist[-UPTIME_KEEP:]
        c.write_json(f, hist, compact=True)
        out = []
        for name in sorted(hist[0]["up"]):
            known = [h["up"][name] for h in hist if h["up"].get(name) is not None]
            out.append({"name": name, "up_now": bool(hist[-1]["up"].get(name)),
                        "pct": int(sum(1 for x in known if x) * 100 / (len(known) or 1)),
                        "recent": [h["up"].get(name) for h in hist[-48:]]})
        return out

    def slow_data(self) -> dict:
        pk = c.arr_key(self.config, "prowlarr")
        indexers = c.try_json(local("prowlarr") + "/api/v1/indexer", {"X-Api-Key": pk}, []) or []
        statuses = c.try_json(local("prowlarr") + "/api/v1/indexerstatus", {"X-Api-Key": pk}, []) or []
        names = {i["id"]: i["name"] for i in indexers}
        by_id = {i["id"]: i for i in indexers}
        on = {i["id"] for i in indexers if i.get("enable")}
        now = time.time()
        # Switched-off indexers keep their last failure; only enabled ones count
        off = [{"name": names.get(s["indexerId"], f"indexer {s['indexerId']}"), "until": s["disabledTill"],
                **({"unresolved": host} if (host := unresolved(site(by_id.get(s["indexerId"]) or {}))) else {})}
               for s in statuses if s.get("indexerId") in on and s.get("disabledTill") and iso_time(s["disabledTill"]) > now]
        since = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stats = c.try_json(local("prowlarr") + f"/api/v1/indexerstats?startDate={since}", {"X-Api-Key": pk})
        auth = c.jellyfin_auth(self.state)
        counts = (c.try_json(local("jellyfin") + "/Items/Counts", auth, {}) or {}) if auth else {}
        bk = c.bazarr_key(self.config)
        badges = (c.try_json(local("bazarr") + f"/api/badges?apikey={bk}", default={}) or {}) if bk else {}
        sk = c.seerr_key(self.config)
        req = (c.try_json(local("seerr") + "/api/v1/request/count", {"X-Api-Key": sk}, {}) or {}) if sk else {}
        disk = shutil.disk_usage(self.media)
        gb = 1024 ** 3
        return {
            "sonarr": self.arr(local("sonarr"), c.arr_key(self.config, "sonarr")),
            "radarr": self.arr(local("radarr"), c.arr_key(self.config, "radarr")),
            "prowlarr": {"indexers": len(indexers), "enabled": len(on), "off": off},
            "library": {"movies": counts.get("MovieCount"), "series": counts.get("SeriesCount"),
                        "episodes": counts.get("EpisodeCount")},
            "subtitles": {"missing_episodes": badges.get("episodes"), "missing_movies": badges.get("movies")},
            "indexer_stats": ({"queries": sum(i.get("numberOfQueries") or 0 for i in stats.get("indexers", [])),
                               "grabs": sum(i.get("numberOfGrabs") or 0 for i in stats.get("indexers", [])),
                               "failed": sum(i.get("numberOfFailedQueries") or 0 for i in stats.get("indexers", []))}
                              if stats else {}),
            "tailscale": self.tailscale(), "uptime": self.uptime(),
            "requests": {k: req.get(k) for k in ("pending", "processing", "available", "total")},
            "disk": {"total_gb": disk.total // gb, "free_gb": disk.free // gb, "warn_gb": self.warn_gb, "min_gb": self.min_gb},
        }

    # ─── What needs you ──────────────────────────────────────────

    def attention(self, fast: dict, slow: dict, torrents: list) -> list:
        out = []
        answering = (fast.get("downloads") or {}).get("answering") or {}
        for name, label in (("qbittorrent", "qBittorrent"), ("sabnzbd", "SABnzbd")):
            if answering.get(name) is False:
                out.append({"level": "error", "text": f"{label} isn't answering: its downloads can't be seen, and new ones can't start",
                            "action": f"nix run .#logs -- {name}; nix run .#restart -- {name}"})
        conn = c.read_text(self.state / "netwatch/connection")
        postimport = c.read_json(self.state / "postimport/status.json", {}) or {}
        disk = slow.get("disk") or {}
        free = disk.get("free_gb", 1e9)
        if conn == "offline":
            out.append({"level": "warn", "text": "The Mac is offline: nothing can download, and Cleanuparr is paused",
                        "action": "It resumes on its own when the connection is back"})
        paused = c.read_json(self.state / "diskwatch/paused.json", None)
        if paused:
            out.append({"level": "error", "text": f"Downloads paused for space: {free} GB free",
                        "action": f"They resume on their own above {paused.get('resume_gb', '?')} GB; delete something to free space"})
        if free < disk.get("min_gb", 10):
            out.append({"level": "error", "text": f"Only {free} GB free: imports have stopped", "action": "Delete something in Sonarr or Radarr"})
        elif free < disk.get("warn_gb", 50):
            out.append({"level": "warn", "text": f"{free} GB free on the media disk", "action": f"Imports stop below {disk.get('min_gb')} GB"})
        off = (slow.get("prowlarr") or {}).get("off") or []
        # A site the DNS doesn't know won't come back by retrying (often an
        # internet provider's block): said apart from passing failures
        unknown = [o for o in off if o.get("unresolved")]
        passing = [o for o in off if not o.get("unresolved")]
        for o in unknown:
            out.append({"level": "warn", "text": f"{o['name']}: its site ({o['unresolved']}) can't be found; your internet provider may block it",
                        "action": f"Set enable = false for {o['name']} under [[indexers]] in config.toml, or use another DNS"})
        if passing:
            out.append({"level": "warn", "text": "Indexers switched off after failures: " + ", ".join(o["name"] for o in passing),
                        "action": "Usually temporary; Prowlarr → Indexers → Test All brings back the ones that work"})
        for app in ("sonarr", "radarr"):
            for h in (slow.get(app) or {}).get("health") or []:
                if h.get("type") == "error":
                    out.append({"level": "error", "text": f"{app.capitalize()}: {h.get('message')}", "action": f"{app.capitalize()} → System → Status"})
        now = time.time()
        for t in torrents:
            if t["progress"] < 1 and t["state"] in ("stalledDL", "metaDL") and now - t.get("added_on", now) > 86400:
                out.append({"level": "warn", "text": f"Not moving for over a day: {t['name'][:70]} ({t.get('num_seeds', 0)} seeders)",
                            "action": "Sonarr/Radarr → Activity: remove it with \"Blocklist release\" to try another"})
        for r in postimport.get("looking") or []:
            out.append({"level": "info", "text": f"Rejected {r['title']}: {r['reason']}", "action": "A better release is being looked for"})
        for r in postimport.get("kept") or []:
            out.append({"level": "warn", "text": f"Kept {r['title']} although {r['reason']}", "action": "No better release found; Interactive Search to pick one"})
        if (self.state / "e2e/paused-indexers.json").exists():
            out.append({"level": "error", "text": "An end-to-end test left Radarr indexers paused", "action": "Run nix run .#install"})
        # What affects watching is shown on the main page; the rest on Manage
        for item in out:
            item["scope"] = "manage"
        if fast.get("jellyfin_up") is False:
            out.insert(0, {"level": "error", "text": "Jellyfin isn't answering: nothing can be played right now",
                           "action": "nix run .#restart -- jellyfin; nix run .#logs -- jellyfin", "scope": "watch"})
        for p in fast.get("playing") or []:
            if p["method"] == "Transcode" and "SubtitleCodecNotSupported" in p["reasons"]:
                out.append({"level": "info", "text": f"{p['user']} is watching {p['title']} with picture subtitles burned into the video (heavy on the CPU)",
                            "action": "Pick a text subtitle (External/SRT) in the player", "scope": "watch"})
        return out

    # ─── Rounds ──────────────────────────────────────────────────

    def round(self, out: Path) -> None:
        slow_due = self.round_no % int(os.environ.get("DASH_SLOW_EVERY", "20")) == 0 or self.slow is None
        if slow_due:
            self.slow = self.slow_data()
            self.slow_at = int(time.time())
        # The media part with it, and every minute while part of it fails
        # (e.g. the services are still starting after a restart)
        if slow_due or self.media_data is None or (self.round_no % 4 == 0 and self.media_data.get("media_failed")):
            self.media_data = dashmedia.carry_over(self.media_data, self.media_part())
        self.round_no += 1
        # None (not []) when qBittorrent doesn't answer
        torrents = c.try_json(f"{local('qbittorrent')}/api/v2/torrents/info")
        torrents = torrents if isinstance(torrents, list) else None
        system = self.system()
        fast = {"system": system, "usage": self.services_usage(system.get("cpus") or 1, system.get("mem_total") or 0),
                "playing": self.playing(), "downloads": self.downloads(torrents),
                "connection": c.read_text(self.state / "netwatch/connection") or "unknown",
                "jellyfin_up": 200 <= c.status_code(f"{local('jellyfin')}/health") < 400,
                "speed_limit": control.Speed(self.config).state(),
                "fixing": dashmedia.fixing_now(self.state),
                "unfinished": unfinished.collect(self.state), "paused": pause.status(self.state)}
        slow = self.slow or {}
        status = {**fast, **slow, **(self.media_data or {}), "attention": self.attention(fast, slow, torrents or []),
                  "updated": int(time.time()), "slow_updated": self.slow_at}
        c.write_json(out, status, mode=0o644, compact=True)

    def media_part(self) -> dict:
        dashmedia.configure(self.config, self.state)
        return dashmedia.collect()


def main() -> None:
    config = Path(os.environ.get("DASH_CONFIG", c.MEDIA / "config"))
    state = Path(os.environ.get("DASH_STATE", c.MEDIA / ".state"))
    media = Path(os.environ.get("DASH_MEDIA", c.MEDIA))
    out = Path(os.environ.get("DASH_OUT", config / "nginx/www/status.json"))
    collector = Collector(config, state, media, int(os.environ.get("DISK_WARN_GB", "50")),
                          int(os.environ.get("DISK_MIN_GB", "10")))
    # The speed-limit control the page uses (127.0.0.1; nginx passes it on)
    try:
        control.serve(config, int(os.environ.get("DASH_CONTROL_PORT", PORTS["control"])))
    except OSError as e:
        c.log(f"the speed control couldn't start: {e}")
    while True:
        try:
            collector.round(out)
        except Exception as e:  # keep the page's data coming; log and retry next round
            c.log(f"round failed: {e}")
        time.sleep(int(os.environ.get("DASH_INTERVAL", "15")))


if __name__ == "__main__":
    main()
