#!/usr/bin/env bash
set -eo pipefail

# ═══════════════════════════════════════════════════════════════════
# Media Server — Smoke tests
# Validates that all services are running, connected, and configured.
# Run after setup.sh to verify everything works.
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.toml"
MEDIA_DIR="$HOME/media"
CONFIG_DIR="$MEDIA_DIR/config"

# ─── Helpers ─────────────────────────────────────────────────────
TESTS_PASSED=0
TESTS_FAILED=0
TESTS_SKIPPED=0

pass() { printf "\033[1;32m   ✓ %s\033[0m\n" "$*"; TESTS_PASSED=$((TESTS_PASSED + 1)); }
fail() { printf "\033[1;31m   ✗ %s\033[0m\n" "$*"; TESTS_FAILED=$((TESTS_FAILED + 1)); }
skip() { printf "\033[1;33m   - %s (skipped)\033[0m\n" "$*"; TESTS_SKIPPED=$((TESTS_SKIPPED + 1)); }
info() { printf "\n\033[1;34m=> %s\033[0m\n" "$*"; }

check() {
  local desc="$1" result="$2"
  if [ "$result" = "true" ]; then pass "$desc"; else fail "$desc"; fi
}

api() {
  local method="$1" url="$2"; shift 2
  curl -sf -X "$method" "$url" -H "Content-Type: application/json" "$@" 2>/dev/null
}

# ─── Prerequisites ───────────────────────────────────────────────
command -v jq  >/dev/null 2>&1 || { echo "jq is required"; exit 1; }
command -v yq  >/dev/null 2>&1 || { echo "yq is required"; exit 1; }
[ -f "$CONFIG_FILE" ] || { echo "config.toml not found"; exit 1; }

CONFIG_JSON=$(yq -p toml -o json '.' "$CONFIG_FILE")
cfg() { echo "$CONFIG_JSON" | jq -r "$1"; }

get_api_key() {
  local svc="$1"
  local xml="$CONFIG_DIR/$svc/config.xml"
  [ -f "$xml" ] && sed -n 's/.*<ApiKey>\(.*\)<\/ApiKey>.*/\1/p' "$xml" 2>/dev/null || echo ""
}

# ─── Read config ─────────────────────────────────────────────────
JELLYFIN_USER=$(cfg '.jellyfin.username')
JELLYFIN_PASS=$(cfg '.jellyfin.password')
QBIT_USER=$(cfg '.qbittorrent.username')
QBIT_PASS=$(cfg '.qbittorrent.password')
ORGANIZR_USER=$(cfg '.organizr.username')
ORGANIZR_PASS=$(cfg '.organizr.password')
ORGANIZR_EMAIL=$(cfg '.organizr.email')

