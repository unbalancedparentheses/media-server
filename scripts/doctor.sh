#!/usr/bin/env bash
# nix run .#doctor: a read-only look at whether things are *working*, not
# just configured (that's `test`). Each finding says what was observed and
# suggests what to do; it changes nothing. Findings are facts about files
# and services; whether they're a problem can depend on your preferences.

do_doctor() {
  local attention=() notes=() fine=()
  need() { attention+=("$1"$'\t'"$2"); }     # finding, suggestion
  note() { notes+=("$1"$'\t'"$2"); }
  good() { fine+=("$1"); }

  info "Doctor (read-only)"
  doctor_services
  doctor_connection
  doctor_indexers
  doctor_downloads
  doctor_subtitles
  doctor_library
  doctor_postimport
  doctor_disk
  doctor_records

  local entry
  echo ""
  if [ "${#attention[@]}" -gt 0 ]; then
    printf "\033[1;31m  Needs attention\033[0m\n"
    for entry in "${attention[@]}"; do
      printf "   ✗ %s\n" "${entry%%$'\t'*}"
      [ -n "${entry#*$'\t'}" ] && printf "     → %s\n" "${entry#*$'\t'}"
    done
    echo ""
  fi
  if [ "${#notes[@]}" -gt 0 ]; then
    printf "\033[1;33m  Worth knowing\033[0m\n"
    for entry in "${notes[@]}"; do
      printf "   ! %s\n" "${entry%%$'\t'*}"
      [ -n "${entry#*$'\t'}" ] && printf "     → %s\n" "${entry#*$'\t'}"
    done
    echo ""
  fi
  printf "\033[1;32m  Looks fine\033[0m\n"
  for entry in "${fine[@]}"; do printf "   ✓ %s\n" "$entry"; done
  echo ""
  [ "${#attention[@]}" -eq 0 ]
}

doctor_services() {
  local svc state down=()
  for svc in $SERVICE_NAMES; do
    state=$(svc_state "$svc")
    [[ "$state" == running* ]] || down+=("$svc ($state)")
  done
  if [ "${#down[@]}" -gt 0 ]; then
    need "Not running: ${down[*]}" "nix run .#logs -- <service> to see why, then nix run .#restart -- <service>"
  else
    good "All services running"
  fi
}

doctor_connection() {
  local conn
  conn=$(cat "$STATE_DIR/netwatch/connection" 2>/dev/null || echo unknown)
  case "$conn" in
    offline) note "netwatch sees the Mac offline: Cleanuparr is paused and nothing can download" "It resumes on its own when the connection is back" ;;
    online) good "Online (netwatch)" ;;
    *) note "netwatch hasn't determined the connection yet" "" ;;
  esac
}

