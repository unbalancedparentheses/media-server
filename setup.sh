#!/usr/bin/env bash
set -eo pipefail

# ═══════════════════════════════════════════════════════════════════
# Media Server — one-command setup for macOS (fresh install or re-run)
# Services come from Nix and run as launchd agents; data lives in ~/media.
#
# Usage: nix run .#install  (or ./setup.sh)   Full setup + verification
#        nix run .#status                     Service state and health
#        nix run .#doctor                     Is it working? Findings + what to do
#        nix run .#logs -- <service>          Follow a service's log
#        nix run .#restart -- [service]       Restart one or all services
#        nix run .#test                       Run verification only
#        nix run .#e2e [-- --keep]            Download → import → Jellyfin test
#        nix run .#backup                     Back up configs
#        nix run .#restore -- <file>          Restore configs from a backup
#        nix run .#update                     Back up, git pull, re-run setup
#        nix run .#uninstall                  Stop and remove the services
#        nix run .#uninstall -- --purge       ...and delete configs and state
# Other flags: --yes (no prompts), --check-config, --preflight, --dry-run
#
# Everything is done by the Python package next to this script
# (mediaserver/cli.py); this only makes sure it runs through Nix.
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Run directly from a checkout: re-exec through the flake, which provides the
# services and tools and sets MEDIA_SERVICES_JSON
if [ -z "${MEDIA_SERVICES_JSON:-}" ]; then
  command -v nix >/dev/null 2>&1 || { echo "Nix is required: https://determinate.systems/nix-installer/" >&2; exit 1; }
  exec nix --extra-experimental-features "nix-command flakes" run "path:$SCRIPT_DIR#install" -- "$@"
fi

PYTHONPATH="$SCRIPT_DIR" PYTHONDONTWRITEBYTECODE=1 exec python3 -m mediaserver.cli "$@"
