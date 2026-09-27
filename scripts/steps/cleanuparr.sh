#!/usr/bin/env bash
# Cleanuparr: removes downloads that stall, never get metadata or fail to
# import, blocklists the release in Sonarr/Radarr and searches for another.

# The account's API key, straight from Cleanuparr's user database
cleanuparr_key() {
  sqlite3 "$CONFIG_DIR/cleanuparr/users.db" 'SELECT api_key FROM users LIMIT 1' 2>/dev/null || true
}

cleanuparr_api() {  # method path [json]
  local method="$1" path="$2"
  shift 2
  api "$method" "$CLEANUPARR_URL/api/$path" -H "X-Api-Key: $CLEANUPARR_KEY" "$@"
}

CLEANUPARR_LOCAL_URL="http://127.0.0.1:11011"

configure_cleanuparr() {
  info "Configuring Cleanuparr..."
  local status

  # The first account can be created by anyone until setup completes, so
  # create it right away. Its API wants 8+ characters; a shorter shared
  # password is set directly below.
  status=$(api GET "$CLEANUPARR_URL/api/auth/status") || { warn "Cleanuparr is not responding"; return 0; }
  if [ "$(jq -r .setupCompleted <<< "$status")" != "true" ]; then
    local account_pass="$JELLYFIN_PASS"
    [ "${#account_pass}" -ge 8 ] || account_pass=$(openssl rand -hex 16)
    api POST "$CLEANUPARR_URL/api/auth/setup/account" \
      -d "$(jq -nc --arg u "$JELLYFIN_USER" --arg p "$account_pass" '{username:$u, password:$p}')" >/dev/null 2>&1 || true
    api POST "$CLEANUPARR_URL/api/auth/setup/complete" >/dev/null 2>&1 || true
  fi
  CLEANUPARR_KEY=$(cleanuparr_key)
  [ -n "$CLEANUPARR_KEY" ] || { warn "Could not read Cleanuparr's API key"; return 0; }
  # Always require the login (older versions skipped it on this Mac)
  cleanuparr_require_login
  set_cleanuparr_login

  local app url key existing id
  for app in sonarr radarr; do
    if [ "$app" = sonarr ]; then url="$SONARR_INTERNAL" key="$SONARR_KEY"; else url="$RADARR_INTERNAL" key="$RADARR_KEY"; fi
    [ -n "$key" ] || continue
    existing=$(cleanuparr_api GET "configuration/$app" || echo "{}")
    id=$(jq -r --arg u "$url/" '[.instances[]? | select(.url == $u) | .id][0] // empty' <<< "$existing")
    # Sent every run so a changed API key reaches it
    local body
    body=$(jq -nc --arg n "${app^}" --arg u "$url" --arg k "$key" '{name:$n, url:$u, apiKey:$k, version:(if $n == "Sonarr" then 4 else 6 end), enabled:true}')
    if [ -n "$id" ]; then
      cleanuparr_api PUT "configuration/$app/instances/$id" -d "$body" >/dev/null || warn "Cleanuparr: could not update ${app^}"
    else
      cleanuparr_api POST "configuration/$app/instances" -d "$body" >/dev/null || warn "Cleanuparr: could not add ${app^}"
    fi
    ok "${app^} connected"
  done

  local qbit
  qbit=$(jq -nc --arg u "$QBIT_USER" --arg p "$QBIT_PASS" \
    '{enabled:true, name:"qBittorrent", typeName:"qBittorrent", type:"Torrent", host:"http://localhost:8081", username:$u, password:$p}')
  id=$(cleanuparr_api GET configuration/download_client | jq -r '[.clients[]? | select(.typeName == "qBittorrent") | .id][0] // empty')
  if [ -n "$id" ]; then
    cleanuparr_api PUT "configuration/download_client/$id" -d "$qbit" >/dev/null || warn "Cleanuparr: could not update qBittorrent"
  else
    cleanuparr_api POST configuration/download_client -d "$qbit" >/dev/null || warn "Cleanuparr: could not add qBittorrent"
  fi
  ok "qBittorrent connected"

  # Stalled: 6 strikes at one check every 5 minutes = removed after about
  # 30 minutes without progress; any progress resets the count
  local stall_strikes
  stall_strikes=$(cfg '.cleanuparr.stalled_strikes // 6')
  local rule
  rule=$(cleanuparr_api GET queue-rules/stall | jq -c '[.[] | select(.name == "Stalled")][0] // empty' 2>/dev/null || true)
  if [ -z "$rule" ]; then
    cleanuparr_api POST queue-rules/stall -d "$(jq -nc --argjson s "$stall_strikes" \
      '{name:"Stalled", enabled:true, maxStrikes:$s, privacyType:"Public", minCompletionPercentage:0,
        maxCompletionPercentage:100, resetStrikesOnProgress:true}')" >/dev/null && \
      ok "Rule: remove public torrents stalled for $stall_strikes checks" || warn "Cleanuparr: could not add the stall rule"
  elif [ "$(jq -r '.maxStrikes' <<< "$rule")" != "$stall_strikes" ] || [ "$(jq -r '.enabled' <<< "$rule")" != true ]; then
    cleanuparr_api PUT "queue-rules/stall/$(jq -r .id <<< "$rule")" \
      -d "$(jq -c --argjson s "$stall_strikes" '.maxStrikes = $s | .enabled = true' <<< "$rule")" >/dev/null && \
      ok "Rule: stalled for $stall_strikes checks (updated)" || warn "Cleanuparr: could not update the stall rule"
  else
    ok "Rule: stalled for $stall_strikes checks"
  fi

  local enabled want qc
  enabled=$(cfg_bool .cleanuparr.enabled true)
  qc=$(cleanuparr_api GET configuration/queue_cleaner || echo "{}")
  want=$(jq -c --argjson on "$enabled" '
    .enabled = $on
    # Also: metadata that never arrives (3 checks) and failed imports (3 tries)
    | .downloadingMetadataMaxStrikes = (if .downloadingMetadataMaxStrikes == 0 then 3 else .downloadingMetadataMaxStrikes end)
    | .failedImport.maxStrikes = (if .failedImport.maxStrikes == 0 then 3 else .failedImport.maxStrikes end)
    | .failedImport.ignorePrivate = true
    # No patterns + Exclude = every failed import counts
    | if (.failedImport.patterns | length) == 0 then .failedImport.patternMode = "Exclude" else . end' <<< "$qc")
  if [ "$want" != "$qc" ]; then
    cleanuparr_api PUT configuration/queue_cleaner -d "$want" >/dev/null || warn "Cleanuparr: could not update the queue cleaner"
  fi
  if [ "$enabled" = true ]; then ok "Queue cleaner on (every 5 minutes)"; else ok "Queue cleaner off (cleanuparr.enabled = false)"; fi
}

