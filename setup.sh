#!/usr/bin/env bash
set -Eeo pipefail
IFS=$'\n\t'

# ═══════════════════════════════════════════════════════════════════
# Media Server — One-command setup (fresh install or re-run)
# Usage: ./setup.sh                      Full setup + verification
#        ./setup.sh --preflight          Fast local prerequisite + config checks
#        ./setup.sh --check-config       Validate config.toml only
#        ./setup.sh --yes                Non-interactive mode (skip prompts)
#        ./setup.sh --test               Run verification only
#        ./setup.sh --update             Backup + pull latest images + restart
#        ./setup.sh --backup             Backup service configs
#        ./setup.sh --restore <file>     Restore configs from backup
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.toml"
MEDIA_DIR="$HOME/media"
CONFIG_DIR="$MEDIA_DIR/config"

# Clean up temp files on exit
TMPDIR_SETUP=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR_SETUP"; }
trap cleanup EXIT

# ─── Helpers ─────────────────────────────────────────────────────
info()  { printf "\n\033[1;34m=> %s\033[0m\n" "$*"; }
ok()    { printf "\033[1;32m   ✓ %s\033[0m\n" "$*"; }
warn()  { printf "\033[1;33m   ! %s\033[0m\n" "$*"; }
err()   { printf "\033[1;31m   ✗ %s\033[0m\n" "$*"; exit 1; }
on_error() {
  local line="$1" cmd="$2" code="$3"
  printf "\033[1;31m   ✗ Command failed (exit %s) at line %s: %s\033[0m\n" "$code" "$line" "$cmd" >&2
  exit "$code"
}
trap 'on_error "$LINENO" "$BASH_COMMAND" "$?"' ERR

has_cmd() { command -v "$1" >/dev/null 2>&1; }
require_cmd() { has_cmd "$1" || err "$1 is required"; }
as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif has_cmd sudo; then
    sudo "$@"
  else
    err "Need root privileges to run: $*"
  fi
}
require_docker_running() {
  require_cmd docker
  docker info >/dev/null 2>&1 || err "Docker is not running"
}
has_docker_compose() { docker compose version >/dev/null 2>&1; }
require_docker_compose() {
  has_docker_compose || err "Docker Compose v2 plugin is required (docker compose)"
}
install_package() {
  local pkg="$1"
  if has_cmd brew; then
    brew install "$pkg"
  elif has_cmd apt-get; then
    as_root apt-get update
    as_root apt-get install -y "$pkg"
  elif has_cmd dnf; then
    as_root dnf install -y "$pkg"
  elif has_cmd pacman; then
    as_root pacman -Sy --noconfirm "$pkg"
  else
    return 1
  fi
}
generate_secret() { openssl rand -base64 24 | tr -d '/+=' | cut -c1-24; }
detect_tailscale_cli() {
  if has_cmd tailscale; then
    command -v tailscale
  elif [ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]; then
    echo "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
  else
    echo ""
  fi
}

detect_timeout_cmd() {
  if has_cmd timeout; then
    echo "timeout"
  elif has_cmd gtimeout; then
    echo "gtimeout"
  else
    echo ""
  fi
}
TIMEOUT_CMD="$(detect_timeout_cmd)"
run_timeout() {
  local seconds="$1"
  shift
  if [ -n "$TIMEOUT_CMD" ]; then
    "$TIMEOUT_CMD" "$seconds" "$@"
  else
    "$@"
  fi
}

extract_cookie() {
  local cookie_name="$1"
  awk -v name="$cookie_name" 'BEGIN{FS="\t"} $0 !~ /^#/ && $6 == name { print $7 }'
}

sed_inplace() {
  local expr="$1" file="$2"
  if sed --version >/dev/null 2>&1; then
    sed -i -e "$expr" "$file"
  else
    sed -i '' -e "$expr" "$file"
  fi
}

try_load_config_json() {
  local path="$1"
  local out=""

  if has_cmd python3; then
    if out="$(python3 - "$path" << 'PY' 2>/dev/null
import json
import sys

path = sys.argv[1]
try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        sys.exit(2)

with open(path, "rb") as f:
    data = tomllib.load(f)

json.dump(data, sys.stdout)
PY
)"; then
      echo "$out"
      return 0
    fi
  fi

  if has_cmd yq; then
    yq -p toml -o json '.' "$path" 2>/dev/null
    return $?
  fi

  return 1
}
load_config_json() {
  local path="$1"
  local out=""
  out="$(try_load_config_json "$path")" || err "Unable to parse config.toml (need Python 3.11+ or python3-tomli, or yq)"
  echo "$out"
}
write_secure_defaults_to_config() {
  local path="$1"
  local jellyfin_pass qbittorrent_pass tubearchivist_pass
  jellyfin_pass="$(generate_secret)"
  qbittorrent_pass="$(generate_secret)"
  tubearchivist_pass="$(generate_secret)"

  python3 - "$path" "$jellyfin_pass" "$qbittorrent_pass" "$tubearchivist_pass" << 'PY'
import re
import sys

path, jf, qb, ta = sys.argv[1:]
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

section = None
for i, line in enumerate(lines):
    m = re.match(r'^\s*\[([^\]]+)\]\s*$', line)
    if m:
        section = m.group(1).strip()
        continue
    if re.match(r'^\s*password\s*=', line):
        if section == "jellyfin":
            lines[i] = 'password = "{}"\n'.format(jf)
        elif section == "qbittorrent":
            lines[i] = 'password = "{}"\n'.format(qb)
        elif section == "tubearchivist":
            lines[i] = 'password = "{}"\n'.format(ta)

with open(path, "w", encoding="utf-8") as f:
    f.writelines(lines)
PY

  ok "Generated secure default passwords in config.toml"
  echo "  Jellyfin password: $jellyfin_pass"
  echo "  qBittorrent password: $qbittorrent_pass"
  echo "  TubeArchivist password: $tubearchivist_pass"
}
ensure_compose_ready() {
  require_docker_running
  require_docker_compose
}

api() {
  local method="$1" url="$2"; shift 2
  curl -sf -X "$method" "$url" -H "Content-Type: application/json" "$@" 2>/dev/null
}

wait_for() {
  local name="$1" url="$2" max=90 i=0
  printf "   Waiting for %-15s" "$name..."
  while true; do
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 2 "$url" 2>/dev/null || echo "000")
    [ "$code" != "000" ] && break
    i=$((i + 1))
    [ "$i" -ge "$max" ] && echo " timeout!" && return 1
    sleep 1
  done
  echo " up"
}

