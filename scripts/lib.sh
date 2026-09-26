#!/usr/bin/env bash
# Shared helpers: output, commands, config parsing, HTTP.

# ─── Helpers ─────────────────────────────────────────────────────
info()  { printf "\n\033[1;34m=> %s\033[0m\n" "$*"; }
ok()    { printf "\033[1;32m   ✓ %s\033[0m\n" "$*"; }
warn()  { printf "\033[1;33m   ! %s\033[0m\n" "$*"; }
err()   { printf "\033[1;31m   ✗ %s\033[0m\n" "$*"; exit 1; }
on_error() {
  local file="$1" line="$2" cmd="$3" code="$4"
  printf "\033[1;31m   ✗ Command failed (exit %s) at %s:%s: %s\033[0m\n" "$code" "${file#"$SCRIPT_DIR"/}" "$line" "$cmd" >&2
  exit "$code"
}
trap 'on_error "${BASH_SOURCE[0]:-setup.sh}" "$LINENO" "$BASH_COMMAND" "$?"' ERR

has_cmd() { command -v "$1" >/dev/null 2>&1; }
require_cmd() { has_cmd "$1" || err "$1 is required"; }
log_dry_run() { printf "\033[1;33m   [DRY-RUN]\033[0m %s\n" "$*"; }
run_cmd() {
  if [ "$DRY_RUN" = "true" ]; then
    log_dry_run "$*"
    return 0
  fi
  "$@"
}
as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif has_cmd sudo; then
    sudo "$@"
  else
    err "Need root privileges to run: $*"
  fi
}
# Show only the start of a secret in output
mask() { printf '%s…' "${1:0:6}"; }
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

urlencode() { printf '%s' "$1" | jq -sRr @uri; }

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
  awk -v name="$cookie_name" 'BEGIN{FS="\t"} ($0 !~ /^#/ || $0 ~ /^#HttpOnly_/) && $6 == name { print $7 }'
}

# qBittorrent's session cookie as "name=value": SID up to 4.x, QBT_SID_<port>
# since 5.0. Reads a curl cookie jar (-c -) on stdin.
extract_qbit_cookie() {
  awk 'BEGIN{FS="\t"} ($0 !~ /^#/ || $0 ~ /^#HttpOnly_/) && ($6 == "SID" || $6 ~ /^QBT_SID_/) { print $6 "=" $7 }'
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
write_credentials_to_config() {
  local path="$1" jf_user="$2" jf_pass="$3" qbit_user="$4" qbit_pass="$5"

  python3 - "$path" "$jf_user" "$jf_pass" "$qbit_user" "$qbit_pass" << 'PY'
import re
import sys

def toml_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')

path, jf_user, jf_pass, qb_user, qb_pass = sys.argv[1:]
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

section = None
for i, line in enumerate(lines):
    m = re.match(r'^\s*\[([^\]]+)\]\s*$', line)
    if m:
        section = m.group(1).strip()
        continue
    if section == "jellyfin":
        if re.match(r'^\s*username\s*=', line):
            lines[i] = 'username = "{}"\n'.format(toml_escape(jf_user))
        elif re.match(r'^\s*password\s*=', line):
            lines[i] = 'password = "{}"\n'.format(toml_escape(jf_pass))
    elif section == "qbittorrent":
        if re.match(r'^\s*username\s*=', line):
            lines[i] = 'username = "{}"\n'.format(toml_escape(qb_user))
        elif re.match(r'^\s*password\s*=', line):
            lines[i] = 'password = "{}"\n'.format(toml_escape(qb_pass))

with open(path, "w", encoding="utf-8") as f:
    f.writelines(lines)
PY
}

write_secure_defaults_to_config() {
  local path="$1"
  local jellyfin_pass qbittorrent_pass
  jellyfin_pass="$(generate_secret)"
  qbittorrent_pass="$(generate_secret)"

  write_credentials_to_config "$path" "admin" "$jellyfin_pass" "admin" "$qbittorrent_pass"

  ok "Generated secure default passwords in config.toml"
  echo "  Jellyfin password: $jellyfin_pass"
  echo "  qBittorrent password: $qbittorrent_pass"
}

prompt_credentials() {
  local path="$1"
  info "Setting up credentials..."
  echo "  Jellyfin credentials are shared across most services"
  echo "  (Seerr, Sonarr, Radarr, Prowlarr, Bazarr, SABnzbd)."
  echo ""

  local jf_user jf_pass jf_pass2 qbit_user qbit_pass qbit_pass2

  read -r -p "  Jellyfin username [admin]: " jf_user
  jf_user="${jf_user:-admin}"
  while true; do
    read -r -s -p "  Jellyfin password: " jf_pass; echo
    [ -z "$jf_pass" ] && warn "Password cannot be empty" && continue
    read -r -s -p "  Confirm password:  " jf_pass2; echo
    [ "$jf_pass" = "$jf_pass2" ] && break
    warn "Passwords don't match — try again"
  done
  ok "Jellyfin: $jf_user"
  echo ""

  read -r -p "  qBittorrent username [admin]: " qbit_user
  qbit_user="${qbit_user:-admin}"
  while true; do
    read -r -s -p "  qBittorrent password: " qbit_pass; echo
    [ -z "$qbit_pass" ] && warn "Password cannot be empty" && continue
    read -r -s -p "  Confirm password:     " qbit_pass2; echo
    [ "$qbit_pass" = "$qbit_pass2" ] && break
    warn "Passwords don't match — try again"
  done
  ok "qBittorrent: $qbit_user"

  write_credentials_to_config "$path" "$jf_user" "$jf_pass" "$qbit_user" "$qbit_pass"
  ok "Credentials saved to config.toml"
}

# Jellyfin 12 only accepts the Authorization header (no X-Emby-Token)
jf_auth() { printf 'Authorization: MediaBrowser Token="%s"' "$1"; }

api() {
  local method="$1" url="$2"; shift 2
  curl -sf -X "$method" "$url" -H "Content-Type: application/json" "$@" 2>/dev/null
}

# wait_for <name> <url>; gives up after $WAIT_MAX seconds (default 120)
wait_for() {
  local name="$1" url="$2" max="${WAIT_MAX:-120}" i=0 code
  printf "   Waiting for %-15s" "$name..."
  while true; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 2 "$url" 2>/dev/null || echo "000")
    { [ "${code:0:1}" = "2" ] || [ "${code:0:1}" = "3" ]; } && break
    i=$((i + 1))
    [ "$i" -ge "$max" ] && echo " timeout!" && return 1
    sleep 1
  done
  echo " up"
}

