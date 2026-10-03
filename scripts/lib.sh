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

# ─── Credentials applied to each service ─────────────────────────
# ~/media/.state/credentials.json (0600, like config.toml) records, per
# service, the login it last verified working. A service's entry only
# advances after its new login was checked, so an interrupted or failed
# change is retried on the next run, and services that need the current
# password to set a new one (Jellyfin) still have it.
creds_file() { printf '%s/credentials.json' "$STATE_DIR"; }

# The record lives in the file only (no copy in memory): Python steps
# (mediaserver/creds.py) read and write the same file during an install.
# creds_load upgrades the older format (one shared jellyfin/qbittorrent
# entry): the shared login is assumed for the services it covered, except
# Cleanuparr, whose password setup may not have applied.
creds_read() {
  local f
  f=$(creds_file)
  [ -f "$f" ] || { echo '{"version":2,"services":{}}'; return 0; }
  jq -c '
    if .version == 2 then .
    else {version: 2, services: (
      (if .jellyfin then
        reduce ("jellyfin", "sonarr", "radarr", "prowlarr", "bazarr", "sabnzbd") as $s ({}; .[$s] = $jf)
       else {} end) as $shared
      | $shared + (if .qbittorrent then {qbittorrent: .qbittorrent} else {} end))}
    end' --argjson jf "$(jq -c '.jellyfin // null' "$f" 2>/dev/null || echo null)" "$f" 2>/dev/null || {
    warn "$(creds_file) is unreadable; every service's login will be re-applied" >&2
    echo '{"version":2,"services":{}}'
  }
}
creds_load() { creds_read >/dev/null; }

# creds_get <service> <username|password>; empty if not recorded
creds_get() { creds_read | jq -r --arg s "$1" --arg f "$2" '.services[$s][$f] // ""'; }

# True if <service>'s recorded login is <user>/<pass>
creds_match() {
  [ "$(creds_get "$1" username)" = "$2" ] && [ "$(creds_get "$1" password)" = "$3" ]
}

# Record <service>'s verified login and write the file atomically
creds_set() {
  local f tmp record
  f=$(creds_file)
  record=$(creds_read | jq -c --arg s "$1" --arg u "$2" --arg p "$3" '.services[$s] = {username: $u, password: $p}')
  mkdir -p "$STATE_DIR"
  tmp="$f.tmp.$$"
  (umask 077 && jq . <<< "$record" > "$tmp") && mv -f "$tmp" "$f"
}

# *arr/Prowlarr form login: 302 to the app on success, back to /login?…loginFailed on failure
arr_login_works() {  # url user pass
  local loc
  loc=$(curl -s -o /dev/null -w '%{redirect_url}' --max-time 15 -X POST "$1/login" \
    --data-urlencode "username=$2" --data-urlencode "password=$3" 2>/dev/null) || return 1
  [ -n "$loc" ] && [[ "$loc" != *loginFailed* ]]
}

# *arr/Prowlarr web login: the API key allows setting it without the old
# password. Applied when the record or the app's settings differ, then
# checked by logging in; only a working login is recorded.
set_arr_login() {
  local label="$1" url="$2" key="$3" api_ver="${4:-v3}" svc="$5" H="X-Api-Key: $3" host id
  host=$(api GET "$url/api/$api_ver/config/host" -H "$H") || { warn "$label: could not read its login settings"; return 0; }
  if creds_match "$svc" "$JELLYFIN_USER" "$JELLYFIN_PASS" && \
     jq -e --arg u "$JELLYFIN_USER" '.username == $u and .authenticationMethod == "forms"' <<< "$host" >/dev/null; then
    ok "$label login: $JELLYFIN_USER"
    return 0
  fi
  id=$(jq -r '.id' <<< "$host")
  api PUT "$url/api/$api_ver/config/host/$id" -H "$H" -d "$(jq -c --arg user "$JELLYFIN_USER" --arg pass "$JELLYFIN_PASS" \
    '.authenticationMethod = "forms" | .authenticationRequired = "enabled" | .username = $user | .password = $pass | .passwordConfirmation = $pass' <<< "$host")" >/dev/null || \
    { warn "$label: could not set its login (retried next run)"; return 0; }
  if arr_login_works "$url" "$JELLYFIN_USER" "$JELLYFIN_PASS"; then
    creds_set "$svc" "$JELLYFIN_USER" "$JELLYFIN_PASS"
    ok "$label login set: $JELLYFIN_USER"
  else
    warn "$label: the new login doesn't work yet (retried next run)"
  fi
}

