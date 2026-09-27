#!/usr/bin/env bash
# Download clients: qBittorrent, SABnzbd, Unpackerr.

configure_qbittorrent() {
  info "Configuring qBittorrent..."

  # Requests from this Mac skip qBittorrent's login (bypass_local_auth),
  # which is how setup, the *arr apps and the dashboard reach it
  local try_user try_pass
  QBIT_COOKIE=""
  for try in "$QBIT_USER|$QBIT_PASS" "$(creds_get qbittorrent username)|$(creds_get qbittorrent password)"; do
    IFS='|' read -r try_user try_pass <<< "$try"
    [ -n "$try_pass" ] || continue
    QBIT_COOKIE=$(api_retry curl -sf --max-time 20 -c - "$QBIT_URL/api/v2/auth/login" \
      --data-urlencode "username=$try_user" --data-urlencode "password=$try_pass" 2>/dev/null | extract_qbit_cookie || echo "")
    [ -n "$QBIT_COOKIE" ] && break
  done
  if [ -z "$QBIT_COOKIE" ]; then
    warn "Could not log in to qBittorrent (check qbittorrent.password in $CONFIG_FILE)"
    return 0
  fi
  ok "Logged in"

  # auto_tmm_enabled: save each download in its category folder
  # (complete/radarr, …) instead of the default folder
  local up_kib
  up_kib=$(cfg '.downloads.upload_limit_kib // 100')
  QBIT_PREFS=$(jq -nc \
    --arg user "$QBIT_USER" \
    --arg save "$DL_COMPLETE" --arg temp "$DL_INCOMPLETE" \
    --argjson ratio "$SEED_RATIO" --argjson seed_time "$SEED_TIME" \
    --arg bind "${ADMIN_BIND:-0.0.0.0}" --argjson up "$up_kib" \
    '{web_ui_username:$user,
      save_path:$save, temp_path:$temp, temp_path_enabled:true,
      web_ui_address:(if $bind == "0.0.0.0" then "*" else $bind end),
      web_ui_port:8081, max_ratio:$ratio, max_seeding_time:$seed_time,
      auto_tmm_enabled:true,
      up_limit:($up * 1024), web_ui_csrf_protection_enabled:true,
      bypass_local_auth:true, bypass_auth_subnet_whitelist_enabled:false}')
  local old_address
  old_address=$(curl -sf "$QBIT_URL/api/v2/app/preferences" -b "$QBIT_COOKIE" 2>/dev/null | jq -r '.web_ui_address // empty' || true)
  if api_retry curl -sf -o /dev/null "$QBIT_URL/api/v2/app/setPreferences" -b "$QBIT_COOKIE" \
      --data-urlencode "json=$QBIT_PREFS" 2>/dev/null; then
    ok "Preferences set (upload limit: $([ "$up_kib" = 0 ] && echo none || echo "$up_kib KiB/s"))"
  else
    warn "Could not set qBittorrent's preferences (retried next run)"
  fi
  set_qbittorrent_password
  # A new listen address only takes effect after a restart
  if [ -n "$old_address" ] && [ "$old_address" != "$(jq -r .web_ui_address <<< "$QBIT_PREFS")" ]; then
    svc_restart qbittorrent >/dev/null 2>&1 && wait_for "qBittorrent" "$QBIT_URL" && ok "qBittorrent restarted to listen on ${ADMIN_BIND:-0.0.0.0}"
  fi

  for cat in sonarr radarr; do
    curl -sf -o /dev/null "$QBIT_URL/api/v2/torrents/createCategory" \
      -b "$QBIT_COOKIE" \
      --data-urlencode "category=$cat" --data-urlencode "savePath=$DL_COMPLETE/$cat" 2>/dev/null && ok "Category: $cat" || \
    curl -sf -o /dev/null "$QBIT_URL/api/v2/torrents/editCategory" \
      -b "$QBIT_COOKIE" \
      --data-urlencode "category=$cat" --data-urlencode "savePath=$DL_COMPLETE/$cat" 2>/dev/null && ok "Category: $cat (updated)" || true
  done
}

QBIT_INI="$CONFIG_DIR/qbittorrent/qBittorrent/config/qBittorrent.ini"

# True if qBittorrent.ini holds <password>'s hash (qBittorrent writes it as
# soon as the password changes). Logging in can't tell: requests from this
# Mac skip the password.
qbit_password_is() {
  python3 - "$QBIT_INI" "$1" << 'PY'
import base64, hashlib, re, sys
path, password = sys.argv[1:]
try:
    text = open(path).read()
except OSError:
    sys.exit(1)
m = re.search(r'WebUI\\Password_PBKDF2="?@ByteArray\(([^:]+):([^)]+)\)', text)
if not m:
    sys.exit(1)
salt, key = base64.b64decode(m.group(1)), base64.b64decode(m.group(2))
sys.exit(0 if hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, len(key)) == key else 1)
PY
}