cfg() { echo "$CONFIG_JSON" | jq -r "$1"; }
cfg_required_string() {
  local jq_path="$1" label="$2" val
  val=$(cfg "$jq_path // empty")
  [ -n "$val" ] && [ "$val" != "null" ] || err "Missing required config: $label"
}
validate_required_config() {
  cfg_required_string '.jellyfin.username' 'jellyfin.username'
  cfg_required_string '.jellyfin.password' 'jellyfin.password'
  cfg_required_string '.qbittorrent.username' 'qbittorrent.username'
  cfg_required_string '.qbittorrent.password' 'qbittorrent.password'
  cfg_required_string '.downloads.complete' 'downloads.complete'
  cfg_required_string '.downloads.incomplete' 'downloads.incomplete'
  cfg_required_string '.quality.sonarr_profile' 'quality.sonarr_profile'
  cfg_required_string '.quality.sonarr_anime_profile' 'quality.sonarr_anime_profile'
  cfg_required_string '.quality.radarr_profile' 'quality.radarr_profile'
}
is_non_negative_number() { [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]; }
is_non_negative_int() { [[ "$1" =~ ^[0-9]+$ ]]; }
validate_config_semantics() {
  local dl_complete dl_incomplete seed_ratio seed_time timezone

  dl_complete=$(cfg '.downloads.complete')
  dl_incomplete=$(cfg '.downloads.incomplete')
  seed_ratio=$(cfg '.downloads.seeding_ratio')
  seed_time=$(cfg '.downloads.seeding_time_minutes')
  timezone=$(cfg '.timezone // empty')

  [[ "$dl_complete" == /* ]] || err "downloads.complete must be an absolute path"
  [[ "$dl_incomplete" == /* ]] || err "downloads.incomplete must be an absolute path"
  is_non_negative_number "$seed_ratio" || err "downloads.seeding_ratio must be a non-negative number"
  is_non_negative_int "$seed_time" || err "downloads.seeding_time_minutes must be a non-negative integer"
  [ -n "$timezone" ] || err "timezone must be set"
}

get_api_key() {
  local f="$CONFIG_DIR/$1/config.xml"
  [ -f "$f" ] && sed -n 's/.*<ApiKey>\(.*\)<\/ApiKey>.*/\1/p' "$f" 2>/dev/null || echo ""
}

# ─── Mode selection ──────────────────────────────────────────────
MODE=""
RESTORE_FILE=""
NON_INTERACTIVE=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes|-y)
      NON_INTERACTIVE=true
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
      err "Usage: ./setup.sh [--yes] [--preflight|--check-config|--test|--update|--backup|--restore <file>]"
      ;;
  esac
done
[ -z "$MODE" ] && MODE="setup"

BACKUP_DIR="$MEDIA_DIR/backups"
MAX_BACKUPS=10
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
OVERRIDE_FILE="$SCRIPT_DIR/docker-compose.override.yml"
# Helper: docker compose with both base and override files
dc() {
  if [ -f "$OVERRIDE_FILE" ]; then
    docker compose -f "$COMPOSE_FILE" -f "$OVERRIDE_FILE" "$@"
  else
    docker compose -f "$COMPOSE_FILE" "$@"
  fi
}

# ─── Backup function ────────────────────────────────────────────
do_backup() {
  [ ! -d "$CONFIG_DIR" ] && err "Config directory not found: $CONFIG_DIR"
  mkdir -p "$BACKUP_DIR"

  local timestamp backup_file services dir backup_size backup_count remove_count
  timestamp=$(date +%Y%m%d_%H%M%S)
  backup_file="$BACKUP_DIR/media-server_${timestamp}.tar.gz"

  info "Backing up service configs..."

  services=""
  for dir in "$CONFIG_DIR"/*/; do
    [ -d "$dir" ] && services="$services $(basename "$dir")"
  done
  ok "Services:$services"

  # Dump Immich Postgres before tarring (file copy of a running DB is unsafe)
  if docker inspect immich-postgres &>/dev/null; then
    docker exec immich-postgres pg_dump -U postgres immich > "$CONFIG_DIR/immich-postgres/immich_dump.sql" 2>/dev/null && \
      ok "Immich Postgres dumped" || warn "Could not dump Immich Postgres"
  fi

  tar czf "$backup_file" -C "$MEDIA_DIR" config/ 2>/dev/null
  backup_size=$(du -sh "$backup_file" | cut -f1)
  ok "Created: $backup_file ($backup_size)"

  shopt -s nullglob
  local backup_files=("$BACKUP_DIR"/media-server_*.tar.gz)
  shopt -u nullglob
  backup_count="${#backup_files[@]}"
  if [ "$backup_count" -gt "$MAX_BACKUPS" ]; then
    local sorted_backups old_ifs
    old_ifs="$IFS"
    IFS=$'\n' sorted_backups=($(ls -1t "${backup_files[@]}"))
    IFS="$old_ifs"
    for old in "${sorted_backups[@]:$MAX_BACKUPS}"; do
      rm -f "$old"
      ok "Pruned: $(basename "$old")"
    done
  fi

  echo ""
  echo "  Backups in $BACKUP_DIR ($backup_count total, keeping last $MAX_BACKUPS)"
  echo "  Restore with: ./setup.sh --restore $backup_file"
  echo ""
}

# ─── Restore function ───────────────────────────────────────────
do_restore() {
  [ -z "$RESTORE_FILE" ] && err "Usage: ./setup.sh --restore <backup-file>"
  [ ! -f "$RESTORE_FILE" ] && err "Backup file not found: $RESTORE_FILE"
  ensure_compose_ready

  info "Restoring from $RESTORE_FILE..."
  echo "  This will overwrite current configs in $CONFIG_DIR"
  if [ "$NON_INTERACTIVE" = "true" ]; then
    ok "Non-interactive mode: restore confirmation auto-accepted"
  else
    echo ""
    read -r -p "  Continue? [y/N] " confirm
    [ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && { echo "  Aborted."; exit 0; }
  fi

  info "Stopping containers..."
  dc down 2>/dev/null || true

  info "Extracting backup..."
  tar xzf "$RESTORE_FILE" -C "$MEDIA_DIR"
  ok "Configs restored"

  info "Starting containers..."
  dc up -d
  ok "All containers started"

  echo ""
  echo "  Restore complete. Run ./setup.sh --test to verify."
  echo ""
}

# ─── Update function ────────────────────────────────────────────
do_update() {
  [ ! -f "$COMPOSE_FILE" ] && err "docker-compose.yml not found"
  ensure_compose_ready

  info "Creating pre-update backup..."
  do_backup

  info "Pulling latest images..."
  dc pull

  info "Restarting containers with new images..."
  dc up -d

  info "Current image versions..."
  dc images --format "table {{.Service}}\t{{.Tag}}\t{{.Size}}"

  local old_images
  old_images=$(docker images --filter "dangling=true" -q 2>/dev/null | wc -l | tr -d ' ')
  if [ "$old_images" -gt 0 ]; then
    info "Cleaning up $old_images old image(s)..."
    docker image prune -f >/dev/null 2>&1
    ok "Old images removed"
  fi

  echo ""
  echo "  Update complete. Run ./setup.sh --test to verify."
  echo ""
}

do_preflight() {
  local failed=0
  local config_json=""
  local value=""

  pf_ok() { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
  pf_fail() { printf "\033[1;31m[FAIL]\033[0m %s\n" "$*"; failed=1; }
  pf_warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }

  preflight_check_cmd() {
    local name="$1"
    if has_cmd "$name"; then
      pf_ok "$name installed"
    else
      pf_fail "$name is missing"
    fi
  }

  echo "Preflight checks for media-server"
  echo ""

  preflight_check_cmd bash
  preflight_check_cmd curl
  preflight_check_cmd jq
  preflight_check_cmd python3

  if has_cmd docker; then
    pf_ok "docker installed"
    if docker info >/dev/null 2>&1; then
      pf_ok "docker daemon is running"
      if has_docker_compose; then
        pf_ok "docker compose plugin available"
      else
        pf_fail "docker compose plugin is missing"
      fi
    else
      pf_fail "docker is installed but daemon is not running"
    fi
  else
    pf_fail "docker is missing"
  fi

  if [ -f "$CONFIG_FILE" ]; then
    pf_ok "config.toml exists"
    if has_cmd jq; then
      if config_json=$(try_load_config_json "$CONFIG_FILE" 2>/dev/null); then
        pf_ok "config.toml parses as valid TOML"
        for path in \
          ".jellyfin.username" \
          ".jellyfin.password" \
          ".qbittorrent.username" \
          ".qbittorrent.password" \
          ".downloads.complete" \
          ".downloads.incomplete" \
          ".quality.sonarr_profile" \
          ".quality.sonarr_anime_profile" \
          ".quality.radarr_profile"; do
          value=$(echo "$config_json" | jq -r "$path // empty")
          if [ -n "$value" ] && [ "$value" != "null" ]; then
            pf_ok "required config present: $path"
          else
            pf_fail "required config missing: $path"
          fi
        done

        value=$(echo "$config_json" | jq -r '.downloads.complete // empty')
        [[ "$value" == /* ]] && pf_ok "downloads.complete is absolute" || pf_fail "downloads.complete must be an absolute path"
        value=$(echo "$config_json" | jq -r '.downloads.incomplete // empty')
        [[ "$value" == /* ]] && pf_ok "downloads.incomplete is absolute" || pf_fail "downloads.incomplete must be an absolute path"
        value=$(echo "$config_json" | jq -r '.downloads.seeding_ratio // empty')
        is_non_negative_number "$value" && pf_ok "downloads.seeding_ratio is valid" || pf_fail "downloads.seeding_ratio must be a non-negative number"
        value=$(echo "$config_json" | jq -r '.downloads.seeding_time_minutes // empty')
        is_non_negative_int "$value" && pf_ok "downloads.seeding_time_minutes is valid" || pf_fail "downloads.seeding_time_minutes must be a non-negative integer"
        value=$(echo "$config_json" | jq -r '.timezone // empty')
        [ -n "$value" ] && pf_ok "timezone is set" || pf_fail "timezone must be set"
      else
        pf_fail "config.toml is invalid TOML"
      fi
    else
      pf_warn "skipping config content validation (jq unavailable)"
    fi
  else
    pf_warn "config.toml is missing (copy config.toml.example first)"
    failed=1
  fi

  if [ -f "$COMPOSE_FILE" ]; then
    pf_ok "docker-compose.yml exists"
    if has_cmd docker && docker info >/dev/null 2>&1 && has_docker_compose; then
      if [ -f "$OVERRIDE_FILE" ]; then
        docker compose -f "$COMPOSE_FILE" -f "$OVERRIDE_FILE" config -q >/dev/null 2>&1 && \
          pf_ok "docker compose config is valid (base + override)" || pf_fail "docker compose config is invalid"
      else
        docker compose -f "$COMPOSE_FILE" config -q >/dev/null 2>&1 && \
          pf_ok "docker compose config is valid" || pf_fail "docker compose config is invalid"
      fi
    else
      pf_warn "skipping docker compose validation (docker/compose unavailable)"
    fi
  else
    pf_fail "docker-compose.yml is missing"
  fi

  echo ""
  if [ "$failed" -eq 0 ]; then
    pf_ok "preflight passed"
  else
    pf_fail "preflight failed"
  fi
  return "$failed"
}
do_check_config() {
  require_cmd jq
  [ -f "$CONFIG_FILE" ] || err "config.toml not found"
  CONFIG_JSON=$(load_config_json "$CONFIG_FILE")
  validate_required_config
  validate_config_semantics

  info "Config validation passed"
  ok "Credentials and required fields are present"
  ok "Download paths and numeric values are valid"
  ok "Timezone is set"
}
smoke_check_generated_files() {
  local missing=0
  local f

  info "Running generated-file smoke checks..."
  for f in "$SCRIPT_DIR/.env" "$SCRIPT_DIR/docker-compose.override.yml" "$CONFIG_DIR/crowdsec/config/acquis.yaml"; do
    if [ -s "$f" ]; then
      ok "Present: $f"
    else
      warn "Missing/empty: $f"
      missing=1
    fi
  done

  if [ "$missing" -ne 0 ]; then
    err "Generated-file smoke checks failed"
  fi

  dc config -q >/dev/null 2>&1 || err "Docker Compose config validation failed"
  ok "Docker Compose config validates"
}

# ─── Mode dispatch ──────────────────────────────────────────────
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

  JELLYFIN_USER=$(cfg '.jellyfin.username')
  JELLYFIN_PASS=$(cfg '.jellyfin.password')
  QBIT_USER=$(cfg '.qbittorrent.username')
  QBIT_PASS=$(cfg '.qbittorrent.password')
  QBIT_URL="http://localhost:8081"
  JELLYFIN_URL="http://localhost:8096"
  SONARR_URL="http://localhost:8989"
  SONARR_ANIME_URL="http://localhost:8990"
  RADARR_URL="http://localhost:7878"
  PROWLARR_URL="http://localhost:9696"
  BAZARR_URL="http://localhost:6767"
  SABNZBD_URL="http://localhost:8080"
  JELLYSEERR_URL="http://localhost:5055"

  SONARR_KEY=$(get_api_key "sonarr")
  SONARR_ANIME_KEY=$(get_api_key "sonarr-anime")
  RADARR_KEY=$(get_api_key "radarr")
  LIDARR_KEY=$(get_api_key "lidarr")
  PROWLARR_KEY=$(get_api_key "prowlarr")
  SABNZBD_KEY=""
  [ -f "$CONFIG_DIR/sabnzbd/sabnzbd.ini" ] && SABNZBD_KEY=$(sed -n 's/^api_key = *//p' "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null || echo "")
  JELLYSEERR_KEY=""
  [ -f "$CONFIG_DIR/jellyseerr/settings.json" ] && JELLYSEERR_KEY=$(jq -r '.main.apiKey // empty' "$CONFIG_DIR/jellyseerr/settings.json" 2>/dev/null)
  LIDARR_URL="http://localhost:8686"
  LAZYLIBRARIAN_URL="http://localhost:5299"
  NAVIDROME_URL="http://localhost:4533"
  KAVITA_URL="http://localhost:5001"
  IMMICH_URL="http://localhost:2283"
  TUBEARCHIVIST_URL="http://localhost:8000"
  TDARR_URL="http://localhost:8265"
  AUTOBRR_URL="http://localhost:7474"
  OPEN_WEBUI_URL="http://localhost:3100"
  DOZZLE_URL="http://localhost:9999"
  BESZEL_URL="http://localhost:8090"
  CROWDSEC_URL="http://localhost:8180"
  SCRUTINY_URL="http://localhost:9091"
  GITEA_URL="http://localhost:3000"
  UPTIME_KUMA_URL="http://localhost:3001"
  HOMEPAGE_URL="http://localhost:3002"

fi

if [ "$MODE" = "setup" ]; then
# ═══════════════════════════════════════════════════════════════════
# 1. PREREQUISITES
# ═══════════════════════════════════════════════════════════════════
info "Checking prerequisites..."

OS_NAME="$(uname -s)"
if [ "$OS_NAME" = "Darwin" ]; then
  # Homebrew (macOS package manager)
  if ! has_cmd brew; then
    info "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
    ok "Homebrew installed"
  else
    ok "Homebrew"
  fi
fi

# Docker
if ! has_cmd docker; then
  if [ "$OS_NAME" = "Darwin" ]; then
    info "Installing Docker Desktop..."
    brew install --cask docker
    ok "Docker Desktop installed — please open it from Applications and wait for it to start"
    echo ""
    echo "  After Docker Desktop is running, re-run this script."
    echo ""
    exit 0
  elif [ "$OS_NAME" = "Linux" ]; then
    info "Installing Docker Engine (Linux)..."
    require_cmd curl
    curl -fsSL https://get.docker.com | as_root sh
    if has_cmd systemctl; then
      as_root systemctl enable --now docker >/dev/null 2>&1 || true
    fi
    ok "Docker installed"
  else
    err "Unsupported OS for automatic Docker install: $OS_NAME"
  fi
fi
if ! docker info >/dev/null 2>&1; then
  if [ "$OS_NAME" = "Linux" ] && docker info 2>&1 | grep -qi "permission denied"; then
    warn "Docker permission denied for user '$USER'."
    warn "Run: sudo usermod -aG docker $USER"
    warn "Then log out/in and re-run setup."
  fi
  err "Docker is installed but not usable. Start Docker (or fix permissions) and re-run this script."
fi
ok "Docker"
require_docker_compose
ok "Docker Compose"

if [ "$OS_NAME" = "Linux" ] && has_cmd getent && getent group docker >/dev/null 2>&1; then
  if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
    as_root usermod -aG docker "$USER" >/dev/null 2>&1 || true
    warn "Added $USER to docker group (or attempted to). You may need to log out/in."
  fi
fi

# python3
if ! has_cmd python3; then
  err "python3 is required. Install Xcode Command Line Tools or Homebrew Python."
else
  ok "python3"
fi

# jq
if ! has_cmd jq; then
  info "Installing jq..."
  install_package jq || err "Could not install jq automatically. Please install jq and re-run."
  ok "jq installed"
else
  ok "jq"
fi

# yq is optional (python3 TOML parser is preferred fallback)
if has_cmd yq; then
  ok "yq"
else
  warn "yq not found (using python TOML parser fallback)"
fi

# Tailscale (remote access)
TS_CLI="$(detect_tailscale_cli)"
if [ -n "$TS_CLI" ]; then
  ok "Tailscale"
elif [ "$OS_NAME" = "Darwin" ] && has_cmd brew; then
  info "Installing Tailscale..."
  brew install --cask tailscale
  TS_CLI="$(detect_tailscale_cli)"
  [ -n "$TS_CLI" ] && ok "Tailscale installed" || warn "Could not detect tailscale CLI after install"
else
  warn "Tailscale not installed (remote access step will be skipped)"
fi

# config.toml
if [ ! -f "$CONFIG_FILE" ]; then
  cp "$SCRIPT_DIR/config.toml.example" "$CONFIG_FILE"
  if [ "$NON_INTERACTIVE" = "true" ]; then
    write_secure_defaults_to_config "$CONFIG_FILE"
    warn "config.toml not found — created with secure generated defaults"
  else
    warn "config.toml not found — created from config.toml.example with defaults"
  fi
fi
CONFIG_JSON=$(load_config_json "$CONFIG_FILE")
validate_required_config
validate_config_semantics

# Check Tailscale connection
TS_IP=""
TS_HOSTNAME=""
if [ -z "$TS_CLI" ]; then
  warn "Skipping Tailscale setup (CLI not found)"
elif ! "$TS_CLI" status &>/dev/null; then
  warn "Tailscale is not connected"
  echo "  Open Tailscale from the menu bar and sign in to enable remote access."
  echo "  You can skip this — local access will still work."
  if [ "$NON_INTERACTIVE" != "true" ]; then
    echo ""
    read -r -p "  Press Enter to continue..."
  fi
else
  TS_IP=$("$TS_CLI" ip -4 2>/dev/null || echo "")
  TS_HOSTNAME=$("$TS_CLI" status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//')
  if [ -n "$TS_IP" ]; then
    ok "Tailscale connected ($TS_IP)"
  fi
  if [ -n "$TS_HOSTNAME" ]; then
    SERVE_STATUS=$("$TS_CLI" serve status 2>/dev/null || echo "")
    if echo "$SERVE_STATUS" | grep -q "https.*443" && echo "$SERVE_STATUS" | grep -q "https.*8096" && echo "$SERVE_STATUS" | grep -q "https.*5055"; then
      ok "Tailscale HTTPS already configured"
    else
      info "Configuring Tailscale HTTPS..."
      run_timeout 10 "$TS_CLI" serve --bg --yes --https=443 http://127.0.0.1:80 </dev/null 2>/dev/null && ok "HTTPS :443 → Nginx" || warn "Failed to configure HTTPS :443"
      run_timeout 10 "$TS_CLI" serve --bg --yes --https=8096 http://127.0.0.1:8096 </dev/null 2>/dev/null && ok "HTTPS :8096 → Jellyfin" || warn "Failed to configure HTTPS :8096"
      run_timeout 10 "$TS_CLI" serve --bg --yes --https=5055 http://127.0.0.1:5055 </dev/null 2>/dev/null && ok "HTTPS :5055 → Jellyseerr" || warn "Failed to configure HTTPS :5055"
    fi
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# 2. DIRECTORY STRUCTURE
# ═══════════════════════════════════════════════════════════════════
info "Creating directory structure..."

mkdir -p "$MEDIA_DIR"/{movies,tv,anime,music,books,photos}
mkdir -p "$MEDIA_DIR"/downloads/torrents/{complete,incomplete}
mkdir -p "$MEDIA_DIR"/downloads/usenet/{complete,incomplete}
mkdir -p "$MEDIA_DIR"/backups
mkdir -p "$MEDIA_DIR"/{youtube,transcode_cache,leaving-soon}
mkdir -p "$MEDIA_DIR"/config/{jellyfin,sonarr,sonarr-anime,radarr,prowlarr,bazarr,sabnzbd,qbittorrent,jellyseerr,recyclarr,flaresolverr,nginx,lidarr,lazylibrarian,navidrome,kavita,unpackerr,autobrr,gluetun,tubearchivist/cache,archivist-es,archivist-redis,tdarr/server,tdarr/configs,tdarr/logs,janitorr,ollama,open-webui,crowdsec/config,crowdsec/data,beszel,immich-ml,immich-postgres,scrutiny,gitea,uptime-kuma,homepage}/logs

# Ensure api-proxy.conf exists as a file (Docker would create it as a directory)
[ -f "$CONFIG_DIR/nginx/api-proxy.conf" ] || touch "$CONFIG_DIR/nginx/api-proxy.conf"

# Elasticsearch writes as uid 1000 — fix permissions on macOS (uid 501)
chmod 777 "$CONFIG_DIR/archivist-es" 2>/dev/null || true

ok "~/media/ directory tree ready"

# Pre-seed SABnzbd config to skip the first-run wizard
if [ ! -f "$CONFIG_DIR/sabnzbd/sabnzbd.ini" ]; then
  SAB_GEN_KEY=$(openssl rand -hex 16)
  cat > "$CONFIG_DIR/sabnzbd/sabnzbd.ini" << SABEOF
__version__ = 19
__encoding__ = utf-8
[misc]
api_key = $SAB_GEN_KEY
download_dir = /downloads/usenet/incomplete
complete_dir = /downloads/usenet/complete
host_whitelist = sabnzbd
SABEOF
  ok "SABnzbd: pre-seeded config (wizard skipped)"
fi

# Pre-seed Kavita appsettings.json with a JWT token key (prevents null TokenKey crash)
if [ ! -f "$CONFIG_DIR/kavita/appsettings.json" ]; then
  KAVITA_TOKEN_KEY=$(openssl rand -base64 128 | tr -d '\n')
  cat > "$CONFIG_DIR/kavita/appsettings.json" << KAVEOF
{
  "TokenKey": "$KAVITA_TOKEN_KEY",
  "Port": 5000,
  "IpAddresses": "0.0.0.0"
}
KAVEOF
  ok "Kavita: pre-seeded appsettings.json (TokenKey generated)"
fi

# ═══════════════════════════════════════════════════════════════════
# 3. DOCKER COMPOSE
# ═══════════════════════════════════════════════════════════════════
info "Generating .env for Docker Compose..."

TZ_VALUE=$(cfg '.timezone // "America/New_York"')

# Generate a stable Immich DB password (reuse existing if present)
if [ -f "$SCRIPT_DIR/.env" ] && grep -q "^IMMICH_DB_PASSWORD=" "$SCRIPT_DIR/.env" 2>/dev/null; then
  IMMICH_DB_PASS=$(sed -n 's/^IMMICH_DB_PASSWORD=//p' "$SCRIPT_DIR/.env")
else
  IMMICH_DB_PASS=$(openssl rand -hex 16)
fi

# VPN settings (optional)
VPN_ENABLE=$(cfg '.vpn.enable // false')
VPN_PROVIDER=$(cfg '.vpn.provider // "mullvad"')
VPN_TYPE=$(cfg '.vpn.type // "wireguard"')
VPN_WG_KEY=$(cfg '.vpn.wireguard_private_key // ""')
VPN_WG_ADDR=$(cfg '.vpn.wireguard_addresses // ""')
VPN_COUNTRIES=$(cfg '.vpn.server_countries // ""')

# TubeArchivist settings
TA_USER=$(cfg '.tubearchivist.username // "admin"')
TA_PASS=$(cfg '.tubearchivist.password // "changeme"')

# Generate a stable TubeArchivist ES password (reuse existing if present)
if [ -f "$SCRIPT_DIR/.env" ] && grep -q "^TA_ELASTIC_PASSWORD=" "$SCRIPT_DIR/.env" 2>/dev/null; then
  TA_ES_PASS=$(sed -n 's/^TA_ELASTIC_PASSWORD=//p' "$SCRIPT_DIR/.env")
else
  TA_ES_PASS=$(openssl rand -hex 16)
fi

COMPOSE_PROFILES_VALUE=""
[ "$VPN_ENABLE" = "true" ] && COMPOSE_PROFILES_VALUE="vpn"

cat > "$SCRIPT_DIR/.env" << EOF
PUID=$(id -u)
PGID=$(id -g)
TZ=$TZ_VALUE
IMMICH_DB_PASSWORD=$IMMICH_DB_PASS
VPN_SERVICE_PROVIDER=$VPN_PROVIDER
VPN_TYPE=$VPN_TYPE
WIREGUARD_PRIVATE_KEY=$VPN_WG_KEY
WIREGUARD_ADDRESSES=$VPN_WG_ADDR
VPN_SERVER_COUNTRIES=$VPN_COUNTRIES
TA_USERNAME=$TA_USER
TA_PASSWORD=$TA_PASS
TA_ELASTIC_PASSWORD=$TA_ES_PASS
BESZEL_AGENT_KEY=$(cfg '.beszel.agent_key // ""')
COMPOSE_PROFILES=$COMPOSE_PROFILES_VALUE
EOF
ok ".env (PUID=$(id -u), PGID=$(id -g), TZ=$TZ_VALUE)"

# Generate docker-compose.override.yml for qBittorrent VPN routing
info "Generating docker-compose.override.yml..."
if [ "$VPN_ENABLE" = "true" ]; then
  OVERRIDE_CONTENT='services:
  gluetun:
    ports:
      - "8081:8081"
  qbittorrent:
    network_mode: "service:gluetun"
    depends_on:
      gluetun:
        condition: service_healthy'
  OVERRIDE_MSG="VPN enabled: qBittorrent routed through gluetun"
else
  OVERRIDE_CONTENT='services:
  qbittorrent:
    ports:
      - "8081:8081"'
  OVERRIDE_MSG="VPN disabled: qBittorrent ports exposed directly"
fi
# Only write if content changed (avoids unnecessary container recreation)
if [ ! -f "$SCRIPT_DIR/docker-compose.override.yml" ] || [ "$(cat "$SCRIPT_DIR/docker-compose.override.yml")" != "$OVERRIDE_CONTENT" ]; then
  printf '%s\n' "$OVERRIDE_CONTENT" > "$SCRIPT_DIR/docker-compose.override.yml"
fi
ok "$OVERRIDE_MSG"

# Generate CrowdSec acquisition config for nginx logs
info "Generating CrowdSec acquisition config..."
mkdir -p "$CONFIG_DIR/crowdsec/config"
cat > "$CONFIG_DIR/crowdsec/config/acquis.yaml" << 'CSEOF'
source: docker
container_name:
  - media-nginx
labels:
  type: nginx
CSEOF
ok "acquis.yaml (reads nginx container logs)"

# Janitorr application.yml is generated later in section 16.10
# after API keys are available from Sonarr/Radarr/Jellyfin

info "Starting containers..."
smoke_check_generated_files

dc up -d

ok "All containers started"

# ═══════════════════════════════════════════════════════════════════
# 4. /etc/hosts
# ═══════════════════════════════════════════════════════════════════
info "Checking /etc/hosts..."

DOMAINS="media.local jellyfin.media.local jellyseerr.media.local sonarr.media.local sonarr-anime.media.local radarr.media.local prowlarr.media.local bazarr.media.local sabnzbd.media.local qbittorrent.media.local lidarr.media.local lazylibrarian.media.local navidrome.media.local kavita.media.local immich.media.local tubearchivist.media.local tdarr.media.local autobrr.media.local open-webui.media.local dozzle.media.local beszel.media.local scrutiny.media.local gitea.media.local uptime-kuma.media.local homepage.media.local"

if grep -qE "^[[:space:]]*127\.0\.0\.1[[:space:]].*\bmedia\.local\b" /etc/hosts 2>/dev/null; then
  ok "Hosts entries already present"
else
  echo ""
  echo "  Adding .media.local domains to /etc/hosts (requires sudo)..."
  echo ""
  if sudo -n true 2>/dev/null; then
    sudo bash -c "echo '' >> /etc/hosts && echo '# Media Server' >> /etc/hosts && echo '127.0.0.1 $DOMAINS' >> /etc/hosts"
    ok "Hosts entries added"
  elif [ "$NON_INTERACTIVE" = "true" ]; then
    warn "Could not update /etc/hosts in non-interactive mode (no passwordless sudo)."
    echo "    echo '127.0.0.1 $DOMAINS' | sudo tee -a /etc/hosts"
  elif sudo bash -c "echo '' >> /etc/hosts && echo '# Media Server' >> /etc/hosts && echo '127.0.0.1 $DOMAINS' >> /etc/hosts"; then
    ok "Hosts entries added"
  else
    warn "Could not update /etc/hosts (no sudo). Run manually:"
    echo "    echo '127.0.0.1 $DOMAINS' | sudo tee -a /etc/hosts"
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# 5. READ CONFIG
# ═══════════════════════════════════════════════════════════════════
info "Reading config..."

JELLYFIN_USER=$(cfg '.jellyfin.username')
JELLYFIN_PASS=$(cfg '.jellyfin.password')
QBIT_USER=$(cfg '.qbittorrent.username')
QBIT_CONFIGURED_PASS=$(cfg '.qbittorrent.password')
DL_COMPLETE=$(cfg '.downloads.complete')
DL_INCOMPLETE=$(cfg '.downloads.incomplete')
SEED_RATIO=$(cfg '.downloads.seeding_ratio')
SEED_TIME=$(cfg '.downloads.seeding_time_minutes')
SUBTITLE_LANGS=$(cfg '[.subtitles.languages[]] | join(",")')
SUBTITLE_PROVIDERS=$(cfg '[.subtitles.providers[]] | join(",")')
SONARR_PROFILE=$(cfg '.quality.sonarr_profile')
SONARR_ANIME_PROFILE=$(cfg '.quality.sonarr_anime_profile')
RADARR_PROFILE=$(cfg '.quality.radarr_profile')

QBIT_URL="http://localhost:8081"
JELLYFIN_URL="http://localhost:8096"
SONARR_URL="http://localhost:8989"
SONARR_ANIME_URL="http://localhost:8990"
RADARR_URL="http://localhost:7878"
PROWLARR_URL="http://localhost:9696"
BAZARR_URL="http://localhost:6767"
SABNZBD_URL="http://localhost:8080"
JELLYSEERR_URL="http://localhost:5055"

LIDARR_URL="http://localhost:8686"
LAZYLIBRARIAN_URL="http://localhost:5299"
NAVIDROME_URL="http://localhost:4533"
KAVITA_URL="http://localhost:5001"
IMMICH_URL="http://localhost:2283"
TUBEARCHIVIST_URL="http://localhost:8000"
TDARR_URL="http://localhost:8265"
AUTOBRR_URL="http://localhost:7474"
OPEN_WEBUI_URL="http://localhost:3100"
DOZZLE_URL="http://localhost:9999"
BESZEL_URL="http://localhost:8090"
CROWDSEC_URL="http://localhost:8180"
SCRUTINY_URL="http://localhost:9091"
GITEA_URL="http://localhost:3000"
UPTIME_KUMA_URL="http://localhost:3001"
HOMEPAGE_URL="http://localhost:3002"

SONARR_INTERNAL="http://sonarr:8989"
SONARR_ANIME_INTERNAL="http://sonarr-anime:8989"
RADARR_INTERNAL="http://radarr:7878"
PROWLARR_INTERNAL="http://prowlarr:9696"
LIDARR_INTERNAL="http://lidarr:8686"

# ═══════════════════════════════════════════════════════════════════
# 6. WAIT FOR SERVICES
# ═══════════════════════════════════════════════════════════════════
info "Waiting for all services..."
wait_for "Jellyfin"     "$JELLYFIN_URL/health"
wait_for "Sonarr"       "$SONARR_URL/ping"
wait_for "Sonarr Anime" "$SONARR_ANIME_URL/ping"
wait_for "Radarr"       "$RADARR_URL/ping"
wait_for "Prowlarr"     "$PROWLARR_URL/ping"
wait_for "Bazarr"       "$BAZARR_URL"
wait_for "SABnzbd"      "$SABNZBD_URL"
wait_for "qBittorrent"  "$QBIT_URL"
wait_for "Jellyseerr"   "$JELLYSEERR_URL"
wait_for "Lidarr"       "$LIDARR_URL/ping"
wait_for "LazyLibrarian" "$LAZYLIBRARIAN_URL"
wait_for "Navidrome"    "$NAVIDROME_URL/ping"
wait_for "Kavita"       "$KAVITA_URL"
wait_for "Autobrr"      "$AUTOBRR_URL/api/healthz/liveness"
wait_for "TubeArchivist" "$TUBEARCHIVIST_URL/api/ping"
wait_for "Tdarr"        "$TDARR_URL"
wait_for "Immich"       "$IMMICH_URL/api/server/ping"
wait_for "Gitea"        "$GITEA_URL/api/v1/version"
wait_for "Uptime Kuma"  "$UPTIME_KUMA_URL"
wait_for "Open WebUI"   "$OPEN_WEBUI_URL"
wait_for "Dozzle"       "$DOZZLE_URL"
wait_for "Beszel"       "$BESZEL_URL/api/health"
wait_for "Homepage"     "$HOMEPAGE_URL"

# ═══════════════════════════════════════════════════════════════════
# 7. API KEYS
# ═══════════════════════════════════════════════════════════════════
info "Reading API keys..."

SONARR_KEY=$(get_api_key "sonarr")
SONARR_ANIME_KEY=$(get_api_key "sonarr-anime")
RADARR_KEY=$(get_api_key "radarr")
LIDARR_KEY=$(get_api_key "lidarr")
PROWLARR_KEY=$(get_api_key "prowlarr")
SABNZBD_KEY=""
if [ -f "$CONFIG_DIR/sabnzbd/sabnzbd.ini" ]; then
  SABNZBD_KEY=$(sed -n 's/^api_key = *//p' "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null || echo "")
  # Ensure Docker hostname is in the whitelist (prevents 403 from Sonarr/Radarr)
  if ! grep -q "^host_whitelist.*sabnzbd" "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null; then
    sed_inplace 's/^host_whitelist = .*/& sabnzbd/' "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null
    docker restart sabnzbd >/dev/null 2>&1 && ok "SABnzbd: added Docker hostname to whitelist" || true
    sleep 3
  fi
fi

# Try configured password first, fall back to temp password from logs
QBIT_TEMP_PASS=$(docker logs qbittorrent 2>&1 | sed -n 's/.*A temporary password is provided for this session: *//p' | tail -1 || echo "")
[ -z "$QBIT_TEMP_PASS" ] && QBIT_TEMP_PASS="adminadmin"
QBIT_PASS="$QBIT_CONFIGURED_PASS"

JELLYSEERR_KEY=""
[ -f "$CONFIG_DIR/jellyseerr/settings.json" ] && JELLYSEERR_KEY=$(jq -r '.main.apiKey // empty' "$CONFIG_DIR/jellyseerr/settings.json" 2>/dev/null)

[ -n "$SONARR_KEY" ]       && ok "Sonarr:       $SONARR_KEY"       || err "Sonarr key not found"
[ -n "$SONARR_ANIME_KEY" ] && ok "Sonarr Anime: $SONARR_ANIME_KEY" || err "Sonarr Anime key not found"
[ -n "$RADARR_KEY" ]       && ok "Radarr:       $RADARR_KEY"       || err "Radarr key not found"
[ -n "$LIDARR_KEY" ]       && ok "Lidarr:       $LIDARR_KEY"       || err "Lidarr key not found"
[ -n "$PROWLARR_KEY" ]     && ok "Prowlarr:     $PROWLARR_KEY"     || err "Prowlarr key not found"
[ -n "$SABNZBD_KEY" ]      && ok "SABnzbd:      $SABNZBD_KEY"      || warn "SABnzbd key not found"
[ -n "$JELLYSEERR_KEY" ]   && ok "Jellyseerr:   ${JELLYSEERR_KEY:0:8}..."  || warn "Jellyseerr key not found (will read after setup)"
ok "qBittorrent:  admin / $QBIT_PASS"

# ═══════════════════════════════════════════════════════════════════
# 8. QBITTORRENT
# ═══════════════════════════════════════════════════════════════════
info "Configuring qBittorrent..."

# Try configured password first, then temp password
QBIT_COOKIE=""
for try_pass in "$QBIT_PASS" "$QBIT_TEMP_PASS"; do
  QBIT_COOKIE=$(curl -sf -c - "$QBIT_URL/api/v2/auth/login" \
    -d "username=$QBIT_USER&password=$try_pass" 2>/dev/null | extract_cookie SID || echo "")
  [ -n "$QBIT_COOKIE" ] && break
done

if [ -n "$QBIT_COOKIE" ]; then
  ok "Logged in"

  # Set permanent password + preferences
  curl -sf -o /dev/null "$QBIT_URL/api/v2/app/setPreferences" \
    -b "SID=$QBIT_COOKIE" \
    --data-urlencode "json={
      \"web_ui_username\": \"$QBIT_USER\",
      \"web_ui_password\": \"$QBIT_PASS\",
      \"save_path\": \"$DL_COMPLETE\",
      \"temp_path\": \"$DL_INCOMPLETE\",
      \"temp_path_enabled\": true,
      \"web_ui_port\": 8081,
      \"max_ratio\": $SEED_RATIO,
      \"max_seeding_time\": $SEED_TIME,
      \"up_limit\": 102400,
      \"web_ui_csrf_protection_enabled\": false,
      \"bypass_auth_subnet_whitelist_enabled\": true,
      \"bypass_auth_subnet_whitelist\": \"172.16.0.0/12,192.168.0.0/16\"
    }" 2>/dev/null && ok "Preferences + credentials set" || warn "Could not set preferences"

  for cat in sonarr sonarr-anime radarr lidarr; do
    curl -sf -o /dev/null "$QBIT_URL/api/v2/torrents/createCategory" \
      -b "SID=$QBIT_COOKIE" \
      -d "category=$cat&savePath=$DL_COMPLETE/$cat" 2>/dev/null && ok "Category: $cat" || \
    curl -sf -o /dev/null "$QBIT_URL/api/v2/torrents/editCategory" \
      -b "SID=$QBIT_COOKIE" \
      -d "category=$cat&savePath=$DL_COMPLETE/$cat" 2>/dev/null && ok "Category: $cat (updated)" || true
  done
else
  warn "Could not log in"
fi

# ═══════════════════════════════════════════════════════════════════
# 9. JELLYFIN
# ═══════════════════════════════════════════════════════════════════
info "Configuring Jellyfin..."

JF_HEADER='X-Emby-Authorization: MediaBrowser Client="setup", Device="script", DeviceId="setup-script", Version="1.0"'

JELLYFIN_STARTUP=$(curl -sf "$JELLYFIN_URL/Startup/Configuration" 2>/dev/null || echo "")
if echo "$JELLYFIN_STARTUP" | grep -q "UICulture"; then
  api POST "$JELLYFIN_URL/Startup/Configuration" -H "$JF_HEADER" \
    -d '{"UICulture":"en-US","MetadataCountryCode":"US","PreferredMetadataLanguage":"en"}' || true
  api POST "$JELLYFIN_URL/Startup/User" -H "$JF_HEADER" \
    -d "{\"Name\":\"$JELLYFIN_USER\",\"Password\":\"$JELLYFIN_PASS\"}" || true
  api POST "$JELLYFIN_URL/Startup/Complete" -H "$JF_HEADER" || true
  ok "Admin user '$JELLYFIN_USER' created"
else
  ok "Already configured"
fi

JF_AUTH_RESP=$(api POST "$JELLYFIN_URL/Users/AuthenticateByName" -H "$JF_HEADER" \
  -d "{\"Username\":\"$JELLYFIN_USER\",\"Pw\":\"$JELLYFIN_PASS\"}" || echo "")
JELLYFIN_TOKEN=$(echo "$JF_AUTH_RESP" | jq -r '.AccessToken // empty' 2>/dev/null || echo "")

JELLYFIN_API_KEY=""
if [ -n "$JELLYFIN_TOKEN" ]; then
  ok "Authenticated"

  EXISTING_LIBS=$(api GET "$JELLYFIN_URL/Library/VirtualFolders" \
    -H "X-Emby-Token: $JELLYFIN_TOKEN" | jq -r '.[].Name' 2>/dev/null || echo "")

  for lib_pair in "Movies:/media/movies:movies" "TV Shows:/media/tv:tvshows" "Anime:/media/anime:tvshows"; do
    lib_name="${lib_pair%%:*}"; rest="${lib_pair#*:}"; lib_path="${rest%%:*}"; lib_type="${rest##*:}"
    if ! echo "$EXISTING_LIBS" | grep -q "^${lib_name}$"; then
      encoded=$(printf '%s' "$lib_name" | jq -sRr @uri)
      api POST "$JELLYFIN_URL/Library/VirtualFolders?name=${encoded}&collectionType=$lib_type&refreshLibrary=false" \
        -H "X-Emby-Token: $JELLYFIN_TOKEN" \
        -d '{"LibraryOptions":{}}' && \
        ok "Created library: $lib_name" || warn "Could not create: $lib_name"
    fi

    # Ensure the path is attached (creating the library doesn't always set it)
    HAS_PATH=$(api GET "$JELLYFIN_URL/Library/VirtualFolders" -H "X-Emby-Token: $JELLYFIN_TOKEN" | \
      jq -r --arg name "$lib_name" --arg path "$lib_path" '.[] | select(.Name == $name) | .Locations[] | select(. == $path)' 2>/dev/null || echo "")
    if [ -z "$HAS_PATH" ]; then
      api POST "$JELLYFIN_URL/Library/VirtualFolders/Paths?refreshLibrary=true" \
        -H "X-Emby-Token: $JELLYFIN_TOKEN" \
        -d "{\"Name\":\"$lib_name\",\"PathInfo\":{\"Path\":\"$lib_path\"}}" && \
        ok "Library '$lib_name' → $lib_path" || warn "Could not add path to $lib_name"
    else
      ok "Library '$lib_name' → $lib_path"
    fi
  done

  EXISTING_KEYS=$(api GET "$JELLYFIN_URL/Auth/Keys" -H "X-Emby-Token: $JELLYFIN_TOKEN" 2>/dev/null | jq '.Items | length' 2>/dev/null || echo "0")
  [ "$EXISTING_KEYS" = "0" ] || [ -z "$EXISTING_KEYS" ] && \
    api POST "$JELLYFIN_URL/Auth/Keys?app=MediaServer" -H "X-Emby-Token: $JELLYFIN_TOKEN" >/dev/null 2>&1 || true
  JELLYFIN_API_KEY=$(api GET "$JELLYFIN_URL/Auth/Keys" -H "X-Emby-Token: $JELLYFIN_TOKEN" 2>/dev/null | jq -r '.Items[-1].AccessToken // empty' 2>/dev/null || echo "")
  [ -n "$JELLYFIN_API_KEY" ] && ok "API key: $JELLYFIN_API_KEY"

  # Enable real-time monitoring and daily scans on all libraries
  LIBS_JSON=$(api GET "$JELLYFIN_URL/Library/VirtualFolders" -H "X-Emby-Token: $JELLYFIN_TOKEN" 2>/dev/null || echo "[]")
  echo "$LIBS_JSON" | jq -c '.[]' 2>/dev/null | while IFS= read -r LIB; do
    LIB_NAME=$(echo "$LIB" | jq -r '.Name')
    LIB_ID=$(echo "$LIB" | jq -r '.ItemId')
    LIB_OPTIONS=$(echo "$LIB" | jq -c '.LibraryOptions' 2>/dev/null)
    if [ -n "$LIB_OPTIONS" ] && [ "$LIB_OPTIONS" != "null" ]; then
      UPDATED_OPTIONS=$(echo "$LIB_OPTIONS" | jq -c '.EnableRealtimeMonitor = true | .AutomaticRefreshIntervalDays = 1')
      api POST "$JELLYFIN_URL/Library/VirtualFolders/LibraryOptions" \
        -H "X-Emby-Token: $JELLYFIN_TOKEN" \
        -d "{\"Id\":\"$LIB_ID\",\"LibraryOptions\":$UPDATED_OPTIONS}" >/dev/null 2>&1 && \
        ok "Library '$LIB_NAME': real-time monitoring + daily scan" || warn "Could not update '$LIB_NAME' options"
    fi
  done

  # Reduce library monitor delay to 15 seconds for faster content detection
  SYS_CONFIG=$(api GET "$JELLYFIN_URL/System/Configuration" -H "X-Emby-Token: $JELLYFIN_TOKEN" 2>/dev/null || echo "")
  if [ -n "$SYS_CONFIG" ] && [ "$SYS_CONFIG" != "null" ]; then
    UPDATED_SYS=$(echo "$SYS_CONFIG" | jq -c '.LibraryMonitorDelay = 15')
    api POST "$JELLYFIN_URL/System/Configuration" \
      -H "X-Emby-Token: $JELLYFIN_TOKEN" \
      -d "$UPDATED_SYS" >/dev/null 2>&1 && \
      ok "Library monitor delay: 15s" || warn "Could not set monitor delay"
  fi
else
  warn "Could not authenticate"
fi

# ═══════════════════════════════════════════════════════════════════
# 10. SABNZBD
# ═══════════════════════════════════════════════════════════════════
if [ -n "$SABNZBD_KEY" ]; then
  info "Configuring SABnzbd..."

  # Set download directories
  curl -sf "$SABNZBD_URL/api?mode=set_config&section=misc&keyword=complete_dir&value=/downloads/usenet/complete&apikey=$SABNZBD_KEY&output=json" >/dev/null 2>&1
  curl -sf "$SABNZBD_URL/api?mode=set_config&section=misc&keyword=download_dir&value=/downloads/usenet/incomplete&apikey=$SABNZBD_KEY&output=json" >/dev/null 2>&1
  ok "Directories: /downloads/usenet/{complete,incomplete}"

  # Create categories
  EXISTING_CATS=$(curl -sf "$SABNZBD_URL/api?mode=get_cats&apikey=$SABNZBD_KEY&output=json" 2>/dev/null | jq -r '.categories[]' 2>/dev/null || echo "")
  for cat in sonarr sonarr-anime radarr lidarr; do
    if ! echo "$EXISTING_CATS" | grep -q "^${cat}$"; then
      curl -sf "$SABNZBD_URL/api?mode=set_config&section=categories&keyword=$cat&apikey=$SABNZBD_KEY&dir=$cat&output=json" >/dev/null 2>&1 && \
        ok "Category: $cat" || warn "Could not create category: $cat"
    else
      ok "Category: $cat"
    fi
  done
fi

# ═══════════════════════════════════════════════════════════════════
# 11. SONARR / RADARR — root folders + download clients
# ═══════════════════════════════════════════════════════════════════
configure_arr() {
  local name="$1" url="$2" key="$3" root_folder="$4" cat_field="$5"
  info "Configuring $name..."
  local H="X-Api-Key: $key"

  # Remove stale root folders (e.g. /downloads) and ensure only the correct one exists
  EXISTING_ROOTS=$(api GET "$url/api/v3/rootfolder" -H "$H" 2>/dev/null || echo "[]")
  while read -r stale_id; do
    [ -n "$stale_id" ] && api DELETE "$url/api/v3/rootfolder/$stale_id" -H "$H" >/dev/null 2>&1 && \
      ok "Removed stale root folder (id: $stale_id)"
  done < <(echo "$EXISTING_ROOTS" | jq -r '.[] | select(.path != "'"$root_folder"'") | .id' 2>/dev/null)

  if echo "$EXISTING_ROOTS" | jq -r '.[].path' 2>/dev/null | grep -q "^${root_folder}$"; then
    ok "Root folder: $root_folder"
  else
    api POST "$url/api/v3/rootfolder" -H "$H" -d "{\"path\":\"$root_folder\"}" >/dev/null && \
      ok "Root folder: $root_folder" || warn "Could not add root folder"
  fi

  EXISTING_DL=$(api GET "$url/api/v3/downloadclient" -H "$H" | jq -r '.[].name' 2>/dev/null || echo "")

  if ! echo "$EXISTING_DL" | grep -q "qBittorrent"; then
    api POST "$url/api/v3/downloadclient" -H "$H" -d '{
      "name":"qBittorrent","implementation":"QBittorrent","configContract":"QBittorrentSettings",
      "enable":true,"protocol":"torrent","priority":1,
      "fields":[{"name":"host","value":"qbittorrent"},{"name":"port","value":8081},
        {"name":"username","value":"'"$QBIT_USER"'"},{"name":"password","value":"'"$QBIT_PASS"'"},
        {"name":"'"$cat_field"'","value":"'"$name"'"}]
    }' >/dev/null 2>&1 && ok "qBittorrent connected (category: $name)" || warn "Could not add qBittorrent"
  else ok "qBittorrent connected"; fi

  if [ -n "$SABNZBD_KEY" ] && ! echo "$EXISTING_DL" | grep -q "SABnzbd"; then
    api POST "$url/api/v3/downloadclient" -H "$H" -d '{
      "name":"SABnzbd","implementation":"Sabnzbd","configContract":"SabnzbdSettings",
      "enable":true,"protocol":"usenet","priority":2,
      "fields":[{"name":"host","value":"sabnzbd"},{"name":"port","value":8080},
        {"name":"apiKey","value":"'"$SABNZBD_KEY"'"},{"name":"'"$cat_field"'","value":"'"$name"'"}]
    }' >/dev/null 2>&1 && ok "SABnzbd connected (category: $name)" || warn "Could not add SABnzbd"
  elif [ -n "$SABNZBD_KEY" ]; then ok "SABnzbd connected"; fi

  # Add Jellyfin notification connection (triggers library scan on import/upgrade)
  if [ -n "$JELLYFIN_API_KEY" ]; then
    EXISTING_NOTIF=$(api GET "$url/api/v3/notification" -H "$H" | jq -r '.[].name' 2>/dev/null || echo "")
    if ! echo "$EXISTING_NOTIF" | grep -q "^Jellyfin$"; then
      api POST "$url/api/v3/notification" -H "$H" -d '{
        "name":"Jellyfin","implementation":"MediaBrowser","configContract":"MediaBrowserSettings",
        "enable":true,"onDownload":true,"onUpgrade":true,"onRename":true,
        "fields":[{"name":"host","value":"jellyfin"},{"name":"port","value":8096},
          {"name":"useSsl","value":false},{"name":"apiKey","value":"'"$JELLYFIN_API_KEY"'"},
          {"name":"updateLibrary","value":true}]
      }' >/dev/null 2>&1 && ok "Jellyfin notification connected" || warn "Could not add Jellyfin notification"
    else ok "Jellyfin notification connected"; fi
  fi

  # Configure web UI authentication (Sonarr v4 / Radarr v5 enable auth by default)
  HOST_CONFIG=$(api GET "$url/api/v3/config/host" -H "$H" 2>/dev/null || echo "")
  if [ -n "$HOST_CONFIG" ] && [ "$HOST_CONFIG" != "null" ]; then
    CURRENT_AUTH_USER=$(echo "$HOST_CONFIG" | jq -r '.username // empty' 2>/dev/null)
    if [ -z "$CURRENT_AUTH_USER" ]; then
      HOST_ID=$(echo "$HOST_CONFIG" | jq -r '.id' 2>/dev/null)
      UPDATED_HOST=$(echo "$HOST_CONFIG" | jq -c \
        --arg user "$JELLYFIN_USER" --arg pass "$JELLYFIN_PASS" \
        '.authenticationMethod = "forms" | .username = $user | .password = $pass | .passwordConfirmation = $pass | .authenticationRequired = "enabled"' 2>/dev/null)
      api PUT "$url/api/v3/config/host/$HOST_ID" -H "$H" -d "$UPDATED_HOST" >/dev/null 2>&1 && \
        ok "Auth set: $JELLYFIN_USER" || warn "Could not set authentication"
    else
      ok "Auth: $CURRENT_AUTH_USER"
    fi
  fi
}

