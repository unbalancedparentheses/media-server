"""Setup's steps that are in Python. setup.sh runs them in order with
python3 -m mediaserver step <name>; each reads what it needs (config.toml,
the services' API keys) itself, so it can run on its own."""
from __future__ import annotations

import importlib

from mediaserver.config import Config

# step name → module in this package (each has run(cfg))
STEPS = {
    "unpackerr": "unpackerr",
    "postimport": "postimport_settings",
}


def main(argv: list[str]) -> int:
    unknown = [n for n in argv if n not in STEPS]
    if not argv or unknown:
        print(f"Usage: python3 -m mediaserver step <{'|'.join(STEPS)}>..." + (f" (unknown: {', '.join(unknown)})" if unknown else ""))
        return 2
    cfg = Config.load()
    for name in argv:
        importlib.import_module(f"mediaserver.steps.{STEPS[name]}").run(cfg)
    return 0
