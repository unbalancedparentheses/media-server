"""The services as launchd user agents (~/Library/LaunchAgents), one per
service, labelled org.media-server.<name>."""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from mediaserver.ui import warn

# Names match flake.nix `services`
SERVICE_NAMES = ["jellyfin", "sonarr", "radarr", "prowlarr", "bazarr", "qbittorrent", "sabnzbd", "unpackerr",
                 "cleanuparr", "seerr", "byparr", "nginx", "diskwatch", "netwatch", "dashstatus", "postimport"]
LABEL_PREFIX = os.environ.get("MEDIA_LABEL_PREFIX", "org.media-server")


def domain() -> str:
    return f"gui/{os.getuid()}"


def label(name: str) -> str:
    return f"{LABEL_PREFIX}.{name}"


def plist(name: str) -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{label(name)}.plist"


def state(name: str) -> str:
    """"running (pid 123)", "stopped (last exit 1)" or "not installed\""""
    result = subprocess.run(["launchctl", "print", f"{domain()}/{label(name)}"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return "not installed"
    pid = re.search(r"^\s*pid = (\d+)$", result.stdout, re.M)
    if pid:
        return f"running (pid {pid.group(1)})"
    code = re.search(r"^\s*last exit code = (.*)$", result.stdout, re.M)
    return f"stopped (last exit {code.group(1) if code else '?'})"


def loaded(name: str) -> bool:
    return subprocess.run(["launchctl", "print", f"{domain()}/{label(name)}"], capture_output=True, check=False).returncode == 0


def bootstrap(name: str) -> bool:
    return subprocess.run(["launchctl", "bootstrap", domain(), str(plist(name))], capture_output=True, check=False).returncode == 0


def _pattern(config_dir: Path, name: str) -> str:
    # Every service's command line names its config directory
    return f"{config_dir}/{name}([/ ]|$)"


def _running(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True, check=False).returncode == 0


def kill_leftovers(config_dir: Path, name: str) -> None:
    """Some services (Bazarr) run their server as a child that outlives the
    launchd job: stop whatever still names the service's config directory"""
    pattern = _pattern(config_dir, name)
    if subprocess.run(["pkill", "-f", pattern], capture_output=True, check=False).returncode != 0:
        return
    for _ in range(10):
        if not _running(pattern):
            return
        time.sleep(1)
    subprocess.run(["pkill", "-9", "-f", pattern], capture_output=True, check=False)
    time.sleep(1)


def stop(config_dir: Path, name: str) -> bool:
    """Stop the service and its leftovers; False if anything survives"""
    if loaded(name):
        subprocess.run(["launchctl", "bootout", f"{domain()}/{label(name)}"], capture_output=True, check=False)
        for _ in range(30):
            if not loaded(name):
                break
            time.sleep(1)
    kill_leftovers(config_dir, name)
    if loaded(name) or _running(_pattern(config_dir, name)):
        warn(f"{name} did not stop")
        return False
    return True


def restart(config_dir: Path, name: str) -> bool:
    """A full stop (including leftover children) and start, rather than
    `launchctl kickstart -k`, which would leave Bazarr's server running"""
    if not loaded(name) or not stop(config_dir, name):
        return False
    return bootstrap(name)
