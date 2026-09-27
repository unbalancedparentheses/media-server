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
# Progress is kept per series in sonarr-anime-migration.json: "added" (id
# in Sonarr) as soon as Sonarr accepts it, "done" once its monitoring is
# copied and checked, so an interrupted run finishes the series it
# started. Series that were already in Sonarr aren't touched.
migrate_anime_sonarr() {
  local db="$CONFIG_DIR/sonarr-anime/sonarr.db" marker="$STATE_DIR/sonarr-anime-migrated"
  local progress="$STATE_DIR/sonarr-anime-migration.json"
  local H="X-Api-Key: $SONARR_KEY" rows row old_id tvdb path have profile_id lookup new new_id state failed=0 added=0
  [ -f "$db" ] && [ ! -f "$marker" ] || return 0
  info "Moving the old anime Sonarr's series into Sonarr..."
  rows=$(sqlite3 -json "file:$db?mode=ro" 'SELECT Id, TvdbId, Path, Monitored, Seasons FROM Series' 2>/dev/null) || {
    warn "Could not read $db; its series were not moved (setup retries next run)"; return 0; }
  have=$(api GET "$SONARR_URL/api/v3/series" -H "$H") || { warn "Could not list Sonarr series"; return 0; }
  state='{"added":{},"done":[]}'
  [ -f "$progress" ] && state=$(jq -c '{added: (.added // {}), done: (.done // [])}' "$progress" 2>/dev/null || echo "$state")
  profile_id=$(api GET "$SONARR_URL/api/v3/qualityprofile" -H "$H" | \
    jq -r --arg a "$ANIME_PROFILE" --arg n "$SONARR_ANIME_PROFILE" '([.[] | select(.name == $a) | .id][0]) // ([.[] | select(.name == $n) | .id][0]) // .[0].id')
  while IFS= read -r row; do
    [ -n "$row" ] || continue
    old_id=$(jq -r .Id <<< "$row"); tvdb=$(jq -r .TvdbId <<< "$row"); path=$(jq -r .Path <<< "$row")
    jq -e --arg t "$tvdb" '.done | index($t)' <<< "$state" >/dev/null && continue
    new_id=$(jq -r --arg t "$tvdb" '.added[$t] // empty' <<< "$state")
    # Added by an earlier, interrupted run: still there?
    [ -n "$new_id" ] && ! jq -e --argjson id "$new_id" 'any(.[]; .id == $id)' <<< "$have" >/dev/null && new_id=""
    if [ -z "$new_id" ]; then
      if jq -e --argjson t "$tvdb" 'any(.[]; .tvdbId == $t)' <<< "$have" >/dev/null; then
        # Already in Sonarr before the migration: leave it as it is
        state=$(jq -c --arg t "$tvdb" '.done += [$t]' <<< "$state")
        migration_progress_save "$progress" "$state"
        continue
      fi
      lookup=$(api GET "$SONARR_URL/api/v3/series/lookup?term=tvdb:$tvdb" -H "$H" | jq -c '.[0] // empty') || lookup=""
      # Added unmonitored with "skip" (Sonarr leaves episode monitoring
      # alone); the old choices are applied below
      new=""
      [ -n "$lookup" ] && new=$(api POST "$SONARR_URL/api/v3/series" -H "$H" -d "$(jq -c \
          --arg path "$path" --argjson pid "$profile_id" \
          '. + {path:$path, qualityProfileId:$pid, monitored:false, seriesType:"anime", seasonFolder:true,
                addOptions:{searchForMissingEpisodes:false, monitor:"skip"}}' <<< "$lookup")") || new=""
      new_id=$(jq -r '.id // empty' <<< "$new" 2>/dev/null || true)
      if [ -z "$new_id" ]; then
        warn "Could not add the series at $path (TVDB $tvdb); retried next run"
        failed=1
        continue
      fi
      state=$(jq -c --arg t "$tvdb" --argjson id "$new_id" '.added[$t] = $id' <<< "$state")
      migration_progress_save "$progress" "$state"
      added=$((added + 1))
    fi
    if migrate_monitoring "$db" "$old_id" "$new_id" "$row"; then
      state=$(jq -c --arg t "$tvdb" '.done += [$t]' <<< "$state")
      migration_progress_save "$progress" "$state"
      ok "Moved: ${path##*/}"
    else
      warn "Series at $path is in Sonarr, but its monitoring wasn't copied yet; retried next run"
      failed=1
    fi
  done < <(jq -c '.[]' <<< "${rows:-[]}")
  [ "$added" -gt 0 ] && api POST "$SONARR_URL/api/v3/command" -H "$H" -d '{"name":"RescanSeries"}' >/dev/null
  if [ "$failed" = 0 ]; then
    touch "$marker"
    ok "Old anime Sonarr's series are in Sonarr; you can delete $CONFIG_DIR/sonarr-anime"
  fi
}