[ -n "$SONARR_KEY" ]       && configure_arr "sonarr"       "$SONARR_URL"       "$SONARR_KEY"       "/media/tv"    "tvCategory"
[ -n "$SONARR_ANIME_KEY" ] && configure_arr "sonarr-anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY" "/media/anime" "tvCategory"
[ -n "$RADARR_KEY" ]       && configure_arr "radarr"       "$RADARR_URL"       "$RADARR_KEY"       "/media/movies" "movieCategory"

# Enable "Unknown" quality in default profiles (public indexers often have unparseable names)
enable_unknown_quality() {
  local url="$1" key="$2" api_ver="${3:-v3}"
  local H="X-Api-Key: $key"
  local PROFILE=$(api GET "$url/api/$api_ver/qualityprofile/1" -H "$H" 2>/dev/null || echo "")
  [ -z "$PROFILE" ] || [ "$PROFILE" = "null" ] && return
  local UNKNOWN_ALLOWED=$(echo "$PROFILE" | jq '[.items[] | select(.quality.id == 0) | .allowed][0]' 2>/dev/null)
  if [ "$UNKNOWN_ALLOWED" = "false" ]; then
    local UPDATED=$(echo "$PROFILE" | jq '.items = [.items[] | if (.quality.id == 0) then .allowed = true else . end]')
    api PUT "$url/api/$api_ver/qualityprofile/1" -H "$H" -d "$UPDATED" >/dev/null 2>&1 && \
      ok "Quality: enabled Unknown quality" || true
  fi
}
[ -n "$SONARR_KEY" ]       && enable_unknown_quality "$SONARR_URL"       "$SONARR_KEY"
[ -n "$SONARR_ANIME_KEY" ] && enable_unknown_quality "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY"

