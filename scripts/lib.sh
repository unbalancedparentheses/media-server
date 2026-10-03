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
  echo "  The Jellyfin login is shared by Seerr, Sonarr, Radarr, Prowlarr, Bazarr,"
  echo "  SABnzbd and Cleanuparr. qBittorrent has its own."
  echo ""
  # With nothing to read (piped or redirected input), explain instead of
  # failing on the prompt
  prompt_read() { read -r "$@" || err "No answer to the password prompt; run with --yes to generate passwords (or edit $CONFIG_FILE first)"; }

  local jf_user jf_pass jf_pass2 qbit_user qbit_pass qbit_pass2

  prompt_read -p "  Jellyfin username [admin]: " jf_user
  jf_user="${jf_user:-admin}"
  while true; do
    prompt_read -s -p "  Jellyfin password: " jf_pass; echo
    [ -z "$jf_pass" ] && warn "Password cannot be empty" && continue
    prompt_read -s -p "  Confirm password:  " jf_pass2; echo
    [ "$jf_pass" = "$jf_pass2" ] && break
    warn "Passwords don't match — try again"
  done
  ok "Jellyfin: $jf_user"
  echo ""

  prompt_read -p "  qBittorrent username [admin]: " qbit_user
  qbit_user="${qbit_user:-admin}"
  while true; do
    prompt_read -s -p "  qBittorrent password: " qbit_pass; echo
    [ -z "$qbit_pass" ] && warn "Password cannot be empty" && continue
    prompt_read -s -p "  Confirm password:     " qbit_pass2; echo
    [ "$qbit_pass" = "$qbit_pass2" ] && break
    warn "Passwords don't match — try again"
  done
  ok "qBittorrent: $qbit_user"

  write_credentials_to_config "$path" "$jf_user" "$jf_pass" "$qbit_user" "$qbit_pass"
  ok "Credentials saved to config.toml"
}

# Every curl is bounded: a service that accepts a connection and never
# answers can't hang setup. A later --max-time on the command line wins
# (curl keeps the last value), e.g. for large downloads.
curl() { command curl --connect-timeout 5 --max-time "${CURL_MAX_TIME:-60}" "$@"; }

cfg() { echo "$CONFIG_JSON" | jq -r "$1"; }
# cfg_bool <path> <default>: "true"/"false"; only a missing value gets the
# default (jq's // would also replace an explicit false)
cfg_bool() { echo "$CONFIG_JSON" | jq -r --argjson d "$2" "if $1 == null then \$d else ($1 == true) end"; }
TIMEZONE_PATH='(.timezone // .qbittorrent.timezone)'
# Every key, type, range and combination (mediaserver/validate.py), all
# problems at once; stops before anything changes
validate_config_semantics() {
  py validate-config || exit 1
}

# The parts already in Python (mediaserver/): python3 -m mediaserver <command>
py() { PYTHONPATH="$SCRIPT_DIR" MEDIA_DIR="$MEDIA_DIR" PYTHONDONTWRITEBYTECODE=1 python3 -m mediaserver "$@"; }



# Write a state record atomically (temp file in the same folder, then
# rename): an interruption leaves the old or the new version, never half.
# Records that describe work in progress must be written with this.
write_atomic() {  # file content
  local tmp
  mkdir -p "$(dirname "$1")" || return 1
  tmp="$1.tmp.$$"
  (umask 077 && printf '%s\n' "$2" > "$tmp") && mv -f "$tmp" "$1"
}

# ─── Operation lock ──────────────────────────────────────────────
# Install, update, restore, backup, uninstall, restart and the e2e test all
# change the same services; only one may run at a time. The lock is a
# directory (mkdir is atomic) holding the owner's PID; a lock whose owner is
# gone is stale and taken over. netwatch leaves Cleanuparr alone while it's
# held.
lock_dir() { printf '%s/lock' "$STATE_DIR"; }
acquire_lock() {
  local dir owner
  dir=$(lock_dir)
  mkdir -p "$STATE_DIR"
  if ! mkdir "$dir" 2>/dev/null; then
    owner=$(cat "$dir/pid" 2>/dev/null || true)
    if [ -n "$owner" ] && [ "$owner" != "$$" ] && kill -0 "$owner" 2>/dev/null; then
      err "Another media-server operation is running (PID $owner: $(ps -o command= -p "$owner" 2>/dev/null | cut -c1-70)). Wait for it to finish, then try again."
    fi
    # The owner is gone (or it's us): take it over
    rm -rf "$dir"
    mkdir "$dir" 2>/dev/null || err "Couldn't take the operation lock ($dir)"
  fi
  printf '%s\n' "$$" > "$dir/pid"
  LOCK_HELD=1
}
release_lock() {
  [ "${LOCK_HELD:-}" = 1 ] || return 0
  [ "$(cat "$(lock_dir)/pid" 2>/dev/null)" = "$$" ] && rm -rf "$(lock_dir)"
  LOCK_HELD=""
  return 0
}
