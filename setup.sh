#!/usr/bin/env bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.json"
CONFIG_DIR="$HOME/media/config"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "Missing config.json — copy config.json.example and fill in your values"
  exit 1
fi

# ─── Read config ─────────────────────────────────────────────────
cfg() { jq -r "$1" "$CONFIG_FILE"; }

JELLYFIN_USER=$(cfg '.jellyfin.username')
JELLYFIN_PASS=$(cfg '.jellyfin.password')
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

SONARR_INTERNAL="http://sonarr:8989"
SONARR_ANIME_INTERNAL="http://sonarr-anime:8989"
RADARR_INTERNAL="http://radarr:7878"
PROWLARR_INTERNAL="http://prowlarr:9696"

# ─── Helpers ─────────────────────────────────────────────────────
info()  { printf "\n\033[1;34m=> %s\033[0m\n" "$*"; }
ok()    { printf "\033[1;32m   ✓ %s\033[0m\n" "$*"; }
warn()  { printf "\033[1;33m   ! %s\033[0m\n" "$*"; }
err()   { printf "\033[1;31m   ✗ %s\033[0m\n" "$*"; }

api() {
  local method="$1" url="$2"; shift 2
  curl -sf -X "$method" "$url" -H "Content-Type: application/json" "$@" 2>/dev/null
}

wait_for() {
  local name="$1" url="$2" max=90 i=0
  printf "   Waiting for %-15s" "$name..."
  while ! curl -sf -o /dev/null --connect-timeout 2 "$url" 2>/dev/null; do
    i=$((i + 1))
    [ "$i" -ge "$max" ] && echo " timeout!" && return 1
    sleep 1
  done
  echo " up"
}

get_api_key() {
  local f="$CONFIG_DIR/$1/config.xml"
  [ -f "$f" ] && sed -n 's/.*<ApiKey>\(.*\)<\/ApiKey>.*/\1/p' "$f" 2>/dev/null || echo ""
}

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

# ═══════════════════════════════════════════════════════════════════
info "Reading API keys..."

SONARR_KEY=$(get_api_key "sonarr")
SONARR_ANIME_KEY=$(get_api_key "sonarr-anime")
RADARR_KEY=$(get_api_key "radarr")
PROWLARR_KEY=$(get_api_key "prowlarr")
SABNZBD_KEY=""
[ -f "$CONFIG_DIR/sabnzbd/sabnzbd.ini" ] && SABNZBD_KEY=$(sed -n 's/^api_key = *//p' "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null || echo "")

QBIT_PASS=$(docker logs qbittorrent 2>&1 | sed -n 's/.*A temporary password is provided for this session: *//p' | tail -1 || echo "")
[ -z "$QBIT_PASS" ] && QBIT_PASS="adminadmin"

[ -n "$SONARR_KEY" ]       && ok "Sonarr:       $SONARR_KEY"       || err "Sonarr key not found"
[ -n "$SONARR_ANIME_KEY" ] && ok "Sonarr Anime: $SONARR_ANIME_KEY" || err "Sonarr Anime key not found"
[ -n "$RADARR_KEY" ]       && ok "Radarr:       $RADARR_KEY"       || err "Radarr key not found"
[ -n "$PROWLARR_KEY" ]     && ok "Prowlarr:     $PROWLARR_KEY"     || err "Prowlarr key not found"
[ -n "$SABNZBD_KEY" ]      && ok "SABnzbd:      $SABNZBD_KEY"      || warn "SABnzbd key not found"
ok "qBittorrent:  admin / $QBIT_PASS"

# ═══════════════════════════════════════════════════════════════════
# 1. QBITTORRENT
# ═══════════════════════════════════════════════════════════════════
info "Configuring qBittorrent..."

QBIT_COOKIE=$(curl -sf -c - "$QBIT_URL/api/v2/auth/login" \
  -d "username=admin&password=$QBIT_PASS" 2>/dev/null | sed -n 's/.*SID[[:space:]]*//p' || echo "")

if [ -n "$QBIT_COOKIE" ]; then
  ok "Logged in"
  curl -sf -o /dev/null "$QBIT_URL/api/v2/app/setPreferences" \
    -b "SID=$QBIT_COOKIE" \
    --data-urlencode "json={
      \"save_path\": \"$DL_COMPLETE\",
      \"temp_path\": \"$DL_INCOMPLETE\",
      \"temp_path_enabled\": true,
      \"web_ui_port\": 8081,
      \"max_ratio\": $SEED_RATIO,
      \"max_seeding_time\": $SEED_TIME
    }" 2>/dev/null && ok "Preferences set (ratio: $SEED_RATIO, seed time: ${SEED_TIME}m)" || warn "Could not set preferences"

  for cat in sonarr sonarr-anime radarr; do
    curl -sf -o /dev/null "$QBIT_URL/api/v2/torrents/createCategory" \
      -b "SID=$QBIT_COOKIE" \
      -d "category=$cat&savePath=$DL_COMPLETE/$cat" 2>/dev/null && ok "Category: $cat" || true
  done
