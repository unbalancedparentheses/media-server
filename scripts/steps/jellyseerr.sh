#!/usr/bin/env bash

configure_jellyseerr() {
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
        echo "$UPDATED" > "$TMPDIR_SETUP/js_settings_tmp.json" && mv "$TMPDIR_SETUP/js_settings_tmp.json" "$JS_SETTINGS"
        sync
        docker restart jellyseerr >/dev/null 2>&1
        ok "Jellyfin server pre-configured"
        sleep 8
        wait_for "Jellyseerr" "$JELLYSEERR_URL"
      fi
    fi
  fi

  # Authenticate — serverType:2 = Jellyfin (required for initial admin creation)
  JS_COOKIE=""
  JS_BODY_BASE=$(jq -nc --arg u "$JELLYFIN_USER" --arg p "$JELLYFIN_PASS" '{username:$u,password:$p,email:"admin@media.local"}')
  for AUTH_BODY in \
    "$(echo "$JS_BODY_BASE" | jq -c '. + {serverType:2}')" \
    "$JS_BODY_BASE"; do
    JS_AUTH_RESP=$(api_retry curl -s -c - -X POST "$JELLYSEERR_URL/api/v1/auth/jellyfin" \
      -H "Content-Type: application/json" \
      -d "$AUTH_BODY" 2>/dev/null || echo "")
    JS_COOKIE=$(echo "$JS_AUTH_RESP" | extract_cookie connect.sid || echo "")
    if [ -n "$JS_COOKIE" ]; then
      ok "Authenticated"
      break
    fi
  done
  [ -z "$JS_COOKIE" ] && warn "Could not authenticate (check Jellyfin credentials)"

  if [ -n "$JS_COOKIE" ]; then
    JA=(-b "connect.sid=$JS_COOKIE")

    # Sync & enable Jellyfin libraries (GET ?sync=true fetches, GET ?enable=ids saves)
    LIBRARIES=$(api GET "$JELLYSEERR_URL/api/v1/settings/jellyfin/library?sync=true" "${JA[@]}" 2>/dev/null || echo "[]")
    if echo "$LIBRARIES" | jq -e '.[0]' >/dev/null 2>&1; then
      LIB_IDS=$(echo "$LIBRARIES" | jq -r '.[].id' | paste -sd ',' -)
      if [ -n "$LIB_IDS" ]; then
        api GET "$JELLYSEERR_URL/api/v1/settings/jellyfin/library?enable=$LIB_IDS" "${JA[@]}" >/dev/null 2>&1 && \
          ok "Libraries synced" || true
      fi
    fi

    # Add Sonarr
    EXISTING_JS_SONARR_NAMES=$(api GET "$JELLYSEERR_URL/api/v1/settings/sonarr" "${JA[@]}" 2>/dev/null | jq -r '.[].name' 2>/dev/null || echo "")

    add_js_sonarr() {
      local name="$1" hostname="$2" key="$3" url="$4" dir="$5" ext_url="$6" is_default="$7" anime="${8:-false}" profile_name="$9"
      if echo "$EXISTING_JS_SONARR_NAMES" | grep -q "^${name}$"; then
        ok "$name already connected"
        return
      fi
      PROFILE=$(arr_profile "$url" "$key" "$profile_name")
      PID=$(echo "$PROFILE" | jq '.id // 1' 2>/dev/null || echo 1)
      PNAME=$(echo "$PROFILE" | jq -r '.name // "Any"' 2>/dev/null || echo "Any")
      JS_SONARR_JSON=$(jq -nc \
        --arg name "$name" --arg host "$hostname" --arg key "$key" \
        --argjson pid "$PID" --arg pname "$PNAME" --arg dir "$dir" \
        --argjson is_default "$is_default" --arg ext "$ext_url" --argjson anime "$anime" \
        '{name:$name, hostname:$host, port:8989, useSsl:false, apiKey:$key,
          baseUrl:"", activeProfileId:$pid, activeProfileName:$pname, activeDirectory:$dir,
          is4k:false, enableSeasonFolders:true, isDefault:$is_default, externalUrl:$ext,
          enableSearch:true} + (if $anime then {seriesType:"anime", animeSeriesType:"anime"} else {} end)')
      api POST "$JELLYSEERR_URL/api/v1/settings/sonarr" "${JA[@]}" -d "$JS_SONARR_JSON" >/dev/null 2>&1 && \
        ok "$name connected (profile: $PNAME)" || warn "Could not add $name"
    }

    [ -n "$SONARR_KEY" ] && \
      add_js_sonarr "Sonarr" "sonarr" "$SONARR_KEY" "$SONARR_URL" "/media/tv" "http://localhost:8989" "true" "false" "$SONARR_PROFILE"
    [ -n "$SONARR_ANIME_KEY" ] && \
      add_js_sonarr "Sonarr Anime" "sonarr-anime" "$SONARR_ANIME_KEY" "$SONARR_ANIME_URL" "/media/anime" "http://localhost:8990" "false" "true" "$SONARR_ANIME_PROFILE"

    # Add Radarr
    EXISTING_JS_RADARR=$(api GET "$JELLYSEERR_URL/api/v1/settings/radarr" "${JA[@]}" 2>/dev/null | jq 'length' 2>/dev/null || echo "0")
    if [ "$EXISTING_JS_RADARR" = "0" ] || [ -z "$EXISTING_JS_RADARR" ]; then
      [ -n "$RADARR_KEY" ] && {
        PROFILE=$(arr_profile "$RADARR_URL" "$RADARR_KEY" "$RADARR_PROFILE")
        PID=$(echo "$PROFILE" | jq '.id // 1' 2>/dev/null || echo 1)
        PNAME=$(echo "$PROFILE" | jq -r '.name // "Any"' 2>/dev/null || echo "Any")
        JS_RADARR_JSON=$(jq -nc \
          --arg key "$RADARR_KEY" --argjson pid "$PID" --arg pname "$PNAME" \
          '{name:"Radarr", hostname:"radarr", port:7878, useSsl:false, apiKey:$key,
            baseUrl:"", activeProfileId:$pid, activeProfileName:$pname, activeDirectory:"/media/movies",
            is4k:false, isDefault:true, externalUrl:"http://localhost:7878", minimumAvailability:"released",
            enableSearch:true}')
        api POST "$JELLYSEERR_URL/api/v1/settings/radarr" "${JA[@]}" -d "$JS_RADARR_JSON" >/dev/null 2>&1 && \
          ok "Radarr connected (profile: $PNAME)" || warn "Could not add Radarr"
      }
    else
      ok "Radarr already connected"
    fi

    # Keep existing connections in line with config.toml: search enabled and
    # the configured quality profile (older setups picked the first profile)
    sync_js_connections sonarr "Sonarr" "$SONARR_URL" "$SONARR_KEY" "$SONARR_PROFILE"
    sync_js_connections sonarr "Sonarr Anime" "$SONARR_ANIME_URL" "$SONARR_ANIME_KEY" "$SONARR_ANIME_PROFILE"
    sync_js_connections radarr "Radarr" "$RADARR_URL" "$RADARR_KEY" "$RADARR_PROFILE"

    api POST "$JELLYSEERR_URL/api/v1/settings/initialize" "${JA[@]}" >/dev/null 2>&1 || true
    ok "Setup finalized"
  else
    warn "Could not authenticate — complete wizard manually at $JELLYSEERR_URL"
  fi
}