# Jellyfin 12 only accepts the Authorization header (no X-Emby-Token)
jf_auth() { printf 'Authorization: MediaBrowser Token="%s"' "$1"; }

# Every curl is bounded: a service that accepts a connection and never
# answers can't hang setup. A later --max-time on the command line wins
# (curl keeps the last value), e.g. for large downloads.
curl() { command curl --connect-timeout 5 --max-time "${CURL_MAX_TIME:-60}" "$@"; }

# HTTP status of a request ("000" if there was no answer), for telling
# "not found" (404) apart from an unreachable service
api_status() {
  local method="$1" url="$2"; shift 2
  curl -s -o /dev/null -w '%{http_code}' -X "$method" "$url" "$@" 2>/dev/null || true
}

api() {
  local method="$1" url="$2"; shift 2
  curl -sf --connect-timeout 5 --max-time "${API_MAX_TIME:-60}" -X "$method" "$url" -H "Content-Type: application/json" "$@" 2>/dev/null
}

# wait_for <name> <url>; gives up after $WAIT_MAX seconds (default 120)
wait_for() {
  local name="$1" url="$2" max="${WAIT_MAX:-120}" start=$SECONDS code
  printf "   Waiting for %-15s" "$name..."
  while true; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 2 --max-time 10 "$url" 2>/dev/null || echo "000")
    { [ "${code:0:1}" = "2" ] || [ "${code:0:1}" = "3" ]; } && break
    [ $((SECONDS - start)) -ge "$max" ] && echo " timeout!" && return 1
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
# cfg_bool <path> <default>: "true"/"false"; only a missing value gets the
# default (jq's // would also replace an explicit false)
cfg_bool() { echo "$CONFIG_JSON" | jq -r --argjson d "$2" "if $1 == null then \$d else ($1 == true) end"; }
TIMEZONE_PATH='(.timezone // .qbittorrent.timezone)'
# Every key, type, range and combination (mediaserver/validate.py), all
# problems at once; stops before anything changes
validate_config_semantics() {
  py validate-config || exit 1
}

# Setup's Jellyfin API key, written by the jellyfin step (mediaserver),
# for the steps still in bash
load_jellyfin_key() { JELLYFIN_API_KEY=$(cat "$STATE_DIR/dashstatus/jellyfin-key" 2>/dev/null || true); }

# The parts already in Python (mediaserver/): python3 -m mediaserver <command>
py() { PYTHONPATH="$SCRIPT_DIR" MEDIA_DIR="$MEDIA_DIR" PYTHONDONTWRITEBYTECODE=1 python3 -m mediaserver "$@"; }

get_api_key() {
  local f="$CONFIG_DIR/$1/config.xml"
  [ -f "$f" ] && sed -n 's/.*<ApiKey>\(.*\)<\/ApiKey>.*/\1/p' "$f" 2>/dev/null || echo ""
}



# Set named fields on an existing *arr/Prowlarr resource (a download client,
# an application...) so changed passwords, API keys and URLs reach it.
# Secrets read back masked, so the update is sent every run.
sync_resource_fields() {  # label resource-url id fields-json api-key
  local label="$1" base="$2" id="$3" fields="$4" H="X-Api-Key: $5" cur
  cur=$(api GET "$base/$id" -H "$H") || { warn "$label: could not read its settings"; return 0; }
  api PUT "$base/$id?forceSave=true" -H "$H" \
    -d "$(jq -c --argjson f "$fields" '.fields |= map(if $f[.name] != null then .value = $f[.name] else . end)' <<< "$cur")" >/dev/null || \
    warn "$label: could not update its settings"
}


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
