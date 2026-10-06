"""diskwatch: watches free space on the media disk.

- Warns with a macOS notification (at most every 6 hours) below warn_gb.
- Protects it ([disk] pause_downloads): below min_gb + reserve_gb, the
  downloads in progress are paused (qBittorrent's downloading torrents, not
  the seeding ones, and SABnzbd), so imports and the file fixes (which need
  room for a rewritten copy) don't run out of space. What it pauses is
  recorded first, and only that is resumed, once there's RESUME_MARGIN_GB
  more than the limit. Downloads that start while paused are paused too.

Runs as a launchd agent (see flake.nix), every DISKWATCH_INTERVAL seconds.
Environment: MEDIA_DIR, DISKWATCH_STATE, DISK_WARN_GB, DISK_MIN_GB,
DISK_RESERVE_GB, DISK_PAUSE ("true"/"false"), DISKWATCH_INTERVAL.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from mediaserver import common as c
from mediaserver.config import local

NOTIFY_EVERY = 6 * 3600
RESUME_MARGIN_GB = 10


def check(media: Path, state: Path, warn_gb: int, min_gb: int, now: float | None = None) -> bool:
    """Warn if free space is below warn_gb; True when a notification was shown"""
    now = time.time() if now is None else now
    free_gb = shutil.disk_usage(media).free // 1024 ** 3
    if free_gb >= warn_gb:
        return False
    c.log(f"low disk space: {free_gb} GB free (warning below {warn_gb} GB)")
    last_file = state / "last-warning"
    try:
        last = float(c.read_text(last_file, "0"))
    except ValueError:
        last = 0
    if now - last < NOTIFY_EVERY:
        return False
    c.notify("Media server: disk almost full",
             f"Only {free_gb} GB free on the media disk. Imports stop below {min_gb} GB; delete something or add space.",
             sound="Basso")
    c.write_atomic(last_file, f"{int(now)}\n")
    return True


# ─── Pausing downloads ───────────────────────────────────────────

class Clients:
    """qBittorrent (it skips its login for this Mac) and SABnzbd"""

    def __init__(self, config: Path):
        self.config = config

    def downloading(self) -> list | None:
        """Hashes of the torrents downloading now; None when qBittorrent didn't answer"""
        r = c.request(local("qbittorrent") + "/api/v2/torrents/info?filter=downloading", timeout=20)
        torrents = r.json(None) if r.ok else None
        return [t["hash"] for t in torrents if t.get("hash")] if isinstance(torrents, list) else None

    def torrents(self, action: str, hashes: list) -> bool:
        """action: "stop" or "start" (qBittorrent 5; 4 called them pause/resume)"""
        if not hashes:
            return True
        old = {"stop": "pause", "start": "resume"}[action]
        for name in (action, old):
            r = c.request(local("qbittorrent") + f"/api/v2/torrents/{name}", "POST", form={"hashes": "|".join(hashes)}, timeout=20)
            if r.ok:
                return True
            if r.status != 404:
                return False
        return False

    def sabnzbd(self, mode: str) -> bool:
        """mode: "pause" or "resume"; True when there's no SABnzbd to tell"""
        key = c.sabnzbd_key(self.config)
        if not key:
            return True
        r = c.request(local("sabnzbd") + f"/api?mode={mode}&apikey={key}&output=json", timeout=20)
        return r.ok and (r.json({}) or {}).get("status") is not False


def protect(media: Path, state: Path, min_gb: int, reserve_gb: int, enabled: bool, clients: Clients,
            free_gb: int | None = None) -> str:
    """"paused", "resumed", "held" (still paused) or "" (nothing to do)"""
    free = shutil.disk_usage(media).free // 1024 ** 3 if free_gb is None else free_gb
    limit = min_gb + reserve_gb
    record = state / "paused.json"
    paused = c.read_json(record, None)
    if paused and (not enabled or free >= limit + RESUME_MARGIN_GB):
        if clients.torrents("start", paused.get("hashes") or []) and (not paused.get("sabnzbd") or clients.sabnzbd("resume")):
            record.unlink(missing_ok=True)
            c.log(f"{free} GB free: resumed the downloads paused for space")
            c.notify("Media server: downloads resumed", f"{free} GB free on the media disk.")
            return "resumed"
        c.log("couldn't resume the downloads yet; retried next check")
        return "held"
    if not enabled or (not paused and free >= limit):
        return ""
    hashes = clients.downloading()
    if hashes is None:
        return "held" if paused else ""
    new = [h for h in hashes if h not in (paused or {}).get("hashes", [])]
    first = not paused
    paused = {"since": (paused or {}).get("since", int(time.time())), "free_gb": free, "limit_gb": limit,
              "resume_gb": limit + RESUME_MARGIN_GB, "hashes": sorted(set((paused or {}).get("hashes", [])) | set(new)),
              "sabnzbd": True}
    # Recorded before pausing: whatever happens next, it's resumed later
    c.write_json(record, paused)
    clients.torrents("stop", new)
    if first:
        clients.sabnzbd("pause")
        c.log(f"only {free} GB free (limit {limit} GB): paused {len(new)} downloads until {limit + RESUME_MARGIN_GB} GB are free")
        c.notify("Media server: downloads paused",
                 f"Only {free} GB free on the media disk. They resume on their own above {limit + RESUME_MARGIN_GB} GB; delete something to free space.",
                 sound="Basso")
        return "paused"
    return "held"


def main() -> None:
    media = Path(os.environ.get("MEDIA_DIR", c.MEDIA))
    state = Path(os.environ.get("DISKWATCH_STATE", media / ".state/diskwatch"))
    state.mkdir(parents=True, exist_ok=True)
    clients = Clients(media / "config")
    while True:
        warn_gb, min_gb = int(os.environ.get("DISK_WARN_GB", "50")), int(os.environ.get("DISK_MIN_GB", "10"))
        check(media, state, warn_gb, min_gb)
        try:
            protect(media, state, min_gb, int(os.environ.get("DISK_RESERVE_GB", "20")),
                    os.environ.get("DISK_PAUSE", "true") == "true", clients)
        except (OSError, ValueError) as e:
            c.log(f"couldn't check the downloads: {e}")
        time.sleep(int(os.environ.get("DISKWATCH_INTERVAL", "120")))


if __name__ == "__main__":
    main()