# ═══════════════════════════════════════════════════════════════════
# 12. PROWLARR — connect apps + FlareSolverr + indexers
# ═══════════════════════════════════════════════════════════════════
if [ -n "$PROWLARR_KEY" ]; then
  info "Configuring Prowlarr..."
  PH="X-Api-Key: $PROWLARR_KEY"

  EXISTING_APPS=$(api GET "$PROWLARR_URL/api/v1/applications" -H "$PH" | jq -r '.[].name' 2>/dev/null || echo "")

  add_prowlarr_app() {
    local name="$1" impl="$2" url="$3" key="$4" cats="$5" tags="${6:-}"
    if ! echo "$EXISTING_APPS" | grep -q "^${name}$"; then
      local tags_json="[]"
      [ -n "$tags" ] && tags_json="[$tags]"
      api POST "$PROWLARR_URL/api/v1/applications" -H "$PH" -d '{
        "name":"'"$name"'","implementation":"'"$impl"'","configContract":"'"$impl"'Settings",
        "syncLevel":"fullSync","tags":'"$tags_json"',
        "fields":[{"name":"prowlarrUrl","value":"'"$PROWLARR_INTERNAL"'"},
          {"name":"baseUrl","value":"'"$url"'"},{"name":"apiKey","value":"'"$key"'"},
          {"name":"syncCategories","value":['"$cats"']}]
      }' >/dev/null 2>&1 && ok "$name connected" || warn "Could not connect $name"
    else ok "$name connected"; fi
  }

  # Create anime tag for routing anime indexers to Sonarr Anime only
  ANIME_TAG_ID=$(api GET "$PROWLARR_URL/api/v1/tag" -H "$PH" | jq -r '.[] | select(.label == "anime") | .id' 2>/dev/null || echo "")
  if [ -z "$ANIME_TAG_ID" ]; then
    ANIME_TAG_ID=$(api POST "$PROWLARR_URL/api/v1/tag" -H "$PH" -d '{"label":"anime"}' | jq -r '.id' 2>/dev/null || echo "")
    [ -n "$ANIME_TAG_ID" ] && ok "Created anime tag (id: $ANIME_TAG_ID)"
  fi

  SONARR_CATS="5000,5010,5020,5030,5040,5045,5050,5090"
  RADARR_CATS="2000,2010,2020,2030,2040,2045,2050,2060,2070,2080,2090"

  # Sonarr Anime gets the anime tag — only anime-tagged indexers sync to it
  [ -n "$SONARR_KEY" ]       && add_prowlarr_app "Sonarr"       "Sonarr" "$SONARR_INTERNAL"       "$SONARR_KEY"       "$SONARR_CATS"
  [ -n "$SONARR_ANIME_KEY" ] && add_prowlarr_app "Sonarr Anime" "Sonarr" "$SONARR_ANIME_INTERNAL" "$SONARR_ANIME_KEY" "$SONARR_CATS" "$ANIME_TAG_ID"
  [ -n "$RADARR_KEY" ]       && add_prowlarr_app "Radarr"       "Radarr" "$RADARR_INTERNAL"       "$RADARR_KEY"       "$RADARR_CATS"

  # FlareSolverr
  EXISTING_PROXIES=$(api GET "$PROWLARR_URL/api/v1/indexerProxy" -H "$PH" | jq -r '.[].name' 2>/dev/null || echo "")
  if ! echo "$EXISTING_PROXIES" | grep -q "FlareSolverr"; then
    api POST "$PROWLARR_URL/api/v1/indexerProxy" -H "$PH" -d '{
      "name":"FlareSolverr","implementation":"FlareSolverr","configContract":"FlareSolverrSettings",
      "fields":[{"name":"host","value":"http://flaresolverr:8191"},{"name":"requestTimeout","value":60}]
    }' >/dev/null 2>&1 && ok "FlareSolverr connected" || warn "Could not add FlareSolverr"
  else ok "FlareSolverr connected"; fi

  # qBittorrent in Prowlarr
  EXISTING_DLC=$(api GET "$PROWLARR_URL/api/v1/downloadclient" -H "$PH" | jq -r '.[].name' 2>/dev/null || echo "")
  if ! echo "$EXISTING_DLC" | grep -q "qBittorrent"; then
    api POST "$PROWLARR_URL/api/v1/downloadclient" -H "$PH" -d '{
      "name":"qBittorrent","implementation":"QBittorrent","configContract":"QBittorrentSettings",
      "enable":true,"protocol":"torrent","priority":1,
      "fields":[{"name":"host","value":"qbittorrent"},{"name":"port","value":8081},
        {"name":"username","value":"'"$QBIT_USER"'"},{"name":"password","value":"'"$QBIT_PASS"'"},
        {"name":"category","value":"prowlarr"}]
    }' >/dev/null 2>&1 && ok "qBittorrent connected to Prowlarr" || true
  fi

  # Create FlareSolverr tag if it doesn't exist
  FLARESOLVERR_TAG_ID=$(api GET "$PROWLARR_URL/api/v1/tag" -H "$PH" | jq -r '.[] | select(.label == "flaresolverr") | .id' 2>/dev/null || echo "")
  if [ -z "$FLARESOLVERR_TAG_ID" ]; then
    FLARESOLVERR_TAG_ID=$(api POST "$PROWLARR_URL/api/v1/tag" -H "$PH" -d '{"label":"flaresolverr"}' | jq -r '.id' 2>/dev/null || echo "")
    [ -n "$FLARESOLVERR_TAG_ID" ] && ok "Created FlareSolverr tag (id: $FLARESOLVERR_TAG_ID)"
  fi

  # Assign tag to FlareSolverr proxy if not already
  if [ -n "$FLARESOLVERR_TAG_ID" ]; then
    PROXY=$(api GET "$PROWLARR_URL/api/v1/indexerProxy" -H "$PH" | jq -c '.[0]' 2>/dev/null || echo "")
    if [ -n "$PROXY" ] && [ "$PROXY" != "null" ]; then
      HAS_TAG=$(echo "$PROXY" | jq --argjson tid "$FLARESOLVERR_TAG_ID" '.tags | index($tid)' 2>/dev/null)
      if [ "$HAS_TAG" = "null" ] || [ -z "$HAS_TAG" ]; then
        PROXY_UPDATED=$(echo "$PROXY" | jq --argjson tid "$FLARESOLVERR_TAG_ID" '.tags += [$tid]' 2>/dev/null)
        PROXY_ID=$(echo "$PROXY" | jq -r '.id' 2>/dev/null)
        api PUT "$PROWLARR_URL/api/v1/indexerProxy/$PROXY_ID" -H "$PH" -d "$PROXY_UPDATED" >/dev/null 2>&1
      fi
    fi
  fi

  # Add indexers from config.json
  INDEXER_COUNT=$(cfg '.indexers | length')
  EXISTING_INDEXERS=$(api GET "$PROWLARR_URL/api/v1/indexer" -H "$PH" | jq -r '.[].name' 2>/dev/null || echo "")
  SCHEMAS=""

  if [ "$INDEXER_COUNT" -gt 0 ] 2>/dev/null; then
    info "Adding indexers from config..."
    for i in $(seq 0 $((INDEXER_COUNT - 1))); do
      IDX_ENABLED=$(cfg ".indexers[$i].enable")
      [ "$IDX_ENABLED" != "true" ] && continue

      IDX_NAME=$(cfg ".indexers[$i].name")
      IDX_DEF=$(cfg ".indexers[$i].definitionName")
      IDX_FLARE=$(cfg ".indexers[$i].flaresolverr // false")
      IDX_ANIME=$(cfg ".indexers[$i].anime // false")

      if echo "$EXISTING_INDEXERS" | grep -q "^${IDX_NAME}$"; then
        ok "$IDX_NAME already added"
        continue
      fi

      # Fetch schemas once (cached)
      if [ -z "$SCHEMAS" ]; then
        SCHEMAS=$(api GET "$PROWLARR_URL/api/v1/indexer/schema" -H "$PH" 2>/dev/null || echo "[]")
      fi

      # Find the matching schema
      SCHEMA=$(echo "$SCHEMAS" | jq -c --arg def "$IDX_DEF" '[.[] | select(.definitionName == $def)] | .[0]' 2>/dev/null)

      if [ -z "$SCHEMA" ] || [ "$SCHEMA" = "null" ]; then
        warn "$IDX_NAME: indexer '$IDX_DEF' not found in Prowlarr schemas"
        continue
      fi

      # Merge user-provided fields into the schema
      USER_FIELDS=$(cfg ".indexers[$i].fields")
      if [ "$USER_FIELDS" != "null" ] && [ "$USER_FIELDS" != "{}" ]; then
        SCHEMA=$(echo "$SCHEMA" | jq -c --argjson uf "$USER_FIELDS" '
          .fields = [.fields[] | if $uf[.name] then .value = $uf[.name] else . end]
        ' 2>/dev/null)
      fi

      # Set name, enable, app profile, and tags (flaresolverr + anime)
      IDX_TAGS="[]"
      [ "$IDX_FLARE" = "true" ] && [ -n "$FLARESOLVERR_TAG_ID" ] && IDX_TAGS=$(echo "$IDX_TAGS" | jq -c ". + [$FLARESOLVERR_TAG_ID]")
      [ "$IDX_ANIME" = "true" ] && [ -n "$ANIME_TAG_ID" ] && IDX_TAGS=$(echo "$IDX_TAGS" | jq -c ". + [$ANIME_TAG_ID]")
      SCHEMA=$(echo "$SCHEMA" | jq -c --arg name "$IDX_NAME" --argjson tags "$IDX_TAGS" \
        '.name = $name | .enable = true | del(.id) | .appProfileId = 1 | .tags = $tags' 2>/dev/null)

      # Write to temp file to avoid shell argument length limits
      echo "$SCHEMA" > "$TMPDIR_SETUP/prowlarr_indexer.json"
      api POST "$PROWLARR_URL/api/v1/indexer" -H "$PH" -d @"$TMPDIR_SETUP/prowlarr_indexer.json" >/dev/null 2>&1 && \
        ok "$IDX_NAME added" || warn "Could not add $IDX_NAME"
    done
    rm -f "$TMPDIR_SETUP/prowlarr_indexer.json"
  fi

  # Configure web UI authentication
  PROWLARR_HOST_CONFIG=$(api GET "$PROWLARR_URL/api/v1/config/host" -H "$PH" 2>/dev/null || echo "")
  if [ -n "$PROWLARR_HOST_CONFIG" ] && [ "$PROWLARR_HOST_CONFIG" != "null" ]; then
    PROWLARR_AUTH_USER=$(echo "$PROWLARR_HOST_CONFIG" | jq -r '.username // empty' 2>/dev/null)
    if [ -z "$PROWLARR_AUTH_USER" ]; then
      PROWLARR_HOST_ID=$(echo "$PROWLARR_HOST_CONFIG" | jq -r '.id' 2>/dev/null)
      PROWLARR_HOST_UPDATED=$(echo "$PROWLARR_HOST_CONFIG" | jq -c \
        --arg user "$JELLYFIN_USER" --arg pass "$JELLYFIN_PASS" \
        '.authenticationMethod = "forms" | .username = $user | .password = $pass | .passwordConfirmation = $pass | .authenticationRequired = "enabled"' 2>/dev/null)
      api PUT "$PROWLARR_URL/api/v1/config/host/$PROWLARR_HOST_ID" -H "$PH" -d "$PROWLARR_HOST_UPDATED" >/dev/null 2>&1 && \
        ok "Auth set: $JELLYFIN_USER" || warn "Could not set authentication"
    else
      ok "Auth: $PROWLARR_AUTH_USER"
    fi
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# 13. SABNZBD — usenet providers
# ═══════════════════════════════════════════════════════════════════
PROVIDER_COUNT=$(cfg '.usenet_providers | length')
if [ "$PROVIDER_COUNT" -gt 0 ] 2>/dev/null && [ -n "$SABNZBD_KEY" ]; then
  info "Configuring SABnzbd usenet providers..."

  for i in $(seq 0 $((PROVIDER_COUNT - 1))); do
    PROV_ENABLED=$(cfg ".usenet_providers[$i].enable")
    [ "$PROV_ENABLED" != "true" ] && continue

    PROV_NAME=$(cfg ".usenet_providers[$i].name")
    PROV_HOST=$(cfg ".usenet_providers[$i].host")
    PROV_PORT=$(cfg ".usenet_providers[$i].port")
    PROV_SSL=$(cfg ".usenet_providers[$i].ssl")
    PROV_USER=$(cfg ".usenet_providers[$i].username")
    PROV_PASS=$(cfg ".usenet_providers[$i].password")
    PROV_CONN=$(cfg ".usenet_providers[$i].connections")

    [ "$PROV_SSL" = "true" ] && SSL_VAL=1 || SSL_VAL=0

    # SABnzbd server config via API
    curl -sf -o /dev/null "$SABNZBD_URL/api" \
      -d "mode=config" \
      -d "name=set_server" \
      -d "apikey=$SABNZBD_KEY" \
      -d "output=json" \
      -d "keyword=$PROV_NAME" \
      -d "host=$PROV_HOST" \
      -d "port=$PROV_PORT" \
      -d "ssl=$SSL_VAL" \
      -d "username=$PROV_USER" \
      -d "password=$PROV_PASS" \
      -d "connections=$PROV_CONN" \
      -d "enable=1" 2>/dev/null && \
      ok "$PROV_NAME ($PROV_HOST:$PROV_PORT)" || warn "Could not add $PROV_NAME"
  done
fi

# ═══════════════════════════════════════════════════════════════════
# 14. BAZARR — connect to Sonarr + Radarr
# ═══════════════════════════════════════════════════════════════════
info "Configuring Bazarr..."

BAZARR_CONFIG=""
for f in "$CONFIG_DIR/bazarr/config/config/config.yaml" "$CONFIG_DIR/bazarr/config/config.yaml"; do
  [ -f "$f" ] && BAZARR_CONFIG="$f" && break
done

if [ -n "$BAZARR_CONFIG" ]; then
  ok "Config: $BAZARR_CONFIG"

  # Use python3 to do targeted updates (preserves all existing config)
  if python3 - "$BAZARR_CONFIG" "$SONARR_KEY" "$RADARR_KEY" "$SUBTITLE_PROVIDERS" "$SUBTITLE_LANGS" << 'PYEOF'
import sys
import json

config_path = sys.argv[1]
sonarr_key = sys.argv[2]
radarr_key = sys.argv[3]
subtitle_providers = sys.argv[4] if len(sys.argv) > 4 else ''
subtitle_langs = sys.argv[5] if len(sys.argv) > 5 else ''

with open(config_path, 'r') as f:
    lines = f.readlines()

# Parse into sections: { section_name: { key: line_index } }
sections = {}
current_section = None
for i, line in enumerate(lines):
    stripped = line.rstrip('\n')
    if stripped and not stripped[0].isspace() and stripped.endswith(':') and stripped != '---':
        current_section = stripped[:-1]
        sections[current_section] = {}
    elif current_section and stripped.startswith('  ') and ':' in stripped:
        key = stripped.split(':')[0].strip()
        sections[current_section][key] = i

def set_value(section, key, value):
    """Update an existing key or append to section."""
    if isinstance(value, bool):
        val_str = 'true' if value else 'false'
    elif isinstance(value, str):
        val_str = f"'{value}'" if value else "''"
    else:
        val_str = str(value)

    if section in sections and key in sections[section]:
        idx = sections[section][key]
        lines[idx] = f'  {key}: {val_str}\n'
    elif section in sections:
        # Find end of section to append
        sec_keys = sections[section]
        if sec_keys:
            last_idx = max(sec_keys.values())
        else:
            # Find section header line
            for j, l in enumerate(lines):
                if l.rstrip('\n') == f'{section}:':
                    last_idx = j
                    break
        lines.insert(last_idx + 1, f'  {key}: {val_str}\n')
        # Rebuild index for this section
        sections[section][key] = last_idx + 1

if sonarr_key:
    set_value('sonarr', 'ip', 'sonarr')
    set_value('sonarr', 'port', 8989)
    set_value('sonarr', 'base_url', '/')
    set_value('sonarr', 'apikey', sonarr_key)
    set_value('sonarr', 'ssl', False)
    set_value('general', 'use_sonarr', True)

if radarr_key:
    set_value('radarr', 'ip', 'radarr')
    set_value('radarr', 'port', 7878)
    set_value('radarr', 'base_url', '/')
    set_value('radarr', 'apikey', radarr_key)
    set_value('radarr', 'ssl', False)
    set_value('general', 'use_radarr', True)

# Configure subtitle providers
if subtitle_providers:
    providers_list = json.dumps(subtitle_providers.split(','))
    set_value('general', 'enabled_providers', providers_list)

# Enable default language profiles for series and movies
if subtitle_langs:
    set_value('general', 'serie_default_enabled', True)
    set_value('general', 'movie_default_enabled', True)

with open(config_path, 'w') as f:
    f.writelines(lines)

print("OK")
PYEOF
  then
    ok "Sonarr + Radarr configured"
    docker restart bazarr >/dev/null 2>&1 && ok "Bazarr restarted" || true
    wait_for "Bazarr" "$BAZARR_URL"
  else
    warn "Could not update Bazarr config"
  fi
else
  warn "Bazarr config file not found"
fi

# Configure Bazarr language profiles via API (form-data POST to settings endpoint)
# Run BEFORE auth setup since the settings API may rewrite config.yaml
if [ -n "$BAZARR_CONFIG" ] && [ -n "$SUBTITLE_LANGS" ]; then
  BAZARR_API_KEY_VAL=$(sed -n '/^auth:/,/^[^ ]/{s/^  apikey: *//p;}' "$BAZARR_CONFIG" 2>/dev/null | head -1)
  if [ -n "$BAZARR_API_KEY_VAL" ]; then
    wait_for "Bazarr" "$BAZARR_URL"
    EXISTING_PROFILES=$(curl -sf "$BAZARR_URL/api/system/languages/profiles?apikey=$BAZARR_API_KEY_VAL" 2>/dev/null || echo "[]")
    PROFILE_COUNT=$(echo "$EXISTING_PROFILES" | jq 'length' 2>/dev/null || echo "0")

    if [ "$PROFILE_COUNT" = "0" ] || [ -z "$PROFILE_COUNT" ]; then
      # Build language items for the profile
      LANG_ITEMS="[]"
      IDX=0
      LANG_ENABLED_ARGS=()
      IFS=',' read -ra LANGS <<< "$SUBTITLE_LANGS"
      for lang in "${LANGS[@]}"; do
        LANG_ITEMS=$(echo "$LANG_ITEMS" | jq --arg code "$lang" --argjson idx "$IDX" \
          '. + [{"id": $idx, "language": $code, "hi": false, "forced": false}]')
        LANG_ENABLED_ARGS+=(-d "languages-enabled=$lang")
        IDX=$((IDX + 1))
      done

      PROFILE_JSON=$(jq -n --argjson items "$LANG_ITEMS" \
        '[{"profileId":1,"name":"Default","cutoff":null,"items":$items,"mustContain":"","mustNotContain":"","originalFormat":null}]')

      curl -sf -X POST "$BAZARR_URL/api/system/settings?apikey=$BAZARR_API_KEY_VAL" \
        "${LANG_ENABLED_ARGS[@]}" \
        --data-urlencode "languages-profiles=$PROFILE_JSON" \
        -d "settings-general-serie_default_profile=1" \
        -d "settings-general-movie_default_profile=1" >/dev/null 2>&1 && \
        ok "Language profile: Default ($(echo "$SUBTITLE_LANGS" | tr ',' ' '))" || warn "Could not create language profile"
    else
      ok "Language profiles already configured ($PROFILE_COUNT)"
    fi
  fi
fi

# Bazarr auth — set via config file and restart (must run after settings API to avoid being overwritten)
if [ -n "$BAZARR_CONFIG" ]; then
  BAZARR_AUTH_TYPE=$(sed -n '/^auth:/,/^[^ ]/{s/^  type: *//p;}' "$BAZARR_CONFIG" 2>/dev/null | head -1)
  if [ -z "$BAZARR_AUTH_TYPE" ] || [ "$BAZARR_AUTH_TYPE" = "null" ] || [ "$BAZARR_AUTH_TYPE" = "''" ]; then
    if python3 - "$BAZARR_CONFIG" "$JELLYFIN_USER" "$JELLYFIN_PASS" << 'PYEOF'
import sys
config_path, user, password = sys.argv[1], sys.argv[2], sys.argv[3]
with open(config_path, 'r') as f:
    lines = f.readlines()
# Find or create auth section
auth_idx = None
for i, line in enumerate(lines):
    if line.strip() == 'auth:':
        auth_idx = i
        break
if auth_idx is None:
    lines.append('\nauth:\n')
    auth_idx = len(lines) - 1
# Remove existing auth keys and rewrite
new_lines = []
in_auth = False
for i, line in enumerate(lines):
    if line.strip() == 'auth:':
        in_auth = True
        new_lines.append(line)
        new_lines.append("  type: 'forms'\n")
        new_lines.append(f"  username: '{user}'\n")
        new_lines.append(f"  password: '{password}'\n")
        continue
    if in_auth:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and not line[0].isspace():
            in_auth = False
            new_lines.append(line)
        elif stripped.split(':')[0].strip() in ('type', 'username', 'password'):
            continue
        else:
            new_lines.append(line)
    else:
        new_lines.append(line)
with open(config_path, 'w') as f:
    f.writelines(new_lines)
print("OK")
PYEOF
    then
      ok "Bazarr auth set: $JELLYFIN_USER"
      docker restart bazarr >/dev/null 2>&1 || true
      wait_for "Bazarr" "$BAZARR_URL"
    else
      warn "Could not set Bazarr auth"
    fi
  else
    ok "Bazarr auth already configured"
  fi
fi

# SABnzbd auth — set username/password via API
if [ -n "$SABNZBD_KEY" ]; then
  SAB_AUTH_USER=$(curl -sf "$SABNZBD_URL/api?mode=get_config&section=misc&apikey=$SABNZBD_KEY&output=json" 2>/dev/null | jq -r '.config.misc.username // empty' 2>/dev/null)
  if [ -z "$SAB_AUTH_USER" ]; then
    curl -sf "$SABNZBD_URL/api?mode=set_config&section=misc&keyword=username&value=$JELLYFIN_USER&apikey=$SABNZBD_KEY&output=json" >/dev/null 2>&1
    curl -sf "$SABNZBD_URL/api?mode=set_config&section=misc&keyword=password&value=$JELLYFIN_PASS&apikey=$SABNZBD_KEY&output=json" >/dev/null 2>&1
    ok "SABnzbd auth set: $JELLYFIN_USER"
  else
    ok "SABnzbd auth: $SAB_AUTH_USER"
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# 15. JELLYSEERR — connect to Jellyfin + Sonarr + Radarr
# ═══════════════════════════════════════════════════════════════════
info "Configuring Jellyseerr..."

# Check initialization state
JS_PUBLIC=$(curl -sf "$JELLYSEERR_URL/api/v1/settings/public" 2>/dev/null || echo "")
JS_INITIALIZED=$(echo "$JS_PUBLIC" | jq -r '.initialized // false' 2>/dev/null)

# If not initialized, pre-configure Jellyfin in settings.json and restart
if [ "$JS_INITIALIZED" != "true" ]; then
  JS_SETTINGS="$CONFIG_DIR/jellyseerr/settings.json"
  if [ -f "$JS_SETTINGS" ] && [ -n "$JELLYFIN_API_KEY" ]; then
    # Get Jellyfin server ID
    JF_SERVER_ID=$(api GET "$JELLYFIN_URL/System/Info/Public" | jq -r '.Id // empty' 2>/dev/null || echo "")

    # Update Jellyfin connection in settings.json (set ip so auth endpoint works)
    UPDATED=$(jq --arg ip "jellyfin" --arg key "$JELLYFIN_API_KEY" --arg sid "$JF_SERVER_ID" '
      .jellyfin.ip = $ip |
      .jellyfin.port = 8096 |
      .jellyfin.useSsl = false |
      .jellyfin.apiKey = $key |
      .jellyfin.serverId = $sid |
      .jellyfin.name = "Jellyfin" |
      .main.mediaServerType = 2 |
      .public.initialized = true
    ' "$JS_SETTINGS" 2>/dev/null)

    if [ -n "$UPDATED" ]; then
      echo "$UPDATED" > "$TMPDIR_SETUP/js_settings_tmp.json" && mv "$TMPDIR_SETUP/js_settings_tmp.json" "$JS_SETTINGS"
      sync
      docker restart jellyseerr >/dev/null 2>&1
      ok "Jellyfin server pre-configured"
      sleep 8
      wait_for "Jellyseerr" "$JELLYSEERR_URL"
    fi
  fi
fi

# Authenticate — serverType:2 = Jellyfin (required for initial admin creation)
JS_COOKIE=""
for AUTH_BODY in \
  "{\"username\":\"$JELLYFIN_USER\",\"password\":\"$JELLYFIN_PASS\",\"email\":\"admin@media.local\",\"serverType\":2}" \
  "{\"username\":\"$JELLYFIN_USER\",\"password\":\"$JELLYFIN_PASS\",\"email\":\"admin@media.local\"}"; do
  JS_AUTH_RESP=$(curl -s -c - -X POST "$JELLYSEERR_URL/api/v1/auth/jellyfin" \
    -H "Content-Type: application/json" \
    -d "$AUTH_BODY" 2>/dev/null || echo "")
  JS_COOKIE=$(echo "$JS_AUTH_RESP" | extract_cookie connect.sid || echo "")
  if [ -n "$JS_COOKIE" ]; then
    ok "Authenticated"
    break
  fi
done
[ -z "$JS_COOKIE" ] && warn "Could not authenticate (check Jellyfin credentials)"

if [ -n "$JS_COOKIE" ]; then
  JA=(-b "connect.sid=$JS_COOKIE")

  # Sync & enable Jellyfin libraries (GET ?sync=true fetches, GET ?enable=ids saves)
  LIBRARIES=$(api GET "$JELLYSEERR_URL/api/v1/settings/jellyfin/library?sync=true" "${JA[@]}" 2>/dev/null || echo "[]")
  if echo "$LIBRARIES" | jq -e '.[0]' >/dev/null 2>&1; then
    LIB_IDS=$(echo "$LIBRARIES" | jq -r '.[].id' | paste -sd ',' -)
    if [ -n "$LIB_IDS" ]; then
      api GET "$JELLYSEERR_URL/api/v1/settings/jellyfin/library?enable=$LIB_IDS" "${JA[@]}" >/dev/null 2>&1 && \
        ok "Libraries synced" || true
    fi
  fi

  # Add Sonarr
  EXISTING_JS_SONARR=$(api GET "$JELLYSEERR_URL/api/v1/settings/sonarr" "${JA[@]}" 2>/dev/null | jq 'length' 2>/dev/null || echo "0")
  if [ "$EXISTING_JS_SONARR" = "0" ] || [ -z "$EXISTING_JS_SONARR" ]; then
    [ -n "$SONARR_KEY" ] && {
      PROFILE=$(api GET "$SONARR_URL/api/v3/qualityprofile" -H "X-Api-Key: $SONARR_KEY" | jq '.[0]' 2>/dev/null)
      PID=$(echo "$PROFILE" | jq '.id // 1' 2>/dev/null || echo 1)
      PNAME=$(echo "$PROFILE" | jq -r '.name // "Any"' 2>/dev/null || echo "Any")
      api POST "$JELLYSEERR_URL/api/v1/settings/sonarr" "${JA[@]}" -d '{
        "name":"Sonarr","hostname":"sonarr","port":8989,"useSsl":false,"apiKey":"'"$SONARR_KEY"'",
        "baseUrl":"","activeProfileId":'"$PID"',"activeProfileName":"'"$PNAME"'","activeDirectory":"/media/tv",
        "is4k":false,"enableSeasonFolders":true,"isDefault":true,"externalUrl":"http://localhost:8989",
        "enableSearch":true
      }' >/dev/null 2>&1 && ok "Sonarr connected" || warn "Could not add Sonarr"
    }
    [ -n "$SONARR_ANIME_KEY" ] && {
      PROFILE=$(api GET "$SONARR_ANIME_URL/api/v3/qualityprofile" -H "X-Api-Key: $SONARR_ANIME_KEY" | jq '.[0]' 2>/dev/null)
      PID=$(echo "$PROFILE" | jq '.id // 1' 2>/dev/null || echo 1)
      PNAME=$(echo "$PROFILE" | jq -r '.name // "Any"' 2>/dev/null || echo "Any")
      api POST "$JELLYSEERR_URL/api/v1/settings/sonarr" "${JA[@]}" -d '{
        "name":"Sonarr Anime","hostname":"sonarr-anime","port":8989,"useSsl":false,"apiKey":"'"$SONARR_ANIME_KEY"'",
        "baseUrl":"","activeProfileId":'"$PID"',"activeProfileName":"'"$PNAME"'","activeDirectory":"/media/anime",
        "is4k":false,"enableSeasonFolders":true,"isDefault":false,"externalUrl":"http://localhost:8990",
        "seriesType":"anime","animeSeriesType":"anime",
        "enableSearch":true
      }' >/dev/null 2>&1 && ok "Sonarr Anime connected" || warn "Could not add Sonarr Anime"
    }
  else
    # Ensure enableSearch is set on existing connections
    while IFS= read -r JS_SONARR; do
      JS_SID=$(echo "$JS_SONARR" | jq -r '.id')
      JS_SEARCH=$(echo "$JS_SONARR" | jq -r '.enableSearch // false')
      if [ "$JS_SEARCH" != "true" ]; then
        UPDATED_JS=$(echo "$JS_SONARR" | jq '.enableSearch = true')
        api PUT "$JELLYSEERR_URL/api/v1/settings/sonarr/$JS_SID" "${JA[@]}" -d "$UPDATED_JS" >/dev/null 2>&1 && \
          ok "Sonarr $JS_SID: enableSearch set" || true
      fi
    done < <(api GET "$JELLYSEERR_URL/api/v1/settings/sonarr" "${JA[@]}" 2>/dev/null | jq -c '.[]' 2>/dev/null)
    ok "Sonarr already connected"
  fi

  # Add Radarr
  EXISTING_JS_RADARR=$(api GET "$JELLYSEERR_URL/api/v1/settings/radarr" "${JA[@]}" 2>/dev/null | jq 'length' 2>/dev/null || echo "0")
  if [ "$EXISTING_JS_RADARR" = "0" ] || [ -z "$EXISTING_JS_RADARR" ]; then
    [ -n "$RADARR_KEY" ] && {
      PROFILE=$(api GET "$RADARR_URL/api/v3/qualityprofile" -H "X-Api-Key: $RADARR_KEY" | jq '.[0]' 2>/dev/null)
      PID=$(echo "$PROFILE" | jq '.id // 1' 2>/dev/null || echo 1)
      PNAME=$(echo "$PROFILE" | jq -r '.name // "Any"' 2>/dev/null || echo "Any")
      api POST "$JELLYSEERR_URL/api/v1/settings/radarr" "${JA[@]}" -d '{
        "name":"Radarr","hostname":"radarr","port":7878,"useSsl":false,"apiKey":"'"$RADARR_KEY"'",
        "baseUrl":"","activeProfileId":'"$PID"',"activeProfileName":"'"$PNAME"'","activeDirectory":"/media/movies",
        "is4k":false,"isDefault":true,"externalUrl":"http://localhost:7878","minimumAvailability":"released",
        "enableSearch":true
      }' >/dev/null 2>&1 && ok "Radarr connected" || warn "Could not add Radarr"
    }
  else
    # Ensure enableSearch is set on existing connections
    while IFS= read -r JS_RADARR; do
      JS_RID=$(echo "$JS_RADARR" | jq -r '.id')
      JS_RSEARCH=$(echo "$JS_RADARR" | jq -r '.enableSearch // false')
      if [ "$JS_RSEARCH" != "true" ]; then
        UPDATED_JR=$(echo "$JS_RADARR" | jq '.enableSearch = true')
        api PUT "$JELLYSEERR_URL/api/v1/settings/radarr/$JS_RID" "${JA[@]}" -d "$UPDATED_JR" >/dev/null 2>&1 && \
          ok "Radarr $JS_RID: enableSearch set" || true
      fi
    done < <(api GET "$JELLYSEERR_URL/api/v1/settings/radarr" "${JA[@]}" 2>/dev/null | jq -c '.[]' 2>/dev/null)
    ok "Radarr already connected"
  fi

  api POST "$JELLYSEERR_URL/api/v1/settings/initialize" "${JA[@]}" >/dev/null 2>&1 || true
  ok "Setup finalized"