doctor_indexers() {
  local statuses names off
  statuses=$(api GET "$PROWLARR_URL/api/v1/indexerstatus" -H "X-Api-Key: $PROWLARR_KEY") || { need "Couldn't read Prowlarr's indexer status" "Is Prowlarr running? nix run .#status"; return; }
  names=$(api GET "$PROWLARR_URL/api/v1/indexer" -H "X-Api-Key: $PROWLARR_KEY" || echo "[]")
  off=$(jq -r --argjson names "$names" '
    ($names | map({key: (.id | tostring), value: .name}) | from_entries) as $n
    | [.[] | select(.disabledTill != null and (.disabledTill | fromdateiso8601) > now)
      | "\($n[.indexerId | tostring] // "indexer \(.indexerId)") (until \(.disabledTill | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601 | strflocaltime("%a %H:%M")))"]
    | join(", ")' <<< "$statuses" 2>/dev/null)
  if [ -n "$off" ]; then
    need "Prowlarr switched off indexers after failures: $off" \
      "Usually temporary (a site down, blocked, or you were offline). Prowlarr → Indexers → Test All clears the ones that work again"
  else
    good "Indexers: none switched off"
  fi
  local app url key health
  for app in Sonarr Radarr; do
    if [ "$app" = Sonarr ]; then url=$SONARR_URL key=$SONARR_KEY; else url=$RADARR_URL key=$RADARR_KEY; fi
    health=$(api GET "$url/api/v3/health" -H "X-Api-Key: $key" | jq -r '.[] | select(.type == "error" or (.source | test("Indexer|DownloadClient|RootFolder|ImportMechanism"))) | "\(.type)\t\(.message)"' 2>/dev/null)
    if [ -n "$health" ]; then
      # Errors stop things working; warnings (e.g. an indexer that was
      # rate-limited or unreachable recently) usually clear on their own
      while IFS=$'\t' read -r type msg; do
        if [ "$type" = error ]; then need "$app: $msg" "$app → System → Status explains it"
        else note "$app: $msg" "Often rate limits or a site that was briefly down; clears after the next successful search"; fi
      done <<< "$health"
    else
      good "$app: no health problems"
    fi
  done
}

doctor_downloads() {
  local torrents now stuck
  torrents=$(curl -sf "$QBIT_URL/api/v2/torrents/info" 2>/dev/null) || { need "Couldn't read qBittorrent's downloads" "Is qBittorrent running? nix run .#status"; return; }
  now=$(date +%s)
  stuck=$(jq -r --argjson now "$now" '
    [.[] | select(.progress < 1 and (.state == "stalledDL" or .state == "metaDL") and ($now - .added_on) > 86400)
      | "\(.name[0:60]) (\((.progress * 100) | floor)%, \(.num_seeds) seeders, added \((($now - .added_on) / 86400) | floor) days ago)"]
    | .[]' <<< "$torrents")
  if [ -n "$stuck" ]; then
    while IFS= read -r line; do
      need "Download not moving for over a day: $line" "Sonarr/Radarr → Activity → remove it with \"Blocklist release\" so another one is tried (Cleanuparr does this for public torrents once they count as stalled)"
    done <<< "$stuck"
  else
    good "Downloads: none stuck for more than a day"
  fi
  local app url key warnings
  for app in Sonarr Radarr; do
    if [ "$app" = Sonarr ]; then url=$SONARR_URL key=$SONARR_KEY; else url=$RADARR_URL key=$RADARR_KEY; fi
    warnings=$(api GET "$url/api/v3/queue?pageSize=200" -H "X-Api-Key: $key" | jq -r '
      .records[]? | select(.trackedDownloadStatus != "ok" and .trackedDownloadStatus != null)
      | "\(.title[0:60]): \([.statusMessages[]?.messages[]?][0] // .trackedDownloadState)"' 2>/dev/null)
    [ -n "$warnings" ] && while IFS= read -r line; do
      need "$app can't finish: $line" "$app → Activity → Queue shows the details (often a failed import: manual import or blocklist)"
    done <<< "$warnings"
  done
}

doctor_subtitles() {
  local k series movies
  k=$(sed -n '/^auth:/,/^[^ ]/{s/^  apikey: *//p;}' "$CONFIG_DIR/bazarr/config/config.yaml" 2>/dev/null | head -1 | tr -d "'")
  [ -n "$k" ] || { note "Couldn't read Bazarr's API key" ""; return; }
  series=$(curl -sf "$BAZARR_URL/api/episodes/wanted?apikey=$k&length=500" 2>/dev/null | \
    jq -r '[.data[] | .seriesTitle] | group_by(.) | map("\(.[0]) (\(length))") | join(", ")' 2>/dev/null)
  movies=$(curl -sf "$BAZARR_URL/api/movies/wanted?apikey=$k&length=500" 2>/dev/null | jq -r '[.data[].title] | join(", ")' 2>/dev/null)
  if [ -n "$series$movies" ]; then
    note "Still without subtitles in your language: ${series:+episodes of $series}${series:+${movies:+; }}${movies:+films: $movies}" \
      "The free providers don't have them (yet). An OpenSubtitles.com account (in Bazarr, then config.toml) finds most"
  else
    good "Subtitles: nothing missing"
  fi
}

# Facts about the files in Jellyfin's libraries
doctor_library() {
  local token libs anime items
  token=$(api POST "$JELLYFIN_URL/Users/AuthenticateByName" -H "$JF_HEADER" \
    -d "$(jq -nc --arg u "$JELLYFIN_USER" --arg p "$JELLYFIN_PASS" '{Username:$u,Pw:$p}')" | jq -r '.AccessToken // empty' 2>/dev/null)
  [ -n "$token" ] || { note "Couldn't log in to Jellyfin to look at the library" ""; return; }
  libs=$(api GET "$JELLYFIN_URL/Library/VirtualFolders" -H "$(jf_auth "$token")" || echo "[]")

  # Anime made in Japanese (Sonarr's original language) whose files have no
  # Japanese audio track, i.e. dubs; shows made in English aren't counted
  anime=$(jq -r '.[] | select(.Name == "Anime") | .ItemId' <<< "$libs")
  if [ -n "$anime" ]; then
    items=$(api GET "$JELLYFIN_URL/Items?Recursive=true&IncludeItemTypes=Episode&Fields=MediaStreams&ParentId=$anime" -H "$(jf_auth "$token")" || echo '{}')
    local dubs japanese
    japanese=$(api GET "$SONARR_URL/api/v3/series" -H "X-Api-Key: $SONARR_KEY" | \
      jq -c '[.[] | select(.seriesType == "anime" and .originalLanguage.name == "Japanese") | .title]' 2>/dev/null || echo "[]")
    dubs=$(jq -r --argjson ja "$japanese" '[.Items[]? | select(.SeriesName as $s | $ja | index($s))
      | select([.MediaStreams[]? | select(.Type == "Audio") | .Language] | index("jpn") | not) | .SeriesName]
      | group_by(.) | map("\(.[0]) (\(length) episodes)") | join(", ")' <<< "$items")
    if [ -n "$dubs" ]; then
      note "Anime with no Japanese audio track: $dubs" \
        "If you want Japanese audio, Sonarr → the series → Interactive Search for a Dual Audio or Japanese release (delete the current files first)"
    else
      good "Anime made in Japanese: every episode has Japanese audio"
    fi
  fi

  # Preferred-language subtitles only as pictures: browsers can't show
  # them, so Jellyfin burns them in (re-encoding the video) if selected
  local lang pics
  lang=$(cfg '.playback.subtitle_language // "eng"')
  items=$(api GET "$JELLYFIN_URL/Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=MediaStreams" -H "$(jf_auth "$token")" || echo '{}')
  pics=$(jq -r --arg l "$lang" '
    [.Items[]? | [.MediaStreams[]? | select(.Type == "Subtitle" and .Language == $l)] as $subs
      | select(($subs | length) > 0 and ($subs | all(.IsTextSubtitleStream | not)))
      | if .SeriesName then .SeriesName else .Name end] | unique | join(", ")' <<< "$items")
  if [ -n "$pics" ]; then
    note "Subtitles in \"$lang\" only as pictures (Blu-ray/DVD) in: $pics" \
      "In a browser, Jellyfin has to burn those into the video (heavy on the CPU). Bazarr looks for a text version; until then, pick another track or turn subtitles off"
  else
    good "Subtitles in \"$lang\": all available as text where present"
  fi
}

# What the checks after each download (postimport) found and did
doctor_postimport() {
  local status="$STATE_DIR/postimport/status.json" age
  if [ ! -f "$status" ]; then
    note "The checks after each download haven't run yet" "They start with the postimport service; nix run .#install sets it up"
    return 0
  fi
  age=$(( $(date +%s) - $(jq -r '.updated // 0' "$status") ))
  # A round can take a while (rewriting a large file), hence the margin
  [ "$age" -gt 3600 ] && note "The checks after each download last ran $((age / 60)) minutes ago" "nix run .#logs postimport shows why"
  local looking kept recent
  looking=$(jq -r '.looking[]? | "\(.title) (\(.reason))"' "$status" | paste -sd ';' - | sed 's/;/; /g')
  kept=$(jq -r '.kept[]? | "\(.title) (\(.reason))"' "$status" | paste -sd ';' - | sed 's/;/; /g')
  [ -n "$looking" ] && note "Downloads rejected, another release is being looked for: $looking" \
    "Nothing to do; you get a notification if nothing better turns up within 6 hours"
  [ -n "$kept" ] && note "Kept although no better release was found: $kept" \
    "Sonarr/Radarr → the title → Interactive Search, to pick one by hand"
  recent=$(jq -r '[.recent[]? | select(.time > (now - 86400))] | length' "$status")
  good "Checks after each download: running ($recent fixes in the last day)"
}

doctor_disk() {
  local free_gb
  free_gb=$(( $(df -Pk "$MEDIA_DIR" | awk 'NR == 2 { print $4 }') / 1024 / 1024 ))
  if [ "$free_gb" -lt "${DISK_MIN_GB:-10}" ]; then
    need "Only $free_gb GB free: Sonarr and Radarr stop importing below ${DISK_MIN_GB:-10} GB" "Delete something in Sonarr or Radarr (with \"Delete files\")"
  elif [ "$free_gb" -lt "${DISK_WARN_GB:-50}" ]; then
    note "$free_gb GB free on the media disk" "Imports stop below ${DISK_MIN_GB:-10} GB"
  else
    good "$free_gb GB free on the media disk"
  fi
}

# Work that an interrupted operation left unfinished
doctor_records() {
  [ -f "$STATE_DIR/e2e/paused-indexers.json" ] && \
    need "An end-to-end test left Radarr indexers paused" "Run nix run .#install (it restores them), or re-enable automatic search in Radarr → Settings → Indexers"
  [ -f "$STATE_DIR/e2e/owned.json" ] && \
    note "An end-to-end test didn't finish cleaning up (record: $STATE_DIR/e2e/owned.json)" "The next nix run .#e2e removes its leftovers"
  [ -f "$CONFIG_DIR/sonarr-anime/sonarr.db" ] && [ ! -f "$STATE_DIR/sonarr-anime-migrated" ] && \
    note "The old anime Sonarr's series aren't all moved yet" "nix run .#install finishes it"
  local owner
  owner=$(cat "$STATE_DIR/lock/pid" 2>/dev/null || true)
  if [ -n "$owner" ]; then
    if kill -0 "$owner" 2>/dev/null; then note "An operation is running right now (PID $owner)" ""
    else note "A previous operation stopped before finishing (stale lock)" "Harmless; the next one takes it over"; fi
  fi
  return 0
}