migration_progress_save() {  # file json
  local tmp="$1.tmp.$$"
  mkdir -p "$(dirname "$1")"
  printf '%s\n' "$2" > "$tmp" && mv -f "$tmp" "$1"
}

# Wait until Sonarr has finished adding a series: its episodes are listed
# and no refresh or scan is pending. Only then are the add options (such as
# monitor "none") applied, so monitoring changed earlier gets overwritten.
sonarr_series_settled() {  # series-id [timeout]
  local id="$1" max="${2:-180}" start=$SECONDS H="X-Api-Key: $SONARR_KEY"
  until [ "$(api GET "$SONARR_URL/api/v3/episode?seriesId=$id" -H "$H" | jq 'length' 2>/dev/null || echo 0)" -gt 0 ] && \
        ! api GET "$SONARR_URL/api/v3/command" -H "$H" | \
          jq -e 'any(.[]; (.name == "RefreshSeries" or .name == "RescanSeries") and (.status == "queued" or .status == "started"))' >/dev/null; do
    [ $((SECONDS - start)) -ge "$max" ] && return 1
    sleep 2
  done
}

# Copy one series' monitoring from the old database: episodes, seasons and
# the series flag, once Sonarr has finished adding it (its refresh and scan
# would otherwise overwrite them), then check it took
migrate_monitoring() {  # db old-series-id new-series-id old-row
  local db="$1" old_id="$2" new_id="$3" row="$4" H="X-Api-Key: $SONARR_KEY" eps old_eps ids series want
  sonarr_series_settled "$new_id" || { warn "Sonarr is still adding series $new_id"; return 1; }
  eps=$(api GET "$SONARR_URL/api/v3/episode?seriesId=$new_id" -H "$H") || return 1
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


# Release filters: create the custom formats in custom-formats/<app>
# (updating ones that already exist) and score them in the quality profiles.
# Filters score -10000 (never grabbed: profiles' minimum score is kept at 0
# or above); a file can set its own score ("mediaServerScore") and limit
# itself to the Anime profile or the other ones ("mediaServerProfiles":
# "anime" / "standard").
JUNK_SCORE=-10000
ANIME_PROFILE="Anime"
apply_junk_filters() {
  local label="$1" url="$2" key="$3" app="$4" H f payload name existing id formats="[]" score scope profiles
  H="X-Api-Key: $key"
  existing=$(api GET "$url/api/v3/customformat" -H "$H" || echo "[]")
  for f in "$SCRIPT_DIR/custom-formats/$app"/*.json; do
    # TRaSH's file format → the API's (fields as a list of name/value pairs)
    payload=$(jq -c '{name, includeCustomFormatWhenRenaming: false,
      specifications: [.specifications[] | {name, implementation, negate, required,
        fields: [.fields | to_entries[] | {name: .key, value: .value}]}]}' "$f")
    name=$(jq -r '.name' <<< "$payload")
    score=$(jq -r ".mediaServerScore // $JUNK_SCORE" "$f")
    scope=$(jq -r '.mediaServerProfiles // "all"' "$f")
    # Preferences config.toml can turn off
    case "$name" in
      "Prefer HEVC") [ "$(cfg_bool .quality.prefer_h265 true)" = true ] || score=0 ;;
      "Prefer English Audio") [ "$(cfg_bool .quality.prefer_english_audio true)" = true ] || score=0 ;;
      "Dubs Only") [ "$(cfg_bool .quality.anime_block_dubs true)" = true ] || score=0 ;;
      "Anime BD Tier"*|"Anime Web Tier"*) [ "$(cfg_bool .quality.anime_release_groups true)" = true ] || score=0 ;;
    esac
    id=$(jq -r --arg n "$name" '.[] | select(.name == $n) | .id' <<< "$existing" | head -1)
    if [ -n "$id" ]; then
      api PUT "$url/api/v3/customformat/$id" -H "$H" -d "$(jq -c --argjson id "$id" '. + {id: $id}' <<< "$payload")" >/dev/null || \
        { warn "$label: could not update custom format $name"; continue; }
    else
      id=$(api POST "$url/api/v3/customformat" -H "$H" -d "$payload" | jq -r '.id // empty') || true
      [ -n "$id" ] || { warn "$label: could not create custom format $name"; continue; }
    fi
    formats=$(jq -c --argjson id "$id" --argjson s "$score" --arg scope "$scope" '. + [{id: $id, score: $s, scope: $scope}]' <<< "$formats")
  done
  [ "$formats" != "[]" ] || return 0

  profiles=$(api GET "$url/api/v3/qualityprofile" -H "$H" || echo "[]")
  local profile updated
  while IFS= read -r profile; do
    [ -n "$profile" ] || continue
    updated=$(jq -c --argjson formats "$formats" --arg anime "$ANIME_PROFILE" '
      (.name == $anime) as $is_anime
      | ($formats | map({key: (.id | tostring),
          value: (if .scope == "all" or (.scope == "anime") == $is_anime then .score else 0 end)}) | from_entries) as $scores
      # Rejection relies on the profile requiring a score of at least 0
      | .minFormatScore = ([.minFormatScore // 0, 0] | max)
      | [.formatItems[].format | tostring] as $have
      | .formatItems = ([.formatItems[] | (.format | tostring) as $f
            | if $scores[$f] != null then .score = $scores[$f] else . end]
          + [$scores | to_entries[] | select(.key as $k | $have | index($k) | not)
              | {format: (.key | tonumber), score: .value}])' <<< "$profile")
    [ "$updated" = "$profile" ] && continue
    api PUT "$url/api/v3/qualityprofile/$(jq -r '.id' <<< "$profile")" -H "$H" -d "$updated" >/dev/null || \
      warn "$label: could not update profile $(jq -r '.name' <<< "$profile")"
  done < <(jq -c '.[]' <<< "$profiles")
  ok "$label: release filters on (BR-DISK, LQ, Upscaled, Extras, Foreign Subtitles$([ "$app" = radarr ] && echo ", 3D"))"
  ok "$label: prefer HEVC $(cfg_bool .quality.prefer_h265 true), English audio $(cfg_bool .quality.prefer_english_audio true)$([ "$app" = sonarr ] && echo "; anime: block dub-only releases $(cfg_bool .quality.anime_block_dubs true), rank release groups $(cfg_bool .quality.anime_release_groups true)")"
}

# Sonarr's "Anime" profile: a copy of quality.sonarr_anime_profile, where
# anime-only filters apply (dub-only releases blocked) and TV preferences
# don't. Seerr's anime requests use it, and anime series still on the base
# profile move to it. Kept in step with the base profile's qualities.
ensure_anime_profile() {
  local H="X-Api-Key: $SONARR_KEY" profiles base anime id moved
  profiles=$(api GET "$SONARR_URL/api/v3/qualityprofile" -H "$H") || { warn "Sonarr: could not read quality profiles"; return 0; }
  base=$(jq -c --arg n "$SONARR_ANIME_PROFILE" '[.[] | select(.name == $n)][0] // empty' <<< "$profiles")
  [ -n "$base" ] || { warn "Sonarr: profile '$SONARR_ANIME_PROFILE' not found; no Anime profile"; return 0; }
  anime=$(jq -c --arg n "$ANIME_PROFILE" '[.[] | select(.name == $n)][0] // empty' <<< "$profiles")
  if [ -z "$anime" ]; then
    api POST "$SONARR_URL/api/v3/qualityprofile" -H "$H" \
      -d "$(jq -c --arg n "$ANIME_PROFILE" 'del(.id) | .name = $n' <<< "$base")" >/dev/null && \
      ok "Sonarr: '$ANIME_PROFILE' profile created (from $SONARR_ANIME_PROFILE)" || { warn "Sonarr: could not create the Anime profile"; return 0; }
  else
    local synced
    synced=$(jq -c --argjson b "$base" '.items = $b.items | .cutoff = $b.cutoff | .upgradeAllowed = $b.upgradeAllowed' <<< "$anime")
    [ "$synced" = "$anime" ] || api PUT "$SONARR_URL/api/v3/qualityprofile/$(jq -r .id <<< "$anime")" -H "$H" -d "$synced" >/dev/null || \
      warn "Sonarr: could not update the Anime profile"
  fi
  id=$(api GET "$SONARR_URL/api/v3/qualityprofile" -H "$H" | jq -r --arg n "$ANIME_PROFILE" '.[] | select(.name == $n) | .id')
  moved=$(api GET "$SONARR_URL/api/v3/series" -H "$H" | jq -c --argjson base "$(jq .id <<< "$base")" \
    '[.[] | select(.seriesType == "anime" and .qualityProfileId == $base) | .id]')
  if [ -n "$id" ] && [ -n "$moved" ] && [ "$moved" != "[]" ]; then
    api PUT "$SONARR_URL/api/v3/series/editor" -H "$H" -d "$(jq -nc --argjson ids "$moved" --argjson p "$id" '{seriesIds: $ids, qualityProfileId: $p}')" >/dev/null && \
      ok "Sonarr: $(jq length <<< "$moved") anime series moved to the '$ANIME_PROFILE' profile" || warn "Sonarr: could not move anime series to the Anime profile"
  fi
  return 0
}

configure_junk_filters() {
  info "Blocking junk releases..."
  [ -n "$SONARR_KEY" ]       && ensure_anime_profile
  [ -n "$SONARR_KEY" ]       && apply_junk_filters "Sonarr"       "$SONARR_URL"       "$SONARR_KEY"       sonarr
  [ -n "$RADARR_KEY" ]       && apply_junk_filters "Radarr"       "$RADARR_URL"       "$RADARR_KEY"       radarr
  return 0
}
