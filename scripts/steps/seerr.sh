#!/usr/bin/env bash

configure_seerr() {
  info "Configuring Seerr..."

  # Sign in with the Jellyfin admin. The very first sign-in also tells Seerr
  # where Jellyfin is (serverType 2 = Jellyfin) and makes that user Seerr's
  # admin; after that Seerr rejects a hostname, so try without one first.
  JS_COOKIE=""
  local body resp
  body=$(jq -nc --arg u "$JELLYFIN_USER" --arg p "$JELLYFIN_PASS" '{username:$u, password:$p, email:"admin@media.local"}')
  for AUTH_BODY in "$body" \
    "$(jq -c '. + {hostname:"localhost", port:8096, useSsl:false, urlBase:"", serverType:2}' <<< "$body")"; do
    resp=$(api_retry curl -s -c - -X POST "$SEERR_URL/api/v1/auth/jellyfin" \
      -H "Content-Type: application/json" -d "$AUTH_BODY" 2>/dev/null || echo "")
    JS_COOKIE=$(echo "$resp" | extract_cookie connect.sid || echo "")
    [ -n "$JS_COOKIE" ] && break
  done
  if [ -n "$JS_COOKIE" ]; then
    ok "Signed in as $JELLYFIN_USER (Jellyfin at localhost:8096)"
  else
    warn "Could not sign in to Seerr with the Jellyfin credentials"
  fi

  if [ -n "$JS_COOKIE" ]; then
    JA=(-b "connect.sid=$JS_COOKIE")

    # A sign-in can succeed without Seerr knowing where Jellyfin is (e.g. an
    # earlier run was interrupted after creating the admin); set it if missing
    local jf_settings
    jf_settings=$(api GET "$SEERR_URL/api/v1/settings/jellyfin" "${JA[@]}" || echo "{}")
    if [ -z "$(jq -r '.ip // ""' <<< "$jf_settings")" ]; then
      api POST "$SEERR_URL/api/v1/settings/jellyfin" "${JA[@]}" \
        -d '{"ip":"localhost","port":8096,"useSsl":false,"urlBase":""}' >/dev/null && \
        ok "Jellyfin address set (localhost:8096)" || warn "Could not set Seerr's Jellyfin address"
    fi

    # Sync & enable Jellyfin libraries (GET ?sync=true fetches, GET ?enable=ids saves)
    LIBRARIES=$(api GET "$SEERR_URL/api/v1/settings/jellyfin/library?sync=true" "${JA[@]}" 2>/dev/null || echo "[]")
    if echo "$LIBRARIES" | jq -e '.[0]' >/dev/null 2>&1; then
      LIB_IDS=$(echo "$LIBRARIES" | jq -r '.[].id' | paste -sd ',' -)
      if [ -n "$LIB_IDS" ]; then
        api GET "$SEERR_URL/api/v1/settings/jellyfin/library?enable=$LIB_IDS" "${JA[@]}" >/dev/null 2>&1 && \
          ok "Libraries synced" || true
      fi
    fi

    # Add Sonarr
    EXISTING_JS_SONARR_NAMES=$(api GET "$SEERR_URL/api/v1/settings/sonarr" "${JA[@]}" 2>/dev/null | jq -r '.[].name' 2>/dev/null || echo "")

    add_js_sonarr() {
      local name="$1" port="$2" key="$3" url="$4" dir="$5" ext_url="$6" is_default="$7" anime="${8:-false}" profile_name="$9"
      if echo "$EXISTING_JS_SONARR_NAMES" | grep -q "^${name}$"; then
        ok "$name already connected"
        return
      fi
      PROFILE=$(arr_profile "$url" "$key" "$profile_name")
      PID=$(echo "$PROFILE" | jq '.id // 1' 2>/dev/null || echo 1)
      PNAME=$(echo "$PROFILE" | jq -r '.name // "Any"' 2>/dev/null || echo "Any")
      JS_SONARR_JSON=$(jq -nc \
        --arg name "$name" --argjson port "$port" --arg key "$key" \
        --argjson pid "$PID" --arg pname "$PNAME" --arg dir "$dir" \
        --argjson is_default "$is_default" --arg ext "$ext_url" --argjson anime "$anime" \
        '{name:$name, hostname:"localhost", port:$port, useSsl:false, apiKey:$key,
          baseUrl:"", activeProfileId:$pid, activeProfileName:$pname, activeDirectory:$dir,
          is4k:false, enableSeasonFolders:true, isDefault:$is_default, externalUrl:$ext,
          enableSearch:true} + (if $anime then {seriesType:"anime", animeSeriesType:"anime"} else {} end)')
      api POST "$SEERR_URL/api/v1/settings/sonarr" "${JA[@]}" -d "$JS_SONARR_JSON" >/dev/null 2>&1 && \
        ok "$name connected (profile: $PNAME)" || warn "Could not add $name"
    }

    [ -n "$SONARR_KEY" ] && \
      add_js_sonarr "Sonarr" 8989 "$SONARR_KEY" "$SONARR_URL" "$TV_DIR" "http://localhost:8989" "true" "false" "$SONARR_PROFILE"
    [ -n "$SONARR_ANIME_KEY" ] && \
      add_js_sonarr "Sonarr Anime" 8990 "$SONARR_ANIME_KEY" "$SONARR_ANIME_URL" "$ANIME_DIR" "http://localhost:8990" "false" "true" "$SONARR_ANIME_PROFILE"

    # Add Radarr
    EXISTING_JS_RADARR=$(api GET "$SEERR_URL/api/v1/settings/radarr" "${JA[@]}" 2>/dev/null | jq 'length' 2>/dev/null || echo "0")
    if [ "$EXISTING_JS_RADARR" = "0" ] || [ -z "$EXISTING_JS_RADARR" ]; then
      [ -n "$RADARR_KEY" ] && {
        PROFILE=$(arr_profile "$RADARR_URL" "$RADARR_KEY" "$RADARR_PROFILE")
        PID=$(echo "$PROFILE" | jq '.id // 1' 2>/dev/null || echo 1)
        PNAME=$(echo "$PROFILE" | jq -r '.name // "Any"' 2>/dev/null || echo "Any")
        JS_RADARR_JSON=$(jq -nc \
          --arg key "$RADARR_KEY" --argjson pid "$PID" --arg pname "$PNAME" \
          '{name:"Radarr", hostname:"localhost", port:7878, useSsl:false, apiKey:$key,
            baseUrl:"", activeProfileId:$pid, activeProfileName:$pname, activeDirectory:"$MOVIES_DIR",
            is4k:false, isDefault:true, externalUrl:"http://localhost:7878", minimumAvailability:"released",
            enableSearch:true}')
        api POST "$SEERR_URL/api/v1/settings/radarr" "${JA[@]}" -d "$JS_RADARR_JSON" >/dev/null 2>&1 && \
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

    api POST "$SEERR_URL/api/v1/settings/initialize" "${JA[@]}" >/dev/null 2>&1 || true
    ok "Setup finalized"
  else
    warn "Could not authenticate — complete wizard manually at $SEERR_URL"
  fi
}

# Quality profile {id,name} named $3 in the *arr at $1, else its first profile
arr_profile() {
  local url="$1" key="$2" name="$3"
  api GET "$url/api/v3/qualityprofile" -H "X-Api-Key: $key" 2>/dev/null | \
    jq -c --arg n "$name" '(map(select(.name == $n)) + .)[0] | {id, name}' 2>/dev/null || echo "{}"
}

# Set enableSearch and the configured profile on an existing Seerr
# connection (kind: sonarr|radarr, matched by connection name)
sync_js_connections() {
  local kind="$1" name="$2" url="$3" key="$4" profile_name="$5" conn id updated profile
  [ -n "$key" ] || return 0
  conn=$(api GET "$SEERR_URL/api/v1/settings/$kind" "${JA[@]}" 2>/dev/null | \
    jq -c --arg n "$name" 'map(select(.name == $n))[0] // empty' 2>/dev/null || echo "")
  [ -n "$conn" ] || return 0
  profile=$(arr_profile "$url" "$key" "$profile_name")
  updated=$(echo "$conn" | jq -c --argjson p "$profile" --arg want "$profile_name" '
    .enableSearch = true
    | if $p.name == $want then .activeProfileId = $p.id | .activeProfileName = $p.name else . end')
  if [ "$updated" != "$conn" ]; then
    id=$(echo "$conn" | jq -r '.id')
    api PUT "$SEERR_URL/api/v1/settings/$kind/$id" "${JA[@]}" -d "$updated" >/dev/null 2>&1 && \
      ok "$name: search on, profile $(echo "$updated" | jq -r '.activeProfileName')" || warn "Could not update $name connection"
  fi
  return 0
}
