"""What every part of the media server shares: where things live, the
services' API keys, HTTP calls, logging, setup's operation lock, atomic
writes and macOS notifications."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

MEDIA = Path(os.environ.get("MEDIA_DIR", Path.home() / "media"))

# Errors an HTTP call to a service can raise (down, refusing, bad answer)
HTTP_ERRORS = (OSError, urllib.error.URLError, ValueError)


def log(message: str) -> None:
    print(f"{time.strftime('%F %T')} {message}", flush=True)


# ─── Files ───────────────────────────────────────────────────────

def read_json(path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_atomic(path, text: str, mode: int | None = None) -> None:
    """Write next to the target, then rename over it: readers never see a
    half-written file"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_json(path, data: Any, mode: int | None = None, compact: bool = False) -> None:
    write_atomic(path, json.dumps(data, separators=(",", ":")) if compact else json.dumps(data, indent=1), mode)


def read_text(path, default: str = "") -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


# ─── Setup's operation lock ──────────────────────────────────────

def operation_running(lock: Path) -> bool:
    """An install/update/restore/e2e holds setup's lock (and is alive)"""
    try:
        owner = int((lock / "pid").read_text().strip())
        os.kill(owner, 0)
        return True
    except (OSError, ValueError):
        return False


# ─── API keys ────────────────────────────────────────────────────

def arr_key(config: Path, app: str) -> str:
    """Sonarr/Radarr/Prowlarr: <ApiKey> in config.xml"""
    m = re.search(r"<ApiKey>(.*?)</ApiKey>", read_text(config / app / "config.xml"))
    return m.group(1) if m else ""


def bazarr_key(config: Path) -> str:
    """auth.apikey in Bazarr's config.yaml (read without a YAML library)"""
    in_auth = False
    for line in read_text(config / "bazarr/config/config.yaml").splitlines():
        if line.startswith("auth:"):
            in_auth = True
        elif in_auth and line and not line.startswith(" "):
            break
        elif in_auth and line.startswith("  apikey:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def seerr_key(config: Path) -> str:
    return (read_json(config / "seerr/settings.json", {}) or {}).get("main", {}).get("apiKey", "")


def sabnzbd_key(config: Path) -> str:
    m = re.search(r"^api_key = *(\S+)", read_text(config / "sabnzbd/sabnzbd.ini"), re.M)
    return m.group(1) if m else ""


def cleanuparr_key(config: Path) -> str:
    try:
        with sqlite3.connect(f"file:{config / 'cleanuparr/users.db'}?mode=ro", uri=True) as db:
            row = db.execute("SELECT api_key FROM users LIMIT 1").fetchone()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""


def jellyfin_auth(state: Path) -> dict:
    """The Authorization header for setup's own Jellyfin key (Jellyfin 12
    doesn't take X-Emby-Token)"""
    key = read_text(state / "dashstatus/jellyfin-key")
    return {"Authorization": f'MediaBrowser Token="{key}"'} if key else {}


# ─── HTTP ────────────────────────────────────────────────────────

def http(url: str, headers: dict | None = None, method: str = "GET", body: Any = None,
         timeout: float = 15, form: dict | None = None) -> bytes:
    """The raw answer; raises on connection errors and HTTP errors (4xx/5xx)"""
    data, hdrs = None, dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_json(url: str, headers: dict | None = None, timeout: float = 15) -> Any:
    raw = http(url, headers, timeout=timeout)
    return json.loads(raw) if raw else {}


def try_json(url: str, headers: dict | None = None, default: Any = None, timeout: float = 10) -> Any:
    """get_json, or default when the service doesn't answer properly"""
    try:
        return get_json(url, headers, timeout)
    except HTTP_ERRORS:
        return default


def status_code(url: str, timeout: float = 5) -> int:
    """The HTTP status (0 when nothing answers)"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (OSError, ValueError):
        return 0


# ─── macOS ───────────────────────────────────────────────────────

def notify(title: str, message: str, sound: str | None = None) -> None:
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    if sound:
        script += f" sound name {json.dumps(sound)}"
    subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, check=False)


def background(cmd: list) -> list:
    """Run with background priority (low CPU, efficiency cores) on macOS"""
    return (["/usr/sbin/taskpolicy", "-b"] + cmd) if os.path.exists("/usr/sbin/taskpolicy") else cmd

