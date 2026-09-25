#!/usr/bin/env bash

run_verification() {
  # shellcheck source=/dev/null
  . "$SCRIPT_DIR/scripts/service_registry.sh"
  init_service_registry

  info "Running end-to-end verification..."

  TESTS_PASSED=0
  TESTS_FAILED=0
  TESTS_SKIPPED=0

  pass() { printf "\033[1;32m   ✓ %s\033[0m\n" "$*"; TESTS_PASSED=$((TESTS_PASSED + 1)); }
  fail() { printf "\033[1;31m   ✗ %s\033[0m\n" "$*"; TESTS_FAILED=$((TESTS_FAILED + 1)); }
  skip() { printf "\033[1;33m   - %s (skipped)\033[0m\n" "$*"; TESTS_SKIPPED=$((TESTS_SKIPPED + 1)); }

  check() {
    local desc="$1" result="$2"
    if [ "$result" = "true" ]; then pass "$desc"; else fail "$desc"; fi
  }

  [ -z "${SEERR_KEY:-}" ] && [ -f "$CONFIG_DIR/seerr/settings.json" ] && \
    SEERR_KEY=$(jq -r '.main.apiKey // empty' "$CONFIG_DIR/seerr/settings.json" 2>/dev/null)

  info "Service health..."
  while IFS='|' read -r name url; do
    [ -z "$name" ] && continue
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url" 2>/dev/null || true)
    if [ -z "$HTTP_CODE" ] || [ "$HTTP_CODE" = "000" ]; then
      sleep 5
      HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url" 2>/dev/null || true)
    fi
    check "$name responds ($HTTP_CODE)" "$([ -n "$HTTP_CODE" ] && [ "$HTTP_CODE" != "000" ] && echo true || echo false)"
  done <<< "$SERVICE_HEALTH_ENDPOINTS"

  info "Download clients..."
  QBIT_COOKIE_V=$(curl -sf -c - "$QBIT_URL/api/v2/auth/login" \
    --data-urlencode "username=$QBIT_USER" --data-urlencode "password=$QBIT_PASS" 2>/dev/null | extract_qbit_cookie || true)
  check "qBittorrent login" "$([ -n "$QBIT_COOKIE_V" ] && echo true || echo false)"

  if [ -n "$QBIT_COOKIE_V" ]; then
    QBIT_CATS=$(curl -sf "$QBIT_URL/api/v2/torrents/categories" -b "$QBIT_COOKIE_V" 2>/dev/null || echo "{}")
    check "qBittorrent category: sonarr" "$(echo "$QBIT_CATS" | jq 'has("sonarr")' 2>/dev/null)"
    check "qBittorrent category: sonarr-anime" "$(echo "$QBIT_CATS" | jq 'has("sonarr-anime")' 2>/dev/null)"
    check "qBittorrent category: radarr" "$(echo "$QBIT_CATS" | jq 'has("radarr")' 2>/dev/null)"
  fi

  if [ -n "$SONARR_KEY" ]; then
    SONARR_DL=$(api GET "$SONARR_URL/api/v3/downloadclient" -H "X-Api-Key: $SONARR_KEY" || echo "[]")
    check "Sonarr → qBittorrent" "$(echo "$SONARR_DL" | jq 'any(.[]; .name == "qBittorrent" and .enable == true)' 2>/dev/null)"
  fi

  if [ -n "$SONARR_ANIME_KEY" ]; then
    SONARR_ANIME_DL=$(api GET "$SONARR_ANIME_URL/api/v3/downloadclient" -H "X-Api-Key: $SONARR_ANIME_KEY" || echo "[]")
    check "Sonarr Anime → qBittorrent" "$(echo "$SONARR_ANIME_DL" | jq 'any(.[]; .name == "qBittorrent" and .enable == true)' 2>/dev/null)"
  fi

  if [ -n "$RADARR_KEY" ]; then
    RADARR_DL=$(api GET "$RADARR_URL/api/v3/downloadclient" -H "X-Api-Key: $RADARR_KEY" || echo "[]")
    check "Radarr → qBittorrent" "$(echo "$RADARR_DL" | jq 'any(.[]; .name == "qBittorrent" and .enable == true)' 2>/dev/null)"
  fi

  info "Root folders..."
  check_root() {
    local name="$1" url="$2" key="$3" dir="$4"
    check "$name → $dir" "$(api GET "$url/api/v3/rootfolder" -H "X-Api-Key: $key" | jq --arg d "$dir" 'any(.[]; .path == $d)' 2>/dev/null || echo false)"
  }
  [ -n "$SONARR_KEY" ] && check_root "Sonarr" "$SONARR_URL" "$SONARR_KEY" "$TV_DIR"
  [ -n "$SONARR_ANIME_KEY" ] && check_root "Sonarr Anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY" "$ANIME_DIR"
  [ -n "$RADARR_KEY" ] && check_root "Radarr" "$RADARR_URL" "$RADARR_KEY" "$MOVIES_DIR"
  [ -n "$RADARR_KEY" ] && check "Radarr → no stale root folders" "$(api GET "$RADARR_URL/api/v3/rootfolder" -H "X-Api-Key: $RADARR_KEY" | jq --arg d "$MOVIES_DIR" '[.[] | .path] | all(. == $d)' 2>/dev/null || echo false)"

  info "Prowlarr..."
  if [ -n "$PROWLARR_KEY" ]; then
    PH="X-Api-Key: $PROWLARR_KEY"
    PROWLARR_APPS=$(api GET "$PROWLARR_URL/api/v1/applications" -H "$PH" || echo "[]")
    check "Prowlarr → Sonarr connected" "$(echo "$PROWLARR_APPS" | jq 'any(.[]; .name == "Sonarr")' 2>/dev/null)"
    check "Prowlarr → Sonarr Anime connected" "$(echo "$PROWLARR_APPS" | jq 'any(.[]; .name == "Sonarr Anime")' 2>/dev/null)"
    check "Prowlarr → Radarr connected" "$(echo "$PROWLARR_APPS" | jq 'any(.[]; .name == "Radarr")' 2>/dev/null)"

    INDEXER_COUNT=$(api GET "$PROWLARR_URL/api/v1/indexer" -H "$PH" | jq '[.[] | select(.enable == true)] | length' 2>/dev/null || echo "0")
    check "Prowlarr → indexers enabled ($INDEXER_COUNT)" "$([ "$INDEXER_COUNT" -gt 0 ] && echo true || echo false)"

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

  info "Jellyfin..."
  JF_HEADER_V='Authorization: MediaBrowser Client="verify", Device="script", DeviceId="verify", Version="1.0"'
  JF_AUTH_V=$(curl -sf -X POST "$JELLYFIN_URL/Users/AuthenticateByName" -H "$JF_HEADER_V" -H "Content-Type: application/json" \
    -d "$(jq -nc --arg u "$JELLYFIN_USER" --arg p "$JELLYFIN_PASS" '{Username:$u,Pw:$p}')" 2>/dev/null || true)
  JF_TOKEN_V=$(echo "$JF_AUTH_V" | jq -r '.AccessToken // empty' 2>/dev/null)
  check "Jellyfin → login" "$([ -n "$JF_TOKEN_V" ] && echo true || echo false)"

  if [ -n "$JF_TOKEN_V" ]; then
    curl -sf "$JELLYFIN_URL/Library/VirtualFolders" -H "$(jf_auth "$JF_TOKEN_V")" > "$TMPDIR_SETUP/jf_verify.json" 2>/dev/null || echo "[]" > "$TMPDIR_SETUP/jf_verify.json"
    for lp in "Movies:$MOVIES_DIR" "TV Shows:$TV_DIR" "Anime:$ANIME_DIR"; do
      ln="${lp%%:*}"; lpath="${lp#*:}"
      HAS=$(jq --arg n "$ln" --arg p "$lpath" '[.[] | select(.Name == $n) | .Locations[] | select(. == $p)] | length > 0' "$TMPDIR_SETUP/jf_verify.json" 2>/dev/null)
      check "Jellyfin → library: $ln" "$HAS"
    done
    REALTIME=$(jq 'all(.[]; .LibraryOptions.EnableRealtimeMonitor == true)' "$TMPDIR_SETUP/jf_verify.json" 2>/dev/null)
    check "Jellyfin → real-time monitoring" "$REALTIME"
    DAILY_SCAN=$(jq 'all(.[]; .LibraryOptions.AutomaticRefreshIntervalDays == 1)' "$TMPDIR_SETUP/jf_verify.json" 2>/dev/null)
    check "Jellyfin → daily scan" "$DAILY_SCAN"
    rm -f "$TMPDIR_SETUP/jf_verify.json"
  fi

  info "Jellyfin sync..."
  check_jellyfin_notification() {
    local name="$1" url="$2" key="$3"
    local NOTIF
    NOTIF=$(api GET "$url/api/v3/notification" -H "X-Api-Key: $key" || echo "[]")
    check "$name → Jellyfin notification" "$(echo "$NOTIF" | jq 'any(.[]; .name == "Jellyfin")' 2>/dev/null)"
  }
  [ -n "$SONARR_KEY" ] && check_jellyfin_notification "Sonarr" "$SONARR_URL" "$SONARR_KEY"
  [ -n "$SONARR_ANIME_KEY" ] && check_jellyfin_notification "Sonarr Anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY"
  [ -n "$RADARR_KEY" ] && check_jellyfin_notification "Radarr" "$RADARR_URL" "$RADARR_KEY"

  info "Seerr..."
  JS_PUBLIC_V=$(api GET "$SEERR_URL/api/v1/settings/public" || echo "{}")
  check "Seerr → initialized" "$(echo "$JS_PUBLIC_V" | jq '.initialized' 2>/dev/null)"

  if [ -n "${SEERR_KEY:-}" ]; then
    JH="X-Api-Key: $SEERR_KEY"
    JS_SONARR_V=$(api GET "$SEERR_URL/api/v1/settings/sonarr" -H "$JH" || echo "[]")
    JS_SONARR_COUNT=$(echo "$JS_SONARR_V" | jq 'length' 2>/dev/null || echo "0")
    check "Seerr → Sonarr connections ($JS_SONARR_COUNT)" "$([ "$JS_SONARR_COUNT" -gt 0 ] && echo true || echo false)"
    JS_SONARR_SEARCH=$(echo "$JS_SONARR_V" | jq 'all(.[]; .enableSearch == true)' 2>/dev/null)
    check "Seerr → Sonarr enableSearch" "$JS_SONARR_SEARCH"

    JS_RADARR_V=$(api GET "$SEERR_URL/api/v1/settings/radarr" -H "$JH" || echo "[]")
    JS_RADARR_COUNT=$(echo "$JS_RADARR_V" | jq 'length' 2>/dev/null || echo "0")
    check "Seerr → Radarr connections ($JS_RADARR_COUNT)" "$([ "$JS_RADARR_COUNT" -gt 0 ] && echo true || echo false)"
    JS_RADARR_SEARCH=$(echo "$JS_RADARR_V" | jq 'all(.[]; .enableSearch == true)' 2>/dev/null)
    check "Seerr → Radarr enableSearch" "$JS_RADARR_SEARCH"

    JS_JELLYFIN_V=$(api GET "$SEERR_URL/api/v1/settings/jellyfin" -H "$JH" || echo "{}")
    JS_LIB_ENABLED=$(echo "$JS_JELLYFIN_V" | jq '[.libraries[] | select(.enabled == true)] | length' 2>/dev/null || echo "0")
    JS_LIB_TOTAL=$(echo "$JS_JELLYFIN_V" | jq '.libraries | length' 2>/dev/null || echo "0")
    check "Seerr → libraries enabled ($JS_LIB_ENABLED/$JS_LIB_TOTAL)" "$([ "$JS_LIB_ENABLED" -gt 0 ] && echo true || echo false)"
  fi

  info "Quality profiles..."
  check_unknown_quality() {
    local name="$1" url="$2" key="$3" api_ver="${4:-v3}"
    local PROFILE UNKNOWN
    PROFILE=$(api GET "$url/api/$api_ver/qualityprofile/1" -H "X-Api-Key: $key" 2>/dev/null || echo "")
    [ -z "$PROFILE" ] && { skip "$name → quality profile"; return; }
    UNKNOWN=$(echo "$PROFILE" | jq '[.items[] | select(.quality.id == 0) | .allowed][0]' 2>/dev/null)
    check "$name → Unknown quality allowed" "$UNKNOWN"
  }
  [ -n "$SONARR_KEY" ] && check_unknown_quality "Sonarr" "$SONARR_URL" "$SONARR_KEY"
  [ -n "$SONARR_ANIME_KEY" ] && check_unknown_quality "Sonarr Anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY"
  [ -n "$RADARR_KEY" ] && check_unknown_quality "Radarr" "$RADARR_URL" "$RADARR_KEY"

  info "Authentication..."
  check_arr_auth() {
    local name="$1" url="$2" key="$3" api_ver="${4:-v3}"
    local HOST AUTH_USER
    HOST=$(api GET "$url/api/$api_ver/config/host" -H "X-Api-Key: $key" 2>/dev/null || echo "")
    [ -z "$HOST" ] && { skip "$name → auth"; return; }
    AUTH_USER=$(echo "$HOST" | jq -r '.username // empty' 2>/dev/null)
    check "$name → auth configured" "$([ -n "$AUTH_USER" ] && echo true || echo false)"
  }
  [ -n "$SONARR_KEY" ] && check_arr_auth "Sonarr" "$SONARR_URL" "$SONARR_KEY"
  [ -n "$SONARR_ANIME_KEY" ] && check_arr_auth "Sonarr Anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY"
  [ -n "$RADARR_KEY" ] && check_arr_auth "Radarr" "$RADARR_URL" "$RADARR_KEY"
  [ -n "$PROWLARR_KEY" ] && check_arr_auth "Prowlarr" "$PROWLARR_URL" "$PROWLARR_KEY" "v1"

  if [ -n "${SABNZBD_KEY:-}" ]; then
    SAB_AUTH_USER=$(curl -sf "$SABNZBD_URL/api?mode=get_config&section=misc&apikey=$SABNZBD_KEY&output=json" 2>/dev/null | jq -r '.config.misc.username // empty' 2>/dev/null || true)
    check "SABnzbd → auth configured" "$([ -n "$SAB_AUTH_USER" ] && echo true || echo false)"
  fi

  BAZARR_CONFIG_FILE=""
  for p in "$CONFIG_DIR/bazarr/config/config/config.yaml" "$CONFIG_DIR/bazarr/config/config.yaml"; do
    [ -f "$p" ] && BAZARR_CONFIG_FILE="$p" && break
  done
  if [ -n "$BAZARR_CONFIG_FILE" ]; then
    BAZARR_AUTH_USER=$(sed -n '/^auth:/,/^[^ ]/{s/^  username: *//p;}' "$BAZARR_CONFIG_FILE" 2>/dev/null | head -1 || echo "")
    check "Bazarr → auth configured" "$([ -n "$BAZARR_AUTH_USER" ] && [ "$BAZARR_AUTH_USER" != "''" ] && echo true || echo false)"
  fi

  info "Health checks..."
  # The *arr apps cache health results; ask for a fresh check (it runs async)
  check_arr_health() {
    local name="$1" url="$2" key="$3" errors i=0
    api POST "$url/api/v3/command" -H "X-Api-Key: $key" -d '{"name":"CheckHealth"}' >/dev/null || true
    while :; do
      errors=$(api GET "$url/api/v3/health" -H "X-Api-Key: $key" | jq '[.[] | select(.type == "error")] | length' 2>/dev/null || echo "?")
      [ "$errors" = "0" ] || [ "$i" -ge 15 ] && break
      sleep 1
      i=$((i + 1))
    done
    check "$name → no health errors" "$([ "$errors" = "0" ] && echo true || echo false)"
  }
  [ -n "$SONARR_KEY" ] && check_arr_health "Sonarr" "$SONARR_URL" "$SONARR_KEY"
  [ -n "$SONARR_ANIME_KEY" ] && check_arr_health "Sonarr Anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY"
  [ -n "$RADARR_KEY" ] && check_arr_health "Radarr" "$RADARR_URL" "$RADARR_KEY"

  info "Landing page..."
  LANDING_HEADERS=$(curl -sf -D - -o /dev/null "$DASHBOARD_URL" 2>/dev/null || echo "")
  LANDING=$(curl -sf "$DASHBOARD_URL" 2>/dev/null || echo "")
  check "Landing page → serves HTML" "$(echo "$LANDING" | grep -q 'Media.*Server' && echo true || echo false)"
  check "Landing page → Content-Type text/html" "$(echo "$LANDING_HEADERS" | grep -qi 'content-type.*text/html' && echo true || echo false)"
  check "Landing page → service grid" "$(echo "$LANDING" | grep -q 'Jellyfin' && echo true || echo false)"
  check "Landing page → downloads widget" "$(echo "$LANDING" | grep -q 'qbt/torrents' && echo true || echo false)"

  QBT_PROXY=$(curl -sf "$DASHBOARD_URL/api/qbt/torrents/info" 2>/dev/null || echo "")
  check "Landing page → qBittorrent proxy" "$(echo "$QBT_PROXY" | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"

  check "Proxy → Sonarr calendar" "$(curl -sf "$DASHBOARD_URL/api/sonarr/calendar" 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
  check "Proxy → Sonarr Anime calendar" "$(curl -sf "$DASHBOARD_URL/api/sonarr-anime/calendar" 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
  check "Proxy → Radarr calendar" "$(curl -sf "$DASHBOARD_URL/api/radarr/calendar" 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
  check "Proxy → Jellyfin latest" "$(curl -sf "$DASHBOARD_URL"'/api/jellyfin/Items?SortBy=DateCreated&SortOrder=Descending&Limit=3&Recursive=true&IncludeItemTypes=Movie,Series' 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
  check "Proxy → Seerr requests" "$(curl -sf "$DASHBOARD_URL/api/seerr/request" 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"
  check "Proxy → SABnzbd queue" "$(curl -sf "$DASHBOARD_URL"'/api/sabnzbd/?mode=queue&output=json' 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin); print("true")' 2>/dev/null || echo "false")"

  info "Access control..."
  http_code() { curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$@" 2>/dev/null || true; }
  check "Proxy → rejects writes (POST Sonarr calendar: $(http_code -X POST "$DASHBOARD_URL/api/sonarr/calendar"))" \
    "$([ "$(http_code -X POST "$DASHBOARD_URL/api/sonarr/calendar")" = "403" ] && echo true || echo false)"
  check "Proxy → hides other endpoints (Jellyfin Auth/Keys: $(http_code "$DASHBOARD_URL/api/jellyfin/Auth/Keys"))" \
    "$([ "$(http_code "$DASHBOARD_URL/api/jellyfin/Auth/Keys")" = "404" ] && echo true || echo false)"
  check "Proxy → SABnzbd limited to queue/history" \
    "$([ "$(http_code "$DASHBOARD_URL"'/api/sabnzbd/?mode=get_config')" = "403" ] && echo true || echo false)"
  # From this Mac qBittorrent skips its login (for the dashboard); from the
  # network it must not
  local lan_ip
  lan_ip=$(ipconfig getifaddr en0 2>/dev/null || true)
  if [ -n "$lan_ip" ] && [ "${ADMIN_BIND:-0.0.0.0}" = "0.0.0.0" ]; then
    check "qBittorrent → requires login from the network" "$([ "$(http_code "http://$lan_ip:8081/api/v2/app/version")" = "403" ] && echo true || echo false)"
  else
    skip "qBittorrent → login from the network (not reachable from here)"
  fi

  info "Junk-release filters..."
  check_junk_filter() {
    local name="$1" url="$2" key="$3" profile="$4" cfs profiles
    cfs=$(api GET "$url/api/v3/customformat" -H "X-Api-Key: $key" || echo "[]")
    profiles=$(api GET "$url/api/v3/qualityprofile" -H "X-Api-Key: $key" || echo "[]")
    check "$name → BR-DISK blocked in $profile" "$(jq --argjson cfs "$cfs" --arg p "$profile" '
      ([$cfs[] | select(.name == "BR-DISK") | .id][0]) as $id
      | any(.[] | select(.name == $p) | .formatItems[]; .format == $id and .score <= -10000)' <<< "$profiles" 2>/dev/null || echo false)"
  }
  [ -n "$SONARR_KEY" ] && check_junk_filter "Sonarr" "$SONARR_URL" "$SONARR_KEY" "$SONARR_PROFILE"
  [ -n "$SONARR_ANIME_KEY" ] && check_junk_filter "Sonarr Anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY" "$SONARR_ANIME_PROFILE"
  [ -n "$RADARR_KEY" ] && check_junk_filter "Radarr" "$RADARR_URL" "$RADARR_KEY" "$RADARR_PROFILE"

  info "Moonfin..."
  check "Moonfin web app (/Moonfin/Web/)" "$(case "$(http_code "$JELLYFIN_URL/Moonfin/Web/")" in 2*|3*) echo true ;; *) echo false ;; esac)"

  info "Services (launchd)..."
  local svc
  for svc in $SERVICE_NAMES; do
    check "Service: $svc ($(svc_state "$svc"))" "$(case "$(svc_state "$svc")" in running*) echo true ;; *) echo false ;; esac)"
  done

  info "Tailscale..."
  TS_CLI="$(detect_tailscale_cli)"
  if [ -n "$TS_CLI" ]; then
    pass "Tailscale installed"
    if "$TS_CLI" status &>/dev/null; then
      TS_IP=$($TS_CLI ip -4 2>/dev/null || echo "")
      if [ -n "$TS_IP" ]; then
        pass "Tailscale connected ($TS_IP)"
      else
        fail "Tailscale connected but no IPv4 address"
      fi
      SERVE_STATUS=$($TS_CLI serve status 2>/dev/null || echo "")
      if echo "$SERVE_STATUS" | grep -q "https"; then
        pass "Tailscale HTTPS configured"
      else
        skip "Tailscale HTTPS not configured"
      fi
    else
      skip "Tailscale not connected (remote access unavailable)"
    fi
  else
    skip "Tailscale not installed"
  fi

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

  if [ "$MODE" = "test" ]; then
    return "$TESTS_FAILED"
  fi

  return 0
}
