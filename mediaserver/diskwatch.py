"""diskwatch: checks free space on the media disk every 30 minutes and shows
a macOS notification (at most every 6 hours) when it runs low. Runs as a
launchd agent (see flake.nix).

Environment: MEDIA_DIR, DISKWATCH_STATE, DISK_WARN_GB, DISK_MIN_GB,
DISKWATCH_INTERVAL.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from mediaserver import common as c

NOTIFY_EVERY = 6 * 3600


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


def main() -> None:
    media = Path(os.environ.get("MEDIA_DIR", c.MEDIA))
    state = Path(os.environ.get("DISKWATCH_STATE", media / ".state/diskwatch"))
    state.mkdir(parents=True, exist_ok=True)
    while True:
        check(media, state, int(os.environ.get("DISK_WARN_GB", "50")), int(os.environ.get("DISK_MIN_GB", "10")))
        time.sleep(int(os.environ.get("DISKWATCH_INTERVAL", "1800")))


if __name__ == "__main__":
    main()
