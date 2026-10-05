"""~/media/.state/credentials.json (0600, like config.toml) records, per
service, the login it last verified working. A service's entry only
advances after its new login was checked, so an interrupted or failed
change is retried on the next run, and services that need the current
password to set a new one (Jellyfin) still have it.

Always read from and written to the file (never cached), so bash and
Python steps in the same install see each other's changes."""
from __future__ import annotations

import json
from pathlib import Path

from mediaserver import common as c
from mediaserver.ui import warn

SHARED = ("jellyfin", "sonarr", "radarr", "prowlarr", "bazarr", "sabnzbd")


def path(state: Path) -> Path:
    return state / "credentials.json"


def load(state: Path) -> dict:
    """The record (version 2), upgrading the older format (one shared
    jellyfin/qbittorrent entry): the shared login is assumed for the
    services it covered, except Cleanuparr, whose password setup may not
    have applied"""
    f = path(state)
    if not f.exists():
        return {"version": 2, "services": {}}
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        warn(f"{f} is unreadable; every service's login will be re-applied")
        return {"version": 2, "services": {}}
    if data.get("version") == 2:
        return data
    services = {s: data["jellyfin"] for s in SHARED} if data.get("jellyfin") else {}
    if data.get("qbittorrent"):
        services["qbittorrent"] = data["qbittorrent"]
    return {"version": 2, "services": services}


def get(state: Path, service: str, field: str) -> str:
    """username or password recorded for <service>; empty if none"""
    return (load(state)["services"].get(service) or {}).get(field, "")


def match(state: Path, service: str, user: str, password: str) -> bool:
    entry = load(state)["services"].get(service) or {}
    return entry.get("username") == user and entry.get("password") == password


def record(state: Path, service: str, user: str, password: str) -> None:
    """Record <service>'s verified login (atomically, 0600)"""
    data = load(state)
    data["services"][service] = {"username": user, "password": password}
    c.write_json(path(state), data, mode=0o600)


def forget(state: Path, service: str) -> None:
    """<service> has no login now (set again, and recorded, if it needs one)"""
    data = load(state)
    if data["services"].pop(service, None) is not None:
        c.write_json(path(state), data, mode=0o600)
