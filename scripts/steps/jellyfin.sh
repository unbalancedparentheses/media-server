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
  # config.toml's login changed since it was last applied: log in with the
  # recorded one and change the password (Jellyfin requires the current one)
  local old_user old_pass
  old_user=$(creds_get jellyfin username) old_pass=$(creds_get jellyfin password)
  if [ -z "$JELLYFIN_TOKEN" ] && [ -n "$old_pass" ] && [ "$old_pass" != "$JELLYFIN_PASS" ]; then
    local new_user="$JELLYFIN_USER" new_pass="$JELLYFIN_PASS" uid
    JELLYFIN_USER="${old_user:-$new_user}" JELLYFIN_PASS="$old_pass"
    jellyfin_login
    JELLYFIN_USER="$new_user" JELLYFIN_PASS="$new_pass"
    if [ -n "$JELLYFIN_TOKEN" ]; then
      uid=$(api GET "$JELLYFIN_URL/Users/Me" -H "$(jf_auth "$JELLYFIN_TOKEN")" | jq -r '.Id // empty')
      if [ -n "$uid" ] && api POST "$JELLYFIN_URL/Users/$uid/Password" -H "$(jf_auth "$JELLYFIN_TOKEN")" \
          -d "$(jq -nc --arg c "$old_pass" --arg n "$JELLYFIN_PASS" '{CurrentPw:$c, NewPw:$n}')" >/dev/null; then
        ok "Jellyfin password changed to the one in config.toml"
      else
        warn "Could not change the Jellyfin password (retried next run)"
      fi
      [ -n "$old_user" ] && [ "$old_user" != "$JELLYFIN_USER" ] && \
        warn "Renaming the Jellyfin user isn't automatic: rename '$old_user' to '$JELLYFIN_USER' in Jellyfin (Dashboard → Users)"
      jellyfin_login
    fi
  fi
  # Only a login that works is recorded; otherwise the old one is kept
  [ -n "$JELLYFIN_TOKEN" ] && creds_set jellyfin "$JELLYFIN_USER" "$JELLYFIN_PASS"

  JELLYFIN_API_KEY=""
  if [ -n "$JELLYFIN_TOKEN" ]; then
    ok "Authenticated"

    local libs_changed=false
    EXISTING_LIBS=$(api GET "$JELLYFIN_URL/Library/VirtualFolders" \
      -H "$(jf_auth "$JELLYFIN_TOKEN")" | jq -r '.[].Name' 2>/dev/null || echo "")

    for lib_pair in "Movies:$MOVIES_DIR:movies" "TV Shows:$TV_DIR:tvshows" "Anime:$ANIME_DIR:tvshows"; do
      lib_name="${lib_pair%%:*}"; rest="${lib_pair#*:}"; lib_path="${rest%%:*}"; lib_type="${rest##*:}"
      if ! echo "$EXISTING_LIBS" | grep -q "^${lib_name}$"; then
        encoded=$(printf '%s' "$lib_name" | jq -sRr @uri)
        api POST "$JELLYFIN_URL/Library/VirtualFolders?name=${encoded}&collectionType=$lib_type&refreshLibrary=false" \
          -H "$(jf_auth "$JELLYFIN_TOKEN")" \
          -d '{"LibraryOptions":{}}' && \
          { ok "Created library: $lib_name"; libs_changed=true; } || warn "Could not create: $lib_name"
      fi

      # Ensure the path is attached (creating the library doesn't always set it)
      HAS_PATH=$(api GET "$JELLYFIN_URL/Library/VirtualFolders" -H "$(jf_auth "$JELLYFIN_TOKEN")" | \
        jq -r --arg name "$lib_name" --arg path "$lib_path" '.[] | select(.Name == $name) | .Locations[] | select(. == $path)' 2>/dev/null || echo "")
      if [ -z "$HAS_PATH" ]; then
        api POST "$JELLYFIN_URL/Library/VirtualFolders/Paths?refreshLibrary=true" \
          -H "$(jf_auth "$JELLYFIN_TOKEN")" \
          -d "$(jq -nc --arg n "$lib_name" --arg p "$lib_path" '{Name:$n, PathInfo:{Path:$p}}')" && \
          { ok "Library '$lib_name' → $lib_path"; libs_changed=true; } || warn "Could not add path to $lib_name"
      else
        ok "Library '$lib_name' → $lib_path"
      fi
    done

    # Setup's own key (named MediaServer), not whichever key happens to be listed last
    JELLYFIN_API_KEY=$(api GET "$JELLYFIN_URL/Auth/Keys" -H "$(jf_auth "$JELLYFIN_TOKEN")" | \
      jq -r '[.Items[]? | select(.AppName == "MediaServer")][0].AccessToken // empty' 2>/dev/null || true)
    if [ -z "$JELLYFIN_API_KEY" ]; then
      api POST "$JELLYFIN_URL/Auth/Keys?app=MediaServer" -H "$(jf_auth "$JELLYFIN_TOKEN")" >/dev/null 2>&1 || true
      JELLYFIN_API_KEY=$(api GET "$JELLYFIN_URL/Auth/Keys" -H "$(jf_auth "$JELLYFIN_TOKEN")" | \
        jq -r '[.Items[]? | select(.AppName == "MediaServer")][0].AccessToken // empty' 2>/dev/null || true)
    fi
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

    set_jellyfin_playback
    set_jellyfin_encoding

    # Reduce library monitor delay to 15 seconds for faster content detection
    SYS_CONFIG=$(api GET "$JELLYFIN_URL/System/Configuration" -H "$(jf_auth "$JELLYFIN_TOKEN")" 2>/dev/null || echo "")
    if [ -n "$SYS_CONFIG" ] && [ "$SYS_CONFIG" != "null" ]; then
      UPDATED_SYS=$(echo "$SYS_CONFIG" | jq -c '.LibraryMonitorDelay = 15')
      api POST "$JELLYFIN_URL/System/Configuration" \
        -H "$(jf_auth "$JELLYFIN_TOKEN")" \
        -d "$UPDATED_SYS" >/dev/null 2>&1 && \
        ok "Library monitor delay: 15s" || warn "Could not set monitor delay"
    fi

    # Jellyfin only starts watching a library for new files after it has
    # been scanned once: scan when a library or folder was just added (the
    # daily scan and real-time monitoring cover the rest)
    if [ "$libs_changed" = true ]; then
      api POST "$JELLYFIN_URL/Library/Refresh" -H "$(jf_auth "$JELLYFIN_TOKEN")" >/dev/null && \
        ok "Library scan started (enables real-time monitoring)" || warn "Could not start a library scan"
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

