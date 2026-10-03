"""byparr: starts Byparr, which gets indexers past Cloudflare's checks.
It isn't in nixpkgs, so its pinned source (flake input) is run with uv, as
upstream documents. Its virtualenv, Python and patched Firefox live in
BYPARR_STATE; the browser is fetched once (a marker records it, written
only after the fetch worked). Runs as a launchd agent (see flake.nix).

Environment: BYPARR_SRC (the source), BYPARR_STATE, BYPARR_UV (uv),
plus Byparr's own HOST, PORT, INVPW_TRUE_HEADLESS.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from mediaserver import common as c

UV_RUN = ["run", "--frozen", "--no-dev"]


def environment(state: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}   # ours, not Byparr's
    env.update(UV_PROJECT_ENVIRONMENT=str(state / "venv"), UV_CACHE_DIR=str(state / "uv-cache"),
               UV_PYTHON_INSTALL_DIR=str(state / "python"), PYTHONDONTWRITEBYTECODE="1")
    return env


def prepare(uv: str, src: Path, state: Path, env: dict) -> bool:
    """The virtualenv in step with the pinned source, and the browser once;
    False (logged) if either failed"""
    state.mkdir(parents=True, exist_ok=True)
    if subprocess.run([uv, "sync", "--frozen", "--no-dev", "--quiet"], cwd=src, env=env, check=False).returncode:
        c.log("couldn't set up Byparr's Python environment (uv sync failed)")
        return False
    marker = state / "browser-fetched"
    if not marker.exists():
        c.log("fetching Byparr's browser (once)")
        if subprocess.run([uv, *UV_RUN, "python", "-m", "invisible_playwright", "fetch"], cwd=src, env=env, check=False).returncode:
            c.log("couldn't fetch Byparr's browser")
            return False
        marker.touch()
    return True


def main() -> int:
    src, state = Path(os.environ["BYPARR_SRC"]), Path(os.environ["BYPARR_STATE"])
    uv = os.environ.get("BYPARR_UV", "uv")
    env = environment(state)
    if not prepare(uv, src, state, env):
        return 1   # launchd starts it again (at most every 10s)
    os.chdir(src)
    os.execvpe(uv, [uv, *UV_RUN, "python", "main.py"], env)
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main())
