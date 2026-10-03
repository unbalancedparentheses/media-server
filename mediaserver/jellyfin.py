"""Talking to Jellyfin as setup: logging in, restarting it until it's ready,
its plugins and plugin repositories."""
from __future__ import annotations

import time
from typing import Any

from mediaserver import api, launchd
from mediaserver.api import ApiError
from mediaserver.config import Config
from mediaserver.ui import ok, warn

CLIENT = 'MediaBrowser Client="setup", Device="script", DeviceId="setup-script", Version="1.0"'


def plain_guid(guid: str) -> str:
    return guid.lower().replace("-", "")


class Jellyfin:
    def __init__(self, cfg: Config, user: str | None = None, password: str | None = None):
        self.cfg, self.url = cfg, cfg.urls.jellyfin
        self.user = cfg.jellyfin_user if user is None else user
        self.password = cfg.jellyfin_pass if password is None else password
        self.token = ""

    def login(self, tries: int = 3) -> bool:
        for attempt in range(tries):
            try:
                answer = api.call("POST", f"{self.url}/Users/AuthenticateByName", {"Authorization": CLIENT},
                                  body={"Username": self.user, "Pw": self.password})
                self.token = (answer or {}).get("AccessToken") or ""
                if self.token:
                    return True
            except ApiError:
                pass
            if attempt + 1 < tries:
                time.sleep(2)
        self.token = ""
        return False

    @property
    def auth(self) -> dict:
        return {"Authorization": f'MediaBrowser Token="{self.token}"'}

    def get(self, path: str) -> Any:
        return api.get(f"{self.url}/{path}", self.auth)

    def post(self, path: str, body: Any = None) -> Any:
        return api.call("POST", f"{self.url}/{path}", self.auth, body=body if body is not None else b"")

    def restart_ready(self) -> bool:
        """After a restart Jellyfin answers /health before it accepts logins
        and has loaded its plugins: restart it, then wait until logging in
        works"""
        launchd.restart(self.cfg.paths.config, "jellyfin")
        time.sleep(3)
        if not api.wait_for("Jellyfin", f"{self.url}/health"):
            return False
        start = time.time()
        while not self.login(tries=1):
            if time.time() - start >= 180:
                warn("Jellyfin restarted but doesn't accept the login yet")
                return False
            time.sleep(2)
        return True

    def plugin(self, guid: str) -> dict | None:
        """The loaded plugin with this GUID (Jellyfin writes ids without dashes)"""
        try:
            plugins = self.get("Plugins") or []
        except ApiError:
            return None
        return next((p for p in plugins if plain_guid(p.get("Id") or "") == plain_guid(guid)), None)

    def wait_plugin(self, guid: str, seconds: int = 60) -> dict | None:
        """The plugin once Jellyfin has loaded it (it can take a few seconds
        after the login works); None if it never shows up"""
        start = time.time()
        while True:
            found = self.plugin(guid)
            if found or time.time() - start >= seconds:
                return found
            time.sleep(2)

    def pin_repository(self, name: str, url: str, pattern: str) -> bool:
        """Make <url> the only plugin repository matching <pattern>. False
        (and nothing written) when the list can't be read: writing a list
        built from a failed read would drop every other repository."""
        try:
            repos = self.get("Repositories")
            if not isinstance(repos, list):
                raise ApiError("not a list")
        except ApiError:
            warn(f"Could not read Jellyfin's plugin repositories; {name}'s wasn't changed")
            return False
        updated = [r for r in repos if pattern not in (r.get("Url") or "")] + [{"Name": name, "Url": url, "Enabled": True}]

        def key(rs: list) -> list:
            return sorted((str(r.get("Url")), str(r.get("Name")), bool(r.get("Enabled"))) for r in rs)
        if key(updated) == key(repos):
            return True
        try:
            self.post("Repositories", updated)
        except ApiError:
            warn(f"Could not set {name}'s plugin repository")
            return False
        ok(f"Plugin repository pinned ({name})")
        return True

    def install_plugin(self, package: str, guid: str, version: str, repo: str) -> bool:
        from urllib.parse import quote
        try:
            self.post(f"Packages/Installed/{quote(package)}?assemblyGuid={guid}&version={version}&repositoryUrl={quote(repo, safe='')}")
            return True
        except ApiError:
            return False
