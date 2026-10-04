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
_workers_held: dict[Path, int] = {}   # state dir → the worker lock an operation holds

# The background workers (postimport, netwatch) share .state/worker.lock
# while they change things, without waiting: if they can't get it, an
# operation has it and they skip. An operation takes it exclusively after
# its own lock and holds it to the end, waiting (WORKER_WAIT at most) for
# the workers' rounds underway to finish. Workers don't block each other
# (netwatch pausing Cleanuparr mustn't wait for a long rewrite); an
# operation and a worker never change things at the same time.
WORKER_WAIT = 30 * 60


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


def _worker_fd(state: Path) -> int:
    state.mkdir(parents=True, exist_ok=True)
    return os.open(state / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)


def acquire(state: Path, wait_workers: float = WORKER_WAIT) -> None:
    """The operation lock, then the workers': waits up to <wait_workers>
    seconds for a background round to finish (0: don't wait)"""
    if state in _held:
        return
    _acquire_operation(state)
    import time
    fd = _worker_fd(state)
    deadline, said = time.time() + wait_workers, False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() >= deadline:
                os.close(fd)
                release(state)
                raise err("The post-import checks are busy with a file; try again in a few minutes") from None
            if not said:
                print("   Waiting for the post-import checks to finish the file they're working on…", flush=True)
                said = True
            time.sleep(2)
    _workers_held[state] = fd


class worker_round:
    """with worker_round(state) as ok: a background worker's round, only
    when no operation holds the worker lock (ok is False then: skip it)"""

    def __init__(self, state: Path):
        self.state, self.fd, self.ok = state, -1, False

    def __enter__(self) -> bool:
        self.fd = _worker_fd(self.state)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)   # shared among workers
            self.ok = True
        except BlockingIOError:
            self.ok = False
        return self.ok

    def __exit__(self, *exc) -> None:
        if self.ok:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _acquire_operation(state: Path) -> None:
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
    wfd = _workers_held.pop(state, None)
    if wfd is not None:
        fcntl.flock(wfd, fcntl.LOCK_UN)
        os.close(wfd)
    fd = _held.pop(state, None)
    if fd is None:
        return
    shutil.rmtree(path(state), ignore_errors=True)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
