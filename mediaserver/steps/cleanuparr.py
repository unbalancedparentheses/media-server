"""Cleanuparr: removes downloads that stall, never get metadata or fail to
import, blocklists the release in Sonarr/Radarr and searches for another."""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mediaserver import api, creds, launchd, logins
from mediaserver import common as c
from mediaserver.api import ApiError
from mediaserver.config import Config, Keys, local
from mediaserver.ui import err, info, ok, warn



class Cleanuparr:
    def __init__(self, cfg: Config):
        self.cfg, self.url, self.config_dir, self.state = cfg, cfg.urls.cleanuparr, cfg.paths.config, cfg.paths.state
        self.key = ""

    def call(self, method: str, path: str, body=None):
        return api.call(method, f"{self.url}/api/{path}", {"X-Api-Key": self.key}, body=body)

    def run(self) -> None:
        info("Configuring Cleanuparr...")
        cfg = self.cfg
        # The first account can be created by anyone until setup completes,
        # so create it right away. Its API wants 8+ characters; a shorter
        # shared password is set directly below.
        try:
            status = api.get(f"{self.url}/api/auth/status")
        except ApiError:
            warn("Cleanuparr is not responding")
            return
        if status.get("setupCompleted") is not True:
            password = cfg.jellyfin_pass if len(cfg.jellyfin_pass) >= 8 else secrets.token_hex(16)
            c.request(f"{self.url}/api/auth/setup/account", "POST", body={"username": cfg.jellyfin_user, "password": password})
            c.request(f"{self.url}/api/auth/setup/complete", "POST", body=b"")
        self.key = c.cleanuparr_key(self.config_dir)
        if not self.key:
            warn("Could not read Cleanuparr's API key")
            return
        # Always require the login (older versions skipped it on this Mac)
        require_login(cfg)
        self.set_login()
        self.connect_apps()
        self.connect_qbittorrent()
        self.stall_rule()
        self.queue_cleaner()

    def connect_apps(self) -> None:
        keys = Keys.read(self.config_dir)
        for app, url, key, version in (("sonarr", "http://localhost:8989", keys.sonarr, 4), ("radarr", "http://localhost:7878", keys.radarr, 6)):
            if not key:
                continue
            name = app.capitalize()
            # Unreadable is not "none": the instance would be added twice
            try:
                existing = self.call("GET", f"configuration/{app}")
            except ApiError:
                warn(f"Cleanuparr: couldn't read its {name} settings; skipped (retried next run)")
                continue
            ids = [i["id"] for i in existing.get("instances") or [] if i.get("url") == f"{url}/"]
            # Sent every run so a changed API key reaches it
            body = {"name": name, "url": url, "apiKey": key, "version": version, "enabled": True}
            try:
                if ids:
                    self.call("PUT", f"configuration/{app}/instances/{ids[0]}", body)
                else:
                    self.call("POST", f"configuration/{app}/instances", body)
                ok(f"{name} connected")
            except ApiError:
                warn(f"Cleanuparr: could not {'update' if ids else 'add'} {name}")

    def connect_qbittorrent(self) -> None:
        body = {"enabled": True, "name": "qBittorrent", "typeName": "qBittorrent", "type": "Torrent",
                "host": "http://localhost:8081", "username": self.cfg.qbit_user, "password": self.cfg.qbit_pass}
        # Unreadable is not "none": qBittorrent would be added twice
        try:
            clients = (self.call("GET", "configuration/download_client") or {}).get("clients") or []
        except ApiError:
            warn("Cleanuparr: couldn't read its download clients; skipped (retried next run)")
            return
        ids = [x["id"] for x in clients if x.get("typeName") == "qBittorrent"]
        try:
            if ids:
                self.call("PUT", f"configuration/download_client/{ids[0]}", body)
            else:
                self.call("POST", "configuration/download_client", body)
            ok("qBittorrent connected")
        except ApiError:
            warn(f"Cleanuparr: could not {'update' if ids else 'add'} qBittorrent")

    def stall_rule(self) -> None:
        """Stalled: 6 strikes at one check every 5 minutes = removed after
        about 30 minutes without progress; any progress resets the count"""
        strikes = self.cfg.get("cleanuparr.stalled_strikes", 6)
        # Unreadable is not "none": the rule would be added twice
        try:
            rule = next((r for r in self.call("GET", "queue-rules/stall") or [] if r.get("name") == "Stalled"), None)
        except ApiError:
            warn("Cleanuparr: couldn't read its queue rules; the stall rule wasn't checked (retried next run)")
            return
        try:
            if rule is None:
                self.call("POST", "queue-rules/stall", {"name": "Stalled", "enabled": True, "maxStrikes": strikes, "privacyType": "Public",
                                                         "minCompletionPercentage": 0, "maxCompletionPercentage": 100,
                                                         "resetStrikesOnProgress": True})
                ok(f"Rule: remove public torrents stalled for {strikes} checks")
            elif rule.get("maxStrikes") != strikes or rule.get("enabled") is not True:
                self.call("PUT", f"queue-rules/stall/{rule['id']}", dict(rule, maxStrikes=strikes, enabled=True))
                ok(f"Rule: stalled for {strikes} checks (updated)")
            else:
                ok(f"Rule: stalled for {strikes} checks")
        except ApiError:
            warn(f"Cleanuparr: could not {'add' if rule is None else 'update'} the stall rule")

    def queue_cleaner(self) -> None:
        enabled = self.cfg.flag("cleanuparr.enabled", True)
        # netwatch keeps the cleaner in line with this while online, and off
        # while offline; it reads the intended state from here
        c.write_atomic(self.state / "netwatch/cleanuparr-wanted", f"{str(enabled).lower()}\n", mode=0o644)
        run_now = enabled
        if c.read_text(self.state / "netwatch/connection") == "offline":
            run_now = False
            if enabled:
                warn("Offline: Cleanuparr's queue cleaner stays paused until the connection is back (netwatch resumes it)")
        try:
            current = self.call("GET", "configuration/queue_cleaner")
        except ApiError:
            warn("Cleanuparr: couldn't read the queue cleaner's settings; not changed (retried next run)")
            return
        want = queue_cleaner_settings(current, run_now)
        if want != current:
            try:
                self.call("PUT", "configuration/queue_cleaner", want)
            except ApiError:
                warn("Cleanuparr: could not update the queue cleaner")
        ok("Queue cleaner on (every 5 minutes)" if enabled else "Queue cleaner off (cleanuparr.enabled = false)")

    def set_login(self) -> None:
        """Give Cleanuparr the shared login. Its API needs the current
        password and 8+ characters, so the login is written to its user
        database (BCrypt, as Cleanuparr stores it) while it's stopped, then
        checked by logging in."""
        user, password = self.cfg.jellyfin_user, self.cfg.jellyfin_pass
        if creds.match(self.state, "cleanuparr", user, password) and logins.cleanuparr(self.url, user, password):
            ok(f"Cleanuparr login: {user}")
            return
        if not launchd.stop(self.config_dir, "cleanuparr"):
            warn("Cleanuparr didn't stop; its login wasn't changed (retried next run)")
            return
        try:
            write_login(self.config_dir / "cleanuparr/users.db", user, password)
        except (sqlite3.Error, ImportError) as e:
            warn(f"Could not write Cleanuparr's login ({e})")
        launchd.bootstrap("cleanuparr")
        api.wait_for("Cleanuparr", f"{self.url}/health")
        if logins.cleanuparr(self.url, user, password):
            creds.record(self.state, "cleanuparr", user, password)
            ok(f"Cleanuparr login set: {user}")
        else:
            warn("Cleanuparr's new login doesn't work (retried next run)")