# Write the Web UI login into qBittorrent.ini (PBKDF2-SHA512, like
# qBittorrent itself); qBittorrent must not be running
qbit_ini_set_login() {  # user pass [address]
  python3 - "$QBIT_INI" "$1" "$2" "${3:-}" << 'PY'
import base64, hashlib, os, sys
path, user, password, bind = sys.argv[1:]
salt = os.urandom(16)
key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, 64)
prefs = {
    "WebUI\\Username": user,
    "WebUI\\Password_PBKDF2": f'"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(key).decode()})"',
}
if bind:
    prefs.update({"WebUI\\Address": bind, "WebUI\\Port": "8081"})
lines = open(path).read().splitlines() if os.path.exists(path) else []
if not any(l.strip() == "[LegalNotice]" for l in lines):
    lines = ["[LegalNotice]", "Accepted=true", ""] + lines
if not any(l.strip() == "[Preferences]" for l in lines):
    lines += ["", "[Preferences]"]
lines = [l for l in lines if l.split("=", 1)[0] not in prefs]
i = lines.index("[Preferences]") + 1
lines[i:i] = [f"{k}={v}" for k, v in prefs.items()]
with open(path, "w") as f:
    f.write("\n".join(lines) + "\n")
os.chmod(path, 0o600)
PY
}

# Set the Web UI password from config.toml and record it once qBittorrent.ini
# shows it. Its API refuses passwords under 6 characters; those are written
# to qBittorrent.ini while it's stopped.
set_qbittorrent_password() {
  if creds_match qbittorrent "$QBIT_USER" "$QBIT_PASS" && qbit_password_is "$QBIT_PASS"; then
    ok "qBittorrent login: $QBIT_USER"
    return 0
  fi
  if [ "${#QBIT_PASS}" -ge 6 ]; then
    curl -sf -o /dev/null "$QBIT_URL/api/v2/app/setPreferences" -b "$QBIT_COOKIE" \
      --data-urlencode "json=$(jq -nc --arg p "$QBIT_PASS" '{web_ui_password:$p}')" 2>/dev/null || true
  elif svc_stop qbittorrent; then
    qbit_ini_set_login "$QBIT_USER" "$QBIT_PASS"
    launchctl bootstrap "$LAUNCHD_DOMAIN" "$(svc_plist qbittorrent)"
    wait_for "qBittorrent" "$QBIT_URL" || true
  fi
  local start=$SECONDS
  until qbit_password_is "$QBIT_PASS"; do
    [ $((SECONDS - start)) -ge 15 ] && { warn "qBittorrent's password didn't change (retried next run)"; return 0; }
    sleep 1
  done
  creds_set qbittorrent "$QBIT_USER" "$QBIT_PASS"
  ok "qBittorrent login set: $QBIT_USER"
}

configure_sabnzbd() {
  if [ -n "$SABNZBD_KEY" ]; then
    info "Configuring SABnzbd..."

    # Set download directories
    local dir_ok=true
    sab_set misc complete_dir "$DOWNLOADS_DIR/usenet/complete" || dir_ok=false
    sab_set misc download_dir "$DOWNLOADS_DIR/usenet/incomplete" || dir_ok=false
    if [ "$dir_ok" = true ]; then ok "Directories: $DOWNLOADS_DIR/usenet/{complete,incomplete}"
    else warn "SABnzbd didn't accept its download folders (retried next run; see $LOG_DIR/sabnzbd.log)"; fi

    # Create categories
    EXISTING_CATS=$(curl -sf "$SABNZBD_URL/api?mode=get_cats&apikey=$SABNZBD_KEY&output=json" 2>/dev/null | jq -r '.categories[]' 2>/dev/null || echo "")
    for cat in sonarr radarr; do
      if ! echo "$EXISTING_CATS" | grep -q "^${cat}$"; then
        curl -sf "$SABNZBD_URL/api?mode=set_config&section=categories&keyword=$cat&apikey=$SABNZBD_KEY&dir=$cat&output=json" >/dev/null 2>&1 && \
          ok "Category: $cat" || warn "Could not create category: $cat"
      else
        ok "Category: $cat"
      fi
    done
  fi
}

