"""Whether a login works, per service: config.toml's login is checked by
actually logging in (or, for qBittorrent, against its stored hash)."""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from mediaserver import common as c


def arr(url: str, user: str, password: str) -> bool:
    """Sonarr/Radarr/Prowlarr form login: a redirect to the app on success,
    back to /login?…loginFailed on failure"""
    r = c.request(f"{url}/login", "POST", form={"username": user, "password": password}, follow=False)
    location = r.headers.get("Location") or r.headers.get("location") or ""
    return 300 <= r.status < 400 and bool(location) and "loginFailed" not in location


def sabnzbd(url: str, user: str, password: str) -> bool:
    """SABnzbd's login form: 303 to the app when the login works"""
    return c.request(f"{url}/login/", "POST", form={"username": user, "password": password}, follow=False).status == 303


def bazarr(url: str, user: str, password: str) -> bool:
    """Bazarr's login API: 204 when the login works"""
    return c.request(f"{url}/api/system/account?action=login", "POST",
                     form={"username": user, "password": password}).status == 204


def cleanuparr(url: str, user: str, password: str) -> bool:
    """Cleanuparr's login API: tokens come back when the login works"""
    r = c.request(f"{url}/api/auth/login", "POST", body={"username": user, "password": password})
    return bool(((r.json({}) or {}).get("tokens") or {}).get("accessToken"))


def qbit_ini(config_dir: Path) -> Path:
    return config_dir / "qbittorrent/qBittorrent/config/qBittorrent.ini"


def qbittorrent_password_is(config_dir: Path, password: str) -> bool:
    """qBittorrent.ini holds <password>'s hash (PBKDF2-SHA512; qBittorrent
    writes it as soon as the password changes). Logging in can't tell:
    requests from this Mac skip the password."""
    m = re.search(r'WebUI\\Password_PBKDF2="?@ByteArray\(([^:]+):([^)]+)\)', c.read_text(qbit_ini(config_dir)))
    if not m:
        return False
    salt, key = base64.b64decode(m.group(1)), base64.b64decode(m.group(2))
    return hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, len(key)) == key


def qbittorrent_cookie(url: str, user: str, password: str) -> str:
    """qBittorrent's session cookie ("name=value"): SID up to 4.x,
    QBT_SID_<port> since 5.0; empty when the login fails"""
    r = c.request(f"{url}/api/v2/auth/login", "POST", form={"username": user, "password": password})
    for k, v in r.headers.items():
        if k.lower() == "set-cookie":
            m = re.match(r"(SID|QBT_SID_\d+)=([^;]+)", v)
            if m:
                return f"{m.group(1)}={m.group(2)}"
    return ""