else
  warn "Could not log in"
fi

# ═══════════════════════════════════════════════════════════════════
# 2. JELLYFIN
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
    if echo "$EXISTING_LIBS" | grep -q "^${lib_name}$"; then
      ok "Library '$lib_name' exists"
    else
      encoded=$(printf '%s' "$lib_name" | jq -sRr @uri)
      api POST "$JELLYFIN_URL/Library/VirtualFolders?name=${encoded}&collectionType=$lib_type&refreshLibrary=true" \
        -H "X-Emby-Token: $JELLYFIN_TOKEN" \
        -d "{\"LibraryOptions\":{},\"PathInfos\":[{\"Path\":\"$lib_path\"}]}" && \
        ok "Added library: $lib_name" || warn "Could not add: $lib_name"
    fi
  done

  EXISTING_KEYS=$(api GET "$JELLYFIN_URL/Auth/Keys" -H "X-Emby-Token: $JELLYFIN_TOKEN" 2>/dev/null | jq '.Items | length' 2>/dev/null || echo "0")
  [ "$EXISTING_KEYS" = "0" ] || [ -z "$EXISTING_KEYS" ] && \
    api POST "$JELLYFIN_URL/Auth/Keys?app=MediaServer" -H "X-Emby-Token: $JELLYFIN_TOKEN" >/dev/null 2>&1 || true
  JELLYFIN_API_KEY=$(api GET "$JELLYFIN_URL/Auth/Keys" -H "X-Emby-Token: $JELLYFIN_TOKEN" 2>/dev/null | jq -r '.Items[-1].AccessToken // empty' 2>/dev/null || echo "")
  [ -n "$JELLYFIN_API_KEY" ] && ok "API key: $JELLYFIN_API_KEY"
else
  warn "Could not authenticate"
fi

# ═══════════════════════════════════════════════════════════════════
# 3. SONARR / RADARR — root folders + download clients
# ═══════════════════════════════════════════════════════════════════
configure_arr() {
  local name="$1" url="$2" key="$3" root_folder="$4"
  info "Configuring $name..."
  local H="X-Api-Key: $key"

  EXISTING=$(api GET "$url/api/v3/rootfolder" -H "$H" | jq -r '.[].path' 2>/dev/null || echo "")
  if echo "$EXISTING" | grep -q "^${root_folder}$"; then
    ok "Root folder: $root_folder"
  else
    api POST "$url/api/v3/rootfolder" -H "$H" -d "{\"path\":\"$root_folder\"}" >/dev/null && \
      ok "Root folder: $root_folder" || warn "Could not add root folder"
  fi

  EXISTING_DL=$(api GET "$url/api/v3/downloadclient" -H "$H" | jq -r '.[].name' 2>/dev/null || echo "")

  if ! echo "$EXISTING_DL" | grep -q "qBittorrent"; then
    api POST "$url/api/v3/downloadclient" -H "$H" -d '{
      "name":"qBittorrent","implementation":"QBittorrent","configContract":"QBittorrentSettings",
      "enable":true,"protocol":"torrent",
      "fields":[{"name":"host","value":"qbittorrent"},{"name":"port","value":8081},
        {"name":"username","value":"admin"},{"name":"password","value":"'"$QBIT_PASS"'"},
        {"name":"category","value":"'"$name"'"}]
    }' >/dev/null 2>&1 && ok "qBittorrent connected" || warn "Could not add qBittorrent"
  else ok "qBittorrent connected"; fi

  if [ -n "$SABNZBD_KEY" ] && ! echo "$EXISTING_DL" | grep -q "SABnzbd"; then
    api POST "$url/api/v3/downloadclient" -H "$H" -d '{
      "name":"SABnzbd","implementation":"Sabnzbd","configContract":"SabnzbdSettings",
      "enable":true,"protocol":"usenet",
      "fields":[{"name":"host","value":"sabnzbd"},{"name":"port","value":8080},
        {"name":"apiKey","value":"'"$SABNZBD_KEY"'"},{"name":"category","value":"'"$name"'"}]
    }' >/dev/null 2>&1 && ok "SABnzbd connected" || warn "Could not add SABnzbd"
  elif [ -n "$SABNZBD_KEY" ]; then ok "SABnzbd connected"; fi
}