else
  warn "Could not authenticate — complete wizard manually at $JELLYSEERR_URL"
fi

# ═══════════════════════════════════════════════════════════════════
# 16. RECYCLARR
# ═══════════════════════════════════════════════════════════════════
info "Writing Recyclarr config..."
RECYCLARR_CONFIG="$CONFIG_DIR/recyclarr/recyclarr.yml"
if [ -n "$SONARR_KEY" ] && [ -n "$RADARR_KEY" ]; then
  ANIME_KEY="${SONARR_ANIME_KEY:-$SONARR_KEY}"
  cat > "$RECYCLARR_CONFIG" << YAML
sonarr:
  main:
    base_url: $SONARR_INTERNAL
    api_key: $SONARR_KEY
    replace_existing_custom_formats: true
    quality_definition:
      type: series
    quality_profiles:
      - name: $SONARR_PROFILE
        reset_unmatched_scores:
          enabled: true
    custom_formats:
      - trash_ids:
          - 32b367365729d530ca1c124a0b180c64
          - 82d40da2bc6923f41e14394075dd4b03
          - e1a997ddb54e3ecbfe06341ad323c458
          - 06d66ab109d4d2eddb2794d21526d140
        assign_scores_to:
          - name: $SONARR_PROFILE
  anime:
    base_url: $SONARR_ANIME_INTERNAL
    api_key: $ANIME_KEY
    quality_definition:
      type: anime
    quality_profiles:
      - name: $SONARR_ANIME_PROFILE
        reset_unmatched_scores:
          enabled: true
