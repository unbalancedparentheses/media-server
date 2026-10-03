"""python3 -m mediaserver <command>: the parts of setup that are in Python
(setup.sh calls these; run them through nix run .#…, which sets things up)."""
from __future__ import annotations

import sys

from mediaserver.ui import SetupError

COMMANDS = {
    "test": "mediaserver.verify",
    "doctor": "mediaserver.doctor",
    "validate-config": "mediaserver.validate",
}


def main(argv: list[str]) -> int:
    import importlib
    try:
        if argv[:1] == ["step"]:
            from mediaserver import steps
            return steps.main(argv[1:])
        if argv[:1] == ["e2e"]:
            from mediaserver import e2e
            return e2e.main(argv[1:])
        if len(argv) < 1 or argv[0] not in COMMANDS:
            print(f"Usage: python3 -m mediaserver {{{'|'.join(COMMANDS)}|e2e [--keep]|step <name>...}}", file=sys.stderr)
            return 2
        return importlib.import_module(COMMANDS[argv[0]]).main()
    except SetupError as e:
        return int(e.code or 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