[ -n "$SONARR_KEY" ]       && configure_arr "sonarr"       "$SONARR_URL"       "$SONARR_KEY"       "/media/tv"
[ -n "$SONARR_ANIME_KEY" ] && configure_arr "sonarr-anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY" "/media/anime"
[ -n "$RADARR_KEY" ]       && configure_arr "radarr"       "$RADARR_URL"       "$RADARR_KEY"       "/media/movies"

# ═══════════════════════════════════════════════════════════════════
# 4. PROWLARR — connect apps + FlareSolverr + indexers from config
# ═══════════════════════════════════════════════════════════════════
if [ -n "$PROWLARR_KEY" ]; then
  info "Configuring Prowlarr..."
  PH="X-Api-Key: $PROWLARR_KEY"

  EXISTING_APPS=$(api GET "$PROWLARR_URL/api/v1/applications" -H "$PH" | jq -r '.[].name' 2>/dev/null || echo "")

  add_prowlarr_app() {
    local name="$1" impl="$2" url="$3" key="$4"
    if ! echo "$EXISTING_APPS" | grep -q "^${name}$"; then
      api POST "$PROWLARR_URL/api/v1/applications" -H "$PH" -d '{
        "name":"'"$name"'","implementation":"'"$impl"'","configContract":"'"$impl"'Settings",
        "syncLevel":"fullSync",
        "fields":[{"name":"prowlarrUrl","value":"'"$PROWLARR_INTERNAL"'"},
          {"name":"baseUrl","value":"'"$url"'"},{"name":"apiKey","value":"'"$key"'"},
          {"name":"syncCategories"}]
      }' >/dev/null 2>&1 && ok "$name connected" || warn "Could not connect $name"
    else ok "$name connected"; fi
  }

  [ -n "$SONARR_KEY" ]       && add_prowlarr_app "Sonarr"       "Sonarr" "$SONARR_INTERNAL"       "$SONARR_KEY"
  [ -n "$SONARR_ANIME_KEY" ] && add_prowlarr_app "Sonarr Anime" "Sonarr" "$SONARR_ANIME_INTERNAL" "$SONARR_ANIME_KEY"
  [ -n "$RADARR_KEY" ]       && add_prowlarr_app "Radarr"       "Radarr" "$RADARR_INTERNAL"       "$RADARR_KEY"

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
      "enable":true,"protocol":"torrent",
      "fields":[{"name":"host","value":"qbittorrent"},{"name":"port","value":8081},
        {"name":"username","value":"admin"},{"name":"password","value":"'"$QBIT_PASS"'"},
        {"name":"category","value":"prowlarr"}]
    }' >/dev/null 2>&1 && ok "qBittorrent connected to Prowlarr" || true
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

      if echo "$EXISTING_INDEXERS" | grep -q "^${IDX_NAME}$"; then
        ok "$IDX_NAME already added"
        continue
      fi

      # Fetch schemas once (cached)
      if [ -z "$SCHEMAS" ]; then
        SCHEMAS=$(api GET "$PROWLARR_URL/api/v1/indexer/schema" -H "$PH" 2>/dev/null || echo "[]")
      fi

      # Find the matching schema
      SCHEMA=$(echo "$SCHEMAS" | jq --arg def "$IDX_DEF" '[.[] | select(.definitionName == $def)] | .[0]' 2>/dev/null)

      if [ -z "$SCHEMA" ] || [ "$SCHEMA" = "null" ]; then
        warn "$IDX_NAME: indexer '$IDX_DEF' not found in Prowlarr schemas"
        continue
      fi

      # Merge user-provided fields into the schema
      USER_FIELDS=$(cfg ".indexers[$i].fields")
      if [ "$USER_FIELDS" != "null" ] && [ "$USER_FIELDS" != "{}" ]; then
        # For each user field, update the matching field in the schema
        SCHEMA=$(echo "$SCHEMA" | jq --argjson uf "$USER_FIELDS" '
          .fields = [.fields[] | if $uf[.name] then .value = $uf[.name] else . end]
        ' 2>/dev/null)
      fi

      # Set the name and enable it
      SCHEMA=$(echo "$SCHEMA" | jq --arg name "$IDX_NAME" '.name = $name | .enable = true | del(.id)' 2>/dev/null)

      api POST "$PROWLARR_URL/api/v1/indexer" -H "$PH" -d "$SCHEMA" >/dev/null 2>&1 && \
        ok "$IDX_NAME added" || warn "Could not add $IDX_NAME"
    done
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# 5. SABNZBD — add usenet providers from config
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
# 6. BAZARR — connect to Sonarr + Radarr
# ═══════════════════════════════════════════════════════════════════
info "Configuring Bazarr..."
sleep 2

BAZARR_API_KEY=""
for f in "$CONFIG_DIR/bazarr/config/config.yaml" "$CONFIG_DIR/bazarr/config/config/config.yaml"; do
  [ -f "$f" ] && BAZARR_API_KEY=$(sed -n 's/^[[:space:]]*apikey: *//p' "$f" 2>/dev/null | head -1) && [ -n "$BAZARR_API_KEY" ] && break
done
for f in "$CONFIG_DIR/bazarr/config/config.ini" "$CONFIG_DIR/bazarr/config.ini"; do
  [ -z "$BAZARR_API_KEY" ] && [ -f "$f" ] && BAZARR_API_KEY=$(sed -n 's/^apikey *= *//p' "$f" 2>/dev/null | head -1) && [ -n "$BAZARR_API_KEY" ] && break
done

if [ -n "$BAZARR_API_KEY" ]; then
  ok "API key: $BAZARR_API_KEY"
  BH="X-API-KEY: $BAZARR_API_KEY"

  [ -n "$SONARR_KEY" ] && api PATCH "$BAZARR_URL/api/system/settings" -H "$BH" -d '{
    "settings":{"sonarr":{"ip":"sonarr","port":8989,"apikey":"'"$SONARR_KEY"'","base_url":"","ssl":false,"series_sync":60,"episodes_sync":60}}
  }' >/dev/null 2>&1 && ok "Sonarr connected" || warn "Could not connect Sonarr"

  [ -n "$RADARR_KEY" ] && api PATCH "$BAZARR_URL/api/system/settings" -H "$BH" -d '{
    "settings":{"radarr":{"ip":"radarr","port":7878,"apikey":"'"$RADARR_KEY"'","base_url":"","ssl":false,"movies_sync":60}}
  }' >/dev/null 2>&1 && ok "Radarr connected" || warn "Could not connect Radarr"

  # Subtitle languages from config
  LANG_JSON=$(cfg '[.subtitles.languages[] | {"name": (if . == "en" then "English" elif . == "es" then "Spanish" elif . == "fr" then "French" elif . == "de" then "German" elif . == "it" then "Italian" elif . == "pt" then "Portuguese" elif . == "ja" then "Japanese" elif . == "ko" then "Korean" elif . == "zh" then "Chinese" else . end), "code2": ., "enabled": true}]')
  PROV_JSON=$(cfg '[.subtitles.providers[]]')

  api PATCH "$BAZARR_URL/api/system/settings" -H "$BH" -d "{
    \"settings\":{\"general\":{\"enabled_providers\":$PROV_JSON}}
  }" >/dev/null 2>&1 && ok "Subtitle providers configured" || true