radarr:
  main:
    base_url: $RADARR_INTERNAL
    api_key: $RADARR_KEY
    replace_existing_custom_formats: true
    quality_definition:
      type: movie
    quality_profiles:
      - name: $RADARR_PROFILE
        reset_unmatched_scores:
          enabled: true
    custom_formats:
      - trash_ids:
          - ed38b889b31be83fda192888e2286d83
          - 90cedc1fea7ea5d11298bebd3d1d3223
          - b8cd450cbfa689c0259a01d9e29ba3d6
        assign_scores_to:
          - name: $RADARR_PROFILE
YAML
  ok "Config written"

  # Trigger initial sync
  docker exec recyclarr recyclarr sync >/dev/null 2>&1 && \
    ok "Recyclarr sync complete" || warn "Recyclarr sync failed (will retry on next container restart)"
else
  warn "Missing API keys, skipping"
fi

# ═══════════════════════════════════════════════════════════════════
# 16.5 UNPACKERR
# ═══════════════════════════════════════════════════════════════════
info "Writing Unpackerr config..."

UNPACKERR_CONF="$CONFIG_DIR/unpackerr/unpackerr.conf"
mkdir -p "$(dirname "$UNPACKERR_CONF")"

cat > "$UNPACKERR_CONF" << UNPACKEOF
## Unpackerr — auto-generated by setup.sh

[[sonarr]]
url = "http://sonarr:8989"
api_key = "$SONARR_KEY"
paths = ["/downloads"]

[[sonarr]]
url = "http://sonarr-anime:8989"
api_key = "$SONARR_ANIME_KEY"
paths = ["/downloads"]

[[radarr]]
url = "http://radarr:7878"
api_key = "$RADARR_KEY"
paths = ["/downloads"]

[[lidarr]]
url = "http://lidarr:8686"
api_key = "$LIDARR_KEY"
paths = ["/downloads"]

UNPACKEOF

ok "Config written"
docker restart unpackerr >/dev/null 2>&1 && ok "Unpackerr restarted with new config" || true

# ═══════════════════════════════════════════════════════════════════
# 16.6 LIDARR — configure download clients and root folders
# ═══════════════════════════════════════════════════════════════════
info "Configuring Lidarr..."
[ -n "$LIDARR_KEY" ] && configure_arr "lidarr" "$LIDARR_URL" "$LIDARR_KEY" "/media/music" "musicCategory"

# Add Lidarr to Prowlarr
if [ -n "$PROWLARR_KEY" ]; then
  PH="X-Api-Key: $PROWLARR_KEY"
  MUSIC_CATS='[3000,3010,3020,3030,3040,3050,3060]'
  [ -n "$LIDARR_KEY" ]  && add_prowlarr_app "Lidarr"  "Lidarr"  "$LIDARR_INTERNAL"  "$LIDARR_KEY"  "$MUSIC_CATS"
fi

# ═══════════════════════════════════════════════════════════════════
# 16.7 NAVIDROME — create admin user
# ═══════════════════════════════════════════════════════════════════
info "Configuring Navidrome..."

ND_CHECK=$(curl -s -o /dev/null -w "%{http_code}" "$NAVIDROME_URL/auth/createAdmin" 2>/dev/null)
if [ "$ND_CHECK" = "200" ]; then
  ND_RESULT=$(curl -s -X POST "$NAVIDROME_URL/auth/createAdmin" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg u "$JELLYFIN_USER" --arg p "$JELLYFIN_PASS" '{username:$u,password:$p}')" 2>/dev/null || true)
  if echo "$ND_RESULT" | jq -e '.id' >/dev/null 2>&1; then
    ok "Admin user created: $JELLYFIN_USER"
  else
    warn "Could not create admin user"
  fi
else
  ok "Already configured"
fi

# ═══════════════════════════════════════════════════════════════════
# 16.8 KAVITA — create admin user and add book library
# ═══════════════════════════════════════════════════════════════════
info "Configuring Kavita..."

KV_RESULT=$(curl -s -X POST "$KAVITA_URL/api/Account/register" \
  -H "Content-Type: application/json" \
  -d "$(jq -nc --arg u "$JELLYFIN_USER" --arg p "$JELLYFIN_PASS" '{username:$u,password:$p}')" 2>/dev/null || true)
if echo "$KV_RESULT" | jq -e '.token' >/dev/null 2>&1; then
  KV_TOKEN=$(echo "$KV_RESULT" | jq -r '.token')
  ok "Admin user created: $JELLYFIN_USER"
  # Add book library
  curl -sf -X POST "$KAVITA_URL/api/Library" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $KV_TOKEN" \
    -d '{"name":"Books","type":2,"folders":["/media/books"],"manageCollections":true,"manageReadingLists":true,"includeInDashboard":true,"includeInRecommended":true,"includeInSearch":true}' >/dev/null 2>&1 && \
    ok "Library 'Books' → /media/books" || warn "Could not add book library"
else
  ok "Already configured"
fi

# ═══════════════════════════════════════════════════════════════════
# 16.9 IMMICH — create admin user
# ═══════════════════════════════════════════════════════════════════
info "Configuring Immich..."

IM_CHECK=$(curl -s "$IMMICH_URL/api/server/ping" 2>/dev/null || true)
if [ -n "$IM_CHECK" ]; then
  IM_RESULT=$(curl -s -X POST "$IMMICH_URL/api/auth/admin-sign-up" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg p "$JELLYFIN_PASS" --arg n "$JELLYFIN_USER" '{email:"admin@media.local",password:$p,name:$n}')" 2>/dev/null || true)
  if echo "$IM_RESULT" | jq -e '.id' >/dev/null 2>&1; then
    ok "Admin user created: $JELLYFIN_USER (admin@media.local)"
  else
    ok "Already configured"
  fi
else
  warn "Immich not responding"
fi

# ═══════════════════════════════════════════════════════════════════
# 16.10 JANITORR — auto-cleanup of old media
# ═══════════════════════════════════════════════════════════════════
info "Configuring Janitorr..."

JANITORR_CONFIG="$CONFIG_DIR/janitorr/application.yml"
if [ ! -f "$JANITORR_CONFIG" ]; then
  mkdir -p "$CONFIG_DIR/janitorr/logs"
  mkdir -p "$MEDIA_DIR/leaving-soon"
  cat > "$JANITORR_CONFIG" << JANITORREOF
logging:
  level:
    com.github.schaka: INFO
  file:
    name: "/logs/janitorr.log"

file-system:
  access: true
  validate-seeding: true
  leaving-soon-dir: "/media/leaving-soon"
  media-server-leaving-soon-dir: "/media/leaving-soon"
  from-scratch: true
  free-space-check-dir: "/"

application:
  dry-run: true
  run-once: false
  whole-tv-show: false
  whole-show-seeding-check: false
  leaving-soon: 14d
  leaving-soon-threshold-offset-percent: 5
  exclusion-tags:
    - "janitorr_keep"

  media-deletion:
    enabled: true
    movie-expiration:
      5: 15d
      10: 30d
      15: 60d
      20: 90d
    season-expiration:
      5: 15d
      10: 30d
      15: 60d
      20: 120d

  tag-based-deletion:
    enabled: false
    minimum-free-disk-percent: 100
    schedules: []

  episode-deletion:
    enabled: false
    clean-older-seasons: false
    tag: janitorr_daily
    max-episodes: 10
    max-age: 30d

