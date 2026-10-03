"""Install, update, restore, backup, uninstall, restart and the e2e test
all change the same services; only one may run at a time. The lock is a
directory (mkdir is atomic) holding the owner's PID; a lock whose owner is
gone is stale and taken over. netwatch leaves Cleanuparr alone while it's
held (common.operation_running)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from mediaserver.ui import err


def path(state: Path) -> Path:
    return state / "lock"


def owner(state: Path) -> int | None:
    """The PID holding the lock, if it's still running"""
    try:
        pid = int((path(state) / "pid").read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def acquire(state: Path) -> None:
    lock = path(state)
    state.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        pid = owner(state)
        if pid and pid != os.getpid():
            command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                     check=False).stdout.strip()[:70]
            raise err(f"Another media-server operation is running (PID {pid}: {command}). Wait for it to finish, then try again.") from None
        # The owner is gone (or it's us): take it over
        shutil.rmtree(lock, ignore_errors=True)
        try:
            lock.mkdir()
        except OSError:
            raise err(f"Couldn't take the operation lock ({lock})") from None
    (lock / "pid").write_text(f"{os.getpid()}\n")


def release(state: Path) -> None:
    """Only our own lock"""
    try:
        if int((path(state) / "pid").read_text().strip()) == os.getpid():
            shutil.rmtree(path(state), ignore_errors=True)
    except (OSError, ValueError):
        pass
