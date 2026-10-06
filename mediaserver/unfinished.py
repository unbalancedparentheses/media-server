"""Work started and not finished yet, for the dashboard's Manage page and
the doctor: what it is, what's left, the last error and when it's tried
again. Read from the records each part keeps:

- postimport: MP4 repackaging mid-way (repackage_pending) and file fixes
  being retried after a failure (failures);
- deleting from the dashboard: plans with steps left (deletions.json);
- diskwatch: downloads paused for space (paused.json);
- an update's rollback record left behind (update-rollback.json).
"""
from __future__ import annotations

import time
from pathlib import Path

from mediaserver import common as c

STEP_NAMES = {"switch": "remove the MKV", "rescan": "Sonarr/Radarr rescan", "jellyfin": "tell Jellyfin",
              "playback": "stream check", "queue": "leave the download queue", "arr": "remove from Radarr/Sonarr",
              "unmonitor": "stop monitoring", "unmonitor_movie": "stop monitoring", "files": "delete files",
              "torrents": "remove torrents", "seerr": "clear the Seerr request", "refresh": "Jellyfin rescan"}
RETRY_FIX_AFTER = 3600   # postimport.RETRY_FIX_AFTER (not imported: postimport is heavy)


def steps(names: list) -> str:
    return ", ".join(STEP_NAMES.get(n, str(n)) for n in names)


def collect(state: Path, now: float | None = None) -> list:
    """[{"what", "left", "error", "retry"}], oldest kinds first"""
    now = time.time() if now is None else now
    out: list = []
    pi = c.read_json(state / "postimport/state.json", {}) or {}
    for key, e in (pi.get("repackage_pending") or {}).items():
        out.append({"what": f"Repackaging {e.get('title') or Path(key).stem} as MP4", "left": steps(e.get("steps") or []),
                    "error": "", "retry": "in the next round (a minute)"})
    for path, f in (pi.get("failures") or {}).items():
        when = f.get("last", now) + RETRY_FIX_AFTER
        out.append({"what": f"Fixing {c.strip_quality(Path(path).stem)}", "left": ", ".join(f.get("what") or []),
                    "error": f"failed {f.get('count', 1)}×, see the postimport log",
                    "retry": "now" if when <= now else time.strftime("at %H:%M", time.localtime(when))})
    for plan in (c.read_json(state / "deletions.json", {}) or {}).values():
        if isinstance(plan, dict) and plan.get("steps"):
            verb = "Stopping looking for" if plan.get("stop") else "Deleting"
            out.append({"what": f"{verb} {plan.get('title')}", "left": steps(plan["steps"]), "error": "",
                        "retry": "do it again from the dashboard to finish"})
    paused = c.read_json(state / "diskwatch/paused.json", None)
    if paused:
        out.append({"what": "Downloads paused for space", "left": f"resume above {paused.get('resume_gb', '?')} GB free",
                    "error": f"{paused.get('free_gb', '?')} GB free when paused", "retry": "every 2 minutes"})
    rollback = c.read_json(state / "update-rollback.json", None)
    if rollback:
        out.append({"what": "An update that didn't finish", "left": f"its checks (back to {str(rollback.get('from', ''))[:7]} if they fail)",
                    "error": "", "retry": "run nix run .#update again"})
    return out