clients:
  sonarr:
    enabled: true
    url: "http://sonarr:8989"
    api-key: "$SONARR_KEY"
    delete-empty-shows: true
    import-exclusions: false
  radarr:
    enabled: true
    url: "http://radarr:7878"
    api-key: "$RADARR_KEY"
    only-delete-files: false
    import-exclusions: false
  bazarr:
    enabled: false
    url: "http://bazarr:6767"
    api-key: ""
  jellyfin:
    enabled: true
    url: "http://jellyfin:8096"
    api-key: "$JELLYFIN_API_KEY"
    username: "$JELLYFIN_USER"
    password: "$JELLYFIN_PASS"
    delete: true
    exclude-favorited: false
    leaving-soon-tv: "Shows (Leaving Soon)"
    leaving-soon-movies: "Movies (Leaving Soon)"
    leaving-soon-type: MOVIES_AND_TV
  emby:
    enabled: false
    url: ""
    api-key: ""
    username: ""
    password: ""
    delete: false
    exclude-favorited: false
    leaving-soon-tv: ""
    leaving-soon-movies: ""
    leaving-soon-type: NONE
  jellyseerr:
    enabled: true
    url: "http://jellyseerr:5055"
    api-key: "$JELLYSEERR_KEY"
    match-server: false
  jellystat:
    enabled: false
    whole-tv-show: false
    url: ""
    api-key: ""
  streamystats:
    enabled: false
    whole-tv-show: false
    url: ""
    api-key: ""
JANITORREOF
  ok "Config written (dry-run mode)"
  docker restart janitorr >/dev/null 2>&1 || true
else
  ok "Already configured"
fi

# ═══════════════════════════════════════════════════════════════════
# 17. HOMEPAGE — dashboard with service widgets
# ═══════════════════════════════════════════════════════════════════
info "Configuring Homepage dashboard..."

HP_DIR="$CONFIG_DIR/homepage"
mkdir -p "$HP_DIR"

# settings.yaml
cat > "$HP_DIR/settings.yaml" <<'HPEOF'
title: Media Server
theme: dark
color: slate
headerStyle: clean
layout:
  Media:
    style: row
    columns: 4
  Library Management:
    style: row
    columns: 4
  Downloads:
    style: row
    columns: 2
  Tools:
    style: row
    columns: 4
HPEOF
ok "settings.yaml"

# docker.yaml — let Homepage talk to Docker socket
cat > "$HP_DIR/docker.yaml" <<'HPEOF'
local:
  socket: /var/run/docker.sock
HPEOF
ok "docker.yaml"

# bookmarks.yaml (empty)
cat > "$HP_DIR/bookmarks.yaml" <<'HPEOF'
[]
HPEOF

# widgets.yaml — system info
cat > "$HP_DIR/widgets.yaml" <<'HPEOF'
- resources:
    cpu: true
    memory: true
    disk: /
- datetime:
    text_size: xl
    format:
      dateStyle: long
      timeStyle: short
HPEOF
ok "widgets.yaml"

# services.yaml — all services with API key integration
cat > "$HP_DIR/services.yaml" <<HPEOF
- Media:
    - Jellyfin:
        icon: jellyfin.svg
        href: http://jellyfin.media.local
        description: Stream your library
        server: local
        container: jellyfin
        widget:
          type: jellyfin
          url: http://jellyfin:8096
          key: ${JELLYFIN_TOKEN:-}
    - Jellyseerr:
        icon: jellyseerr.svg
        href: http://jellyseerr.media.local
        description: Request movies & TV
        server: local
        container: jellyseerr
        widget:
          type: jellyseerr
          url: http://jellyseerr:5055
          key: ${JELLYSEERR_KEY:-}
    - Navidrome:
        icon: navidrome.svg
        href: http://navidrome.media.local
        description: Stream your music
        server: local
        container: navidrome
    - Kavita:
        icon: kavita.svg
        href: http://kavita.media.local
        description: Read books & comics
        server: local
        container: kavita
- Library Management:
    - Sonarr:
        icon: sonarr.svg
        href: http://sonarr.media.local
        description: TV show automation
        server: local
        container: sonarr
        widget:
          type: sonarr
          url: http://sonarr:8989
          key: ${SONARR_KEY:-}
    - Sonarr Anime:
        icon: sonarr.svg
        href: http://sonarr-anime.media.local
        description: Anime series
        server: local
        container: sonarr-anime
        widget:
          type: sonarr
          url: http://sonarr-anime:8989
          key: ${SONARR_ANIME_KEY:-}
    - Radarr:
        icon: radarr.svg
        href: http://radarr.media.local
        description: Movie automation
        server: local
        container: radarr
        widget:
          type: radarr
          url: http://radarr:7878
          key: ${RADARR_KEY:-}
    - Lidarr:
        icon: lidarr.svg
        href: http://lidarr.media.local
        description: Music automation
        server: local
        container: lidarr
        widget:
          type: lidarr
          url: http://lidarr:8686
          key: ${LIDARR_KEY:-}
    - Prowlarr:
        icon: prowlarr.svg
        href: http://prowlarr.media.local
        description: Indexer management
        server: local
        container: prowlarr
        widget:
          type: prowlarr
          url: http://prowlarr:9696
          key: ${PROWLARR_KEY:-}
    - Bazarr:
        icon: bazarr.svg
        href: http://bazarr.media.local
        description: Subtitle downloads
        server: local
        container: bazarr
        widget:
          type: bazarr
          url: http://bazarr:6767
          key: $([ -f "$CONFIG_DIR/bazarr/config/config/config.yaml" ] && sed -n 's/^apikey: *//p' "$CONFIG_DIR/bazarr/config/config/config.yaml" 2>/dev/null || echo "")
- Downloads:
    - qBittorrent:
        icon: qbittorrent.svg
        href: http://qbittorrent.media.local
        description: Torrent client
        server: local
        container: qbittorrent
        widget:
          type: qbittorrent
          url: http://qbittorrent:8081
          username: ${QBIT_USER:-admin}
          password: ${QBIT_PASS:-changeme}
    - SABnzbd:
        icon: sabnzbd.svg
        href: http://sabnzbd.media.local
        description: Usenet client
        server: local
        container: sabnzbd
        widget:
          type: sabnzbd
          url: http://sabnzbd:8080
          key: ${SABNZBD_KEY:-}
- Tools:
    - Immich:
        icon: immich.svg
        href: http://immich.media.local
        description: Photo management
        server: local
        container: immich
    - Gitea:
        icon: gitea.svg
        href: http://gitea.media.local
        description: Git mirror & hosting
        server: local
        container: gitea
    - Uptime Kuma:
        icon: uptime-kuma.svg
        href: http://uptime-kuma.media.local
        description: Service uptime monitoring
        server: local
        container: uptime-kuma
        widget:
          type: uptimekuma
          url: http://uptime-kuma:3001
          slug: default
    - Scrutiny:
        icon: scrutiny.svg
        href: http://scrutiny.media.local
        description: Disk health monitoring
        server: local
        container: scrutiny
HPEOF
ok "services.yaml"

# ═══════════════════════════════════════════════════════════════════
# 18. API PROXY CONFIG (landing page widgets)
# ═══════════════════════════════════════════════════════════════════
info "Generating API proxy config for landing page..."

# Re-read Jellyseerr key (may have been created during Jellyseerr setup above)
[ -z "$JELLYSEERR_KEY" ] && [ -f "$CONFIG_DIR/jellyseerr/settings.json" ] && \
  JELLYSEERR_KEY=$(jq -r '.main.apiKey // empty' "$CONFIG_DIR/jellyseerr/settings.json" 2>/dev/null)

API_PROXY="$CONFIG_DIR/nginx/api-proxy.conf"
cat > "$API_PROXY" << PROXYEOF
# Auto-generated by setup.sh — API proxy endpoints for landing page widgets
# Keys are injected here so they never reach the browser.
# Rate-limited to prevent abuse.
# Uses nginx variables + rewrite for upstream resolution so nginx starts even if a service is down.
# IMPORTANT: set must come BEFORE rewrite ... break; (both are rewrite-module directives)

# qBittorrent
location /api/qbt/ {
    limit_req zone=api burst=50 nodelay;
    set \$upstream_api_qbt http://qbittorrent:8081;
    rewrite ^/api/qbt/(.*)\$ /api/v2/\$1 break;
    proxy_pass \$upstream_api_qbt;
    proxy_set_header Host qbittorrent;
    proxy_set_header Referer http://qbittorrent:8081;
    proxy_set_header Origin http://qbittorrent:8081;
}

# Sonarr
location /api/sonarr/ {
    limit_req zone=api burst=50 nodelay;
    set \$upstream_api_sonarr http://sonarr:8989;
    rewrite ^/api/sonarr/(.*)\$ /api/v3/\$1 break;
    proxy_pass \$upstream_api_sonarr;
    proxy_set_header X-Api-Key $SONARR_KEY;
}

# Sonarr Anime
location /api/sonarr-anime/ {
    limit_req zone=api burst=50 nodelay;
    set \$upstream_api_sonarr_anime http://sonarr-anime:8989;
    rewrite ^/api/sonarr-anime/(.*)\$ /api/v3/\$1 break;
    proxy_pass \$upstream_api_sonarr_anime;
    proxy_set_header X-Api-Key $SONARR_ANIME_KEY;
}

# Radarr
location /api/radarr/ {
    limit_req zone=api burst=50 nodelay;
    set \$upstream_api_radarr http://radarr:7878;
    rewrite ^/api/radarr/(.*)\$ /api/v3/\$1 break;
    proxy_pass \$upstream_api_radarr;
    proxy_set_header X-Api-Key $RADARR_KEY;
}

# Jellyfin
location /api/jellyfin/ {
    limit_req zone=api burst=50 nodelay;
    set \$upstream_api_jellyfin http://jellyfin:8096;
    rewrite ^/api/jellyfin/(.*)\$ /\$1 break;
    proxy_pass \$upstream_api_jellyfin;
    proxy_set_header X-Emby-Token $JELLYFIN_API_KEY;
}

# Jellyseerr
location /api/jellyseerr/ {
    limit_req zone=api burst=50 nodelay;
    set \$upstream_api_jellyseerr http://jellyseerr:5055;
    rewrite ^/api/jellyseerr/(.*)\$ /api/v1/\$1 break;
    proxy_pass \$upstream_api_jellyseerr;
    proxy_set_header X-Api-Key $JELLYSEERR_KEY;
}

# SABnzbd (key in query param)
location /api/sabnzbd/ {
    limit_req zone=api burst=50 nodelay;
    set \$upstream_api_sabnzbd http://sabnzbd:8080;
    proxy_pass \$upstream_api_sabnzbd/api?apikey=${SABNZBD_KEY}&\$args;
}
PROXYEOF

ok "api-proxy.conf written"

# Reload nginx to pick up the new proxy config
sleep 2
if docker exec media-nginx nginx -t >/dev/null 2>&1; then
  docker exec media-nginx nginx -s reload >/dev/null 2>&1 && \
    ok "nginx reloaded" || warn "Could not reload nginx (will apply on next restart)"
else
  warn "nginx config test failed — skipping reload"
fi

fi  # end setup mode

# ═══════════════════════════════════════════════════════════════════
# 18. END-TO-END VERIFICATION
# ═══════════════════════════════════════════════════════════════════
info "Running end-to-end verification..."

TESTS_PASSED=0
TESTS_FAILED=0
TESTS_SKIPPED=0

pass() { printf "\033[1;32m   ✓ %s\033[0m\n" "$*"; TESTS_PASSED=$((TESTS_PASSED + 1)); }
fail() { printf "\033[1;31m   ✗ %s\033[0m\n" "$*"; TESTS_FAILED=$((TESTS_FAILED + 1)); }
skip() { printf "\033[1;33m   - %s (skipped)\033[0m\n" "$*"; TESTS_SKIPPED=$((TESTS_SKIPPED + 1)); }

check() {
  local desc="$1" result="$2"
  if [ "$result" = "true" ]; then pass "$desc"; else fail "$desc"; fi
}

# Re-read Jellyseerr key from disk (may have been created during setup)
[ -z "${JELLYSEERR_KEY:-}" ] && [ -f "$CONFIG_DIR/jellyseerr/settings.json" ] && \
  JELLYSEERR_KEY=$(jq -r '.main.apiKey // empty' "$CONFIG_DIR/jellyseerr/settings.json" 2>/dev/null)

# --- 1. Service health ---
info "Service health..."

for svc_url in \
  "Jellyfin:$JELLYFIN_URL/health" \
  "Sonarr:$SONARR_URL/ping" \
  "Sonarr Anime:$SONARR_ANIME_URL/ping" \
  "Radarr:$RADARR_URL/ping" \
  "Prowlarr:$PROWLARR_URL/ping" \
  "Bazarr:$BAZARR_URL" \
  "SABnzbd:$SABNZBD_URL" \
  "qBittorrent:$QBIT_URL" \
  "Jellyseerr:$JELLYSEERR_URL" \
  "Lidarr:$LIDARR_URL/ping" \
  "LazyLibrarian:$LAZYLIBRARIAN_URL" \
  "Navidrome:$NAVIDROME_URL/ping" \
  "Kavita:$KAVITA_URL" \
  "Immich:$IMMICH_URL/api/server/ping" \
  "TubeArchivist:$TUBEARCHIVIST_URL/api/ping" \
  "Tdarr:$TDARR_URL" \
  "Autobrr:$AUTOBRR_URL/api/healthz/liveness" \
  "Scrutiny:$SCRUTINY_URL/api/health" \
  "Gitea:$GITEA_URL/api/v1/version" \
  "Uptime Kuma:$UPTIME_KUMA_URL" \
  "Open WebUI:$OPEN_WEBUI_URL" \
  "Dozzle:$DOZZLE_URL" \
  "Beszel:$BESZEL_URL/api/health" \
  "CrowdSec:$CROWDSEC_URL/health" \
  "Homepage:$HOMEPAGE_URL" \
  ; do
  name="${svc_url%%:*}"
  url="${svc_url#*:}"
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url" 2>/dev/null || true)
  # Retry once after 5s if service appears down (may still be restarting)
  if [ -z "$HTTP_CODE" ] || [ "$HTTP_CODE" = "000" ]; then
    sleep 5
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url" 2>/dev/null || true)
  fi
  check "$name responds ($HTTP_CODE)" "$([ -n "$HTTP_CODE" ] && [ "$HTTP_CODE" != "000" ] && echo true || echo false)"
done

# --- 2. Download clients ---
info "Download clients..."

QBIT_COOKIE_V=$(curl -sf -c - "$QBIT_URL/api/v2/auth/login" -d "username=$QBIT_USER&password=$QBIT_PASS" 2>/dev/null | extract_cookie SID)
check "qBittorrent login" "$([ -n "$QBIT_COOKIE_V" ] && echo true || echo false)"

if [ -n "$QBIT_COOKIE_V" ]; then
  QBIT_CATS=$(curl -sf "$QBIT_URL/api/v2/torrents/categories" -b "SID=$QBIT_COOKIE_V" 2>/dev/null || echo "{}")
  check "qBittorrent category: sonarr" "$(echo "$QBIT_CATS" | jq 'has("sonarr")' 2>/dev/null)"
  check "qBittorrent category: radarr" "$(echo "$QBIT_CATS" | jq 'has("radarr")' 2>/dev/null)"
fi

if [ -n "$SONARR_KEY" ]; then
  SONARR_DL=$(api GET "$SONARR_URL/api/v3/downloadclient" -H "X-Api-Key: $SONARR_KEY" || echo "[]")
  check "Sonarr → qBittorrent" "$(echo "$SONARR_DL" | jq 'any(.[]; .name == "qBittorrent" and .enable == true)' 2>/dev/null)"
fi

if [ -n "$SONARR_ANIME_KEY" ]; then
  SONARR_ANIME_DL=$(api GET "$SONARR_ANIME_URL/api/v3/downloadclient" -H "X-Api-Key: $SONARR_ANIME_KEY" || echo "[]")
  check "Sonarr Anime → qBittorrent" "$(echo "$SONARR_ANIME_DL" | jq 'any(.[]; .name == "qBittorrent" and .enable == true)' 2>/dev/null)"
fi

if [ -n "$RADARR_KEY" ]; then
  RADARR_DL=$(api GET "$RADARR_URL/api/v3/downloadclient" -H "X-Api-Key: $RADARR_KEY" || echo "[]")
  check "Radarr → qBittorrent" "$(echo "$RADARR_DL" | jq 'any(.[]; .name == "qBittorrent" and .enable == true)' 2>/dev/null)"
fi

# --- 3. Root folders ---
info "Root folders..."

[ -n "$SONARR_KEY" ] && check "Sonarr → /media/tv" \
  "$(api GET "$SONARR_URL/api/v3/rootfolder" -H "X-Api-Key: $SONARR_KEY" | jq 'any(.[]; .path == "/media/tv")' 2>/dev/null)"

[ -n "$SONARR_ANIME_KEY" ] && check "Sonarr Anime → /media/anime" \
  "$(api GET "$SONARR_ANIME_URL/api/v3/rootfolder" -H "X-Api-Key: $SONARR_ANIME_KEY" | jq 'any(.[]; .path == "/media/anime")' 2>/dev/null)"

