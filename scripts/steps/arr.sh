#!/usr/bin/env bash
# Sonarr / Radarr.

configure_arr() {
  local name="$1" url="$2" key="$3" root_folder="$4" cat_field="$5" api_ver="${6:-v3}"
  info "Configuring $name..."
  local H="X-Api-Key: $key"

  # Root folders: exactly the given ones (newline-separated); others, like
  # a stale /downloads, are removed
  local root
  EXISTING_ROOTS=$(api_retry api GET "$url/api/$api_ver/rootfolder" -H "$H" 2>/dev/null || echo "[]")
  while read -r stale_id; do
    [ -n "$stale_id" ] && api DELETE "$url/api/$api_ver/rootfolder/$stale_id" -H "$H" >/dev/null 2>&1 && \
      ok "Removed stale root folder (id: $stale_id)"
  done < <(echo "$EXISTING_ROOTS" | jq -r --arg roots "$root_folder" \
    '($roots | split("\n")) as $want | .[] | select(.path as $p | $want | index($p) | not) | .id' 2>/dev/null)
  for root in $root_folder; do
    if echo "$EXISTING_ROOTS" | jq -e --arg p "$root" 'any(.[]; .path == $p)' >/dev/null 2>&1; then
      ok "Root folder: $root"
    else
      api_retry api POST "$url/api/$api_ver/rootfolder" -H "$H" -d "$(jq -nc --arg p "$root" '{path:$p}')" >/dev/null && \
        ok "Root folder: $root" || warn "Could not add root folder $root"
    fi
  done

  local clients qbit_id sab_id
  clients=$(api GET "$url/api/$api_ver/downloadclient" -H "$H" 2>/dev/null || echo "[]")
  qbit_id=$(jq -r '[.[] | select(.implementation == "QBittorrent") | .id][0] // empty' <<< "$clients")
  sab_id=$(jq -r '[.[] | select(.implementation == "Sabnzbd") | .id][0] // empty' <<< "$clients")

  if [ -z "$qbit_id" ]; then
    QBIT_DL_JSON=$(jq -nc --arg u "$QBIT_USER" --arg p "$QBIT_PASS" --arg cf "$cat_field" --arg cn "$name" \
      '{name:"qBittorrent",implementation:"QBittorrent",configContract:"QBittorrentSettings",
        enable:true,protocol:"torrent",priority:1,
        fields:[{name:"host",value:"localhost"},{name:"port",value:8081},
          {name:"username",value:$u},{name:"password",value:$p},
          {name:$cf,value:$cn}]}')
    api POST "$url/api/$api_ver/downloadclient" -H "$H" -d "$QBIT_DL_JSON" >/dev/null 2>&1 && \
      ok "qBittorrent connected (category: $name)" || warn "Could not add qBittorrent"
  else
    # Keep the login in step with config.toml
    sync_resource_fields "$name qBittorrent client" "$url/api/$api_ver/downloadclient" "$qbit_id" \
      "$(jq -nc --arg u "$QBIT_USER" --arg p "$QBIT_PASS" '{username:$u, password:$p}')" "$key"
    ok "qBittorrent connected"
  fi

  if [ -n "$SABNZBD_KEY" ] && [ -z "$sab_id" ]; then
    api POST "$url/api/$api_ver/downloadclient" -H "$H" -d '{
      "name":"SABnzbd","implementation":"Sabnzbd","configContract":"SabnzbdSettings",
      "enable":true,"protocol":"usenet","priority":2,
      "fields":[{"name":"host","value":"localhost"},{"name":"port","value":8080},
        {"name":"apiKey","value":"'"$SABNZBD_KEY"'"},{"name":"'"$cat_field"'","value":"'"$name"'"}]
    }' >/dev/null 2>&1 && ok "SABnzbd connected (category: $name)" || warn "Could not add SABnzbd"
  elif [ -n "$SABNZBD_KEY" ]; then
    sync_resource_fields "$name SABnzbd client" "$url/api/$api_ver/downloadclient" "$sab_id" \
      "$(jq -nc --arg k "$SABNZBD_KEY" '{apiKey:$k}')" "$key"
    ok "SABnzbd connected"
  fi

  # Add Jellyfin notification connection (triggers library scan on import/upgrade)
  if [ -n "$JELLYFIN_API_KEY" ]; then
    local notif_id
    notif_id=$(api GET "$url/api/$api_ver/notification" -H "$H" | jq -r '[.[] | select(.implementation == "MediaBrowser") | .id][0] // empty' 2>/dev/null || true)
    if [ -z "$notif_id" ]; then
      api POST "$url/api/$api_ver/notification" -H "$H" -d '{
        "name":"Jellyfin","implementation":"MediaBrowser","configContract":"MediaBrowserSettings",
        "enable":true,"onDownload":true,"onUpgrade":true,"onRename":true,
        "fields":[{"name":"host","value":"localhost"},{"name":"port","value":8096},
          {"name":"useSsl","value":false},{"name":"apiKey","value":"'"$JELLYFIN_API_KEY"'"},
          {"name":"updateLibrary","value":true}]
      }' >/dev/null 2>&1 && ok "Jellyfin notification connected" || warn "Could not add Jellyfin notification"
    else
      # Setup's own Jellyfin key (older versions could store another app's)
      sync_resource_fields "$name Jellyfin notification" "$url/api/$api_ver/notification" "$notif_id" \
        "$(jq -nc --arg k "$JELLYFIN_API_KEY" '{apiKey:$k}')" "$key"
      ok "Jellyfin notification connected"
    fi
  fi

  set_arr_login "$name" "$url" "$key" "$api_ver" "$name"
}

