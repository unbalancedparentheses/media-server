#!/usr/bin/env bash
set -Eeo pipefail
IFS=$'\n\t'

# ═══════════════════════════════════════════════════════════════════
# Media Server — One-command setup (fresh install or re-run)
# Usage: ./setup.sh                      Full setup + verification
#        ./setup.sh --preflight          Fast local prerequisite + config checks
#        ./setup.sh --check-config       Validate config.toml only
#        ./setup.sh --yes                Non-interactive mode (skip prompts)
#        ./setup.sh --dry-run            Print actions without mutating state
#        ./setup.sh --test               Run verification only
#        ./setup.sh --update             Backup + pull latest images + restart
#        ./setup.sh --backup             Backup service configs
#        ./setup.sh --restore <file>     Restore configs from backup
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.toml"
MEDIA_DIR="$HOME/media"
CONFIG_DIR="$MEDIA_DIR/config"
BACKUP_DIR="$MEDIA_DIR/backups"
MAX_BACKUPS=10
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
OVERRIDE_FILE="$SCRIPT_DIR/docker-compose.override.yml"

# shellcheck source=scripts/lib.sh
. "$SCRIPT_DIR/scripts/lib.sh"
# shellcheck source=scripts/service_registry.sh
. "$SCRIPT_DIR/scripts/service_registry.sh"
# shellcheck source=scripts/setup-services.sh
. "$SCRIPT_DIR/scripts/setup-services.sh"
# shellcheck source=scripts/verify.sh
. "$SCRIPT_DIR/scripts/verify.sh"
# shellcheck source=scripts/maintenance.sh
. "$SCRIPT_DIR/scripts/maintenance.sh"
for step in "$SCRIPT_DIR"/scripts/steps/*.sh; do
  # shellcheck source=/dev/null
  . "$step"
done

# Clean up temp files on exit
TMPDIR_SETUP=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR_SETUP"; }
trap cleanup EXIT

# ─── Mode selection ──────────────────────────────────────────────
# ─── Mode selection ──────────────────────────────────────────────
MODE=""
RESTORE_FILE=""
NON_INTERACTIVE=false
DRY_RUN=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes|-y)
      NON_INTERACTIVE=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --preflight|--check-config|--test|--update|--backup)
      [ -n "$MODE" ] && err "Only one mode can be used at a time"
      MODE="${1#--}"
      MODE="${MODE//-/_}"
      shift
      ;;
    --restore)
      [ -n "$MODE" ] && err "Only one mode can be used at a time"
      MODE="restore"
      shift
      [ "$#" -gt 0 ] || err "Usage: ./setup.sh --restore <backup-file>"
      RESTORE_FILE="$1"
      shift
      ;;
    *)
      err "Usage: ./setup.sh [--yes] [--dry-run] [--preflight|--check-config|--test|--update|--backup|--restore <file>]"
      ;;
  esac
done
[ -z "$MODE" ] && MODE="setup"

run_setup() {
  install_prerequisites
  ensure_config
  configure_tailscale
  create_directories
  write_compose_env
  start_stack
  update_hosts_file
  read_setup_config
  wait_for_services
  load_api_keys
  configure_qbittorrent
  configure_jellyfin
  configure_sabnzbd
  configure_arrs
  configure_prowlarr
  configure_usenet_providers
  configure_bazarr
  configure_sabnzbd_auth
  configure_recyclarr
  configure_jellyseerr
  configure_unpackerr
  configure_lidarr
  configure_navidrome
  configure_immich
  configure_janitorr
  write_api_proxy
}

print_summary() {
  local ts_cli ts_hostname=""
  ts_cli="$(detect_tailscale_cli)"
  [ -n "$ts_cli" ] && ts_hostname=$("$ts_cli" status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//' || true)

  echo ""
  echo "  Setup Complete!"
  echo ""
  echo "  Local:"
  echo "    Dashboard:    http://media.local"
  echo "    Request:      http://jellyseerr.media.local"
  echo "    Watch:        http://jellyfin.media.local"
  echo "    All services: http://media.local"
  echo ""
  if [ -n "$ts_hostname" ]; then
    echo "  Remote (HTTPS):"
    echo "    Jellyfin:     https://$ts_hostname:8096"
    echo "    Jellyseerr:   https://$ts_hostname:5055"
    echo "    Landing page: https://$ts_hostname"
    echo ""
  fi
  echo "  Quick start:"
  echo "    1. Go to http://jellyseerr.media.local"
  echo "    2. Search for a series or movie"
  echo "    3. Click Request"
  echo "    4. Watch at http://jellyfin.media.local"
  if [ -n "$ts_hostname" ]; then
    echo "    Remote? Use https://$ts_hostname:8096"
  fi
  echo ""
}

# ─── Mode dispatch ──────────────────────────────────────────────
# ─── Mode dispatch ──────────────────────────────────────────────
if [ "$DRY_RUN" = "true" ] && [ "$MODE" = "setup" ]; then
  info "Dry-run mode: validating prerequisites and config only (no writes, no container changes)"
  do_preflight || true
  if [ -f "$CONFIG_FILE" ]; then
    do_check_config
  else
    warn "config.toml not found; skipping --check-config in dry-run"
  fi
  info "Dry-run complete"
  exit 0
fi
if [ "$DRY_RUN" = "true" ] && [ "$MODE" = "update" ]; then
  info "Dry-run mode: would run backup, pull images, and restart containers"
  exit 0
fi
if [ "$DRY_RUN" = "true" ] && [ "$MODE" = "restore" ]; then
  info "Dry-run mode: would stop containers, extract backup, and restart"
  exit 0
fi
if [ "$DRY_RUN" = "true" ] && [ "$MODE" = "backup" ]; then
  info "Dry-run mode: would create backup archive from $CONFIG_DIR"
  exit 0
fi
if [ "$MODE" = "check_config" ]; then do_check_config; exit 0; fi
if [ "$MODE" = "preflight" ]; then
  if do_preflight; then
    exit 0
  else
    exit 1
  fi
fi
if [ "$MODE" = "backup" ];  then do_backup; exit 0; fi
if [ "$MODE" = "restore" ]; then do_restore; exit 0; fi
if [ "$MODE" = "update" ];  then do_update; exit 0; fi


if [ "$MODE" = "test" ]; then
  require_cmd jq
  require_cmd python3
  ensure_compose_ready
  [ -f "$CONFIG_FILE" ] || err "config.toml not found"
  CONFIG_JSON=$(load_config_json "$CONFIG_FILE")
  validate_required_config
  validate_config_semantics
  ADMIN_BIND=$(cfg '.network.admin_bind // "0.0.0.0"')
  NGINX_IP=$(sed -n 's/^NGINX_IP="*\([^"]*\)"*$/\1/p' "$SCRIPT_DIR/.env" 2>/dev/null || true)
  init_service_registry
  JELLYFIN_USER=$(cfg '.jellyfin.username')
  JELLYFIN_PASS=$(cfg '.jellyfin.password')
  QBIT_USER=$(cfg '.qbittorrent.username')
  QBIT_PASS=$(cfg '.qbittorrent.password')
  read_api_keys
  VERIFY_EXIT=0
  run_verification || VERIFY_EXIT=$?
  exit "$VERIFY_EXIT"
fi

run_setup
# Failed checks are reported, not fatal; the function also relies on errexit
# being off inside it (as it is when called from a || list)
run_verification || true
print_summary