else
  warn "Bazarr API key not found"
fi

# ═══════════════════════════════════════════════════════════════════
# 7. JELLYSEERR — connect to Jellyfin + Sonarr + Radarr
# ═══════════════════════════════════════════════════════════════════
info "Configuring Jellyseerr..."

JF_HEADER2='X-Emby-Authorization: MediaBrowser Client="setup", Device="script", DeviceId="setup-script", Version="1.0"'

JS_STATUS=$(curl -sf "$JELLYSEERR_URL/api/v1/settings/about" 2>/dev/null || echo "")
if ! echo "$JS_STATUS" | grep -q "version"; then
  api POST "$JELLYSEERR_URL/api/v1/settings/jellyfin" \
    -d '{"hostname":"jellyfin","port":8096,"useSsl":false,"urlBase":""}' >/dev/null 2>&1 && \
    ok "Jellyfin server set" || warn "Could not set Jellyfin"
fi

JS_AUTH=$(api POST "$JELLYSEERR_URL/api/v1/auth/jellyfin" \
  -d "{\"username\":\"$JELLYFIN_USER\",\"password\":\"$JELLYFIN_PASS\",\"hostname\":\"jellyfin\",\"port\":8096,\"useSsl\":false,\"email\":\"admin@media.local\"}" || echo "")

