#!/usr/bin/env bash
# Sonarr / Radarr.

configure_arr() {
  local name="$1" url="$2" key="$3" root_folder="$4" cat_field="$5" api_ver="${6:-v3}"
  info "Configuring $name..."
  local H="X-Api-Key: $key"

  # Remove stale root folders (e.g. /downloads) and ensure only the correct one exists
  EXISTING_ROOTS=$(api_retry api GET "$url/api/$api_ver/rootfolder" -H "$H" 2>/dev/null || echo "[]")
  while read -r stale_id; do
    [ -n "$stale_id" ] && api DELETE "$url/api/$api_ver/rootfolder/$stale_id" -H "$H" >/dev/null 2>&1 && \
      ok "Removed stale root folder (id: $stale_id)"
  done < <(echo "$EXISTING_ROOTS" | jq -r '.[] | select(.path != "'"$root_folder"'") | .id' 2>/dev/null)

  if echo "$EXISTING_ROOTS" | jq -r '.[].path' 2>/dev/null | grep -q "^${root_folder}$"; then
    ok "Root folder: $root_folder"
  else
    api_retry api POST "$url/api/$api_ver/rootfolder" -H "$H" -d "{\"path\":\"$root_folder\"}" >/dev/null && \
      ok "Root folder: $root_folder" || warn "Could not add root folder"
  fi

  EXISTING_DL=$(api GET "$url/api/$api_ver/downloadclient" -H "$H" | jq -r '.[].name' 2>/dev/null || echo "")

  if ! echo "$EXISTING_DL" | grep -q "qBittorrent"; then
    QBIT_DL_JSON=$(jq -nc --arg u "$QBIT_USER" --arg p "$QBIT_PASS" --arg cf "$cat_field" --arg cn "$name" \
      '{name:"qBittorrent",implementation:"QBittorrent",configContract:"QBittorrentSettings",
        enable:true,protocol:"torrent",priority:1,
        fields:[{name:"host",value:"qbittorrent"},{name:"port",value:8081},
          {name:"username",value:$u},{name:"password",value:$p},
          {name:$cf,value:$cn}]}')
    api POST "$url/api/$api_ver/downloadclient" -H "$H" -d "$QBIT_DL_JSON" >/dev/null 2>&1 && \
      ok "qBittorrent connected (category: $name)" || warn "Could not add qBittorrent"
  else ok "qBittorrent connected"; fi

  if [ -n "$SABNZBD_KEY" ] && ! echo "$EXISTING_DL" | grep -q "SABnzbd"; then
    api POST "$url/api/$api_ver/downloadclient" -H "$H" -d '{
      "name":"SABnzbd","implementation":"Sabnzbd","configContract":"SabnzbdSettings",
      "enable":true,"protocol":"usenet","priority":2,
      "fields":[{"name":"host","value":"sabnzbd"},{"name":"port","value":8080},
        {"name":"apiKey","value":"'"$SABNZBD_KEY"'"},{"name":"'"$cat_field"'","value":"'"$name"'"}]
    }' >/dev/null 2>&1 && ok "SABnzbd connected (category: $name)" || warn "Could not add SABnzbd"
  elif [ -n "$SABNZBD_KEY" ]; then ok "SABnzbd connected"; fi

  # Add Jellyfin notification connection (triggers library scan on import/upgrade)
  if [ -n "$JELLYFIN_API_KEY" ]; then
    EXISTING_NOTIF=$(api GET "$url/api/$api_ver/notification" -H "$H" | jq -r '.[].name' 2>/dev/null || echo "")
    if ! echo "$EXISTING_NOTIF" | grep -q "^Jellyfin$"; then
      api POST "$url/api/$api_ver/notification" -H "$H" -d '{
        "name":"Jellyfin","implementation":"MediaBrowser","configContract":"MediaBrowserSettings",
        "enable":true,"onDownload":true,"onUpgrade":true,"onRename":true,
        "fields":[{"name":"host","value":"jellyfin"},{"name":"port","value":8096},
          {"name":"useSsl","value":false},{"name":"apiKey","value":"'"$JELLYFIN_API_KEY"'"},
          {"name":"updateLibrary","value":true}]
      }' >/dev/null 2>&1 && ok "Jellyfin notification connected" || warn "Could not add Jellyfin notification"
    else ok "Jellyfin notification connected"; fi
  fi

  # Configure web UI authentication (Sonarr v4 / Radarr v5 enable auth by default)
  HOST_CONFIG=$(api GET "$url/api/$api_ver/config/host" -H "$H" 2>/dev/null || echo "")
  if [ -n "$HOST_CONFIG" ] && [ "$HOST_CONFIG" != "null" ]; then
    CURRENT_AUTH_USER=$(echo "$HOST_CONFIG" | jq -r '.username // empty' 2>/dev/null)
    if [ -z "$CURRENT_AUTH_USER" ]; then
      HOST_ID=$(echo "$HOST_CONFIG" | jq -r '.id' 2>/dev/null)
      UPDATED_HOST=$(echo "$HOST_CONFIG" | jq -c \
        --arg user "$JELLYFIN_USER" --arg pass "$JELLYFIN_PASS" \
        '.authenticationMethod = "forms" | .username = $user | .password = $pass | .passwordConfirmation = $pass | .authenticationRequired = "enabled"' 2>/dev/null)
      api PUT "$url/api/$api_ver/config/host/$HOST_ID" -H "$H" -d "$UPDATED_HOST" >/dev/null 2>&1 && \
        ok "Auth set: $JELLYFIN_USER" || warn "Could not set authentication"
    else
      ok "Auth: $CURRENT_AUTH_USER"
    fi
  fi
}

enable_unknown_quality() {
  local url="$1" key="$2" api_ver="${3:-v3}"
  local H="X-Api-Key: $key"
  local PROFILE UNKNOWN_ALLOWED UPDATED
  PROFILE=$(api GET "$url/api/$api_ver/qualityprofile/1" -H "$H" 2>/dev/null || echo "")
  { [ -z "$PROFILE" ] || [ "$PROFILE" = "null" ]; } && return
  UNKNOWN_ALLOWED=$(echo "$PROFILE" | jq '[.items[] | select(.quality.id == 0) | .allowed][0]' 2>/dev/null)
  if [ "$UNKNOWN_ALLOWED" = "false" ]; then
    UPDATED=$(echo "$PROFILE" | jq '.items = [.items[] | if (.quality.id == 0) then .allowed = true else . end]')
    api PUT "$url/api/$api_ver/qualityprofile/1" -H "$H" -d "$UPDATED" >/dev/null 2>&1 && \
      ok "Quality: enabled Unknown quality" || true
  fi
}

configure_arrs() {
  [ -n "$SONARR_KEY" ]       && configure_arr "sonarr"       "$SONARR_URL"       "$SONARR_KEY"       "/media/tv"    "tvCategory"
  [ -n "$SONARR_ANIME_KEY" ] && configure_arr "sonarr-anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY" "/media/anime" "tvCategory"
  [ -n "$RADARR_KEY" ]       && configure_arr "radarr"       "$RADARR_URL"       "$RADARR_KEY"       "/media/movies" "movieCategory"

  [ -n "$SONARR_KEY" ]       && enable_unknown_quality "$SONARR_URL"       "$SONARR_KEY"
  [ -n "$SONARR_ANIME_KEY" ] && enable_unknown_quality "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY"
  [ -n "$RADARR_KEY" ]       && enable_unknown_quality "$RADARR_URL"       "$RADARR_KEY"
}