# Playback defaults for the Jellyfin user ([playback] in config.toml):
# subtitles always on in the preferred language (Jellyfin falls back to
# another one when the file has none in it), and the preferred audio
# language when a file has it (Japanese: dual-audio anime plays in
# Japanese; everything else uses the file's default track)
set_jellyfin_playback() {
  local me uid conf want
  me=$(api GET "$JELLYFIN_URL/Users/Me" -H "$(jf_auth "$JELLYFIN_TOKEN")") || { warn "Could not read the Jellyfin user's settings"; return 0; }
  uid=$(jq -r .Id <<< "$me")
  conf=$(jq -c .Configuration <<< "$me")
  want=$(jq -c --arg mode "$(cfg '.playback.subtitle_mode // "Always"')" --arg sub "$(cfg '.playback.subtitle_language // "eng"')" \
    --arg audio "$(cfg '.playback.audio_language // "jpn"')" '
    .SubtitleMode = $mode | .SubtitleLanguagePreference = $sub
    | .AudioLanguagePreference = (if $audio == "" then null else $audio end)
    # With a preferred audio language, pick by language, not the default flag
    | .PlayDefaultAudioTrack = ($audio == "")' <<< "$conf")
  if [ "$want" = "$conf" ]; then
    set_jellyfin_remux "$uid"
    ok "Playback: subtitles $(jq -r .SubtitleMode <<< "$want") ($(jq -r .SubtitleLanguagePreference <<< "$want")), audio $(jq -r '.AudioLanguagePreference // "default track"' <<< "$want")"
    return 0
  fi
  set_jellyfin_remux "$uid"
  # /Users/Configuration?userId= on Jellyfin 10.9+, /Users/<id>/Configuration before
  if api POST "$JELLYFIN_URL/Users/Configuration?userId=$uid" -H "$(jf_auth "$JELLYFIN_TOKEN")" -d "$want" >/dev/null || \
     api POST "$JELLYFIN_URL/Users/$uid/Configuration" -H "$(jf_auth "$JELLYFIN_TOKEN")" -d "$want" >/dev/null; then
    ok "Playback: subtitles $(jq -r .SubtitleMode <<< "$want") ($(jq -r .SubtitleLanguagePreference <<< "$want")), audio $(jq -r '.AudioLanguagePreference // "default track"' <<< "$want") (updated)"
  else
    warn "Could not set the Jellyfin user's playback settings"
  fi
}

# Hardware video conversion ([playback] hardware_acceleration): Apple's
# VideoToolbox encodes and decodes (H.264, HEVC, VP9, AV1) when a TV or
# phone can't play a file directly, instead of the CPU; it also does
# HDR-to-SDR tone mapping. Off: Jellyfin's default (software).
set_jellyfin_encoding() {
  local conf want on
  on=$(cfg_bool .playback.hardware_acceleration true)
  conf=$(api GET "$JELLYFIN_URL/System/Configuration/encoding" -H "$(jf_auth "$JELLYFIN_TOKEN")") || { warn "Could not read Jellyfin's transcoding settings"; return 0; }
  want=$(jq -c --argjson on "$on" '
    if $on then
      .HardwareAccelerationType = "videotoolbox" | .EnableHardwareEncoding = true
      | .HardwareDecodingCodecs = ["h264", "hevc", "vp9", "av1"]
      | .EnableDecodingColorDepth10Hevc = true | .EnableDecodingColorDepth10Vp9 = true
      | .EnableVideoToolboxTonemapping = true
    else
      .HardwareAccelerationType = "none"
    end' <<< "$conf")
  if [ "$want" = "$conf" ]; then
    ok "Transcoding: $([ "$on" = true ] && echo "hardware (VideoToolbox)" || echo software)"
  elif api POST "$JELLYFIN_URL/System/Configuration/encoding" -H "$(jf_auth "$JELLYFIN_TOKEN")" -d "$want" >/dev/null; then
    ok "Transcoding: $([ "$on" = true ] && echo "hardware (VideoToolbox)" || echo software) (updated)"
  else
    warn "Could not set Jellyfin's transcoding settings"
  fi
}

# [playback] allow_remux (default false): when a device can't play a file
# directly, Jellyfin either copies the video into a stream ("remux") or
# converts it. Copying keeps the file's own keyframes, which in Blu-ray
# encodes can be 10 s apart, and browsers' players can stall on that
# (playback stopping at a fixed minute). Off: such devices get the video
# converted by the hardware encoder with a keyframe every 3 s. Devices that
# play the file directly (Safari, most TV apps) aren't affected.
# Downloading to devices (to watch offline, e.g. on a flight) stays allowed.
set_jellyfin_remux() {  # user-id
  local policy want
  want=$(cfg_bool .playback.allow_remux false)
  policy=$(api GET "$JELLYFIN_URL/Users/$1" -H "$(jf_auth "$JELLYFIN_TOKEN")" | jq -c '.Policy' 2>/dev/null) || return 0
  [ -n "$policy" ] && [ "$policy" != null ] || return 0
  [ "$(jq -r '.EnablePlaybackRemuxing' <<< "$policy")" = "$want" ] && \
    [ "$(jq -r '.EnableContentDownloading' <<< "$policy")" = true ] && return 0
  api POST "$JELLYFIN_URL/Users/$1/Policy" -H "$(jf_auth "$JELLYFIN_TOKEN")" \
    -d "$(jq -c --argjson w "$want" '.EnablePlaybackRemuxing = $w | .EnableContentDownloading = true' <<< "$policy")" >/dev/null && \
    ok "Playback: remuxing $([ "$want" = true ] && echo allowed || echo "off (devices that can't play a file directly get it converted)"); downloads to devices allowed" || \
    warn "Could not change the Jellyfin user's playback settings"
}
