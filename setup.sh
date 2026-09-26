#!/usr/bin/env bash
set -Eeo pipefail
IFS=$'\n\t'

# ═══════════════════════════════════════════════════════════════════
# Media Server — one-command setup for macOS (fresh install or re-run)
# Services come from Nix and run as launchd agents; data lives in ~/media.
#
# Usage: nix run .#install  (or ./setup.sh)   Full setup + verification
#        nix run .#status                     Service state and health
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
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Run directly from a checkout: re-exec through the flake, which provides the
# services and tools (jq, python3, …) and sets MEDIA_SERVICES_JSON
if [ -z "${MEDIA_SERVICES_JSON:-}" ]; then
  command -v nix >/dev/null 2>&1 || { echo "Nix is required: https://determinate.systems/nix-installer/" >&2; exit 1; }
  exec nix run "path:$SCRIPT_DIR#install" -- "$@"
fi

MEDIA_DIR="${MEDIA_DIR:-$HOME/media}"
CONFIG_FILE="$MEDIA_DIR/config.toml"
CONFIG_DIR="$MEDIA_DIR/config"
STATE_DIR="$MEDIA_DIR/.state"
LOG_DIR="$MEDIA_DIR/logs"
BACKUP_DIR="$MEDIA_DIR/backups"
MAX_BACKUPS=10

