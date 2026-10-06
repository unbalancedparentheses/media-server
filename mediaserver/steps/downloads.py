"""Download clients: qBittorrent (torrents) and SABnzbd (Usenet, with the
providers from config.toml)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time

from mediaserver import api, creds, launchd, logins
from mediaserver import common as c
from mediaserver.config import Config, Keys
from mediaserver.ui import info, ok, warn

CATEGORIES = ("sonarr", "radarr")


# ─── qBittorrent ─────────────────────────────────────────────────

def ini_set_login(config_dir, user: str, password: str, bind: str = "") -> None:
    """Write the Web UI login into qBittorrent.ini (PBKDF2-SHA512, like
    qBittorrent itself); qBittorrent must not be running"""
    path = logins.qbit_ini(config_dir)
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, 64)
    prefs = {"WebUI\\Username": user,
             "WebUI\\Password_PBKDF2": f'"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(key).decode()})"'}
    if bind:
        prefs.update({"WebUI\\Address": bind, "WebUI\\Port": "8081"})
    lines = path.read_text().splitlines() if path.exists() else []
    if not any(line.strip() == "[LegalNotice]" for line in lines):
        lines = ["[LegalNotice]", "Accepted=true", ""] + lines
    if not any(line.strip() == "[Preferences]" for line in lines):
        lines += ["", "[Preferences]"]
    lines = [line for line in lines if line.split("=", 1)[0] not in prefs]
    i = lines.index("[Preferences]") + 1
    lines[i:i] = [f"{k}={v}" for k, v in prefs.items()]
    c.write_atomic(path, "\n".join(lines) + "\n", mode=0o600)


def seed_qbittorrent(cfg: Config) -> None:
    """The Web UI login written before first start, which avoids the random
    first-run password that's only printed to qBittorrent's log. On macOS
    qBittorrent reads qBittorrent.ini. An existing password is kept here;
    the qbittorrent step changes it when config.toml's differs."""
    ini = logins.qbit_ini(cfg.paths.config)
    if ini.exists() and "WebUI\\Password_PBKDF2=" in ini.read_text():
        return
    bind = cfg.get("network.admin_bind", "0.0.0.0")
    ini_set_login(cfg.paths.config, cfg.qbit_user, cfg.qbit_pass, "*" if bind == "0.0.0.0" else bind)
    ok("qBittorrent: qBittorrent.ini")


def preferences(cfg: Config) -> dict:
    complete = cfg.paths.downloads / "torrents/complete"
    bind = cfg.get("network.admin_bind", "0.0.0.0")
    # auto_tmm_enabled: save each download in its category folder
    # (complete/radarr, …) instead of the default folder. Requests from this
    # Mac skip qBittorrent's login (bypass_local_auth), which is how setup,
    # the *arr apps and the dashboard reach it.
    return {"web_ui_username": cfg.qbit_user, "save_path": str(complete),
            "temp_path": str(cfg.paths.downloads / "torrents/incomplete"), "temp_path_enabled": True,
            "web_ui_address": "*" if bind == "0.0.0.0" else bind, "web_ui_port": 8081,
            "max_ratio": cfg.get("downloads.seeding_ratio"), "max_seeding_time": cfg.get("downloads.seeding_time_minutes"),
            "auto_tmm_enabled": True, "up_limit": int(cfg.get("downloads.upload_limit_kib", 100)) * 1024,
            "web_ui_csrf_protection_enabled": True, "bypass_local_auth": True, "bypass_auth_subnet_whitelist_enabled": False}


class Qbittorrent:
    def __init__(self, cfg: Config):
        self.cfg, self.url, self.cookie = cfg, cfg.urls.qbittorrent, ""

    def login(self) -> bool:
        tries = [(self.cfg.qbit_user, self.cfg.qbit_pass),
                 (creds.get(self.cfg.paths.state, "qbittorrent", "username"), creds.get(self.cfg.paths.state, "qbittorrent", "password"))]
        for user, password in tries:
            if not password:
                continue
            for _ in range(3):
                self.cookie = logins.qbittorrent_cookie(self.url, user, password)
                if self.cookie:
                    return True
                time.sleep(2)
        return False

    def get(self, path: str):
        return api.get(f"{self.url}/api/v2/{path}", {"Cookie": self.cookie}, timeout=20)

    def post(self, path: str, form: dict, tries: int = 1) -> bool:
        for attempt in range(tries):
            if c.request(f"{self.url}/api/v2/{path}", "POST", {"Cookie": self.cookie}, form=form, timeout=20).ok:
                return True
            if attempt + 1 < tries:
                time.sleep(2)
        return False


