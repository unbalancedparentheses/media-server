"""config.toml and everything derived from it: where things live, the
services' addresses and the settings the operations use."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mediaserver import common as c


@dataclass
class Paths:
    media: Path

    @property
    def config_file(self) -> Path:
        return self.media / "config.toml"

    @property
    def config(self) -> Path:
        return self.media / "config"

    @property
    def state(self) -> Path:
        return self.media / ".state"

    @property
    def logs(self) -> Path:
        return self.media / "logs"

    @property
    def backups(self) -> Path:
        return self.media / "backups"

    @property
    def movies(self) -> Path:
        return self.media / "movies"

    @property
    def tv(self) -> Path:
        return self.media / "tv"

    @property
    def anime(self) -> Path:
        return self.media / "anime"

    @property
    def downloads(self) -> Path:
        return self.media / "downloads"


def default_paths() -> Paths:
    return Paths(Path(os.environ.get("MEDIA_DIR", Path.home() / "media")))


PORTS = {"jellyfin": 8096, "sonarr": 8989, "radarr": 7878, "prowlarr": 9696, "bazarr": 6767, "qbittorrent": 8081,
         "sabnzbd": 8080, "seerr": 5055, "byparr": 8191, "cleanuparr": 11011,
         # The dashboard's speed-limit control (dashstatus, this Mac only; nginx passes it on)
         "control": 8099}


def local(name: str, host: str = "127.0.0.1") -> str:
    """Where a service answers on this Mac. MEDIASERVER_URL_<NAME> overrides
    it (the tests point the steps at fake services that way)."""
    return os.environ.get(f"MEDIASERVER_URL_{name.upper()}") or f"http://{host}:{PORTS[name]}"


@dataclass
class Urls:
    """Where each service answers. Admin UIs listen on network.admin_bind;
    they're reached there unless it's every interface."""
    admin_bind: str = "0.0.0.0"
    dashboard_port: int = 80

    @property
    def admin_host(self) -> str:
        return "localhost" if self.admin_bind == "0.0.0.0" else self.admin_bind

    def admin(self, name: str) -> str:
        return local(name, self.admin_host)

    @property
    def qbittorrent(self): return self.admin("qbittorrent")
    @property
    def sonarr(self): return self.admin("sonarr")
    @property
    def radarr(self): return self.admin("radarr")
    @property
    def prowlarr(self): return self.admin("prowlarr")
    @property
    def bazarr(self): return self.admin("bazarr")
    @property
    def sabnzbd(self): return self.admin("sabnzbd")
    @property
    def cleanuparr(self): return self.admin("cleanuparr")
    @property
    def jellyfin(self): return local("jellyfin", "localhost")
    @property
    def seerr(self): return local("seerr", "localhost")
    @property
    def byparr(self): return local("byparr")

    @property
    def dashboard(self) -> str:
        return os.environ.get("MEDIASERVER_URL_DASHBOARD") or \
            "http://localhost" + ("" if self.dashboard_port == 80 else f":{self.dashboard_port}")

    def health_endpoints(self) -> list[tuple[str, str]]:
        return [("Jellyfin", f"{self.jellyfin}/health"), ("Sonarr", f"{self.sonarr}/ping"),
                ("Radarr", f"{self.radarr}/ping"), ("Prowlarr", f"{self.prowlarr}/ping"),
                ("Bazarr", self.bazarr), ("SABnzbd", self.sabnzbd), ("qBittorrent", self.qbittorrent),
                ("Seerr", f"{self.seerr}/api/v1/status"), ("Byparr", f"{self.byparr}/health"),
                ("Cleanuparr", f"{self.cleanuparr}/health"), ("Dashboard", self.dashboard)]


def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass
class Config:
    """config.toml, with defaults for what's optional"""
    data: dict
    paths: Paths = field(default_factory=default_paths)

    @classmethod
    def load(cls, paths: Paths | None = None) -> Config:
        paths = paths or default_paths()
        return cls(load_toml(paths.config_file), paths)

    def get(self, dotted: str, default: Any = None) -> Any:
        """config.get("playback.subtitle_mode", "Always"); a missing value
        (or an explicit empty one where noted by callers) gets the default"""
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def flag(self, dotted: str, default: bool) -> bool:
        """A true/false setting; only a missing value gets the default"""
        value = self.get(dotted)
        return default if value is None else value is True

    @property
    def urls(self) -> Urls:
        return Urls(self.get("network.admin_bind", "0.0.0.0"), int(self.get("network.dashboard_port", 80)))

    @property
    def timezone(self) -> str:
        return self.get("timezone") or self.get("qbittorrent.timezone") or ""

    # Logins
    @property
    def jellyfin_user(self) -> str: return self.get("jellyfin.username", "")
    @property
    def jellyfin_pass(self) -> str: return self.get("jellyfin.password", "")
    @property
    def admin_local_only(self) -> bool:
        """The admin pages answer on this Mac only (network.admin_bind): then
        they don't ask for a login there"""
        return self.get("network.admin_bind", "0.0.0.0") == "127.0.0.1"
    @property
    def qbit_user(self) -> str: return self.get("qbittorrent.username", "")
    @property
    def qbit_pass(self) -> str: return self.get("qbittorrent.password", "")

    # Quality profiles
    @property
    def sonarr_profile(self) -> str: return self.get("quality.sonarr_profile", "")
    @property
    def sonarr_anime_profile(self) -> str: return self.get("quality.sonarr_anime_profile", "")
    @property
    def radarr_profile(self) -> str: return self.get("quality.radarr_profile", "")

    # Disk
    @property
    def disk_warn_gb(self) -> int: return int(self.get("disk.warn_free_gb", 50))
    @property
    def disk_min_gb(self) -> int: return int(self.get("disk.min_free_gb", 10))

    def byparr_needed(self) -> bool:
        """An enabled indexer goes through Byparr (flaresolverr = true)"""
        return any(i.get("enable") is True and i.get("flaresolverr") is True for i in self.get("indexers", []) or [])


@dataclass
class Keys:
    """The services' API keys, read from their own config files"""
    sonarr: str = ""
    radarr: str = ""
    prowlarr: str = ""
    sabnzbd: str = ""
    seerr: str = ""

    @classmethod
    def read(cls, config_dir: Path) -> Keys:
        return cls(c.arr_key(config_dir, "sonarr"), c.arr_key(config_dir, "radarr"), c.arr_key(config_dir, "prowlarr"),
                   c.sabnzbd_key(config_dir), c.seerr_key(config_dir))