enable_unknown_quality() {
  local url="$1" key="$2" api_ver="${3:-v3}"
  local H="X-Api-Key: $key"
  local PROFILE UNKNOWN_ALLOWED UPDATED
  PROFILE=$(api GET "$url/api/$api_ver/qualityprofile/1" -H "$H" 2>/dev/null || echo "")
  { [ -z "$PROFILE" ] || [ "$PROFILE" = "null" ]; } && return
  UNKNOWN_ALLOWED=$(echo "$PROFILE" | jq '[.items[] | select(.quality.id == 0) | .allowed][0]' 2>/dev/null)
  if [ "$UNKNOWN_ALLOWED" = "false" ]; then
    UPDATED=$(echo "$PROFILE" | jq '.items = [.items[] | if (.quality.id == 0) then .allowed = true else . end]')
    api PUT "$url/api/$api_ver/qualityprofile/1" -H "$H" -d "$UPDATED" >/dev/null 2>&1 && \
      ok "Quality: enabled Unknown quality" || true
  fi
}

# Refuse imports that would leave less than disk.min_free_gb free
set_min_free_space() {
  local label="$1" url="$2" key="$3" H="X-Api-Key: $3" mm want
  want=$(( DISK_MIN_GB * 1024 ))
  mm=$(api GET "$url/api/v3/config/mediamanagement" -H "$H") || { warn "$label: could not read media management settings"; return 0; }
  [ "$(jq -r '.minimumFreeSpaceWhenImporting' <<< "$mm")" = "$want" ] && return 0
  api PUT "$url/api/v3/config/mediamanagement" -H "$H" -d "$(jq -c --argjson w "$want" '.minimumFreeSpaceWhenImporting = $w' <<< "$mm")" >/dev/null && \
    ok "$label: imports stop below $DISK_MIN_GB GB free" || warn "$label: could not set minimum free space"
}

# Older versions ran a second Sonarr for anime. Add its series to Sonarr
# (as anime, keeping their folders, no search), copy its series, season and
# episode monitoring exactly, and rescan so the existing files are picked
# up. Its data folder is left in place.
migrate_anime_sonarr() {
  local db="$CONFIG_DIR/sonarr-anime/sonarr.db" marker="$STATE_DIR/sonarr-anime-migrated"
  local H="X-Api-Key: $SONARR_KEY" rows row old_id tvdb path have profile_id lookup new failed=0 added=0
  [ -f "$db" ] && [ ! -f "$marker" ] || return 0
  info "Moving the old anime Sonarr's series into Sonarr..."
  rows=$(sqlite3 -json "file:$db?mode=ro" 'SELECT Id, TvdbId, Path, Monitored, Seasons FROM Series' 2>/dev/null) || {
    warn "Could not read $db; its series were not moved (setup retries next run)"; return 0; }
  have=$(api GET "$SONARR_URL/api/v3/series" -H "$H" | jq -c '[.[].tvdbId]') || { warn "Could not list Sonarr series"; return 0; }
  profile_id=$(api GET "$SONARR_URL/api/v3/qualityprofile" -H "$H" | \
    jq -r --arg n "$SONARR_ANIME_PROFILE" '([.[] | select(.name == $n) | .id][0]) // .[0].id')
  while IFS= read -r row; do
    [ -n "$row" ] || continue
    old_id=$(jq -r .Id <<< "$row"); tvdb=$(jq -r .TvdbId <<< "$row"); path=$(jq -r .Path <<< "$row")
    jq -e --argjson t "$tvdb" 'index($t)' <<< "$have" >/dev/null && continue
    lookup=$(api GET "$SONARR_URL/api/v3/series/lookup?term=tvdb:$tvdb" -H "$H" | jq -c '.[0] // empty') || lookup=""
    # Added unmonitored with "skip" (Sonarr leaves episode monitoring alone);
    # the old choices are applied below
    new=""
    [ -n "$lookup" ] && new=$(api POST "$SONARR_URL/api/v3/series" -H "$H" -d "$(jq -c \
        --arg path "$path" --argjson pid "$profile_id" \
        '. + {path:$path, qualityProfileId:$pid, monitored:false, seriesType:"anime", seasonFolder:true,
              addOptions:{searchForMissingEpisodes:false, monitor:"skip"}}' <<< "$lookup")") || new=""
    if [ -n "$new" ] && migrate_monitoring "$db" "$old_id" "$(jq -r .id <<< "$new")" "$row"; then
      ok "Moved: $(jq -r .title <<< "$new")"
      added=$((added + 1))
    else
      warn "Could not move the series at $path (TVDB $tvdb)"
      failed=1
    fi
  done < <(jq -c '.[]' <<< "${rows:-[]}")
  [ "$added" -gt 0 ] && api POST "$SONARR_URL/api/v3/command" -H "$H" -d '{"name":"RescanSeries"}' >/dev/null
  if [ "$failed" = 0 ]; then
    touch "$marker"
    ok "Old anime Sonarr's series are in Sonarr; you can delete $CONFIG_DIR/sonarr-anime"
  fi
}

