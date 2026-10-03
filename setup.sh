#!/usr/bin/env bash
set -Eeo pipefail
IFS=$'\n\t'

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
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Run directly from a checkout: re-exec through the flake, which provides the
# services and tools (jq, python3, …) and sets MEDIA_SERVICES_JSON
if [ -z "${MEDIA_SERVICES_JSON:-}" ]; then
  command -v nix >/dev/null 2>&1 || { echo "Nix is required: https://determinate.systems/nix-installer/" >&2; exit 1; }
  exec nix --extra-experimental-features "nix-command flakes" run "path:$SCRIPT_DIR#install" -- "$@"
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
# shellcheck source=scripts/maintenance.sh
. "$SCRIPT_DIR/scripts/maintenance.sh"
for step in "$SCRIPT_DIR"/scripts/steps/*.sh; do
  # shellcheck source=/dev/null
  . "$step"
done

# Defaults until config.toml is loaded (uninstall and logs don't need it)
init_service_registry

# Clean up temp files on exit
TMPDIR_SETUP=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR_SETUP"; release_lock; }
trap cleanup EXIT

# ─── Mode selection ──────────────────────────────────────────────
MODE=""
MODE_ARG=""
NON_INTERACTIVE=false
DRY_RUN=false
PURGE=false
E2E_KEEP=false
USAGE="Usage: setup.sh [--yes] [--dry-run] [--preflight|--check-config|--test|--e2e [--keep]|--status|--doctor|--open|--logs <service>|--restart [service]|--update|--backup|--restore <file>|--uninstall [--purge]]"
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
    --preflight|--check-config|--test|--e2e|--status|--doctor|--open|--update|--backup|--uninstall)
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

# Each step is timed (measure before optimizing); the summary lists the slowest
STEP_TIMES=""
timed() {
  local start=$SECONDS name="$1"
  [ "$1" = py ] && name="$3"   # py step <name>
  "$@"
  STEP_TIMES+="$((SECONDS - start)) $name"$'\n'
}

run_setup() {
  SETUP_STARTED=$SECONDS
  timed check_platform
  timed ensure_config
  timed configure_tailscale
  timed py step directories
  timed py step service-configs
  timed py step services
  timed py step wait
  timed py step api-keys
  # Radarr indexers an interrupted e2e test left paused
  py e2e --resume-indexers || true
  timed py step qbittorrent
  timed py step jellyfin
  timed py step sabnzbd
  timed py step arrs
  timed py step junk-filters
  timed py step prowlarr
  timed py step usenet-providers
  timed py step bazarr
  timed py step sabnzbd-login
  timed py step seerr
  timed py step moonbase
  timed py step intro-skipper
  timed py step unpackerr
  timed py step cleanuparr
  timed py step postimport
  timed py step api-proxy
}

# The first install that succeeds opens the dashboard; later runs are
# usually config changes, so they don't. Never with --yes, in CI or without
# a terminal (scripted installs).
open_dashboard_once() {
  local marker="$STATE_DIR/dashboard-opened"
  [ -e "$marker" ] && return 0
  touch "$marker"
  if [ "$NON_INTERACTIVE" = true ] || [ -n "${CI:-}" ] || [ ! -t 1 ]; then return 0; fi
  info "Opening the dashboard ($DASHBOARD_URL); next time: nix run .#open"
  open "$DASHBOARD_URL" >/dev/null 2>&1 || true
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
  echo "  What the checks above prove: every service is running, configured,"
  echo "  connected to the others, and accepts your login. They don't prove that"
  echo "  releases are found (that depends on the indexers), that playback works"
  echo "  on your devices, or that Moonfin loads offline in a browser."
  echo "    Is it working right now?        nix run .#doctor"
  echo "    Request → library → subtitles:  nix run .#e2e"
  echo ""
  echo "  Setup took $((SECONDS - SETUP_STARTED))s; slowest: $(printf '%s' "$STEP_TIMES" | sort -rn | head -3 | awk '{printf "%s%s %ss", (NR>1?", ":""), $2, $1}')"
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

# Operations that change the services take the lock (read-only ones don't)
case "$MODE" in
  setup|update|restore|backup|uninstall|restart|e2e) acquire_lock ;;
esac

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
if [ "$MODE" = "open" ]; then
  [ -f "$CONFIG_FILE" ] || err "$CONFIG_FILE not found — run 'nix run .#install' first"
  CONFIG_JSON=$(load_config_json "$CONFIG_FILE")
  load_runtime_settings
  open "$DASHBOARD_URL" || err "Couldn't open $DASHBOARD_URL"
  exit 0
fi
if [ "$MODE" = "test" ] || [ "$MODE" = "status" ] || [ "$MODE" = "e2e" ] || [ "$MODE" = "doctor" ]; then
  [ -f "$CONFIG_FILE" ] || err "$CONFIG_FILE not found — run 'nix run .#install' first"
  CONFIG_JSON=$(load_config_json "$CONFIG_FILE")
  validate_config_semantics
  load_runtime_settings
  if [ "$MODE" = "status" ]; then do_status; exit 0; fi
  if [ "$MODE" = "doctor" ]; then DOCTOR_EXIT=0; py doctor || DOCTOR_EXIT=$?; exit "$DOCTOR_EXIT"; fi
  if [ "$MODE" = "e2e" ]; then
    E2E_EXIT=0
    if [ "$E2E_KEEP" = true ]; then py e2e --keep || E2E_EXIT=$?; else py e2e || E2E_EXIT=$?; fi
    exit "$E2E_EXIT"
  fi
  VERIFY_EXIT=0
  py test || VERIFY_EXIT=$?
  exit "$VERIFY_EXIT"
fi

run_setup
VERIFY_FAILED=0
py test || VERIFY_FAILED=$?
if [ "$VERIFY_FAILED" -gt 0 ]; then
  printf "\n\033[1;31m  Setup finished, but %s verification check(s) failed (see above).\033[0m\n" "$VERIFY_FAILED"
  echo "  Fix the cause and re-run 'nix run .#install', or check again with 'nix run .#test'."
  echo ""
  exit 1
fi
print_summary
open_dashboard_once