# Usenet providers ([[usenet_providers]]) as SABnzbd servers. set_config on
# the servers section creates or updates one (SABnzbd answers "not
# implemented" to name=set_server). enable = false switches an existing
# server off rather than skipping it.
configure_usenet_providers() {
  PROVIDER_COUNT=$(cfg '.usenet_providers | length' 2>/dev/null || echo "0")
  [[ "$PROVIDER_COUNT" =~ ^[0-9]+$ ]] || PROVIDER_COUNT=0
  [ "$PROVIDER_COUNT" -gt 0 ] && [ -n "$SABNZBD_KEY" ] || return 0
  info "Configuring SABnzbd usenet providers..."
  local servers i name resp
  servers=$(curl -sf "$SABNZBD_URL/api?mode=get_config&section=servers&apikey=$SABNZBD_KEY&output=json" 2>/dev/null | \
    jq -c '[.config.servers[]? | {name, enable}]' 2>/dev/null || echo "[]")
  for i in $(seq 0 $((PROVIDER_COUNT - 1))); do
    name=$(cfg ".usenet_providers[$i].name")
    if [ "$(cfg ".usenet_providers[$i].enable")" != "true" ]; then
      if jq -e --arg n "$name" 'any(.[]; .name == $n and .enable == 1)' <<< "$servers" >/dev/null; then
        sab_set servers enable 0 "$name" && ok "$name disabled" || warn "Could not disable $name in SABnzbd"
      fi
      continue
    fi
    resp=$(api_retry curl -sf "$SABNZBD_URL/api" \
      --data-urlencode "mode=set_config" --data-urlencode "section=servers" \
      --data-urlencode "keyword=$name" \
      --data-urlencode "host=$(cfg ".usenet_providers[$i].host")" \
      --data-urlencode "port=$(cfg ".usenet_providers[$i].port")" \
      --data-urlencode "ssl=$([ "$(cfg ".usenet_providers[$i].ssl")" = true ] && echo 1 || echo 0)" \
      --data-urlencode "username=$(cfg ".usenet_providers[$i].username")" \
      --data-urlencode "password=$(cfg ".usenet_providers[$i].password")" \
      --data-urlencode "connections=$(cfg ".usenet_providers[$i].connections")" \
      --data-urlencode "enable=1" \
      --data-urlencode "apikey=$SABNZBD_KEY" --data-urlencode "output=json" 2>/dev/null || true)
    if jq -e --arg n "$name" 'any(.config.servers[]?; .name == $n and .enable == 1)' <<< "$resp" >/dev/null 2>&1; then
      ok "$name ($(cfg ".usenet_providers[$i].host"):$(cfg ".usenet_providers[$i].port"))"
    else
      warn "Could not add $name to SABnzbd ($(jq -r '.error // "no answer"' <<< "$resp" 2>/dev/null || echo "no answer"))"
    fi
  done
}

# One SABnzbd setting via its API, retried; non-zero if it never took
sab_set() {  # section keyword value [server-name]
  if [ -n "${4:-}" ]; then
    # A server's setting: the server is the keyword, the setting a field
    api_retry curl -sf -o /dev/null "$SABNZBD_URL/api" --data-urlencode "mode=set_config" \
      --data-urlencode "section=$1" --data-urlencode "keyword=$4" --data-urlencode "$2=$3" \
      --data-urlencode "apikey=$SABNZBD_KEY" --data-urlencode "output=json" 2>/dev/null
    return
  fi
  api_retry curl -sf -o /dev/null "$SABNZBD_URL/api" --data-urlencode "mode=set_config" \
    --data-urlencode "section=$1" --data-urlencode "keyword=$2" --data-urlencode "value=$3" \
    --data-urlencode "apikey=$SABNZBD_KEY" --data-urlencode "output=json" 2>/dev/null
}

# SABnzbd's login form: 303 to the app when the login works
sab_login_works() {
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$SABNZBD_URL/login/" \
    --data-urlencode "username=$1" --data-urlencode "password=$2" 2>/dev/null)" = "303" ]
}

configure_sabnzbd_auth() {
  [ -n "$SABNZBD_KEY" ] || return 0
  if creds_match sabnzbd "$JELLYFIN_USER" "$JELLYFIN_PASS" && sab_login_works "$JELLYFIN_USER" "$JELLYFIN_PASS"; then
    ok "SABnzbd login: $JELLYFIN_USER"
    return 0
  fi
  if sab_set misc username "$JELLYFIN_USER" && sab_set misc password "$JELLYFIN_PASS" && \
     sab_login_works "$JELLYFIN_USER" "$JELLYFIN_PASS"; then
    creds_set sabnzbd "$JELLYFIN_USER" "$JELLYFIN_PASS"
    ok "SABnzbd login set: $JELLYFIN_USER"
  else
    warn "Could not set SABnzbd's login (retried next run; see $LOG_DIR/sabnzbd.log)"
  fi
}

configure_unpackerr() {
  info "Writing Unpackerr config..."

  UNPACKERR_CONF="$CONFIG_DIR/unpackerr/unpackerr.conf"
  mkdir -p "$(dirname "$UNPACKERR_CONF")"

  UNPACKERR_NEW=$(cat << UNPACKEOF
## Unpackerr — auto-generated by setup.sh

[[sonarr]]
url = "$SONARR_INTERNAL"
api_key = "$SONARR_KEY"
paths = ["$DOWNLOADS_DIR"]

[[radarr]]
url = "$RADARR_INTERNAL"
api_key = "$RADARR_KEY"
paths = ["$DOWNLOADS_DIR"]
UNPACKEOF
  )

  # It holds the Sonarr/Radarr API keys
  [ -f "$UNPACKERR_CONF" ] && chmod 600 "$UNPACKERR_CONF"
  if [ ! -f "$UNPACKERR_CONF" ] || [ "$(cat "$UNPACKERR_CONF")" != "$UNPACKERR_NEW" ]; then
    (umask 077 && printf '%s\n' "$UNPACKERR_NEW" > "$UNPACKERR_CONF")
    ok "Config written"
    svc_restart unpackerr >/dev/null 2>&1 && ok "Unpackerr restarted with new config" || true
  else
    ok "Config unchanged"
  fi
}
