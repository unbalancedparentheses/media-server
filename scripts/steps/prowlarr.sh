#!/usr/bin/env bash

configure_prowlarr() {
  if [ -n "$PROWLARR_KEY" ]; then
    info "Configuring Prowlarr..."
    PH="X-Api-Key: $PROWLARR_KEY"

    EXISTING_APPS=$(api GET "$PROWLARR_URL/api/v1/applications" -H "$PH" | jq -r '.[].name' 2>/dev/null || echo "")

    SONARR_CATS="5000,5010,5020,5030,5040,5045,5050,5090"
    RADARR_CATS="2000,2010,2020,2030,2040,2045,2050,2060,2070,2080,2090"

    # One Sonarr handles TV and anime; every indexer syncs to it and Radarr
    [ -n "$SONARR_KEY" ]       && add_prowlarr_app "Sonarr"       "Sonarr" "$SONARR_INTERNAL"       "$SONARR_KEY"       "$SONARR_CATS"
    # Older setups had a separate anime Sonarr
    local old_app
    old_app=$(api GET "$PROWLARR_URL/api/v1/applications" -H "$PH" | jq -r '.[] | select(.name == "Sonarr Anime") | .id' 2>/dev/null || true)
    [ -n "$old_app" ] && api DELETE "$PROWLARR_URL/api/v1/applications/$old_app" -H "$PH" >/dev/null && \
      ok "Removed the old Sonarr Anime app"
    [ -n "$RADARR_KEY" ]       && add_prowlarr_app "Radarr"       "Radarr" "$RADARR_INTERNAL"       "$RADARR_KEY"       "$RADARR_CATS"

    # Byparr speaks the FlareSolverr API, so it's added as a FlareSolverr proxy
    EXISTING_PROXIES=$(api GET "$PROWLARR_URL/api/v1/indexerProxy" -H "$PH" | jq -r '.[].name' 2>/dev/null || echo "")
    if ! echo "$EXISTING_PROXIES" | grep -q "Byparr"; then
      api POST "$PROWLARR_URL/api/v1/indexerProxy" -H "$PH" -d '{
        "name":"Byparr","implementation":"FlareSolverr","configContract":"FlareSolverrSettings",
        "fields":[{"name":"host","value":"'"$BYPARR_URL"'"},{"name":"requestTimeout","value":60}]
      }' >/dev/null 2>&1 && ok "Byparr connected" || warn "Could not add Byparr"
    else ok "Byparr connected"; fi

    # qBittorrent in Prowlarr
    local prowl_qbit_id
    prowl_qbit_id=$(api GET "$PROWLARR_URL/api/v1/downloadclient" -H "$PH" | jq -r '[.[] | select(.implementation == "QBittorrent") | .id][0] // empty' 2>/dev/null || true)
    if [ -n "$prowl_qbit_id" ]; then
      sync_resource_fields "Prowlarr qBittorrent client" "$PROWLARR_URL/api/v1/downloadclient" "$prowl_qbit_id" \
        "$(jq -nc --arg u "$QBIT_USER" --arg p "$QBIT_PASS" '{username:$u, password:$p}')" "$PROWLARR_KEY"
    else
      PROWL_QBIT_JSON=$(jq -nc --arg u "$QBIT_USER" --arg p "$QBIT_PASS" \
        '{name:"qBittorrent",implementation:"QBittorrent",configContract:"QBittorrentSettings",
          enable:true,protocol:"torrent",priority:1,
          fields:[{name:"host",value:"localhost"},{name:"port",value:8081},
            {name:"username",value:$u},{name:"password",value:$p},
            {name:"category",value:"prowlarr"}]}')
      api POST "$PROWLARR_URL/api/v1/downloadclient" -H "$PH" -d "$PROWL_QBIT_JSON" >/dev/null 2>&1 && \
        ok "qBittorrent connected to Prowlarr" || true
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
    INDEXER_COUNT=$(cfg '.indexers | length' 2>/dev/null || echo "0")
    [[ "$INDEXER_COUNT" =~ ^[0-9]+$ ]] || INDEXER_COUNT=0
    EXISTING_INDEXERS=$(api GET "$PROWLARR_URL/api/v1/indexer" -H "$PH" | jq -r '.[].name' 2>/dev/null || echo "")
    SCHEMAS=""

    if [ "$INDEXER_COUNT" -gt 0 ]; then
      info "Adding indexers from config..."
      for i in $(seq 0 $((INDEXER_COUNT - 1))); do
        IDX_ENABLED=$(cfg ".indexers[$i].enable")
        IDX_NAME=$(cfg ".indexers[$i].name")
        if [ "$IDX_ENABLED" != "true" ]; then
          # Switched off in config.toml: disable it in Prowlarr too (kept, not deleted)
          local off_idx
          off_idx=$(api GET "$PROWLARR_URL/api/v1/indexer" -H "$PH" | jq -c --arg n "$IDX_NAME" '.[] | select(.name == $n and .enable)' 2>/dev/null || true)
          [ -n "$off_idx" ] && api PUT "$PROWLARR_URL/api/v1/indexer/$(jq -r .id <<< "$off_idx")?forceSave=true" -H "$PH" \
            -d "$(jq -c '.enable = false' <<< "$off_idx")" >/dev/null && ok "$IDX_NAME disabled"
          continue
        fi

        IDX_DEF=$(cfg ".indexers[$i].definitionName")
        IDX_FLARE=$(cfg ".indexers[$i].flaresolverr // false")

        if echo "$EXISTING_INDEXERS" | grep -q "^${IDX_NAME}$"; then
          # Route an existing indexer through Byparr if config now asks for it
          if [ "$IDX_FLARE" = "true" ] && [ -n "$FLARESOLVERR_TAG_ID" ]; then
            local existing_idx
            existing_idx=$(api GET "$PROWLARR_URL/api/v1/indexer" -H "$PH" | jq -c --arg n "$IDX_NAME" '.[] | select(.name == $n)')
            if ! jq -e --argjson t "$FLARESOLVERR_TAG_ID" '.tags | index($t)' <<< "$existing_idx" >/dev/null; then
              api PUT "$PROWLARR_URL/api/v1/indexer/$(jq -r .id <<< "$existing_idx")?forceSave=true" -H "$PH" \
                -d "$(jq -c --argjson t "$FLARESOLVERR_TAG_ID" '.tags += [$t]' <<< "$existing_idx")" >/dev/null && \
                ok "$IDX_NAME: now routed through Byparr" || warn "$IDX_NAME: could not route through Byparr"
              continue
            fi
          fi
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

        # Set name, enable, app profile, and tags (flaresolverr)
        IDX_TAGS="[]"
        [ "$IDX_FLARE" = "true" ] && [ -n "$FLARESOLVERR_TAG_ID" ] && IDX_TAGS=$(echo "$IDX_TAGS" | jq -c ". + [$FLARESOLVERR_TAG_ID]")
        SCHEMA=$(echo "$SCHEMA" | jq -c --arg name "$IDX_NAME" --argjson tags "$IDX_TAGS" \
          '.name = $name | .enable = true | del(.id) | .appProfileId = 1 | .tags = $tags' 2>/dev/null)

        # Write to temp file to avoid shell argument length limits
        echo "$SCHEMA" > "$TMPDIR_SETUP/prowlarr_indexer.json"
        api POST "$PROWLARR_URL/api/v1/indexer" -H "$PH" -d @"$TMPDIR_SETUP/prowlarr_indexer.json" >/dev/null 2>&1 && \
          ok "$IDX_NAME added" || warn "Could not add $IDX_NAME"
      done
      rm -f "$TMPDIR_SETUP/prowlarr_indexer.json"
    fi

    set_arr_login "Prowlarr" "$PROWLARR_URL" "$PROWLARR_KEY" v1
  fi
}