api_retry() {
  local retries=3 delay=2 i
  for i in $(seq 1 "$retries"); do
    if "$@" ; then return 0; fi
    [ "$i" -lt "$retries" ] && sleep "$delay"
  done
  return 1
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
  cfg_required_string '.quality.sonarr_profile' 'quality.sonarr_profile'
  cfg_required_string '.quality.sonarr_anime_profile' 'quality.sonarr_anime_profile'
  cfg_required_string '.quality.radarr_profile' 'quality.radarr_profile'
}
# Quality profiles that ship with a fresh Sonarr/Radarr. Recyclarr used to
# create the TRaSH profiles on top of these; without it, only the built-ins
# exist, so a name outside this list would silently fall back to "Any".
is_builtin_profile() {
  case "$1" in
    Any|SD|HD-720p|HD-1080p|Ultra-HD|"HD - 720p/1080p") return 0 ;;
    *) return 1 ;;
  esac
}
validate_quality_profile() {
  local key="$1" name
  name=$(cfg ".quality.$key")
  is_builtin_profile "$name" || \
    err "quality.$key: unknown profile '$name' (see config.toml.example for supported names)"
}
TIMEZONE_PATH='(.timezone // .qbittorrent.timezone)'
is_non_negative_number() { [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]; }
is_non_negative_int() { [[ "$1" =~ ^[0-9]+$ ]]; }
validate_config_semantics() {
  local seed_ratio seed_time timezone admin_bind dashboard_port

  seed_ratio=$(cfg '.downloads.seeding_ratio')
  seed_time=$(cfg '.downloads.seeding_time_minutes')
  timezone=$(cfg "$TIMEZONE_PATH // empty")
  # Older config.toml.example put timezone after [qbittorrent], so it parsed
  # as qbittorrent.timezone; accept it but ask for it to be moved
  if [ -z "$(cfg '.timezone // empty')" ] && [ -n "$timezone" ]; then
    warn "timezone is inside [qbittorrent] in config.toml; move it above the first [section]"
  fi

  local jf_pass qbit_pass
  jf_pass=$(cfg '.jellyfin.password // ""')
  qbit_pass=$(cfg '.qbittorrent.password // ""')
  [ "$jf_pass" = "changeme" ] && err "jellyfin.password is still the default 'changeme' — set a real password in config.toml"
  [ "$qbit_pass" = "changeme" ] && err "qbittorrent.password is still the default 'changeme' — set a real password in config.toml"

  is_non_negative_number "$seed_ratio" || err "downloads.seeding_ratio must be a non-negative number"
  is_non_negative_int "$seed_time" || err "downloads.seeding_time_minutes must be a non-negative integer"
  [ -n "$timezone" ] || err "timezone must be set"
  admin_bind=$(cfg '.network.admin_bind // "0.0.0.0"')
  [[ "$admin_bind" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || err "network.admin_bind must be an IPv4 address (e.g. 0.0.0.0 or 127.0.0.1)"
  local disk_warn disk_min
  disk_warn=$(cfg '.disk.warn_free_gb // 50')
  disk_min=$(cfg '.disk.min_free_gb // 10')
  is_non_negative_int "$disk_warn" || err "disk.warn_free_gb must be a whole number of GB"
  is_non_negative_int "$disk_min" || err "disk.min_free_gb must be a whole number of GB"
  dashboard_port=$(cfg '.network.dashboard_port // 80')
  is_non_negative_int "$dashboard_port" || err "network.dashboard_port must be a port number"

  validate_quality_profile sonarr_profile
  validate_quality_profile sonarr_anime_profile
  validate_quality_profile radarr_profile
}

get_api_key() {
  local f="$CONFIG_DIR/$1/config.xml"
  [ -f "$f" ] && sed -n 's/.*<ApiKey>\(.*\)<\/ApiKey>.*/\1/p' "$f" 2>/dev/null || echo ""
}


