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
if [ -f "$CONFIG_DIR/sabnzbd/sabnzbd.ini" ]; then
  SABNZBD_KEY=$(sed -n 's/^api_key = *//p' "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null || echo "")
  # Ensure Docker hostname is in the whitelist (prevents 403 from Sonarr/Radarr)
  if ! grep -q "^host_whitelist.*sabnzbd" "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null; then
    sed -i '' 's/^host_whitelist = .*/& sabnzbd/' "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null
    docker restart sabnzbd >/dev/null 2>&1 && ok "SABnzbd: added Docker hostname to whitelist" || true
    sleep 3
  fi
fi

# Try configured password first, fall back to temp password from logs
QBIT_TEMP_PASS=$(docker logs qbittorrent 2>&1 | sed -n 's/.*A temporary password is provided for this session: *//p' | tail -1 || echo "")
[ -z "$QBIT_TEMP_PASS" ] && QBIT_TEMP_PASS="adminadmin"
QBIT_PASS="$QBIT_CONFIGURED_PASS"

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

# Try configured password first, then temp password
QBIT_COOKIE=""
for try_pass in "$QBIT_PASS" "$QBIT_TEMP_PASS"; do
  QBIT_COOKIE=$(curl -sf -c - "$QBIT_URL/api/v2/auth/login" \
    -d "username=$QBIT_USER&password=$try_pass" 2>/dev/null | sed -n 's/.*SID[[:space:]]*//p' || echo "")
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
      \"max_seeding_time\": $SEED_TIME
    }" 2>/dev/null && ok "Preferences + credentials set" || warn "Could not set preferences"

  for cat in sonarr sonarr-anime radarr; do
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
# 2b. SABNZBD — create categories so Sonarr/Radarr can connect
# ═══════════════════════════════════════════════════════════════════
if [ -n "$SABNZBD_KEY" ]; then
  info "Configuring SABnzbd..."

  # Set download directories
  curl -sf "$SABNZBD_URL/api?mode=set_config&section=misc&keyword=complete_dir&value=/downloads/usenet/complete&apikey=$SABNZBD_KEY&output=json" >/dev/null 2>&1
  curl -sf "$SABNZBD_URL/api?mode=set_config&section=misc&keyword=download_dir&value=/downloads/usenet/incomplete&apikey=$SABNZBD_KEY&output=json" >/dev/null 2>&1
  ok "Directories: /downloads/usenet/{complete,incomplete}"

  # Create categories
  EXISTING_CATS=$(curl -sf "$SABNZBD_URL/api?mode=get_cats&apikey=$SABNZBD_KEY&output=json" 2>/dev/null | jq -r '.categories[]' 2>/dev/null || echo "")
  for cat in sonarr sonarr-anime radarr; do
    if ! echo "$EXISTING_CATS" | grep -q "^${cat}$"; then
      curl -sf "$SABNZBD_URL/api?mode=set_config&section=categories&keyword=$cat&apikey=$SABNZBD_KEY&dir=$cat&output=json" >/dev/null 2>&1 && \
        ok "Category: $cat" || warn "Could not create category: $cat"
    else
      ok "Category: $cat"
    fi
  done
fi

# ═══════════════════════════════════════════════════════════════════
# 3. SONARR / RADARR — root folders + download clients
# ═══════════════════════════════════════════════════════════════════
configure_arr() {
  local name="$1" url="$2" key="$3" root_folder="$4" cat_field="$5"
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
}

[ -n "$SONARR_KEY" ]       && configure_arr "sonarr"       "$SONARR_URL"       "$SONARR_KEY"       "/media/tv"    "tvCategory"
[ -n "$SONARR_ANIME_KEY" ] && configure_arr "sonarr-anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY" "/media/anime" "tvCategory"
[ -n "$RADARR_KEY" ]       && configure_arr "radarr"       "$RADARR_URL"       "$RADARR_KEY"       "/media/movies" "movieCategory"