def qbittorrent(cfg: Config) -> None:
    info("Configuring qBittorrent...")
    q = Qbittorrent(cfg)
    if not q.login():
        warn(f"Could not log in to qBittorrent (check qbittorrent.password in {cfg.paths.config_file})")
        return
    ok("Logged in")
    prefs = preferences(cfg)
    try:
        old_address = (q.get("app/preferences") or {}).get("web_ui_address")
    except api.ApiError:
        old_address = None
    up = int(cfg.get("downloads.upload_limit_kib", 100))
    if q.post("app/setPreferences", {"json": json.dumps(prefs)}, tries=3):
        ok(f"Preferences set (upload limit: {'none' if up == 0 else f'{up} KiB/s'})")
    else:
        warn("Could not set qBittorrent's preferences (retried next run)")
    set_qbittorrent_password(cfg, q)
    vpn_settings(cfg)
    # A new listen address only takes effect after a restart
    if old_address and old_address != prefs["web_ui_address"] \
            and launchd.restart(cfg.paths.config, "qbittorrent") and api.wait_for("qBittorrent", q.url):
        ok(f"qBittorrent restarted to listen on {cfg.get('network.admin_bind', '0.0.0.0')}")
    categories(cfg, q)


def categories(cfg: Config, q: Qbittorrent) -> None:
    """Create or fix each category's folder; unchanged ones are left alone"""
    complete = cfg.paths.downloads / "torrents/complete"
    try:
        cats = q.get("torrents/categories") or {}
    except api.ApiError:
        warn("Couldn't read qBittorrent's categories (retried next run)")
        return
    for cat in CATEGORIES:
        folder = str(complete / cat)
        current = (cats.get(cat) or {}).get("savePath") if cat in cats else None
        if current == folder:
            ok(f"Category: {cat}")
        elif cat not in cats:
            if q.post("torrents/createCategory", {"category": cat, "savePath": folder}):
                ok(f"Category: {cat} (created)")
            else:
                warn(f"Could not create category: {cat}")
        elif q.post("torrents/editCategory", {"category": cat, "savePath": folder}):
            ok(f"Category: {cat} (folder fixed)")
        else:
            warn(f"Could not update category: {cat}")


def set_qbittorrent_password(cfg: Config, q: Qbittorrent) -> None:
    """The Web UI password from config.toml, recorded once qBittorrent.ini
    shows it. Its API refuses passwords under 6 characters; those are
    written to qBittorrent.ini while it's stopped."""
    user, password, config_dir = cfg.qbit_user, cfg.qbit_pass, cfg.paths.config
    if creds.match(cfg.paths.state, "qbittorrent", user, password) and logins.qbittorrent_password_is(config_dir, password):
        ok(f"qBittorrent login: {user}")
        return
    if len(password) >= 6:
        q.post("app/setPreferences", {"json": json.dumps({"web_ui_password": password})})
    elif launchd.stop(config_dir, "qbittorrent"):
        ini_set_login(config_dir, user, password)
        launchd.bootstrap("qbittorrent")
        api.wait_for("qBittorrent", q.url)
    for _ in range(15):
        if logins.qbittorrent_password_is(config_dir, password):
            creds.record(cfg.paths.state, "qbittorrent", user, password)
            ok(f"qBittorrent login set: {user}")
            return
        time.sleep(1)
    warn("qBittorrent's password didn't change (retried next run)")


# ─── SABnzbd ─────────────────────────────────────────────────────

class Sabnzbd:
    def __init__(self, cfg: Config, key: str):
        self.cfg, self.url, self.key = cfg, cfg.urls.sabnzbd, key

    def api(self, fields: dict, tries: int = 1):
        """SABnzbd's API (one URL with a mode); the decoded answer, or None"""
        for attempt in range(tries):
            r = c.request(f"{self.url}/api", "POST", form={**fields, "apikey": self.key, "output": "json"})
            if r.ok:
                return r.json({}) or {}
            if attempt + 1 < tries:
                time.sleep(2)
        return None

    def set(self, section: str, keyword: str, value: str, server: str | None = None) -> bool:
        """One setting, retried. A server's setting: the server is the
        keyword, the setting a field."""
        fields = ({"mode": "set_config", "section": section, "keyword": server, keyword: value} if server
                  else {"mode": "set_config", "section": section, "keyword": keyword, "value": value})
        answer = self.api(fields, tries=3)
        # An HTTP 200 can still be SABnzbd saying no ({"status": false, "error": …})
        return isinstance(answer, dict) and answer.get("status") is not False and not answer.get("error")

    def servers(self):
        """The servers ([{name, enable}]); None when SABnzbd didn't answer.
        A fresh SABnzbd leaves the list out: that's no servers."""
        answer = self.api({"mode": "get_config", "section": "servers"})
        if answer is None:
            return None
        servers = (answer.get("config") or {}).get("servers") if isinstance(answer, dict) else None
        try:
            return [{"name": s["name"], "enable": s.get("enable")} for s in servers or []]
        except (KeyError, TypeError):
            return None


