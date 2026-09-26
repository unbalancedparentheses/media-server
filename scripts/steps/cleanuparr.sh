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

configure_cleanuparr() {
  info "Configuring Cleanuparr..."
  local status

  # The first account can be created by anyone until setup completes, so
  # create it now (with the Jellyfin login, like the other admin UIs)
  status=$(api GET "$CLEANUPARR_URL/api/auth/status") || { warn "Cleanuparr is not responding"; return 0; }
  if [ "$(jq -r .setupCompleted <<< "$status")" != "true" ]; then
    if [ "${#JELLYFIN_PASS}" -lt 8 ] || [ "${#JELLYFIN_USER}" -lt 3 ]; then
      warn "Cleanuparr needs a username of 3+ and a password of 8+ characters; set it up at $CLEANUPARR_URL"
      return 0
    fi
    api POST "$CLEANUPARR_URL/api/auth/setup/account" \
      -d "$(jq -nc --arg u "$JELLYFIN_USER" --arg p "$JELLYFIN_PASS" '{username:$u, password:$p}')" >/dev/null 2>&1 || true
    api POST "$CLEANUPARR_URL/api/auth/setup/complete" >/dev/null 2>&1 || true
  fi
  CLEANUPARR_KEY=$(cleanuparr_key)
  [ -n "$CLEANUPARR_KEY" ] || { warn "Could not read Cleanuparr's API key"; return 0; }
  ok "Cleanuparr login: $JELLYFIN_USER"
  sync_cleanuparr_login

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
  if ! cleanuparr_api GET queue-rules/stall | jq -e 'any(.[]; .name == "Stalled")' >/dev/null 2>&1; then
    cleanuparr_api POST queue-rules/stall -d "$(jq -nc --argjson s "$stall_strikes" \
      '{name:"Stalled", enabled:true, maxStrikes:$s, privacyType:"Public", minCompletionPercentage:0,
        maxCompletionPercentage:100, resetStrikesOnProgress:true}')" >/dev/null && \
      ok "Rule: remove public torrents stalled for $stall_strikes checks" || warn "Cleanuparr: could not add the stall rule"
  else
    ok "Rule: stalled downloads"
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

# Follow a password change in config.toml, like the other admin UIs
sync_cleanuparr_login() {
  [ "${CREDS_CHANGED:-false}" = true ] && [ -n "${APPLIED_JF_PASS:-}" ] || return 0
  local current="$APPLIED_JF_PASS"
  if [ "$current" != "$JELLYFIN_PASS" ]; then
    cleanuparr_api PUT account/password \
      -d "$(jq -nc --arg c "$current" --arg n "$JELLYFIN_PASS" '{currentPassword:$c, newPassword:$n}')" >/dev/null && \
      current="$JELLYFIN_PASS" || { warn "Cleanuparr: could not change its password (change it in Settings → Account)"; return 0; }
  fi
  if [ "${APPLIED_JF_USER:-$JELLYFIN_USER}" != "$JELLYFIN_USER" ]; then
    cleanuparr_api PUT account/username \
      -d "$(jq -nc --arg c "$current" --arg n "$JELLYFIN_USER" '{currentPassword:$c, newUsername:$n}')" >/dev/null || \
      warn "Cleanuparr: could not change its username"
  fi
  ok "Cleanuparr login updated"
}