# ═══════════════════════════════════════════════════════════════════
# 4. PROWLARR — connect apps + FlareSolverr + indexers from config
# ═══════════════════════════════════════════════════════════════════
if [ -n "$PROWLARR_KEY" ]; then
  info "Configuring Prowlarr..."
  PH="X-Api-Key: $PROWLARR_KEY"

  EXISTING_APPS=$(api GET "$PROWLARR_URL/api/v1/applications" -H "$PH" | jq -r '.[].name' 2>/dev/null || echo "")

  add_prowlarr_app() {
    local name="$1" impl="$2" url="$3" key="$4" cats="$5"
    if ! echo "$EXISTING_APPS" | grep -q "^${name}$"; then
      api POST "$PROWLARR_URL/api/v1/applications" -H "$PH" -d '{
        "name":"'"$name"'","implementation":"'"$impl"'","configContract":"'"$impl"'Settings",
        "syncLevel":"fullSync",
        "fields":[{"name":"prowlarrUrl","value":"'"$PROWLARR_INTERNAL"'"},
          {"name":"baseUrl","value":"'"$url"'"},{"name":"apiKey","value":"'"$key"'"},
          {"name":"syncCategories","value":['"$cats"']}]
      }' >/dev/null 2>&1 && ok "$name connected" || warn "Could not connect $name"
    else ok "$name connected"; fi
  }

  SONARR_CATS="5000,5010,5020,5030,5040,5045,5050,5090"
  RADARR_CATS="2000,2010,2020,2030,2040,2045,2050,2060,2070,2080,2090"

  [ -n "$SONARR_KEY" ]       && add_prowlarr_app "Sonarr"       "Sonarr" "$SONARR_INTERNAL"       "$SONARR_KEY"       "$SONARR_CATS"
  [ -n "$SONARR_ANIME_KEY" ] && add_prowlarr_app "Sonarr Anime" "Sonarr" "$SONARR_ANIME_INTERNAL" "$SONARR_ANIME_KEY" "$SONARR_CATS"
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
        {"name":"username","value":"admin"},{"name":"password","value":"'"$QBIT_PASS"'"},
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

      # Set name, enable, app profile, and optionally FlareSolverr tag
      if [ "$IDX_FLARE" = "true" ] && [ -n "$FLARESOLVERR_TAG_ID" ]; then
        SCHEMA=$(echo "$SCHEMA" | jq -c --arg name "$IDX_NAME" --argjson tid "$FLARESOLVERR_TAG_ID" \
          '.name = $name | .enable = true | del(.id) | .appProfileId = 1 | .tags = [$tid]' 2>/dev/null)
      else
        SCHEMA=$(echo "$SCHEMA" | jq -c --arg name "$IDX_NAME" \
          '.name = $name | .enable = true | del(.id) | .appProfileId = 1' 2>/dev/null)
      fi

      # Write to temp file to avoid shell argument length limits
      echo "$SCHEMA" > /tmp/prowlarr_indexer.json
      api POST "$PROWLARR_URL/api/v1/indexer" -H "$PH" -d @/tmp/prowlarr_indexer.json >/dev/null 2>&1 && \
        ok "$IDX_NAME added" || warn "Could not add $IDX_NAME"
    done
    rm -f /tmp/prowlarr_indexer.json
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
# 6. BAZARR — connect to Sonarr + Radarr (via config file)
# ═══════════════════════════════════════════════════════════════════
info "Configuring Bazarr..."

BAZARR_CONFIG=""
for f in "$CONFIG_DIR/bazarr/config/config/config.yaml" "$CONFIG_DIR/bazarr/config/config.yaml"; do
  [ -f "$f" ] && BAZARR_CONFIG="$f" && break
done

if [ -n "$BAZARR_CONFIG" ]; then
  ok "Config: $BAZARR_CONFIG"

  # Use python3 to do targeted updates (preserves all existing config)
  python3 - "$BAZARR_CONFIG" "$SONARR_KEY" "$RADARR_KEY" << 'PYEOF'
import sys

config_path = sys.argv[1]
sonarr_key = sys.argv[2]
radarr_key = sys.argv[3]

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

with open(config_path, 'w') as f:
    f.writelines(lines)

print("OK")
PYEOF

  if [ $? -eq 0 ]; then
    ok "Sonarr + Radarr configured"
    docker restart bazarr >/dev/null 2>&1 && ok "Bazarr restarted" || true
  else
    warn "Could not update Bazarr config"
  fi
else
  warn "Bazarr config file not found"
fi

# ═══════════════════════════════════════════════════════════════════
# 7. JELLYSEERR — connect to Jellyfin + Sonarr + Radarr
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
      echo "$UPDATED" > "$JS_SETTINGS"
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
  if echo "$JS_AUTH_RESP" | grep -q "connect.sid"; then
    JS_COOKIE=$(echo "$JS_AUTH_RESP" | sed -n 's/.*connect.sid[[:space:]]*//p')
    ok "Authenticated"
    break
  fi
done
[ -z "$JS_COOKIE" ] && warn "Could not authenticate (check Jellyfin credentials)"

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
      PROFILE=$(api GET "$SONARR_URL/api/v3/qualityprofile" -H "X-Api-Key: $SONARR_KEY" | jq '.[0]' 2>/dev/null)
      PID=$(echo "$PROFILE" | jq '.id // 1' 2>/dev/null || echo 1)
      PNAME=$(echo "$PROFILE" | jq -r '.name // "Any"' 2>/dev/null || echo "Any")
      api POST "$JELLYSEERR_URL/api/v1/settings/sonarr" "${JA[@]}" -d '{
        "name":"Sonarr","hostname":"sonarr","port":8989,"useSsl":false,"apiKey":"'"$SONARR_KEY"'",
        "baseUrl":"","activeProfileId":'"$PID"',"activeProfileName":"'"$PNAME"'","activeDirectory":"/media/tv",
        "is4k":false,"enableSeasonFolders":true,"isDefault":true,"externalUrl":"http://localhost:8989"
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
        "seriesType":"anime","animeSeriesType":"anime"
      }' >/dev/null 2>&1 && ok "Sonarr Anime connected" || warn "Could not add Sonarr Anime"
    }
  else ok "Sonarr already connected"; fi

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