# Quality profile {id,name} named $3 in the *arr at $1, else its first profile
arr_profile() {
  local url="$1" key="$2" name="$3"
  api GET "$url/api/v3/qualityprofile" -H "X-Api-Key: $key" 2>/dev/null | \
    jq -c --arg n "$name" '(map(select(.name == $n)) + .)[0] | {id, name}' 2>/dev/null || echo "{}"
}

# Set enableSearch and the configured profile on an existing Jellyseerr
# connection (kind: sonarr|radarr, matched by connection name)
sync_js_connections() {
  local kind="$1" name="$2" url="$3" key="$4" profile_name="$5" conn id updated profile
  [ -n "$key" ] || return 0
  conn=$(api GET "$JELLYSEERR_URL/api/v1/settings/$kind" "${JA[@]}" 2>/dev/null | \
    jq -c --arg n "$name" 'map(select(.name == $n))[0] // empty' 2>/dev/null || echo "")
  [ -n "$conn" ] || return 0
  profile=$(arr_profile "$url" "$key" "$profile_name")
  updated=$(echo "$conn" | jq -c --argjson p "$profile" --arg want "$profile_name" '
    .enableSearch = true
    | if $p.name == $want then .activeProfileId = $p.id | .activeProfileName = $p.name else . end')
  if [ "$updated" != "$conn" ]; then
    id=$(echo "$conn" | jq -r '.id')
    api PUT "$JELLYSEERR_URL/api/v1/settings/$kind/$id" "${JA[@]}" -d "$updated" >/dev/null 2>&1 && \
      ok "$name: search on, profile $(echo "$updated" | jq -r '.activeProfileName')" || warn "Could not update $name connection"
  fi
  return 0
}