# shellcheck source=scripts/lib.sh
. "$SCRIPT_DIR/scripts/lib.sh"
# shellcheck source=scripts/service_registry.sh
. "$SCRIPT_DIR/scripts/service_registry.sh"
# shellcheck source=scripts/launchd.sh
. "$SCRIPT_DIR/scripts/launchd.sh"
# shellcheck source=scripts/setup-services.sh
. "$SCRIPT_DIR/scripts/setup-services.sh"
# shellcheck source=scripts/verify.sh
. "$SCRIPT_DIR/scripts/verify.sh"
# shellcheck source=scripts/maintenance.sh
. "$SCRIPT_DIR/scripts/maintenance.sh"
# shellcheck source=scripts/e2e.sh
. "$SCRIPT_DIR/scripts/e2e.sh"
for step in "$SCRIPT_DIR"/scripts/steps/*.sh; do
  # shellcheck source=/dev/null
  . "$step"
done

# Defaults until config.toml is loaded (uninstall and logs don't need it)
init_service_registry

# Clean up temp files on exit
TMPDIR_SETUP=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR_SETUP"; }
trap cleanup EXIT

# ─── Mode selection ──────────────────────────────────────────────
MODE=""
MODE_ARG=""
NON_INTERACTIVE=false
DRY_RUN=false
PURGE=false
E2E_KEEP=false
USAGE="Usage: setup.sh [--yes] [--dry-run] [--preflight|--check-config|--test|--e2e [--keep]|--status|--logs <service>|--restart [service]|--update|--backup|--restore <file>|--uninstall [--purge]]"
set_mode() {
  [ -n "$MODE" ] && err "Only one mode can be used at a time"
  MODE="$1"
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes|-y) NON_INTERACTIVE=true ;;
    --dry-run) DRY_RUN=true ;;
    --purge) PURGE=true ;;
    --keep) E2E_KEEP=true ;;
    --preflight|--check-config|--test|--e2e|--status|--update|--backup|--uninstall)
      MODE_NAME="${1#--}"
      set_mode "${MODE_NAME//-/_}"
      ;;
    --logs|--restore)
      set_mode "${1#--}"
      shift
      [ "$#" -gt 0 ] || err "$USAGE"
      MODE_ARG="$1"
      ;;
    --restart)
      set_mode restart
      if [ "$#" -gt 1 ] && [ "${2#--}" = "$2" ]; then MODE_ARG="$2"; shift; fi
      ;;
    -h|--help) echo "$USAGE"; exit 0 ;;
    *) err "$USAGE" ;;
  esac
  shift
done
[ -z "$MODE" ] && MODE="setup"
[ "$PURGE" = "true" ] && [ "$MODE" != "uninstall" ] && err "--purge only goes with --uninstall"

run_setup() {
  check_platform
  ensure_config
  configure_tailscale
  create_directories
  write_service_configs
  start_stack
  read_setup_config
  wait_for_services
  load_api_keys
  configure_qbittorrent
  configure_jellyfin
  configure_sabnzbd
  configure_arrs
  configure_junk_filters
  configure_prowlarr
  configure_usenet_providers
  configure_bazarr
  configure_sabnzbd_auth
  configure_seerr
  configure_moonbase
  configure_unpackerr
  write_api_proxy
}

print_summary() {
  # Set by configure_tailscale only when it published the services
  local ts_hostname="${TS_HOSTNAME:-}" lan_ip
  lan_ip=$(ipconfig getifaddr en0 2>/dev/null || true)

  echo ""
  echo "  Setup Complete!"
  echo ""
  echo "  On this Mac:"
  echo "    Watch (Moonfin): http://localhost:8096/Moonfin/Web/"
  echo "    Request:         http://localhost:5055"
  echo "    Dashboard:       $DASHBOARD_URL"
  if [ -n "$lan_ip" ]; then
    echo "  On your network:   replace localhost with $lan_ip"
  fi
  echo ""
  if [ -n "$ts_hostname" ]; then
    echo "  Remote (HTTPS over Tailscale):"
    echo "    Watch:     https://$ts_hostname:8096/Moonfin/Web/"
    echo "    Request:   https://$ts_hostname:5055"
    echo "    Dashboard: https://$ts_hostname"
    echo ""
  fi
  echo "  Apps: install Moonfin (App Store, Google Play, Amazon) and point it at"
  echo "  http://${lan_ip:-<this Mac>}:8096. Log in with your Jellyfin user."
  echo ""
  echo "  Manage: nix run .#status | .#logs -- <service> | .#restart | .#uninstall"
  echo "  Keep the Mac awake while serving: System Settings → Energy → Prevent"
  echo "  automatic sleeping when the display is off."
  echo ""
}

# ─── Mode dispatch ──────────────────────────────────────────────
if [ "$DRY_RUN" = "true" ]; then
  case "$MODE" in
    setup)
      info "Dry-run mode: validating prerequisites and config only (no writes, no service changes)"
      do_preflight || true
      [ -f "$CONFIG_FILE" ] && do_check_config
      info "Dry-run complete"
      ;;
    update) info "Dry-run mode: would back up, git pull and re-run setup" ;;
    restore) info "Dry-run mode: would stop services, restore $MODE_ARG and restart" ;;
    backup) info "Dry-run mode: would back up $CONFIG_DIR and $CONFIG_FILE" ;;
    uninstall) info "Dry-run mode: would stop and remove the launchd agents$([ "$PURGE" = "true" ] && echo ", then delete configs and state")" ;;
    *) err "--dry-run is not supported with --$MODE" ;;
  esac
  exit 0
fi

case "$MODE" in
  check_config) do_check_config; exit 0 ;;
  preflight) do_preflight; exit $? ;;
  backup) do_backup; exit 0 ;;
  restore) RESTORE_FILE="$MODE_ARG"; do_restore; exit 0 ;;
  update) do_update; exit 0 ;;
  uninstall) do_uninstall; exit 0 ;;
  logs) do_logs "$MODE_ARG"; exit 0 ;;
  restart) do_restart "$MODE_ARG"; exit 0 ;;
esac

# The remaining modes need a valid config
if [ "$MODE" = "test" ] || [ "$MODE" = "status" ] || [ "$MODE" = "e2e" ]; then
  [ -f "$CONFIG_FILE" ] || err "$CONFIG_FILE not found — run 'nix run .#install' first"
  CONFIG_JSON=$(load_config_json "$CONFIG_FILE")
  validate_required_config
  validate_config_semantics
  load_runtime_settings
  read_setup_config
  read_api_keys
  if [ "$MODE" = "status" ]; then do_status; exit 0; fi
  if [ "$MODE" = "e2e" ]; then
    E2E_EXIT=0
    do_e2e || E2E_EXIT=$?
    exit "$E2E_EXIT"
  fi
  VERIFY_EXIT=0
  run_verification || VERIFY_EXIT=$?
  exit "$VERIFY_EXIT"
fi

run_setup
# Called from a || list: run_verification relies on errexit being off inside
VERIFY_FAILED=0
run_verification || VERIFY_FAILED=$?
if [ "$VERIFY_FAILED" -gt 0 ]; then
  printf "\n\033[1;31m  Setup finished, but %s verification check(s) failed (see above).\033[0m\n" "$VERIFY_FAILED"
  echo "  Fix the cause and re-run 'nix run .#install', or check again with 'nix run .#test'."
  echo ""
  exit 1
fi
print_summary