# Copy one series' monitoring from the old database: episodes, seasons and
# the series flag, once Sonarr has finished adding it (its refresh and scan
# would otherwise overwrite them), then check it took
migrate_monitoring() {  # db old-series-id new-series-id old-row
  local db="$1" old_id="$2" new_id="$3" row="$4" H="X-Api-Key: $SONARR_KEY" start=$SECONDS eps old_eps ids series want
  while :; do
    eps=$(api GET "$SONARR_URL/api/v3/episode?seriesId=$new_id" -H "$H" || echo "[]")
    if [ "$(jq 'length' <<< "$eps")" -gt 0 ] && \
       ! api GET "$SONARR_URL/api/v3/command" -H "$H" | jq -e 'any(.[]; (.name == "RefreshSeries" or .name == "RescanSeries") and (.status == "queued" or .status == "started"))' >/dev/null; then
      break
    fi
    [ $((SECONDS - start)) -ge 180 ] && { warn "Sonarr is still adding series $new_id"; return 1; }
    sleep 2
  done
  old_eps=$(sqlite3 -json "file:$db?mode=ro" "SELECT SeasonNumber AS s, EpisodeNumber AS e, Monitored AS m FROM Episodes WHERE SeriesId = $old_id" 2>/dev/null) || return 1
  old_eps="${old_eps:-[]}"
  for want in true false; do
    ids=$(jq -c --argjson old "$old_eps" --argjson want "$want" '
      ($old | map(select((.m == 1) == $want) | "\(.s)x\(.e)")) as $keys
      | [.[] | select(("\(.seasonNumber)x\(.episodeNumber)") as $k | $keys | index($k)) | .id]' <<< "$eps")
    [ "$ids" = "[]" ] && continue
    api PUT "$SONARR_URL/api/v3/episode/monitor" -H "$H" -d "$(jq -nc --argjson ids "$ids" --argjson m "$want" '{episodeIds:$ids, monitored:$m}')" >/dev/null || return 1
  done
  series=$(api GET "$SONARR_URL/api/v3/series/$new_id" -H "$H") || return 1
  api PUT "$SONARR_URL/api/v3/series/$new_id" -H "$H" -d "$(jq -c --argjson row "$row" '
    ($row.Seasons | fromjson | map({key: (.seasonNumber | tostring), value: .monitored}) | from_entries) as $old
    | .monitored = ($row.Monitored == 1)
    | .seasons |= map(if $old[(.seasonNumber | tostring)] != null then .monitored = $old[(.seasonNumber | tostring)] else . end)' <<< "$series")" >/dev/null || return 1

  # Every episode both databases know must now match
  eps=$(api GET "$SONARR_URL/api/v3/episode?seriesId=$new_id" -H "$H") || return 1
  jq -e --argjson old "$old_eps" '
    ($old | map({key: "\(.s)x\(.e)", value: (.m == 1)}) | from_entries) as $want
    | all(.[]; $want["\(.seasonNumber)x\(.episodeNumber)"] as $w | $w == null or .monitored == $w)' <<< "$eps" >/dev/null || \
    { warn "Episode monitoring of series $new_id didn't match the old Sonarr's"; return 1; }
}

configure_arrs() {
  # One Sonarr for TV and anime: Seerr sends anime to the anime folder
  [ -n "$SONARR_KEY" ]       && configure_arr "sonarr"       "$SONARR_URL"       "$SONARR_KEY"       "$TV_DIR"$'\n'"$ANIME_DIR" "tvCategory"
  [ -n "$RADARR_KEY" ]       && configure_arr "radarr"       "$RADARR_URL"       "$RADARR_KEY"       "$MOVIES_DIR" "movieCategory"
  [ -n "$SONARR_KEY" ]       && set_min_free_space "Sonarr" "$SONARR_URL" "$SONARR_KEY"
  [ -n "$SONARR_KEY" ]       && migrate_anime_sonarr
  [ -n "$RADARR_KEY" ]       && set_min_free_space "Radarr" "$RADARR_URL" "$RADARR_KEY"

  [ -n "$SONARR_KEY" ]       && enable_unknown_quality "$SONARR_URL"       "$SONARR_KEY"
  [ -n "$RADARR_KEY" ]       && enable_unknown_quality "$RADARR_URL"       "$RADARR_KEY"
}


# Junk-release filters: create the TRaSH custom formats in custom-formats/<app>
# (updating ones that already exist) and score them -10000 in every quality
# profile, whose minimum score is kept at 0 or above, so matches are rejected.
JUNK_SCORE=-10000
apply_junk_filters() {
  local label="$1" url="$2" key="$3" app="$4" H f payload name existing id scores="{}" score profiles
  H="X-Api-Key: $key"
  existing=$(api GET "$url/api/v3/customformat" -H "$H" || echo "[]")
  for f in "$SCRIPT_DIR/custom-formats/$app"/*.json; do
    # TRaSH's file format → the API's (fields as a list of name/value pairs)
    payload=$(jq -c '{name, includeCustomFormatWhenRenaming: false,
      specifications: [.specifications[] | {name, implementation, negate, required,
        fields: [.fields | to_entries[] | {name: .key, value: .value}]}]}' "$f")
    name=$(jq -r '.name' <<< "$payload")
    # Filters score -10000 (never grab); files can set their own score, e.g.
    # +100 for "Prefer HEVC", which [quality] prefer_h265 = false turns off
    score=$(jq -r ".mediaServerScore // $JUNK_SCORE" "$f")
    [ "$name" = "Prefer HEVC" ] && [ "$(cfg_bool .quality.prefer_h265 true)" != "true" ] && score=0
    id=$(jq -r --arg n "$name" '.[] | select(.name == $n) | .id' <<< "$existing" | head -1)
    if [ -n "$id" ]; then
      api PUT "$url/api/v3/customformat/$id" -H "$H" -d "$(jq -c --argjson id "$id" '. + {id: $id}' <<< "$payload")" >/dev/null || \
        { warn "$label: could not update custom format $name"; continue; }
    else
      id=$(api POST "$url/api/v3/customformat" -H "$H" -d "$payload" | jq -r '.id // empty') || true
      [ -n "$id" ] || { warn "$label: could not create custom format $name"; continue; }
    fi
    scores=$(jq -c --arg id "$id" --argjson s "$score" '. + {($id): $s}' <<< "$scores")
  done
  [ "$scores" != "{}" ] || return 0

  profiles=$(api GET "$url/api/v3/qualityprofile" -H "$H" || echo "[]")
  local profile updated
  while IFS= read -r profile; do
    [ -n "$profile" ] || continue
    updated=$(jq -c --argjson scores "$scores" '
      # Rejection relies on the profile requiring a score of at least 0
      .minFormatScore = ([.minFormatScore // 0, 0] | max)
      | [.formatItems[].format | tostring] as $have
      | .formatItems = ([.formatItems[] | (.format | tostring) as $f
            | if $scores[$f] != null then .score = $scores[$f] else . end]
          + [$scores | to_entries[] | select(.key as $k | $have | index($k) | not)
              | {format: (.key | tonumber), score: .value}])' <<< "$profile")
    [ "$updated" = "$profile" ] && continue
    api PUT "$url/api/v3/qualityprofile/$(jq -r '.id' <<< "$profile")" -H "$H" -d "$updated" >/dev/null || \
      warn "$label: could not update profile $(jq -r '.name' <<< "$profile")"
  done < <(jq -c '.[]' <<< "$profiles")
  ok "$label: release filters on (BR-DISK, LQ, Upscaled, Extras, Foreign Subtitles$([ "$app" = radarr ] && echo ", 3D")); HEVC preferred: $(cfg_bool .quality.prefer_h265 true)"
}

configure_junk_filters() {
  info "Blocking junk releases..."
  [ -n "$SONARR_KEY" ]       && apply_junk_filters "Sonarr"       "$SONARR_URL"       "$SONARR_KEY"       sonarr
  [ -n "$RADARR_KEY" ]       && apply_junk_filters "Radarr"       "$RADARR_URL"       "$RADARR_KEY"       radarr
  return 0
}