[ -n "$RADARR_KEY" ] && check "Radarr → /media/movies" \
  "$(api GET "$RADARR_URL/api/v3/rootfolder" -H "X-Api-Key: $RADARR_KEY" | jq 'any(.[]; .path == "/media/movies")' 2>/dev/null)"

[ -n "$RADARR_KEY" ] && check "Radarr → no stale root folders" \
  "$(api GET "$RADARR_URL/api/v3/rootfolder" -H "X-Api-Key: $RADARR_KEY" | jq '[.[] | .path] | all(. == "/media/movies")' 2>/dev/null)"

# --- 4. Prowlarr ---
info "Prowlarr..."

if [ -n "$PROWLARR_KEY" ]; then
  PH="X-Api-Key: $PROWLARR_KEY"
  PROWLARR_APPS=$(api GET "$PROWLARR_URL/api/v1/applications" -H "$PH" || echo "[]")
  check "Prowlarr → Sonarr connected" "$(echo "$PROWLARR_APPS" | jq 'any(.[]; .name == "Sonarr")' 2>/dev/null)"
  check "Prowlarr → Sonarr Anime connected" "$(echo "$PROWLARR_APPS" | jq 'any(.[]; .name == "Sonarr Anime")' 2>/dev/null)"
  check "Prowlarr → Radarr connected" "$(echo "$PROWLARR_APPS" | jq 'any(.[]; .name == "Radarr")' 2>/dev/null)"

  INDEXER_COUNT=$(api GET "$PROWLARR_URL/api/v1/indexer" -H "$PH" | jq '[.[] | select(.enable == true)] | length' 2>/dev/null || echo "0")
  check "Prowlarr → indexers enabled ($INDEXER_COUNT)" "$([ "$INDEXER_COUNT" -gt 0 ] && echo true || echo false)"

  SEARCH_RESULTS=$(curl -sf --max-time 30 "$PROWLARR_URL/api/v1/search?query=test&type=movie&limit=3" -H "$PH" 2>/dev/null || echo "[]")
  SEARCH_COUNT=$(echo "$SEARCH_RESULTS" | jq 'length' 2>/dev/null || echo "0")
  if [ "$SEARCH_COUNT" -gt 0 ] 2>/dev/null; then
    pass "Prowlarr → search works ($SEARCH_COUNT results)"
  elif [ "$INDEXER_COUNT" -gt 0 ] 2>/dev/null; then
    skip "Prowlarr → search (indexers may be rate-limited)"
  else
    fail "Prowlarr → search works"
  fi
fi

# --- 5. Jellyfin ---
info "Jellyfin..."

JF_HEADER_V='X-Emby-Authorization: MediaBrowser Client="verify", Device="script", DeviceId="verify", Version="1.0"'
JF_AUTH_V=$(curl -sf -X POST "$JELLYFIN_URL/Users/AuthenticateByName" -H "$JF_HEADER_V" \
  -H "Content-Type: application/json" -d "{\"Username\":\"$JELLYFIN_USER\",\"Pw\":\"$JELLYFIN_PASS\"}" 2>/dev/null)
JF_TOKEN_V=$(echo "$JF_AUTH_V" | jq -r '.AccessToken // empty' 2>/dev/null)
check "Jellyfin → login" "$([ -n "$JF_TOKEN_V" ] && echo true || echo false)"

if [ -n "$JF_TOKEN_V" ]; then
  curl -sf "$JELLYFIN_URL/Library/VirtualFolders" -H "X-Emby-Token: $JF_TOKEN_V" > "$TMPDIR_SETUP/jf_verify.json" 2>/dev/null
  for lp in "Movies:/media/movies" "TV Shows:/media/tv" "Anime:/media/anime"; do
    ln="${lp%%:*}"; lpath="${lp#*:}"
    HAS=$(jq --arg n "$ln" --arg p "$lpath" \
      '[.[] | select(.Name == $n) | .Locations[] | select(. == $p)] | length > 0' "$TMPDIR_SETUP/jf_verify.json" 2>/dev/null)
    check "Jellyfin → library: $ln" "$HAS"
  done
  REALTIME=$(jq 'all(.[]; .LibraryOptions.EnableRealtimeMonitor == true)' "$TMPDIR_SETUP/jf_verify.json" 2>/dev/null)
  check "Jellyfin → real-time monitoring" "$REALTIME"
  DAILY_SCAN=$(jq 'all(.[]; .LibraryOptions.AutomaticRefreshIntervalDays == 1)' "$TMPDIR_SETUP/jf_verify.json" 2>/dev/null)
  check "Jellyfin → daily scan" "$DAILY_SCAN"
  rm -f "$TMPDIR_SETUP/jf_verify.json"
fi

# --- 6. Jellyfin sync ---
info "Jellyfin sync..."

check_jellyfin_notification() {
  local name="$1" url="$2" key="$3"
  local NOTIF=$(api GET "$url/api/v3/notification" -H "X-Api-Key: $key" || echo "[]")
  check "$name → Jellyfin notification" "$(echo "$NOTIF" | jq 'any(.[]; .name == "Jellyfin")' 2>/dev/null)"
}

[ -n "$SONARR_KEY" ] && check_jellyfin_notification "Sonarr" "$SONARR_URL" "$SONARR_KEY"
[ -n "$SONARR_ANIME_KEY" ] && check_jellyfin_notification "Sonarr Anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY"
[ -n "$RADARR_KEY" ] && check_jellyfin_notification "Radarr" "$RADARR_URL" "$RADARR_KEY"

# --- 7. Jellyseerr ---
info "Jellyseerr..."

JS_PUBLIC_V=$(api GET "$JELLYSEERR_URL/api/v1/settings/public" || echo "{}")
check "Jellyseerr → initialized" "$(echo "$JS_PUBLIC_V" | jq '.initialized' 2>/dev/null)"

if [ -n "${JELLYSEERR_KEY:-}" ]; then
  JH="X-Api-Key: $JELLYSEERR_KEY"

  JS_SONARR_V=$(api GET "$JELLYSEERR_URL/api/v1/settings/sonarr" -H "$JH" || echo "[]")
  JS_SONARR_COUNT=$(echo "$JS_SONARR_V" | jq 'length' 2>/dev/null || echo "0")
  check "Jellyseerr → Sonarr connections ($JS_SONARR_COUNT)" "$([ "$JS_SONARR_COUNT" -gt 0 ] && echo true || echo false)"
  JS_SONARR_SEARCH=$(echo "$JS_SONARR_V" | jq 'all(.[]; .enableSearch == true)' 2>/dev/null)
  check "Jellyseerr → Sonarr enableSearch" "$JS_SONARR_SEARCH"

  JS_RADARR_V=$(api GET "$JELLYSEERR_URL/api/v1/settings/radarr" -H "$JH" || echo "[]")
  JS_RADARR_COUNT=$(echo "$JS_RADARR_V" | jq 'length' 2>/dev/null || echo "0")
  check "Jellyseerr → Radarr connections ($JS_RADARR_COUNT)" "$([ "$JS_RADARR_COUNT" -gt 0 ] && echo true || echo false)"
  JS_RADARR_SEARCH=$(echo "$JS_RADARR_V" | jq 'all(.[]; .enableSearch == true)' 2>/dev/null)
  check "Jellyseerr → Radarr enableSearch" "$JS_RADARR_SEARCH"

  JS_JELLYFIN_V=$(api GET "$JELLYSEERR_URL/api/v1/settings/jellyfin" -H "$JH" || echo "{}")
  JS_LIB_ENABLED=$(echo "$JS_JELLYFIN_V" | jq '[.libraries[] | select(.enabled == true)] | length' 2>/dev/null || echo "0")
  JS_LIB_TOTAL=$(echo "$JS_JELLYFIN_V" | jq '.libraries | length' 2>/dev/null || echo "0")
  check "Jellyseerr → libraries enabled ($JS_LIB_ENABLED/$JS_LIB_TOTAL)" "$([ "$JS_LIB_ENABLED" -gt 0 ] && echo true || echo false)"
fi

# --- 8. Quality profiles ---
info "Quality profiles..."

check_unknown_quality() {
  local name="$1" url="$2" key="$3"
  local PROFILE=$(api GET "$url/api/v3/qualityprofile/1" -H "X-Api-Key: $key" 2>/dev/null || echo "")
  [ -z "$PROFILE" ] && { skip "$name → quality profile"; return; }
  local UNKNOWN=$(echo "$PROFILE" | jq '[.items[] | select(.quality.id == 0) | .allowed][0]' 2>/dev/null)
  check "$name → Unknown quality allowed" "$UNKNOWN"
}

[ -n "$SONARR_KEY" ] && check_unknown_quality "Sonarr" "$SONARR_URL" "$SONARR_KEY"
[ -n "$SONARR_ANIME_KEY" ] && check_unknown_quality "Sonarr Anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY"

# --- 9. Authentication ---
info "Authentication..."

check_arr_auth() {
  local name="$1" url="$2" key="$3" api_ver="${4:-v3}"
  local HOST=$(api GET "$url/api/$api_ver/config/host" -H "X-Api-Key: $key" 2>/dev/null || echo "")
  [ -z "$HOST" ] && { skip "$name → auth"; return; }
  local AUTH_USER=$(echo "$HOST" | jq -r '.username // empty' 2>/dev/null)
  check "$name → auth configured" "$([ -n "$AUTH_USER" ] && echo true || echo false)"
}

[ -n "$SONARR_KEY" ] && check_arr_auth "Sonarr" "$SONARR_URL" "$SONARR_KEY"
[ -n "$SONARR_ANIME_KEY" ] && check_arr_auth "Sonarr Anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY"
[ -n "$RADARR_KEY" ] && check_arr_auth "Radarr" "$RADARR_URL" "$RADARR_KEY"
[ -n "$PROWLARR_KEY" ] && check_arr_auth "Prowlarr" "$PROWLARR_URL" "$PROWLARR_KEY" "v1"

if [ -n "${SABNZBD_KEY:-}" ]; then
  SAB_AUTH_USER=$(curl -sf "$SABNZBD_URL/api?mode=get_config&section=misc&apikey=$SABNZBD_KEY&output=json" 2>/dev/null | jq -r '.config.misc.username // empty' 2>/dev/null)
  check "SABnzbd → auth configured" "$([ -n "$SAB_AUTH_USER" ] && echo true || echo false)"
fi

BAZARR_CONFIG_FILE=""
for p in "$CONFIG_DIR/bazarr/config/config/config.yaml" "$CONFIG_DIR/bazarr/config/config.yaml"; do
  [ -f "$p" ] && BAZARR_CONFIG_FILE="$p" && break
done
if [ -n "$BAZARR_CONFIG_FILE" ]; then
  BAZARR_AUTH_USER=$(sed -n '/^auth:/,/^[^ ]/{s/^  username: *//p;}' "$BAZARR_CONFIG_FILE" 2>/dev/null | head -1 || echo "")
  check "Bazarr → auth configured" "$([ -n "$BAZARR_AUTH_USER" ] && [ "$BAZARR_AUTH_USER" != "''" ] && echo true || echo false)"
fi

# --- 10. Health checks ---
info "Health checks..."

[ -n "$SONARR_KEY" ] && {
  SONARR_ERRORS=$(api GET "$SONARR_URL/api/v3/health" -H "X-Api-Key: $SONARR_KEY" | jq '[.[] | select(.type == "error")] | length' 2>/dev/null || echo "0")
  check "Sonarr → no health errors" "$([ "$SONARR_ERRORS" = "0" ] && echo true || echo false)"
}

[ -n "$RADARR_KEY" ] && {
  RADARR_ERRORS=$(api GET "$RADARR_URL/api/v3/health" -H "X-Api-Key: $RADARR_KEY" | jq '[.[] | select(.type == "error")] | length' 2>/dev/null || echo "0")
  check "Radarr → no health errors" "$([ "$RADARR_ERRORS" = "0" ] && echo true || echo false)"
}

# --- 12. Landing page & proxy ---
info "Landing page..."

LANDING_HEADERS=$(curl -sf -D - -o /dev/null http://localhost 2>/dev/null || echo "")
LANDING=$(curl -sf http://localhost 2>/dev/null || echo "")
check "Landing page → serves HTML" "$(echo "$LANDING" | grep -q 'Media.*Server' && echo true || echo false)"
check "Landing page → Content-Type text/html" "$(echo "$LANDING_HEADERS" | grep -qi 'content-type.*text/html' && echo true || echo false)"
check "Landing page → service grid" "$(echo "$LANDING" | grep -q 'Jellyfin' && echo true || echo false)"
check "Landing page → downloads widget" "$(echo "$LANDING" | grep -q 'qbt/torrents' && echo true || echo false)"

QBT_PROXY=$(curl -sf http://localhost/api/qbt/torrents/info 2>/dev/null || echo "")
check "Landing page → qBittorrent proxy" "$(echo "$QBT_PROXY" | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"

check "Proxy → Sonarr calendar" "$(curl -sf http://localhost/api/sonarr/calendar 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
check "Proxy → Sonarr Anime calendar" "$(curl -sf http://localhost/api/sonarr-anime/calendar 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
check "Proxy → Radarr calendar" "$(curl -sf http://localhost/api/radarr/calendar 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
check "Proxy → Jellyfin latest" "$(curl -sf 'http://localhost/api/jellyfin/Items?SortBy=DateCreated&SortOrder=Descending&Limit=3&Recursive=true&IncludeItemTypes=Movie,Series' 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
check "Proxy → Jellyseerr requests" "$(curl -sf http://localhost/api/jellyseerr/request 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
check "Proxy → SABnzbd queue" "$(curl -sf 'http://localhost/api/sabnzbd/?mode=queue&output=json' 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"

# --- 13. Docker containers ---
info "Docker containers..."

for container in jellyfin sonarr sonarr-anime radarr lidarr lazylibrarian prowlarr bazarr sabnzbd qbittorrent jellyseerr flaresolverr media-nginx recyclarr unpackerr autobrr tubearchivist archivist-es archivist-redis tdarr janitorr ollama open-webui watchtower dozzle crowdsec beszel navidrome kavita immich immich-machine-learning immich-redis immich-postgres scrutiny gitea uptime-kuma homepage; do
  STATUS=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo "missing")
  check "Container: $container" "$([ "$STATUS" = "running" ] && echo true || echo false)"
done

# --- 14. Tailscale ---
info "Tailscale..."
TS_CLI="$(detect_tailscale_cli)"
if [ -n "$TS_CLI" ]; then
  pass "Tailscale installed"
  if "$TS_CLI" status &>/dev/null; then
    TS_IP=$("$TS_CLI" ip -4 2>/dev/null || echo "")
    if [ -n "$TS_IP" ]; then
      pass "Tailscale connected ($TS_IP)"
    else
      fail "Tailscale connected but no IPv4 address"
    fi
    SERVE_STATUS=$("$TS_CLI" serve status 2>/dev/null || echo "")
    if echo "$SERVE_STATUS" | grep -q "https"; then
      pass "Tailscale HTTPS configured"
    else
      skip "Tailscale HTTPS not configured"
    fi
  else
    skip "Tailscale not connected (remote access unavailable)"
  fi
else
  skip "Tailscale not installed"
fi

# --- 15. Summary ---
TOTAL=$((TESTS_PASSED + TESTS_FAILED))
echo ""
echo "  ────────────────────────────────────────────────────────────"
if [ "$TESTS_FAILED" -eq 0 ]; then
  printf "\033[1;32m   All %d checks passed!" "$TOTAL"
  [ "$TESTS_SKIPPED" -gt 0 ] && printf " (%d skipped)" "$TESTS_SKIPPED"
  printf "\033[0m\n"
else
  printf "\033[1;31m   %d/%d checks failed" "$TESTS_FAILED" "$TOTAL"
  [ "$TESTS_SKIPPED" -gt 0 ] && printf " (%d skipped)" "$TESTS_SKIPPED"
  printf "\033[0m\n"
fi
echo ""

[ "$MODE" = "test" ] && exit "$TESTS_FAILED"

# ═══════════════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "  Setup Complete!"
echo ""
echo "  Local:"
echo "    Dashboard:    http://media.local"
echo "    Request:      http://jellyseerr.media.local"
echo "    Watch:        http://jellyfin.media.local"
echo "    All services: http://media.local"
echo ""
TS_CLI="$(detect_tailscale_cli)"
TS_HOSTNAME=""
[ -n "$TS_CLI" ] && TS_HOSTNAME=$("$TS_CLI" status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//')
if [ -n "$TS_HOSTNAME" ]; then
echo "  Remote (HTTPS):"
echo "    Jellyfin:     https://$TS_HOSTNAME:8096"
echo "    Jellyseerr:   https://$TS_HOSTNAME:5055"
echo "    Landing page: https://$TS_HOSTNAME"
echo ""
fi
echo "  Quick start:"
echo "    1. Go to http://jellyseerr.media.local"
echo "    2. Search for a series or movie"
echo "    3. Click Request"
echo "    4. Watch at http://jellyfin.media.local"
if [ -n "$TS_HOSTNAME" ]; then
echo "    Remote? Use https://$TS_HOSTNAME:8096"
fi
echo ""
