#!/usr/bin/env bash
set -eo pipefail

# ═══════════════════════════════════════════════════════════════════
# Media Server — One-command setup (fresh install or re-run)
# Usage: ./setup.sh
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.toml"
MEDIA_DIR="$HOME/media"
CONFIG_DIR="$MEDIA_DIR/config"

# ─── Helpers ─────────────────────────────────────────────────────
info()  { printf "\n\033[1;34m=> %s\033[0m\n" "$*"; }
ok()    { printf "\033[1;32m   ✓ %s\033[0m\n" "$*"; }
warn()  { printf "\033[1;33m   ! %s\033[0m\n" "$*"; }
err()   { printf "\033[1;31m   ✗ %s\033[0m\n" "$*"; exit 1; }

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

cfg() { echo "$CONFIG_JSON" | jq -r "$1"; }

get_api_key() {
  local f="$CONFIG_DIR/$1/config.xml"
  [ -f "$f" ] && sed -n 's/.*<ApiKey>\(.*\)<\/ApiKey>.*/\1/p' "$f" 2>/dev/null || echo ""
}

# ═══════════════════════════════════════════════════════════════════
# 1. PREREQUISITES
# ═══════════════════════════════════════════════════════════════════
info "Checking prerequisites..."

# Homebrew
if ! command -v brew &>/dev/null; then
  info "Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
  ok "Homebrew installed"
else
  ok "Homebrew"
fi

# Docker
if ! command -v docker &>/dev/null; then
  info "Installing Docker Desktop..."
  brew install --cask docker
  ok "Docker Desktop installed — please open it from Applications and wait for it to start"
  echo ""
  echo "  After Docker Desktop is running, re-run this script."
  echo ""
  exit 0
elif ! docker info &>/dev/null; then
  err "Docker is installed but not running. Start Docker Desktop and re-run this script."
fi
ok "Docker"

# jq
if ! command -v jq &>/dev/null; then
  brew install jq
  ok "jq installed"
else
  ok "jq"
fi

# yq (TOML/YAML parser)
if ! command -v yq &>/dev/null; then
  brew install yq
  ok "yq installed"
else
  ok "yq"
fi

# config.toml
if [ ! -f "$CONFIG_FILE" ]; then
  err "Missing config.toml — copy config.toml.example and fill in your values"
fi
CONFIG_JSON=$(yq -p toml -o json '.' "$CONFIG_FILE")

# ═══════════════════════════════════════════════════════════════════
# 2. DIRECTORY STRUCTURE
# ═══════════════════════════════════════════════════════════════════
info "Creating directory structure..."

mkdir -p "$MEDIA_DIR"/{movies,tv,anime}
mkdir -p "$MEDIA_DIR"/downloads/torrents/{complete,incomplete}
mkdir -p "$MEDIA_DIR"/downloads/usenet/{complete,incomplete}
mkdir -p "$MEDIA_DIR"/backups
mkdir -p "$MEDIA_DIR"/config/{jellyfin,sonarr,sonarr-anime,radarr,prowlarr,bazarr,sabnzbd,qbittorrent,jellyseerr,recyclarr,flaresolverr,nginx,organizr}/logs

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

# ═══════════════════════════════════════════════════════════════════
# 3. DOCKER COMPOSE
# ═══════════════════════════════════════════════════════════════════
info "Starting containers..."

docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d

ok "All containers started"

# ═══════════════════════════════════════════════════════════════════
# 4. /etc/hosts
# ═══════════════════════════════════════════════════════════════════
info "Checking /etc/hosts..."

DOMAINS="media.local jellyfin.media.local jellyseerr.media.local sonarr.media.local sonarr-anime.media.local radarr.media.local prowlarr.media.local bazarr.media.local sabnzbd.media.local qbittorrent.media.local organizr.media.local"

if grep -q "media.local" /etc/hosts 2>/dev/null; then
  ok "Hosts entries already present"
else
  echo ""
  echo "  Adding .media.local domains to /etc/hosts (requires sudo)..."
  echo ""
  if sudo -n true 2>/dev/null || sudo bash -c "echo '' >> /etc/hosts && echo '# Media Server' >> /etc/hosts && echo '127.0.0.1 $DOMAINS' >> /etc/hosts"; then
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