SONARR_KEY=$(get_api_key "sonarr")
SONARR_ANIME_KEY=$(get_api_key "sonarr-anime")
RADARR_KEY=$(get_api_key "radarr")
PROWLARR_KEY=$(get_api_key "prowlarr")
SABNZBD_KEY=""
[ -f "$CONFIG_DIR/sabnzbd/sabnzbd.ini" ] && SABNZBD_KEY=$(sed -n 's/^api_key = *//p' "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null || echo "")

JELLYSEERR_KEY=""
[ -f "$CONFIG_DIR/jellyseerr/settings.json" ] && JELLYSEERR_KEY=$(jq -r '.main.apiKey // empty' "$CONFIG_DIR/jellyseerr/settings.json" 2>/dev/null)

ORGANIZR_API_KEY=""
for p in "$CONFIG_DIR/organizr/www/organizr/data/config/config.php"; do
  [ -f "$p" ] && ORGANIZR_API_KEY=$(sed -n "s/.*'organizrAPI' => '\([^']*\)'.*/\1/p" "$p" 2>/dev/null || echo "")
done

# ─── URLs ────────────────────────────────────────────────────────
JELLYFIN_URL="http://localhost:8096"
SONARR_URL="http://localhost:8989"
SONARR_ANIME_URL="http://localhost:8990"
RADARR_URL="http://localhost:7878"
PROWLARR_URL="http://localhost:9696"
BAZARR_URL="http://localhost:6767"
SABNZBD_URL="http://localhost:8080"
QBIT_URL="http://localhost:8081"
JELLYSEERR_URL="http://localhost:5055"
ORGANIZR_URL="http://localhost:9090"

echo ""
echo "  ┌──────────────────────────────────────────────────────────┐"
echo "  │              Media Server — Smoke Tests                  │"
echo "  └──────────────────────────────────────────────────────────┘"

# ═══════════════════════════════════════════════════════════════════
# 1. SERVICE HEALTH
# ═══════════════════════════════════════════════════════════════════
info "Service health..."

for svc_url in \
  "Jellyfin:$JELLYFIN_URL/health" \
  "Sonarr:$SONARR_URL/ping" \
  "Sonarr Anime:$SONARR_ANIME_URL/ping" \
  "Radarr:$RADARR_URL/ping" \
  "Prowlarr:$PROWLARR_URL/ping" \
  "Bazarr:$BAZARR_URL" \
  "SABnzbd:$SABNZBD_URL" \
  "qBittorrent:$QBIT_URL" \
  "Jellyseerr:$JELLYSEERR_URL" \
  "Organizr:$ORGANIZR_URL"; do
  name="${svc_url%%:*}"
  url="${svc_url#*:}"
  HTTP_CODE=$(curl -sf -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url" 2>/dev/null || echo "000")
  check "$name responds ($HTTP_CODE)" "$([ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 400 ] && echo true || echo false)"
done

# ═══════════════════════════════════════════════════════════════════
# 2. DOWNLOAD CLIENTS
# ═══════════════════════════════════════════════════════════════════
info "Download clients..."

# qBittorrent login
QBIT_COOKIE=$(curl -sf -c - "$QBIT_URL/api/v2/auth/login" -d "username=$QBIT_USER&password=$QBIT_PASS" 2>/dev/null | sed -n 's/.*SID[[:space:]]*//p')
check "qBittorrent login" "$([ -n "$QBIT_COOKIE" ] && echo true || echo false)"

if [ -n "$QBIT_COOKIE" ]; then
  QBIT_CATS=$(curl -sf "$QBIT_URL/api/v2/torrents/categories" -b "SID=$QBIT_COOKIE" 2>/dev/null || echo "{}")
  check "qBittorrent category: sonarr" "$(echo "$QBIT_CATS" | jq 'has("sonarr")' 2>/dev/null)"
  check "qBittorrent category: radarr" "$(echo "$QBIT_CATS" | jq 'has("radarr")' 2>/dev/null)"
fi

# Sonarr → qBittorrent
if [ -n "$SONARR_KEY" ]; then
  SONARR_DL=$(api GET "$SONARR_URL/api/v3/downloadclient" -H "X-Api-Key: $SONARR_KEY" || echo "[]")
  check "Sonarr → qBittorrent" "$(echo "$SONARR_DL" | jq 'any(.[]; .name == "qBittorrent" and .enable == true)' 2>/dev/null)"
fi

# Sonarr Anime → qBittorrent
if [ -n "$SONARR_ANIME_KEY" ]; then
  SONARR_ANIME_DL=$(api GET "$SONARR_ANIME_URL/api/v3/downloadclient" -H "X-Api-Key: $SONARR_ANIME_KEY" || echo "[]")
  check "Sonarr Anime → qBittorrent" "$(echo "$SONARR_ANIME_DL" | jq 'any(.[]; .name == "qBittorrent" and .enable == true)' 2>/dev/null)"
fi

# Radarr → qBittorrent
if [ -n "$RADARR_KEY" ]; then
  RADARR_DL=$(api GET "$RADARR_URL/api/v3/downloadclient" -H "X-Api-Key: $RADARR_KEY" || echo "[]")
  check "Radarr → qBittorrent" "$(echo "$RADARR_DL" | jq 'any(.[]; .name == "qBittorrent" and .enable == true)' 2>/dev/null)"
fi

# ═══════════════════════════════════════════════════════════════════
# 3. ROOT FOLDERS
# ═══════════════════════════════════════════════════════════════════
info "Root folders..."

[ -n "$SONARR_KEY" ] && check "Sonarr → /media/tv" \
  "$(api GET "$SONARR_URL/api/v3/rootfolder" -H "X-Api-Key: $SONARR_KEY" | jq 'any(.[]; .path == "/media/tv")' 2>/dev/null)"

[ -n "$SONARR_ANIME_KEY" ] && check "Sonarr Anime → /media/anime" \
  "$(api GET "$SONARR_ANIME_URL/api/v3/rootfolder" -H "X-Api-Key: $SONARR_ANIME_KEY" | jq 'any(.[]; .path == "/media/anime")' 2>/dev/null)"

[ -n "$RADARR_KEY" ] && check "Radarr → /media/movies" \
  "$(api GET "$RADARR_URL/api/v3/rootfolder" -H "X-Api-Key: $RADARR_KEY" | jq 'any(.[]; .path == "/media/movies")' 2>/dev/null)"

# ═══════════════════════════════════════════════════════════════════
# 4. PROWLARR
# ═══════════════════════════════════════════════════════════════════
info "Prowlarr..."

if [ -n "$PROWLARR_KEY" ]; then
  PH="X-Api-Key: $PROWLARR_KEY"
  PROWLARR_APPS=$(api GET "$PROWLARR_URL/api/v1/applications" -H "$PH" || echo "[]")
  check "Prowlarr → Sonarr connected" "$(echo "$PROWLARR_APPS" | jq 'any(.[]; .name == "Sonarr")' 2>/dev/null)"
  check "Prowlarr → Sonarr Anime connected" "$(echo "$PROWLARR_APPS" | jq 'any(.[]; .name == "Sonarr Anime")' 2>/dev/null)"
  check "Prowlarr → Radarr connected" "$(echo "$PROWLARR_APPS" | jq 'any(.[]; .name == "Radarr")' 2>/dev/null)"

  INDEXER_COUNT=$(api GET "$PROWLARR_URL/api/v1/indexer" -H "$PH" | jq '[.[] | select(.enable == true)] | length' 2>/dev/null || echo "0")
  check "Prowlarr → indexers enabled ($INDEXER_COUNT)" "$([ "$INDEXER_COUNT" -gt 0 ] && echo true || echo false)"

  # Search test
  SEARCH_RESULTS=$(curl -sf --max-time 30 "$PROWLARR_URL/api/v1/search?query=test&type=movie&limit=3" -H "$PH" 2>/dev/null || echo "[]")
  SEARCH_COUNT=$(echo "$SEARCH_RESULTS" | jq 'length' 2>/dev/null || echo "0")
  if [ "$SEARCH_COUNT" -gt 0 ] 2>/dev/null; then
    pass "Prowlarr → search works ($SEARCH_COUNT results)"
  elif [ "$INDEXER_COUNT" -gt 0 ] 2>/dev/null; then
    skip "Prowlarr → search (indexers may be rate-limited)"
  else
    fail "Prowlarr → search works"
  fi
fi

# ═══════════════════════════════════════════════════════════════════
# 5. JELLYFIN
# ═══════════════════════════════════════════════════════════════════
info "Jellyfin..."

JF_HEADER='X-Emby-Authorization: MediaBrowser Client="test", Device="script", DeviceId="test", Version="1.0"'
JF_AUTH=$(curl -sf -X POST "$JELLYFIN_URL/Users/AuthenticateByName" -H "$JF_HEADER" \
  -H "Content-Type: application/json" -d "{\"Username\":\"$JELLYFIN_USER\",\"Pw\":\"$JELLYFIN_PASS\"}" 2>/dev/null)
JF_TOKEN=$(echo "$JF_AUTH" | jq -r '.AccessToken // empty' 2>/dev/null)
check "Jellyfin → login" "$([ -n "$JF_TOKEN" ] && echo true || echo false)"

if [ -n "$JF_TOKEN" ]; then
  curl -sf "$JELLYFIN_URL/Library/VirtualFolders" -H "X-Emby-Token: $JF_TOKEN" > /tmp/jf_test.json 2>/dev/null
  for lp in "Movies:/media/movies" "TV Shows:/media/tv" "Anime:/media/anime"; do
    ln="${lp%%:*}"; lpath="${lp#*:}"
    HAS=$(jq --arg n "$ln" --arg p "$lpath" \
      '[.[] | select(.Name == $n) | .Locations[] | select(. == $p)] | length > 0' /tmp/jf_test.json 2>/dev/null)
    check "Jellyfin → library: $ln" "$HAS"
  done
  rm -f /tmp/jf_test.json
fi

# ═══════════════════════════════════════════════════════════════════
# 6. JELLYSEERR
# ═══════════════════════════════════════════════════════════════════
info "Jellyseerr..."

JS_PUBLIC=$(api GET "$JELLYSEERR_URL/api/v1/settings/public" || echo "{}")
check "Jellyseerr → initialized" "$(echo "$JS_PUBLIC" | jq '.initialized' 2>/dev/null)"

if [ -n "$JELLYSEERR_KEY" ]; then
  JH="X-Api-Key: $JELLYSEERR_KEY"

  # Sonarr connections with enableSearch
  JS_SONARR=$(api GET "$JELLYSEERR_URL/api/v1/settings/sonarr" -H "$JH" || echo "[]")
  JS_SONARR_COUNT=$(echo "$JS_SONARR" | jq 'length' 2>/dev/null || echo "0")
  check "Jellyseerr → Sonarr connections ($JS_SONARR_COUNT)" "$([ "$JS_SONARR_COUNT" -gt 0 ] && echo true || echo false)"

  JS_SONARR_SEARCH=$(echo "$JS_SONARR" | jq 'all(.[]; .enableSearch == true)' 2>/dev/null)
  check "Jellyseerr → Sonarr enableSearch" "$JS_SONARR_SEARCH"

  # Radarr connections with enableSearch
  JS_RADARR=$(api GET "$JELLYSEERR_URL/api/v1/settings/radarr" -H "$JH" || echo "[]")
  JS_RADARR_COUNT=$(echo "$JS_RADARR" | jq 'length' 2>/dev/null || echo "0")
  check "Jellyseerr → Radarr connections ($JS_RADARR_COUNT)" "$([ "$JS_RADARR_COUNT" -gt 0 ] && echo true || echo false)"

  JS_RADARR_SEARCH=$(echo "$JS_RADARR" | jq 'all(.[]; .enableSearch == true)' 2>/dev/null)
  check "Jellyseerr → Radarr enableSearch" "$JS_RADARR_SEARCH"
fi

# ═══════════════════════════════════════════════════════════════════
# 7. QUALITY PROFILES
# ═══════════════════════════════════════════════════════════════════
info "Quality profiles..."

check_unknown_quality() {
  local name="$1" url="$2" key="$3"
  local PROFILE=$(api GET "$url/api/v3/qualityprofile/1" -H "X-Api-Key: $key" 2>/dev/null || echo "")
  [ -z "$PROFILE" ] && { skip "$name → quality profile"; return; }
  local UNKNOWN=$(echo "$PROFILE" | jq '[.items[] | select(.quality.id == 0) | .allowed][0]' 2>/dev/null)
  check "$name → Unknown quality allowed" "$UNKNOWN"
}

[ -n "$SONARR_KEY" ] && check_unknown_quality "Sonarr" "$SONARR_URL" "$SONARR_KEY"
[ -n "$SONARR_ANIME_KEY" ] && check_unknown_quality "Sonarr Anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY"

# ═══════════════════════════════════════════════════════════════════
# 8. AUTH CONFIGURED
# ═══════════════════════════════════════════════════════════════════
info "Authentication..."

check_arr_auth() {
  local name="$1" url="$2" key="$3" api_ver="${4:-v3}"
  local HOST=$(api GET "$url/api/$api_ver/config/host" -H "X-Api-Key: $key" 2>/dev/null || echo "")
  [ -z "$HOST" ] && { skip "$name → auth"; return; }
  local AUTH_USER=$(echo "$HOST" | jq -r '.username // empty' 2>/dev/null)
  check "$name → auth configured" "$([ -n "$AUTH_USER" ] && echo true || echo false)"
}

[ -n "$SONARR_KEY" ] && check_arr_auth "Sonarr" "$SONARR_URL" "$SONARR_KEY"
[ -n "$SONARR_ANIME_KEY" ] && check_arr_auth "Sonarr Anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY"
[ -n "$RADARR_KEY" ] && check_arr_auth "Radarr" "$RADARR_URL" "$RADARR_KEY"
[ -n "$PROWLARR_KEY" ] && check_arr_auth "Prowlarr" "$PROWLARR_URL" "$PROWLARR_KEY" "v1"

# SABnzbd auth
if [ -n "$SABNZBD_KEY" ]; then
  SAB_AUTH_USER=$(curl -sf "$SABNZBD_URL/api?mode=get_config&section=misc&apikey=$SABNZBD_KEY&output=json" 2>/dev/null | jq -r '.config.misc.username // empty' 2>/dev/null)
  check "SABnzbd → auth configured" "$([ -n "$SAB_AUTH_USER" ] && echo true || echo false)"
fi

# Bazarr auth
BAZARR_CONFIG_FILE=""
for p in "$CONFIG_DIR/bazarr/config/config/config.yaml" "$CONFIG_DIR/bazarr/config/config.yaml"; do
  [ -f "$p" ] && BAZARR_CONFIG_FILE="$p" && break
done
if [ -n "$BAZARR_CONFIG_FILE" ]; then
  BAZARR_AUTH_TYPE=$(grep -A1 '^auth:' "$BAZARR_CONFIG_FILE" 2>/dev/null | grep 'type:' | grep -v "''" | head -1)
  check "Bazarr → auth configured" "$([ -n "$BAZARR_AUTH_TYPE" ] && echo true || echo false)"
fi

# ═══════════════════════════════════════════════════════════════════
# 9. ORGANIZR
# ═══════════════════════════════════════════════════════════════════
info "Organizr..."

ORGANIZR_CODE=$(curl -sf -o /dev/null -w "%{http_code}" "$ORGANIZR_URL" 2>/dev/null || echo "000")
check "Organizr → responds ($ORGANIZR_CODE)" "$([ "$ORGANIZR_CODE" = "200" ] && echo true || echo false)"

if [ -n "$ORGANIZR_API_KEY" ]; then
  ORG_TABS=$(curl -sf "$ORGANIZR_URL/api/v2/tabs" -H "Token: $ORGANIZR_API_KEY" 2>/dev/null || echo "")
  ORG_TAB_COUNT=$(echo "$ORG_TABS" | jq '[.response.data.tabs[] | select(.type == 1)] | length' 2>/dev/null || echo "0")
  check "Organizr → service tabs ($ORG_TAB_COUNT)" "$([ "$ORG_TAB_COUNT" -gt 0 ] && echo true || echo false)"
fi

# ═══════════════════════════════════════════════════════════════════
# 10. HEALTH CHECKS
# ═══════════════════════════════════════════════════════════════════
info "Health checks..."

[ -n "$SONARR_KEY" ] && {
  SONARR_ERRORS=$(api GET "$SONARR_URL/api/v3/health" -H "X-Api-Key: $SONARR_KEY" | jq '[.[] | select(.type == "error")] | length' 2>/dev/null || echo "0")
  check "Sonarr → no health errors" "$([ "$SONARR_ERRORS" = "0" ] && echo true || echo false)"
}

[ -n "$RADARR_KEY" ] && {
  RADARR_ERRORS=$(api GET "$RADARR_URL/api/v3/health" -H "X-Api-Key: $RADARR_KEY" | jq '[.[] | select(.type == "error")] | length' 2>/dev/null || echo "0")
  check "Radarr → no health errors" "$([ "$RADARR_ERRORS" = "0" ] && echo true || echo false)"
}

# ═══════════════════════════════════════════════════════════════════
# 11. LANDING PAGE & PROXY
# ═══════════════════════════════════════════════════════════════════
info "Landing page..."

LANDING_HEADERS=$(curl -sf -D - -o /dev/null http://localhost 2>/dev/null || echo "")
LANDING=$(curl -sf http://localhost 2>/dev/null || echo "")
check "Landing page → serves HTML" "$(echo "$LANDING" | grep -q 'Media.*Server' && echo true || echo false)"
check "Landing page → Content-Type text/html" "$(echo "$LANDING_HEADERS" | grep -qi 'content-type.*text/html' && echo true || echo false)"
check "Landing page → service grid" "$(echo "$LANDING" | grep -q 'Jellyfin' && echo true || echo false)"
check "Landing page → downloads widget" "$(echo "$LANDING" | grep -q 'qbt/torrents' && echo true || echo false)"

QBT_PROXY=$(curl -sf http://localhost/api/qbt/torrents/info 2>/dev/null || echo "")
check "Landing page → qBittorrent proxy" "$(echo "$QBT_PROXY" | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"

# API proxy endpoints for widgets
check "Proxy → Sonarr calendar" "$(curl -sf http://localhost/api/sonarr/calendar 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
check "Proxy → Sonarr Anime calendar" "$(curl -sf http://localhost/api/sonarr-anime/calendar 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
check "Proxy → Radarr calendar" "$(curl -sf http://localhost/api/radarr/calendar 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
check "Proxy → Jellyfin latest" "$(curl -sf 'http://localhost/api/jellyfin/Items?SortBy=DateCreated&SortOrder=Descending&Limit=3&Recursive=true&IncludeItemTypes=Movie,Series' 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
check "Proxy → Jellyseerr requests" "$(curl -sf http://localhost/api/jellyseerr/request 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
check "Proxy → SABnzbd queue" "$(curl -sf 'http://localhost/api/sabnzbd/?mode=queue&output=json' 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"

# ═══════════════════════════════════════════════════════════════════
# 12. DOCKER CONTAINERS
# ═══════════════════════════════════════════════════════════════════
info "Docker containers..."

for container in jellyfin sonarr sonarr-anime radarr prowlarr bazarr sabnzbd qbittorrent jellyseerr flaresolverr organizr media-nginx recyclarr; do
  STATUS=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo "missing")
  check "Container: $container" "$([ "$STATUS" = "running" ] && echo true || echo false)"
done

# ═══════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════
TOTAL=$((TESTS_PASSED + TESTS_FAILED))
echo ""
echo "  ────────────────────────────────────────────────────────────"
if [ "$TESTS_FAILED" -eq 0 ]; then
  printf "\033[1;32m   All %d checks passed!" "$TOTAL"
  [ "$TESTS_SKIPPED" -gt 0 ] && printf " (%d skipped)" "$TESTS_SKIPPED"
  printf "\033[0m\n"
else
  printf "\033[1;31m   %d/%d checks failed" "$TESTS_FAILED" "$TOTAL"
  [ "$TESTS_SKIPPED" -gt 0 ] && printf " (%d skipped)" "$TESTS_SKIPPED"
  printf "\033[0m\n"
fi
echo ""

exit "$TESTS_FAILED"