# Cleanuparr's login API: tokens come back when the login works
cleanuparr_login_works() {
  api POST "$CLEANUPARR_URL/api/auth/login" -d "$(jq -nc --arg u "$1" --arg p "$2" '{username:$u, password:$p}')" | \
    jq -e '.tokens.accessToken // empty' >/dev/null 2>&1
}

# Give Cleanuparr the shared login. Its API needs the current password and
# 8+ characters, so the login is written to its user database (BCrypt, as
# Cleanuparr stores it) while it's stopped, then checked by logging in.
set_cleanuparr_login() {
  if creds_match cleanuparr "$JELLYFIN_USER" "$JELLYFIN_PASS" && cleanuparr_login_works "$JELLYFIN_USER" "$JELLYFIN_PASS"; then
    ok "Cleanuparr login: $JELLYFIN_USER"
    return 0
  fi
  svc_stop cleanuparr || { warn "Cleanuparr didn't stop; its login wasn't changed (retried next run)"; return 0; }
  python3 - "$CONFIG_DIR/cleanuparr/users.db" "$JELLYFIN_USER" "$JELLYFIN_PASS" << 'PY' || warn "Could not write Cleanuparr's login"
import bcrypt, sqlite3, sys
from datetime import datetime, timezone
path, user, password = sys.argv[1:]
# $2a$ like BCrypt.Net; same algorithm as bcrypt's $2b$
digest = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode().replace("$2b$", "$2a$", 1)
db = sqlite3.connect(path)
db.execute("UPDATE users SET username = ?, password_hash = ?, failed_login_attempts = 0, lockout_end = NULL, updated_at = ?",
           (user, digest, datetime.now(timezone.utc).isoformat()))
db.execute("DELETE FROM refresh_tokens")  # sign out existing sessions
db.commit()
PY
  launchctl bootstrap "$LAUNCHD_DOMAIN" "$(svc_plist cleanuparr)"
  wait_for "Cleanuparr" "$CLEANUPARR_URL/health" || true
  if cleanuparr_login_works "$JELLYFIN_USER" "$JELLYFIN_PASS"; then
    creds_set cleanuparr "$JELLYFIN_USER" "$JELLYFIN_PASS"
    ok "Cleanuparr login set: $JELLYFIN_USER"
  else
    warn "Cleanuparr's new login doesn't work (retried next run)"
  fi
}

# Turn off "no login for local addresses" (which also covers the LAN).
# Runs before the services start, so Cleanuparr never listens on a wider
# address with it on; if it can't be turned off, setup stops rather than
# start Cleanuparr unprotected.
cleanuparr_require_login() {
  local db="$CONFIG_DIR/cleanuparr/cleanuparr.db" key general
  [ -f "$db" ] || return 0
  if svc_loaded cleanuparr; then
    key=$(cleanuparr_key)
    general=$(api GET "$CLEANUPARR_LOCAL_URL/api/configuration/general" -H "X-Api-Key: $key" || true)
    if [ -n "$general" ]; then
      [ "$(jq -r '.auth.disableAuthForLocalAddresses' <<< "$general")" = "false" ] && return 0
      api PUT "$CLEANUPARR_LOCAL_URL/api/configuration/general" -H "X-Api-Key: $key" \
        -d "$(jq -c '.auth.disableAuthForLocalAddresses = false' <<< "$general")" >/dev/null && \
        { ok "Cleanuparr: login required again"; return 0; }
    fi
    # Not answering: stop it and change the setting on disk
    svc_stop cleanuparr || err "Cleanuparr didn't stop, so its login requirement couldn't be turned on; stopping here (stop it with 'nix run .#restart -- cleanuparr' and re-run)"
  fi
  sqlite3 "$db" 'UPDATE general_configs SET auth_disable_auth_for_local_addresses = 0 WHERE auth_disable_auth_for_local_addresses = 1' 2>/dev/null || \
    err "Couldn't turn on Cleanuparr's login requirement in $db; stopping here so it doesn't start without one"
  [ "$(sqlite3 "$db" 'SELECT COUNT(*) FROM general_configs WHERE auth_disable_auth_for_local_addresses = 1' 2>/dev/null)" = "0" ] || \
    err "Cleanuparr's login requirement is still off in $db; stopping here"
}