def queue_cleaner_settings(current: dict, enabled: bool) -> dict:
    """The queue cleaner as setup wants it, keeping everything else"""
    want: dict[str, Any] = {**current, "enabled": enabled}
    # Also: metadata that never arrives (3 checks) and failed imports (3 tries)
    if not want.get("downloadingMetadataMaxStrikes"):
        want["downloadingMetadataMaxStrikes"] = 3
    failed: dict[str, Any] = dict(want.get("failedImport") or {})
    if not failed.get("maxStrikes"):
        failed["maxStrikes"] = 3
    failed["ignorePrivate"] = True
    # No patterns + Exclude = every failed import counts
    if not failed.get("patterns"):
        failed["patternMode"] = "Exclude"
    want["failedImport"] = failed
    return want


def write_login(db_path: Path, user: str, password: str) -> None:
    import bcrypt
    # $2a$ like BCrypt.Net; same algorithm as bcrypt's $2b$
    digest = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode().replace("$2b$", "$2a$", 1)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE users SET username = ?, password_hash = ?, failed_login_attempts = 0, lockout_end = NULL, updated_at = ?",
                   (user, digest, datetime.now(timezone.utc).isoformat()))
        db.execute("DELETE FROM refresh_tokens")  # sign out existing sessions


def require_login(cfg: Config) -> None:
    """Turn off "no login for local addresses" (which also covers the
    LAN). Runs before the services start too, so Cleanuparr never listens
    on a wider address with it on; if it can't be turned off, setup stops
    rather than start Cleanuparr unprotected."""
    config_dir = cfg.paths.config
    db = config_dir / "cleanuparr/cleanuparr.db"
    if not db.exists():
        return
    if launchd.loaded("cleanuparr"):
        key = c.cleanuparr_key(config_dir)
        try:
            general = api.get(f"{local('cleanuparr')}/api/configuration/general", {"X-Api-Key": key})
            if (general.get("auth") or {}).get("disableAuthForLocalAddresses") is False:
                return
            general["auth"]["disableAuthForLocalAddresses"] = False
            api.call("PUT", f"{local('cleanuparr')}/api/configuration/general", {"X-Api-Key": key}, body=general)
            ok("Cleanuparr: login required again")
            return
        except (ApiError, KeyError, TypeError, AttributeError):
            pass
        # Not answering: stop it and change the setting on disk
        if not launchd.stop(config_dir, "cleanuparr"):
            raise err("Cleanuparr didn't stop, so its login requirement couldn't be turned on; stopping here "
                      "(stop it with 'nix run .#restart -- cleanuparr' and re-run)")
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE general_configs SET auth_disable_auth_for_local_addresses = 0 WHERE auth_disable_auth_for_local_addresses = 1")
            left = conn.execute("SELECT COUNT(*) FROM general_configs WHERE auth_disable_auth_for_local_addresses = 1").fetchone()[0]
    except sqlite3.Error:
        raise err(f"Couldn't turn on Cleanuparr's login requirement in {db}; stopping here so it doesn't start without one")
    if left:
        raise err(f"Cleanuparr's login requirement is still off in {db}; stopping here")


def run(cfg: Config) -> None:
    Cleanuparr(cfg).run()


def run_require_login(cfg: Config) -> None:
    require_login(cfg)
