#!/usr/bin/env bash

configure_jellyfin() {
  info "Configuring Jellyfin..."


  JELLYFIN_STARTUP=$(curl -sf "$JELLYFIN_URL/Startup/Configuration" 2>/dev/null || echo "")
  if echo "$JELLYFIN_STARTUP" | grep -q "UICulture"; then
    api POST "$JELLYFIN_URL/Startup/Configuration" -H "$JF_HEADER" \
      -d '{"UICulture":"en-US","MetadataCountryCode":"US","PreferredMetadataLanguage":"en"}' || true
    # GET creates the first user; POST renames it and sets its password.
    # Only finish the wizard once that worked, or Jellyfin ends up with no
    # usable admin and the wizard can't be re-run.
    api GET "$JELLYFIN_URL/Startup/User" -H "$JF_HEADER" >/dev/null || true
    api POST "$JELLYFIN_URL/Startup/User" -H "$JF_HEADER" \
      -d "$(jq -nc --arg n "$JELLYFIN_USER" --arg p "$JELLYFIN_PASS" '{Name:$n,Password:$p}')" >/dev/null || \
      err "Could not create the Jellyfin admin user; finish the wizard at $JELLYFIN_URL and re-run setup"
    api POST "$JELLYFIN_URL/Startup/Complete" -H "$JF_HEADER" || true
    ok "Admin user '$JELLYFIN_USER' created"
  else
    ok "Already configured"
  fi

  jellyfin_login

  JELLYFIN_API_KEY=""
  if [ -n "$JELLYFIN_TOKEN" ]; then
    ok "Authenticated"

    EXISTING_LIBS=$(api GET "$JELLYFIN_URL/Library/VirtualFolders" \
      -H "$(jf_auth "$JELLYFIN_TOKEN")" | jq -r '.[].Name' 2>/dev/null || echo "")

    for lib_pair in "Movies:$MOVIES_DIR:movies" "TV Shows:$TV_DIR:tvshows" "Anime:$ANIME_DIR:tvshows"; do
      lib_name="${lib_pair%%:*}"; rest="${lib_pair#*:}"; lib_path="${rest%%:*}"; lib_type="${rest##*:}"
      if ! echo "$EXISTING_LIBS" | grep -q "^${lib_name}$"; then
        encoded=$(printf '%s' "$lib_name" | jq -sRr @uri)
        api POST "$JELLYFIN_URL/Library/VirtualFolders?name=${encoded}&collectionType=$lib_type&refreshLibrary=false" \
          -H "$(jf_auth "$JELLYFIN_TOKEN")" \
          -d '{"LibraryOptions":{}}' && \
          ok "Created library: $lib_name" || warn "Could not create: $lib_name"
      fi

      # Ensure the path is attached (creating the library doesn't always set it)
      HAS_PATH=$(api GET "$JELLYFIN_URL/Library/VirtualFolders" -H "$(jf_auth "$JELLYFIN_TOKEN")" | \
        jq -r --arg name "$lib_name" --arg path "$lib_path" '.[] | select(.Name == $name) | .Locations[] | select(. == $path)' 2>/dev/null || echo "")
      if [ -z "$HAS_PATH" ]; then
        api POST "$JELLYFIN_URL/Library/VirtualFolders/Paths?refreshLibrary=true" \
          -H "$(jf_auth "$JELLYFIN_TOKEN")" \
          -d "{\"Name\":\"$lib_name\",\"PathInfo\":{\"Path\":\"$lib_path\"}}" && \
          ok "Library '$lib_name' → $lib_path" || warn "Could not add path to $lib_name"
      else
        ok "Library '$lib_name' → $lib_path"
      fi
    done

    EXISTING_KEYS=$(api GET "$JELLYFIN_URL/Auth/Keys" -H "$(jf_auth "$JELLYFIN_TOKEN")" 2>/dev/null | jq '.Items | length' 2>/dev/null || echo "0")
    if [ "$EXISTING_KEYS" = "0" ] || [ -z "$EXISTING_KEYS" ]; then
      api POST "$JELLYFIN_URL/Auth/Keys?app=MediaServer" -H "$(jf_auth "$JELLYFIN_TOKEN")" >/dev/null 2>&1 || true
    fi
    JELLYFIN_API_KEY=$(api GET "$JELLYFIN_URL/Auth/Keys" -H "$(jf_auth "$JELLYFIN_TOKEN")" 2>/dev/null | jq -r '.Items[-1].AccessToken // empty' 2>/dev/null || echo "")
    [ -n "$JELLYFIN_API_KEY" ] && ok "API key: $(mask "$JELLYFIN_API_KEY")"

    # Enable real-time monitoring and daily scans on all libraries
    LIBS_JSON=$(api GET "$JELLYFIN_URL/Library/VirtualFolders" -H "$(jf_auth "$JELLYFIN_TOKEN")" 2>/dev/null || echo "[]")
    echo "$LIBS_JSON" | jq -c '.[]' 2>/dev/null | while IFS= read -r LIB; do
      LIB_NAME=$(echo "$LIB" | jq -r '.Name')
      LIB_ID=$(echo "$LIB" | jq -r '.ItemId')
      LIB_OPTIONS=$(echo "$LIB" | jq -c '.LibraryOptions' 2>/dev/null)
      if [ -n "$LIB_OPTIONS" ] && [ "$LIB_OPTIONS" != "null" ]; then
        UPDATED_OPTIONS=$(echo "$LIB_OPTIONS" | jq -c '.EnableRealtimeMonitor = true | .AutomaticRefreshIntervalDays = 1')
        api POST "$JELLYFIN_URL/Library/VirtualFolders/LibraryOptions" \
          -H "$(jf_auth "$JELLYFIN_TOKEN")" \
          -d "{\"Id\":\"$LIB_ID\",\"LibraryOptions\":$UPDATED_OPTIONS}" >/dev/null 2>&1 && \
          ok "Library '$LIB_NAME': real-time monitoring + daily scan" || warn "Could not update '$LIB_NAME' options"
      fi
    done

    # Reduce library monitor delay to 15 seconds for faster content detection
    SYS_CONFIG=$(api GET "$JELLYFIN_URL/System/Configuration" -H "$(jf_auth "$JELLYFIN_TOKEN")" 2>/dev/null || echo "")
    if [ -n "$SYS_CONFIG" ] && [ "$SYS_CONFIG" != "null" ]; then
      UPDATED_SYS=$(echo "$SYS_CONFIG" | jq -c '.LibraryMonitorDelay = 15')
      api POST "$JELLYFIN_URL/System/Configuration" \
        -H "$(jf_auth "$JELLYFIN_TOKEN")" \
        -d "$UPDATED_SYS" >/dev/null 2>&1 && \
        ok "Library monitor delay: 15s" || warn "Could not set monitor delay"
    fi
  else
    warn "Could not authenticate"
  fi
}

JF_HEADER='Authorization: MediaBrowser Client="setup", Device="script", DeviceId="setup-script", Version="1.0"'

# Sets JELLYFIN_TOKEN (empty if the login fails)
jellyfin_login() {
  local resp
  resp=$(api_retry api POST "$JELLYFIN_URL/Users/AuthenticateByName" -H "$JF_HEADER" \
    -d "$(jq -nc --arg u "$JELLYFIN_USER" --arg p "$JELLYFIN_PASS" '{Username:$u,Pw:$p}')" || echo "")
  JELLYFIN_TOKEN=$(echo "$resp" | jq -r '.AccessToken // empty' 2>/dev/null || echo "")
}
