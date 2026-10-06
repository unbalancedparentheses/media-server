"""Nothing the dashboard serves carries a secret. Every service's API key
and the config.toml passwords are looked for in all it hands out: each
file in nginx's www folder and each /api/ path nginx's own api-proxy.conf
lets through (its control endpoints included). A tool in this space once
shipped an endpoint that returned every connected app's keys; this checks
we never do. Used by the install checks (verify) and the doctor.

What it can't tell is said, not passed over: a path that didn't answer (or
answered with a server error) is "unverified"; a secret too short to look
for without matching ordinary text (under MIN_LENGTH) is "unchecked".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from mediaserver import common as c
from mediaserver.config import Config

MIN_LENGTH = 8
# The control endpoints' reads (POSTs change things: not asked)
CONTROL = ("/api/control/speed", "/api/control/pause", "/api/control/delete?item=0")


@dataclass
class Result:
    found: list = field(default_factory=list)        # "<secret> in <path>"
    unverified: list = field(default_factory=list)   # paths that didn't answer
    unchecked: list = field(default_factory=list)    # secrets too short to look for
    paths: int = 0

    @property
    def clean(self) -> bool:
        return not self.found and not self.unverified


def secrets(cfg: Config) -> dict:
    """name → value, for the ones that are set"""
    p = cfg.paths
    found = {f"{app} API key": c.arr_key(p.config, app) for app in ("sonarr", "radarr", "prowlarr")}
    found.update({"Bazarr API key": c.bazarr_key(p.config), "Seerr API key": c.seerr_key(p.config),
                  "SABnzbd API key": c.sabnzbd_key(p.config), "Cleanuparr API key": c.cleanuparr_key(p.config),
                  "Jellyfin API key": c.read_text(p.state / "dashstatus/jellyfin-key"),
                  "Jellyfin password": cfg.jellyfin_pass, "qBittorrent password": cfg.qbit_pass})
    return {k: v for k, v in found.items() if v}


def served(cfg: Config) -> list[str]:
    """Every path the dashboard serves: its files and the /api/ paths in
    nginx's api-proxy.conf (with a query its location expects)"""
    www = cfg.paths.config / "nginx/www"
    files = sorted("/" + f.relative_to(www).as_posix() for f in www.rglob("*") if f.is_file()) if www.is_dir() else []
    proxy = c.read_text(cfg.paths.config / "nginx/api-proxy.conf")
    apis = sorted(set(re.findall(r"^location\s*=\s*(/api/\S+)\s*\{", proxy, re.M)) - {p.split("?")[0] for p in CONTROL})
    apis = [a + ("?mode=queue&output=json" if a.endswith("/sabnzbd/") else "") for a in apis]
    return ["/", *files, *apis, *CONTROL]


def find(cfg: Config) -> Result:
    keys = secrets(cfg)
    result = Result(unchecked=sorted(k for k, v in keys.items() if len(v) < MIN_LENGTH))
    searchable = {k: v for k, v in keys.items() if len(v) >= MIN_LENGTH}
    for path in served(cfg):
        r = c.request(cfg.urls.dashboard + path, timeout=20)
        result.paths += 1
        if r.status == 0 or r.status >= 500:
            result.unverified.append(f"{path} ({r.status or 'no answer'})")
            continue
        body = r.body.decode(errors="replace")
        result.found += [f"{name} in {path}" for name, value in searchable.items() if value in body]
    return result