JS_COOKIE=""
if echo "$JS_AUTH" | jq -e '.id' >/dev/null 2>&1; then
  JS_COOKIE=$(curl -sf -c - -X POST "$JELLYSEERR_URL/api/v1/auth/jellyfin" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$JELLYFIN_USER\",\"password\":\"$JELLYFIN_PASS\",\"hostname\":\"jellyfin\",\"port\":8096,\"useSsl\":false,\"email\":\"admin@media.local\"}" 2>/dev/null \
    | sed -n 's/.*connect.sid[[:space:]]*//p' || echo "")
  ok "Authenticated"
fi

if [ -n "$JS_COOKIE" ]; then
  JA=(-b "connect.sid=$JS_COOKIE")

  # Sync & enable Jellyfin libraries
  LIBRARIES=$(api GET "$JELLYSEERR_URL/api/v1/settings/jellyfin/library" "${JA[@]}" 2>/dev/null || echo "[]")
  if echo "$LIBRARIES" | jq -e '.[0]' >/dev/null 2>&1; then
    ENABLED=$(echo "$LIBRARIES" | jq '[.[] | .enabled = true]')
    api POST "$JELLYSEERR_URL/api/v1/settings/jellyfin/library" "${JA[@]}" -d "$ENABLED" >/dev/null 2>&1 && \
      ok "Libraries synced" || true
  fi

  # Add Sonarr
  EXISTING_JS_SONARR=$(api GET "$JELLYSEERR_URL/api/v1/settings/sonarr" "${JA[@]}" 2>/dev/null | jq 'length' 2>/dev/null || echo "0")
  if [ "$EXISTING_JS_SONARR" = "0" ] || [ -z "$EXISTING_JS_SONARR" ]; then
    [ -n "$SONARR_KEY" ] && {
      PID=$(api GET "$SONARR_URL/api/v3/qualityprofile" -H "X-Api-Key: $SONARR_KEY" | jq '.[0].id // 1' 2>/dev/null || echo 1)
      api POST "$JELLYSEERR_URL/api/v1/settings/sonarr" "${JA[@]}" -d '{
        "name":"Sonarr","hostname":"sonarr","port":8989,"useSsl":false,"apiKey":"'"$SONARR_KEY"'",
        "baseUrl":"","activeProfileId":'"$PID"',"activeDirectory":"/media/tv",
        "is4k":false,"enableSeasonFolders":true,"isDefault":true,"externalUrl":"http://localhost:8989"
      }' >/dev/null 2>&1 && ok "Sonarr connected" || warn "Could not add Sonarr"
    }
    [ -n "$SONARR_ANIME_KEY" ] && {
      PID=$(api GET "$SONARR_ANIME_URL/api/v3/qualityprofile" -H "X-Api-Key: $SONARR_ANIME_KEY" | jq '.[0].id // 1' 2>/dev/null || echo 1)
      api POST "$JELLYSEERR_URL/api/v1/settings/sonarr" "${JA[@]}" -d '{
        "name":"Sonarr Anime","hostname":"sonarr-anime","port":8989,"useSsl":false,"apiKey":"'"$SONARR_ANIME_KEY"'",
        "baseUrl":"","activeProfileId":'"$PID"',"activeDirectory":"/media/anime",
        "is4k":false,"enableSeasonFolders":true,"isDefault":false,"externalUrl":"http://localhost:8990",
        "seriesType":"anime","animeSeriesType":"anime"
      }' >/dev/null 2>&1 && ok "Sonarr Anime connected" || warn "Could not add Sonarr Anime"
    }
  else ok "Sonarr already connected"; fi

  # Add Radarr
  EXISTING_JS_RADARR=$(api GET "$JELLYSEERR_URL/api/v1/settings/radarr" "${JA[@]}" 2>/dev/null | jq 'length' 2>/dev/null || echo "0")
  if [ "$EXISTING_JS_RADARR" = "0" ] || [ -z "$EXISTING_JS_RADARR" ]; then
    [ -n "$RADARR_KEY" ] && {
      PID=$(api GET "$RADARR_URL/api/v3/qualityprofile" -H "X-Api-Key: $RADARR_KEY" | jq '.[0].id // 1' 2>/dev/null || echo 1)
      api POST "$JELLYSEERR_URL/api/v1/settings/radarr" "${JA[@]}" -d '{
        "name":"Radarr","hostname":"radarr","port":7878,"useSsl":false,"apiKey":"'"$RADARR_KEY"'",
        "baseUrl":"","activeProfileId":'"$PID"',"activeDirectory":"/media/movies",
        "is4k":false,"isDefault":true,"externalUrl":"http://localhost:7878","minimumAvailability":"released"
      }' >/dev/null 2>&1 && ok "Radarr connected" || warn "Could not add Radarr"
    }
  else ok "Radarr already connected"; fi

  api POST "$JELLYSEERR_URL/api/v1/settings/initialize" "${JA[@]}" >/dev/null 2>&1 || true
  ok "Setup finalized"
