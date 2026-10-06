"""diskwatch: watches free space on the media disk.

- Warns with a macOS notification (at most every 6 hours) below warn_gb.
- Protects it ([disk] pause_downloads): below min_gb + reserve_gb, the
  downloads in progress are paused (qBittorrent's downloading torrents, not
  the seeding ones, and SABnzbd), so imports and the file fixes (which need
  room for a rewritten copy) don't run out of space; resumed once there's
  RESUME_MARGIN_GB more than the limit. The hold is shared with the VPN's
  (holds.py): each check reconciles with what the clients actually do,
  retrying until it shows, and a client resumes only when nothing holds it
  (SABnzbd paused by hand before stays paused).

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
from mediaserver import holds

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

def protect(media: Path, state: Path, min_gb: int, reserve_gb: int, enabled: bool, clients: holds.Clients,
            free_gb: int | None = None) -> str:
    """"paused", "resumed", "held" (still held, or a change not confirmed
    yet: retried next check) or "" (nothing to do). The hold is shared with
    the VPN's (holds.py): a client resumes only when neither holds it."""
    free = shutil.disk_usage(media).free // 1024 ** 3 if free_gb is None else free_gb
    limit = min_gb + reserve_gb
    was = "disk" in holds.held(state)
    on = enabled and free < (limit + RESUME_MARGIN_GB if was else limit)
    if not on and not was and not holds.load(state)["torrents"]:
        return ""
    confirmed = holds.reconcile(state, clients, "disk", on, {"free_gb": free, "limit_gb": limit, "resume_gb": limit + RESUME_MARGIN_GB})
    if on and not was:
        c.log(f"only {free} GB free (limit {limit} GB): pausing downloads until {limit + RESUME_MARGIN_GB} GB are free")
        c.notify("Media server: downloads paused",
                 f"Only {free} GB free on the media disk. They resume on their own above {limit + RESUME_MARGIN_GB} GB; delete something to free space.",
                 sound="Basso")
        return "paused"
    if was and not on:
        c.log(f"{free} GB free: resuming the downloads paused for space")
        c.notify("Media server: downloads resumed", f"{free} GB free on the media disk.")
        return "resumed"
    if not confirmed:
        c.log("the downloads don't match the hold yet; retried next check")
    return "held" if on or not confirmed else ""


def main() -> None:
    media = Path(os.environ.get("MEDIA_DIR", c.MEDIA))
    state = Path(os.environ.get("DISKWATCH_STATE", media / ".state/diskwatch"))
    state.mkdir(parents=True, exist_ok=True)
    clients = holds.Clients(media / "config")
    while True:
        warn_gb, min_gb = int(os.environ.get("DISK_WARN_GB", "50")), int(os.environ.get("DISK_MIN_GB", "10"))
        check(media, state, warn_gb, min_gb)
        try:
            protect(media, state.parent, min_gb, int(os.environ.get("DISK_RESERVE_GB", "20")),
                    os.environ.get("DISK_PAUSE", "true") == "true", clients)
        except (OSError, ValueError) as e:
            c.log(f"couldn't check the downloads: {e}")
        time.sleep(int(os.environ.get("DISKWATCH_INTERVAL", "120")))


if __name__ == "__main__":
    main()
