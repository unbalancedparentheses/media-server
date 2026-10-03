"""Install, update, restore, backup, uninstall, restart and the e2e test
all change the same services; only one may run at a time.

The exclusion is an OS lock (flock) on .state/operation.lock, held while
the operation runs: the kernel gives it to one process only and releases it
when that process ends, however it ends, so there's no stale lock to guess
about. .state/lock/pid names the holder for netwatch and postimport, which
leave the services alone while it's held (common.operation_running)."""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from pathlib import Path

from mediaserver import common as c
from mediaserver.ui import err

_held: dict[Path, int] = {}   # state dir → the open, locked file


def path(state: Path) -> Path:
    return state / "lock"


def owner(state: Path) -> int | None:
    """The PID recorded as holding the lock, if it's still running"""
    try:
        pid = int((path(state) / "pid").read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def acquire(state: Path) -> None:
    if state in _held:
        return
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / "operation.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        pid = owner(state)
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                 check=False).stdout.strip()[:70] if pid else ""
        raise err(f"Another media-server operation is running{f' (PID {pid}: {command})' if pid else ''}. "
                  "Wait for it to finish, then try again.") from None
    _held[state] = fd
    # Ours now: whatever an earlier holder left behind is stale
    shutil.rmtree(path(state), ignore_errors=True)
    path(state).mkdir()
    c.write_atomic(path(state) / "pid", f"{os.getpid()}\n")


def release(state: Path) -> None:
    """Only a lock this process holds"""
    fd = _held.pop(state, None)
    if fd is None:
        return
    shutil.rmtree(path(state), ignore_errors=True)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