# Register an *arr app in Prowlarr (uses $PH, $EXISTING_APPS, $PROWLARR_URL)
add_prowlarr_app() {
  local name="$1" impl="$2" url="$3" key="$4" cats="$5" tags="${6:-}"
  if ! echo "$EXISTING_APPS" | grep -q "^${name}$"; then
    local tags_json="[]"
    [ -n "$tags" ] && tags_json="[$tags]"
    api_retry api POST "$PROWLARR_URL/api/v1/applications" -H "$PH" -d '{
      "name":"'"$name"'","implementation":"'"$impl"'","configContract":"'"$impl"'Settings",
      "syncLevel":"fullSync","tags":'"$tags_json"',
      "fields":[{"name":"prowlarrUrl","value":"'"$PROWLARR_INTERNAL"'"},
        {"name":"baseUrl","value":"'"$url"'"},{"name":"apiKey","value":"'"$key"'"},
        {"name":"syncCategories","value":['"$cats"']}]
    }' >/dev/null 2>&1 && ok "$name connected" || warn "Could not connect $name"
  else
    # Keep the URLs and API key current (e.g. after a Sonarr reinstall)
    local app_id
    app_id=$(api GET "$PROWLARR_URL/api/v1/applications" -H "$PH" | jq -r --arg n "$name" '.[] | select(.name == $n) | .id')
    [ -n "$app_id" ] && sync_resource_fields "Prowlarr $name app" "$PROWLARR_URL/api/v1/applications" "$app_id" \
      "$(jq -nc --arg p "$PROWLARR_INTERNAL" --arg b "$url" --arg k "$key" '{prowlarrUrl:$p, baseUrl:$b, apiKey:$k}')" "$PROWLARR_KEY"
    ok "$name connected"
  fi
}