def sabnzbd(cfg: Config) -> None:
    key = Keys.read(cfg.paths.config).sabnzbd
    if not key:
        return
    info("Configuring SABnzbd...")
    s, usenet = Sabnzbd(cfg, key), cfg.paths.downloads / "usenet"
    if s.set("misc", "complete_dir", str(usenet / "complete")) and s.set("misc", "download_dir", str(usenet / "incomplete")):
        ok(f"Directories: {usenet}/{{complete,incomplete}}")
    else:
        warn(f"SABnzbd didn't accept its download folders (retried next run; see {cfg.paths.logs}/sabnzbd.log)")
    existing = (s.api({"mode": "get_cats"}) or {}).get("categories") or []
    for cat in CATEGORIES:
        if cat in existing or s.api({"mode": "set_config", "section": "categories", "keyword": cat, "dir": cat}) is not None:
            ok(f"Category: {cat}")
        else:
            warn(f"Could not create category: {cat}")


def usenet_providers(cfg: Config) -> None:
    """[[usenet_providers]] as SABnzbd servers. set_config on the servers
    section creates or updates one (SABnzbd answers "not implemented" to
    name=set_server). enable = false switches an existing server off
    rather than skipping it."""
    providers = cfg.get("usenet_providers", []) or []
    key = Keys.read(cfg.paths.config).sabnzbd
    if not providers or not key:
        return
    info("Configuring SABnzbd usenet providers...")
    s = Sabnzbd(cfg, key)
    # Unknown (not empty) if the list can't be read: providers to disable
    # are then reported and retried next run, not silently skipped
    servers = s.servers()
    for p in providers:
        name = p["name"]
        if p.get("enable") is not True:
            if servers is None:
                warn(f"Couldn't read SABnzbd's servers, so {name} wasn't checked (retried next run)")
            elif any(x["name"] == name and x["enable"] == 1 for x in servers):
                # SABnzbd answers errors with HTTP 200 too: check the server really is off
                s.set("servers", "enable", "0", server=name)
                if any(x["name"] == name and x["enable"] == 0 for x in s.servers() or []):
                    ok(f"{name} disabled")
                else:
                    warn(f"Could not disable {name} in SABnzbd (retried next run)")
            continue
        answer = s.api({"mode": "set_config", "section": "servers", "keyword": name, "host": p.get("host", ""),
                        "port": str(p.get("port", "")), "ssl": "1" if p.get("ssl") is True else "0",
                        "username": p.get("username", ""), "password": p.get("password", ""),
                        "connections": str(p.get("connections", "")), "enable": "1"}, tries=3) or {}
        if any(x.get("name") == name and x.get("enable") == 1 for x in (answer.get("config") or {}).get("servers") or []):
            ok(f"{name} ({p.get('host')}:{p.get('port')})")
        else:
            warn(f"Could not add {name} to SABnzbd ({answer.get('error') or 'no answer'})")


def vpn_settings(cfg: Config) -> None:
    """[vpn] for netwatch, which keeps the kill switch (mediaserver/vpn.py)"""
    want = {"enabled": cfg.flag("vpn.enabled", False), "interface": cfg.get("vpn.interface", "") or ""}
    path = cfg.paths.state / "netwatch/vpn.json"
    if c.read_json(path, None) != want:
        c.write_json(path, want, mode=0o644)
    # Applied now, not a minute later by netwatch
    from mediaserver import vpn
    result = vpn.keep(path.parent, vpn.Clients(cfg.paths.config))
    status = c.read_json(path.parent / "vpn-status.json", {}) or {}
    if result == "up" and status.get("protected"):
        ok(f"VPN kill switch: qBittorrent bound to {status.get('interface')} (no traffic if it drops); SABnzbd paused within a minute if it does")
    elif result == "down":
        warn("VPN kill switch: the VPN isn't connected; downloads are blocked until it is")
    elif result == "up":
        warn("VPN kill switch: qBittorrent didn't take the binding yet; netwatch retries every minute")
    elif result == "":
        warn("VPN kill switch: qBittorrent didn't answer; netwatch applies it within a minute")


def sabnzbd_login(cfg: Config) -> None:
    key = Keys.read(cfg.paths.config).sabnzbd
    if not key:
        return
    user, password, url = cfg.jellyfin_user, cfg.jellyfin_pass, cfg.urls.sabnzbd
    if cfg.admin_local_only:
        # It answers on this Mac only: no login (set again if that changes)
        s = Sabnzbd(cfg, key)
        if logins.opens_without_login(url) or (s.set("misc", "username", "") and s.set("misc", "password", "")
                                                and logins.opens_without_login(url)):
            creds.forget(cfg.paths.state, "sabnzbd")
            ok("SABnzbd: no login (it answers on this Mac only)")
        else:
            warn(f"Could not turn off SABnzbd's login (retried next run; see {cfg.paths.logs}/sabnzbd.log)")
        return
    if creds.match(cfg.paths.state, "sabnzbd", user, password) and logins.sabnzbd(url, user, password):
        ok(f"SABnzbd login: {user}")
        return
    s = Sabnzbd(cfg, key)
    if s.set("misc", "username", user) and s.set("misc", "password", password) and logins.sabnzbd(url, user, password):
        creds.record(cfg.paths.state, "sabnzbd", user, password)
        ok(f"SABnzbd login set: {user}")
    else:
        warn(f"Could not set SABnzbd's login (retried next run; see {cfg.paths.logs}/sabnzbd.log)")