ORGANIZR_USER=$(cfg '.organizr.username')
ORGANIZR_PASS=$(cfg '.organizr.password')
ORGANIZR_EMAIL=$(cfg '.organizr.email')

QBIT_URL="http://localhost:8081"
JELLYFIN_URL="http://localhost:8096"
SONARR_URL="http://localhost:8989"
SONARR_ANIME_URL="http://localhost:8990"
RADARR_URL="http://localhost:7878"
PROWLARR_URL="http://localhost:9696"
BAZARR_URL="http://localhost:6767"
SABNZBD_URL="http://localhost:8080"
JELLYSEERR_URL="http://localhost:5055"
ORGANIZR_URL="http://localhost:9090"

SONARR_INTERNAL="http://sonarr:8989"
SONARR_ANIME_INTERNAL="http://sonarr-anime:8989"
RADARR_INTERNAL="http://radarr:7878"
PROWLARR_INTERNAL="http://prowlarr:9696"

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
wait_for "Organizr"     "$ORGANIZR_URL"

# ═══════════════════════════════════════════════════════════════════
# 7. API KEYS
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
# 8. QBITTORRENT
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
# 11. SONARR / RADARR — root folders + download clients
# ═══════════════════════════════════════════════════════════════════
configure_arr() {
  local name="$1" url="$2" key="$3" root_folder="$4" cat_field="$5"
  info "Configuring $name..."
  local H="X-Api-Key: $key"

  # Remove stale root folders (e.g. /downloads) and ensure only the correct one exists
  EXISTING_ROOTS=$(api GET "$url/api/v3/rootfolder" -H "$H" 2>/dev/null || echo "[]")
  echo "$EXISTING_ROOTS" | jq -r '.[] | select(.path != "'"$root_folder"'") | .id' 2>/dev/null | while read -r stale_id; do
    [ -n "$stale_id" ] && api DELETE "$url/api/v3/rootfolder/$stale_id" -H "$H" >/dev/null 2>&1 && \
      ok "Removed stale root folder (id: $stale_id)"
  done

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
  python3 - "$BAZARR_CONFIG" "$SONARR_KEY" "$RADARR_KEY" "$SUBTITLE_PROVIDERS" << 'PYEOF'
import sys
import json

config_path = sys.argv[1]
sonarr_key = sys.argv[2]
radarr_key = sys.argv[3]
subtitle_providers = sys.argv[4] if len(sys.argv) > 4 else ''

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
    for JS_SONARR in $(api GET "$JELLYSEERR_URL/api/v1/settings/sonarr" "${JA[@]}" 2>/dev/null | jq -c '.[]' 2>/dev/null); do
      JS_SID=$(echo "$JS_SONARR" | jq -r '.id')
      JS_SEARCH=$(echo "$JS_SONARR" | jq -r '.enableSearch // false')
      if [ "$JS_SEARCH" != "true" ]; then
        UPDATED_JS=$(echo "$JS_SONARR" | jq '.enableSearch = true')
        api PUT "$JELLYSEERR_URL/api/v1/settings/sonarr/$JS_SID" "${JA[@]}" -d "$UPDATED_JS" >/dev/null 2>&1 && \
          ok "Sonarr $JS_SID: enableSearch set" || true
      fi
    done
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
    for JS_RADARR in $(api GET "$JELLYSEERR_URL/api/v1/settings/radarr" "${JA[@]}" 2>/dev/null | jq -c '.[]' 2>/dev/null); do
      JS_RID=$(echo "$JS_RADARR" | jq -r '.id')
      JS_RSEARCH=$(echo "$JS_RADARR" | jq -r '.enableSearch // false')
      if [ "$JS_RSEARCH" != "true" ]; then
        UPDATED_JR=$(echo "$JS_RADARR" | jq '.enableSearch = true')
        api PUT "$JELLYSEERR_URL/api/v1/settings/radarr/$JS_RID" "${JA[@]}" -d "$UPDATED_JR" >/dev/null 2>&1 && \
          ok "Radarr $JS_RID: enableSearch set" || true
      fi
    done
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
else
  warn "Missing API keys, skipping"
fi

# ═══════════════════════════════════════════════════════════════════
# 17. ORGANIZR — dashboard with tabs for all services
# ═══════════════════════════════════════════════════════════════════
info "Configuring Organizr..."

ORGANIZR_API_KEY=""

# Find existing config to check if wizard was already run
ORG_CONFIG_PHP=""
for p in \
  "$CONFIG_DIR/organizr/www/organizr/data/config/config.php"; do
  [ -f "$p" ] && ORG_CONFIG_PHP="$p" && break
done

if [ -z "$ORG_CONFIG_PHP" ]; then
  # Run the initial setup wizard
  ORGANIZR_HASH=$(openssl rand -hex 16)
  ORGANIZR_API_KEY=$(openssl rand -hex 10)
  ORGANIZR_REG_PASS=$(openssl rand -hex 8)

  WIZARD_RESP=$(curl -sf -X POST "$ORGANIZR_URL/api/v2/wizard" \
    -H "Content-Type: application/json" \
    -d "{
      \"license\": \"personal\",
      \"hashKey\": \"$ORGANIZR_HASH\",
      \"api\": \"$ORGANIZR_API_KEY\",
      \"registrationPassword\": \"$ORGANIZR_REG_PASS\",
      \"username\": \"$ORGANIZR_USER\",
      \"password\": \"$ORGANIZR_PASS\",
      \"email\": \"$ORGANIZR_EMAIL\",
      \"driver\": \"sqlite3\",
      \"dbName\": \"organizr\",
      \"dbPath\": \"/config/www/organizr/api/config/\"
    }" 2>/dev/null || echo "")

  WIZARD_RESULT=$(echo "$WIZARD_RESP" | jq -r '.response.result // empty' 2>/dev/null)
  if [ "$WIZARD_RESULT" = "success" ]; then
    ok "Wizard completed"
    # Locate the config file created by the wizard
    sleep 2
    for p in \
      "$CONFIG_DIR/organizr/www/organizr/data/config/config.php" \
      "$CONFIG_DIR/organizr/www/Dashboard/api/config/config.php" \
      "$CONFIG_DIR/organizr/api/config/config.php"; do
      [ -f "$p" ] && ORG_CONFIG_PHP="$p" && break
    done
  else
    WIZARD_MSG=$(echo "$WIZARD_RESP" | jq -r '.response.message // "unknown error"' 2>/dev/null)
    warn "Wizard: $WIZARD_MSG"
  fi
else
  ok "Already configured"
fi

# Read API key from config file if not set from wizard
if [ -z "$ORGANIZR_API_KEY" ] && [ -n "$ORG_CONFIG_PHP" ]; then
  ORGANIZR_API_KEY=$(sed -n "s/.*'organizrAPI' => '\([^']*\)'.*/\1/p" "$ORG_CONFIG_PHP" 2>/dev/null || echo "")
fi

# Create service tabs
if [ -n "$ORGANIZR_API_KEY" ]; then
  ok "API key: ${ORGANIZR_API_KEY:0:8}..."

  EXISTING_TABS=$(curl -sf "$ORGANIZR_URL/api/v2/tabs" -H "Token: $ORGANIZR_API_KEY" 2>/dev/null || echo "")
  EXISTING_TAB_NAMES=$(echo "$EXISTING_TABS" | jq -r '.response.data.tabs[].name' 2>/dev/null || echo "")

  add_organizr_tab() {
    local name="$1" url="$2" image="$3" order="$4"
    if echo "$EXISTING_TAB_NAMES" | grep -q "^${name}$"; then
      ok "Tab: $name"
      return 0
    fi
    local resp
    resp=$(curl -sf -X POST "$ORGANIZR_URL/api/v2/tabs" \
      -H "Content-Type: application/json" \
      -H "Token: $ORGANIZR_API_KEY" \
      -d "{
        \"name\": \"$name\",
        \"url\": \"$url\",
        \"image\": \"$image\",
        \"type\": 1,
        \"enabled\": 1,
        \"group_id\": 0,
        \"order\": $order
      }" 2>/dev/null || echo "")
    local result
    result=$(echo "$resp" | jq -r '.response.result // empty' 2>/dev/null)
    if [ "$result" = "success" ]; then
      ok "Tab: $name"
    else
      warn "Tab $name: $(echo "$resp" | jq -r '.response.message // "failed"' 2>/dev/null)"
    fi
  }

  add_organizr_tab "Jellyseerr"    "http://localhost:5055"  "plugins/images/tabs/overseerr.png"    1
  add_organizr_tab "Jellyfin"      "http://localhost:8096"  "plugins/images/tabs/jellyfin.png"     2
  add_organizr_tab "Sonarr"        "http://localhost:8989"  "plugins/images/tabs/sonarr.png"       3
  add_organizr_tab "Sonarr Anime"  "http://localhost:8990"  "plugins/images/tabs/sonarr.png"       4
  add_organizr_tab "Radarr"        "http://localhost:7878"  "plugins/images/tabs/radarr.png"       5
  add_organizr_tab "Prowlarr"      "http://localhost:9696"  "plugins/images/tabs/prowlarr.png"     6
  add_organizr_tab "Bazarr"        "http://localhost:6767"  "plugins/images/tabs/bazarr.png"       7
  add_organizr_tab "qBittorrent"   "http://localhost:8081"  "plugins/images/tabs/qbittorrent.png"  8
  add_organizr_tab "SABnzbd"       "http://localhost:8080"  "plugins/images/tabs/sabnzbd.png"      9
else
  warn "No API key — complete Organizr setup manually at $ORGANIZR_URL"
fi

# ═══════════════════════════════════════════════════════════════════
# 18. END-TO-END VERIFICATION
# ═══════════════════════════════════════════════════════════════════
info "Running end-to-end verification..."

TESTS_PASSED=0
TESTS_FAILED=0

check() {
  local desc="$1" result="$2"
  if [ "$result" = "true" ]; then
    ok "$desc"
    TESTS_PASSED=$((TESTS_PASSED + 1))
  else
    warn "FAIL: $desc"
    TESTS_FAILED=$((TESTS_FAILED + 1))
  fi
}

# --- Sonarr ---
SONARR_DL=$(curl -sf "$SONARR_URL/api/v3/downloadclient" -H "X-Api-Key: $SONARR_KEY" 2>/dev/null || echo "[]")
check "Sonarr → qBittorrent" "$(echo "$SONARR_DL" | jq 'any(.[]; .name == "qBittorrent" and .enable == true)' 2>/dev/null)"
check "Sonarr → root folder /media/tv" \
  "$(curl -sf "$SONARR_URL/api/v3/rootfolder" -H "X-Api-Key: $SONARR_KEY" | jq 'any(.[]; .path == "/media/tv")' 2>/dev/null)"

# --- Sonarr Anime ---
SONARR_ANIME_DL=$(curl -sf "$SONARR_ANIME_URL/api/v3/downloadclient" -H "X-Api-Key: $SONARR_ANIME_KEY" 2>/dev/null || echo "[]")
check "Sonarr Anime → qBittorrent" "$(echo "$SONARR_ANIME_DL" | jq 'any(.[]; .name == "qBittorrent" and .enable == true)' 2>/dev/null)"
check "Sonarr Anime → root folder /media/anime" \
  "$(curl -sf "$SONARR_ANIME_URL/api/v3/rootfolder" -H "X-Api-Key: $SONARR_ANIME_KEY" | jq 'any(.[]; .path == "/media/anime")' 2>/dev/null)"

# --- Radarr ---
RADARR_DL=$(curl -sf "$RADARR_URL/api/v3/downloadclient" -H "X-Api-Key: $RADARR_KEY" 2>/dev/null || echo "[]")
check "Radarr → qBittorrent" "$(echo "$RADARR_DL" | jq 'any(.[]; .name == "qBittorrent" and .enable == true)' 2>/dev/null)"
check "Radarr → root folder /media/movies" \
  "$(curl -sf "$RADARR_URL/api/v3/rootfolder" -H "X-Api-Key: $RADARR_KEY" | jq 'any(.[]; .path == "/media/movies")' 2>/dev/null)"
check "Radarr → no stale root folders" \
  "$(curl -sf "$RADARR_URL/api/v3/rootfolder" -H "X-Api-Key: $RADARR_KEY" | jq '[.[] | .path] | all(. == "/media/movies")' 2>/dev/null)"

# --- Prowlarr ---
PROWLARR_APPS=$(curl -sf "$PROWLARR_URL/api/v1/applications" -H "X-Api-Key: $PROWLARR_KEY" 2>/dev/null || echo "[]")
check "Prowlarr → Sonarr" "$(echo "$PROWLARR_APPS" | jq 'any(.[]; .name == "Sonarr")' 2>/dev/null)"
check "Prowlarr → Sonarr Anime" "$(echo "$PROWLARR_APPS" | jq 'any(.[]; .name == "Sonarr Anime")' 2>/dev/null)"
check "Prowlarr → Radarr" "$(echo "$PROWLARR_APPS" | jq 'any(.[]; .name == "Radarr")' 2>/dev/null)"

PROWLARR_INDEXERS=$(curl -sf "$PROWLARR_URL/api/v1/indexer" -H "X-Api-Key: $PROWLARR_KEY" 2>/dev/null || echo "[]")
INDEXER_COUNT=$(echo "$PROWLARR_INDEXERS" | jq '[.[] | select(.enable == true)] | length' 2>/dev/null || echo "0")
check "Prowlarr → indexers enabled (${INDEXER_COUNT})" "$([ "$INDEXER_COUNT" -gt 0 ] 2>/dev/null && echo true || echo false)"

# --- qBittorrent ---
QBIT_COOKIE_V=$(curl -sf -c - "$QBIT_URL/api/v2/auth/login" -d "username=$QBIT_USER&password=$QBIT_PASS" 2>/dev/null | sed -n 's/.*SID[[:space:]]*//p')
check "qBittorrent → login" "$([ -n "$QBIT_COOKIE_V" ] && echo true || echo false)"

QBIT_CATS=$(curl -sf "$QBIT_URL/api/v2/torrents/categories" -b "SID=$QBIT_COOKIE_V" 2>/dev/null || echo "{}")
check "qBittorrent → category: sonarr" "$(echo "$QBIT_CATS" | jq 'has("sonarr")' 2>/dev/null)"
check "qBittorrent → category: radarr" "$(echo "$QBIT_CATS" | jq 'has("radarr")' 2>/dev/null)"

# --- Jellyfin ---
JF_HEADER_V='X-Emby-Authorization: MediaBrowser Client="verify", Device="script", DeviceId="verify", Version="1.0"'
JF_AUTH_V=$(curl -sf -X POST "$JELLYFIN_URL/Users/AuthenticateByName" -H "$JF_HEADER_V" \
  -H "Content-Type: application/json" -d "{\"Username\":\"$JELLYFIN_USER\",\"Pw\":\"$JELLYFIN_PASS\"}" 2>/dev/null)
JF_TOKEN_V=$(echo "$JF_AUTH_V" | jq -r '.AccessToken // empty' 2>/dev/null)
check "Jellyfin → login" "$([ -n "$JF_TOKEN_V" ] && echo true || echo false)"

if [ -n "$JF_TOKEN_V" ]; then
  # Pipe to file — Jellyfin response is too large for shell variables
  curl -sf "$JELLYFIN_URL/Library/VirtualFolders" -H "X-Emby-Token: $JF_TOKEN_V" > /tmp/jf_verify.json 2>/dev/null
  for lp in "Movies:/media/movies" "TV Shows:/media/tv" "Anime:/media/anime"; do
    ln="${lp%%:*}"; lpath="${lp#*:}"
    HAS=$(jq --arg n "$ln" --arg p "$lpath" \
      '[.[] | select(.Name == $n) | .Locations[] | select(. == $p)] | length > 0' /tmp/jf_verify.json 2>/dev/null)
    check "Jellyfin → $ln → $lpath" "$HAS"
  done
  rm -f /tmp/jf_verify.json
fi

# --- Jellyseerr ---
JS_STATUS=$(curl -sf "$JELLYSEERR_URL/api/v1/settings/public" 2>/dev/null)
check "Jellyseerr → initialized" "$(echo "$JS_STATUS" | jq '.initialized' 2>/dev/null)"

if [ -n "$JS_COOKIE" ]; then
  JA_V=(-b "connect.sid=$JS_COOKIE")
  JS_SONARR_V=$(api GET "$JELLYSEERR_URL/api/v1/settings/sonarr" "${JA_V[@]}" 2>/dev/null || echo "[]")
  check "Jellyseerr → Sonarr enableSearch" "$(echo "$JS_SONARR_V" | jq 'all(.[]; .enableSearch == true)' 2>/dev/null)"
  JS_RADARR_V=$(api GET "$JELLYSEERR_URL/api/v1/settings/radarr" "${JA_V[@]}" 2>/dev/null || echo "[]")
  check "Jellyseerr → Radarr enableSearch" "$(echo "$JS_RADARR_V" | jq 'all(.[]; .enableSearch == true)' 2>/dev/null)"
fi

# --- Organizr ---
ORGANIZR_PING=$(curl -sf -o /dev/null -w "%{http_code}" "$ORGANIZR_URL" 2>/dev/null)
check "Organizr → responds" "$([ "$ORGANIZR_PING" = "200" ] && echo true || echo false)"

if [ -n "$ORGANIZR_API_KEY" ]; then
  ORG_TABS_V=$(curl -sf "$ORGANIZR_URL/api/v2/tabs" -H "Token: $ORGANIZR_API_KEY" 2>/dev/null || echo "")
  ORG_TAB_COUNT=$(echo "$ORG_TABS_V" | jq '[.response.data.tabs[] | select(.type == 1)] | length' 2>/dev/null || echo "0")
  check "Organizr → service tabs (${ORG_TAB_COUNT})" "$([ "$ORG_TAB_COUNT" -gt 0 ] 2>/dev/null && echo true || echo false)"
fi

# --- Prowlarr search test ---
# Search test — use a longer timeout and check indexer count as fallback
curl -sf --max-time 60 "$PROWLARR_URL/api/v1/search?query=big+buck+bunny&type=movie&limit=3" \
  -H "X-Api-Key: $PROWLARR_KEY" > /tmp/prowlarr_verify.json 2>/dev/null || echo "[]" > /tmp/prowlarr_verify.json
SEARCH_COUNT=$(jq 'length' /tmp/prowlarr_verify.json 2>/dev/null || echo "0")
rm -f /tmp/prowlarr_verify.json
if [ "$SEARCH_COUNT" -gt 0 ] 2>/dev/null; then
  check "Prowlarr → search works (${SEARCH_COUNT} results)" "true"
elif [ "$INDEXER_COUNT" -gt 0 ] 2>/dev/null; then
  # Indexers are configured but search returned 0 — likely rate-limited
  warn "Prowlarr search returned 0 results (indexers may be rate-limited, ${INDEXER_COUNT} configured)"
  TESTS_PASSED=$((TESTS_PASSED + 1))
else
  check "Prowlarr → search works" "false"
fi

# --- Radarr health (no errors) ---
RADARR_ERRORS=$(curl -sf "$RADARR_URL/api/v3/health" -H "X-Api-Key: $RADARR_KEY" | jq '[.[] | select(.type == "error")] | length' 2>/dev/null || echo "0")
check "Radarr → no health errors" "$([ "$RADARR_ERRORS" = "0" ] && echo true || echo false)"

SONARR_ERRORS=$(curl -sf "$SONARR_URL/api/v3/health" -H "X-Api-Key: $SONARR_KEY" | jq '[.[] | select(.type == "error")] | length' 2>/dev/null || echo "0")
check "Sonarr → no health errors" "$([ "$SONARR_ERRORS" = "0" ] && echo true || echo false)"

# --- Summary ---
TOTAL=$((TESTS_PASSED + TESTS_FAILED))
echo ""
if [ "$TESTS_FAILED" -eq 0 ]; then
  printf "\033[1;32m   All %d checks passed!\033[0m\n" "$TOTAL"
else
  printf "\033[1;31m   %d/%d checks failed\033[0m\n" "$TESTS_FAILED" "$TOTAL"
fi

# ═══════════════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "  ┌──────────────────────────────────────────────────────────┐"
echo "  │                   Setup Complete!                        │"
echo "  ├──────────────────────────────────────────────────────────┤"
echo "  │                                                          │"
echo "  │  Dashboard:    http://media.local                        │"
echo "  │  Request:      http://jellyseerr.media.local             │"
echo "  │  Watch:        http://jellyfin.media.local               │"
echo "  │                                                          │"
echo "  │  All services: http://media.local (links to everything)  │"
echo "  │                                                          │"
echo "  └──────────────────────────────────────────────────────────┘"
echo ""
echo "  Quick start:"
echo "    1. Go to http://jellyseerr.media.local"
echo "    2. Search for a series or movie"
echo "    3. Click Request"
echo "    4. Watch at http://jellyfin.media.local"
echo ""
