#!/usr/bin/env bash
# Intro Skipper: a Jellyfin plugin that detects intros and credits (by audio
# fingerprint) and marks them as media segments, so players show "Skip".

# Pinned like Moonbase: the manifest at a fixed commit (Jellyfin checks each
# download against its checksum) and the version to install. To update,
# point both at a newer commit/version of intro-skipper/manifest (the 12/
# folder is for Jellyfin 12).
INTRO_SKIPPER_COMMIT="70d9a498f6204b2eb3b978a3112a275371d7e775"
INTRO_SKIPPER_VERSION="12.0.4.0"
INTRO_SKIPPER_REPO_URL="https://raw.githubusercontent.com/intro-skipper/manifest/$INTRO_SKIPPER_COMMIT/12/manifest.json"
INTRO_SKIPPER_GUID="c83d86bb-a1e0-4c35-a113-e2101cf4ee6b"

configure_intro_skipper() {
  info "Configuring Intro Skipper..."
  if [ -z "${JELLYFIN_TOKEN:-}" ]; then
    warn "Skipping: not logged in to Jellyfin"
    return 0
  fi
  local JT plugin_id
  JT=$(jf_auth "$JELLYFIN_TOKEN")
  intro_skipper_pin_repository "$JT" || return 0

  plugin_id=$(intro_skipper_plugin_id)
  if [ -z "$plugin_id" ]; then
    api POST "$JELLYFIN_URL/Packages/Installed/Intro%20Skipper?assemblyGuid=$INTRO_SKIPPER_GUID&version=$INTRO_SKIPPER_VERSION&repositoryUrl=$(urlencode "$INTRO_SKIPPER_REPO_URL")" -H "$JT" >/dev/null || \
      { warn "Could not install Intro Skipper $INTRO_SKIPPER_VERSION"; return 0; }
    # Installation is asynchronous; the plugin loads on the next restart
    local start=$SECONDS
    until compgen -G "$CONFIG_DIR/jellyfin/data/plugins/Intro Skipper*" >/dev/null; do
      [ $((SECONDS - start)) -ge 90 ] && { warn "Intro Skipper download didn't finish; re-run setup"; return 0; }
      sleep 1
    done
    ok "Intro Skipper installed; restarting Jellyfin"
    svc_restart jellyfin
    sleep 3
    wait_for "Jellyfin" "$JELLYFIN_URL/health"
    jellyfin_login
    JT=$(jf_auth "$JELLYFIN_TOKEN")
    plugin_id=$(intro_skipper_plugin_id)
    [ -n "$plugin_id" ] || { warn "Intro Skipper didn't load after restart (see $LOG_DIR/jellyfin.log)"; return 0; }
    # Analyze what's already in the library once; new episodes are
    # analyzed as they're added
    local task_id
    task_id=$(api GET "$JELLYFIN_URL/ScheduledTasks" -H "$JT" | \
      jq -r '[.[] | select(.Key == "CPBIntroSkipperDetectIntrosCredits" or (.Name | test("Detect.*(Intro|Segment)"; "i")))][0].Id // empty' 2>/dev/null || true)
    [ -n "$task_id" ] && api POST "$JELLYFIN_URL/ScheduledTasks/Running/$task_id" -H "$JT" >/dev/null && \
      ok "Analyzing the library for intros and credits (runs in the background)"
  fi
  intro_skipper_enable_libraries "$JT"
  local installed
  installed=$(api GET "$JELLYFIN_URL/Plugins" -H "$JT" | jq -r --arg id "$plugin_id" '.[] | select(.Id == $id) | .Version' 2>/dev/null || true)
  if [ "$installed" = "$INTRO_SKIPPER_VERSION" ]; then
    ok "Intro Skipper $installed loaded"
  else
    warn "Intro Skipper $installed is installed; setup pins $INTRO_SKIPPER_VERSION"
  fi
}

intro_skipper_plugin_id() {
  api GET "$JELLYFIN_URL/Plugins" -H "$(jf_auth "$JELLYFIN_TOKEN")" | \
    jq -r --arg g "$INTRO_SKIPPER_GUID" '.[] | select((.Id | ascii_downcase | gsub("-"; "")) == ($g | gsub("-"; ""))) | .Id' | head -1 || true
}

intro_skipper_pin_repository() {  # jellyfin-auth-header
  jellyfin_pin_repository "$1" "Intro Skipper $INTRO_SKIPPER_VERSION" "$INTRO_SKIPPER_REPO_URL" "intro-skipper"
}

# Jellyfin 12 runs a media-segment provider only for libraries that list it
# (library options → Media Segment Providers); enable Intro Skipper for the
# TV and Anime libraries so new episodes are analyzed too
intro_skipper_enable_libraries() {  # jellyfin-auth-header
  local libs lib name id options updated
  libs=$(api GET "$JELLYFIN_URL/Library/VirtualFolders" -H "$1") || { warn "Could not read Jellyfin's libraries"; return 0; }
  while IFS= read -r lib; do
    name=$(jq -r .Name <<< "$lib"); id=$(jq -r .ItemId <<< "$lib"); options=$(jq -c .LibraryOptions <<< "$lib")
    updated=$(jq -c '.MediaSegmentProviderOrder = (["Intro Skipper"] + ((.MediaSegmentProviderOrder // []) - ["Intro Skipper"]))
      | .DisabledMediaSegmentProviders = ((.DisabledMediaSegmentProviders // []) - ["Intro Skipper"])' <<< "$options")
    if [ "$updated" = "$options" ]; then
      ok "Intro Skipper on for '$name'"
    elif api POST "$JELLYFIN_URL/Library/VirtualFolders/LibraryOptions" -H "$1" \
        -d "$(jq -nc --arg id "$id" --argjson o "$updated" '{Id: $id, LibraryOptions: $o}')" >/dev/null; then
      ok "Intro Skipper on for '$name'"
    else
      warn "Could not turn on Intro Skipper for '$name'"
    fi
  done < <(jq -c '.[] | select(.CollectionType == "tvshows")' <<< "$libs")
}
