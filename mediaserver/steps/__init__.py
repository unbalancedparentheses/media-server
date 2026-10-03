"""Setup's steps that are in Python. setup.sh runs them in order with
python3 -m mediaserver step <name>; each reads what it needs (config.toml,
the services' API keys) itself, so it can run on its own."""
from __future__ import annotations

import importlib

from mediaserver.config import Config

# step name → module in this package, and its function (run by default),
# which takes the Config
STEPS = {
    "unpackerr": "unpackerr",
    "postimport": "postimport_settings",
    "cleanuparr": "cleanuparr",
    # Before the services start, too (see cleanuparr.require_login)
    "cleanuparr-require-login": "cleanuparr:run_require_login",
    "moonbase": "moonbase",
    "intro-skipper": "introskipper",
    "bazarr": "bazarr",
    "seerr": "seerr",
    "prowlarr": "prowlarr",
    "qbittorrent": "downloads:qbittorrent",
    "sabnzbd": "downloads:sabnzbd",
    "usenet-providers": "downloads:usenet_providers",
    "sabnzbd-login": "downloads:sabnzbd_login",
    # Before qBittorrent's first start (host setup)
    "seed-qbittorrent": "downloads:seed_qbittorrent",
}


def main(argv: list[str]) -> int:
    unknown = [n for n in argv if n not in STEPS]
    if not argv or unknown:
        print(f"Usage: python3 -m mediaserver step <{'|'.join(STEPS)}>..." + (f" (unknown: {', '.join(unknown)})" if unknown else ""))
        return 2
    cfg = Config.load()
    for name in argv:
        module, _, function = STEPS[name].partition(":")
        getattr(importlib.import_module(f"mediaserver.steps.{module}"), function or "run")(cfg)
    return 0