else
  warn "Could not authenticate — complete wizard manually at $JELLYSEERR_URL"
fi

# ═══════════════════════════════════════════════════════════════════
# 8. RECYCLARR — generate config with real keys + profile names
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
else
  warn "Missing API keys, skipping"
fi

# ═══════════════════════════════════════════════════════════════════
info "Done! All services connected."
echo ""
echo "  ┌──────────────────────────────────────────────────────────┐"
echo "  │  Dashboard:    http://localhost                          │"
echo "  │  Jellyfin:     http://localhost:8096  ($JELLYFIN_USER)  │"
echo "  │  Jellyseerr:   http://localhost:5055  (request portal)  │"
echo "  └──────────────────────────────────────────────────────────┘"
echo ""
echo "  Connections:"
echo "    Prowlarr  → Sonarr, Sonarr Anime, Radarr, FlareSolverr"
echo "    Sonarr    → qBittorrent, SABnzbd"
echo "    Radarr    → qBittorrent, SABnzbd"
echo "    Bazarr    → Sonarr, Radarr"
echo "    Jellyseerr→ Jellyfin, Sonarr, Sonarr Anime, Radarr"
echo "    Recyclarr → Sonarr, Sonarr Anime, Radarr"
echo ""
echo "  To add indexers, edit config.json and set 'enable: true',"
echo "  then run ./setup.sh again. It's idempotent."
echo ""
