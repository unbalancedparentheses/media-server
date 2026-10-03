"""The services as launchd user agents (~/Library/LaunchAgents), one per
service, labelled org.media-server.<name>."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

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
