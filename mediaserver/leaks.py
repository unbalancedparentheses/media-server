"""Nothing the dashboard serves carries a secret: every service's API key
and the config.toml passwords are looked for in what it hands out (its
pages, status.json, the control endpoints). A tool in this space once
shipped an endpoint that returned every connected app's keys; this checks
we never do. Used by the install checks (verify) and the doctor."""
from __future__ import annotations

from mediaserver import common as c
from mediaserver.config import Config

SERVED = ("/", "/admin.html", "/status.json", "/api/control/speed", "/api/control/pause")


def secrets(cfg: Config) -> dict:
    """name → value, for the ones that are set (long enough to search for)"""
    p = cfg.paths
    found = {f"{app} API key": c.arr_key(p.config, app) for app in ("sonarr", "radarr", "prowlarr")}
    found.update({"Bazarr API key": c.bazarr_key(p.config), "Seerr API key": c.seerr_key(p.config),
                  "SABnzbd API key": c.sabnzbd_key(p.config), "Cleanuparr API key": c.cleanuparr_key(p.config),
                  "Jellyfin API key": c.read_text(p.state / "dashstatus/jellyfin-key"),
                  "Jellyfin password": cfg.jellyfin_pass, "qBittorrent password": cfg.qbit_pass})
    return {k: v for k, v in found.items() if v and len(v) >= 6}


def find(cfg: Config) -> list[str]:
    """"<secret> in <path>" for each one served; [] when none is"""
    keys = secrets(cfg)
    out = []
    for path in SERVED:
        body = c.request(cfg.urls.dashboard + path, timeout=15).body.decode(errors="replace")
        out += [f"{name} in {path}" for name, value in keys.items() if value in body]
    return out
