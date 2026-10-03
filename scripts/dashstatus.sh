#!/usr/bin/env bash
# dashstatus: gathers the dashboard's live data into one file,
# ~/media/config/nginx/www/status.json, which the page reads. The API keys
# stay here; the browser only ever sees the summary. Runs as a launchd
# agent (see flake.nix).
#
# Every DASH_INTERVAL seconds (15): what's playing (and why Jellyfin is
# converting it, if it is), transfer speeds, system load and memory, the
# connection. Every DASH_SLOW_EVERY rounds (20, i.e. 5 minutes): library
# counts, Sonarr/Radarr queues, missing items and health, Prowlarr's
# indexers, Bazarr's missing subtitles, Seerr's requests, disk space, and
# the media side from dashmedia.py (continue watching, latest, requests'
# real state, upcoming releases, library health).
# "attention" lists what needs you, each with a suggested action.
#
# Environment: DASH_CONFIG (~/media/config), DASH_STATE (~/media/.state),
# DASH_MEDIA (~/media), DASH_OUT, DISK_WARN_GB, DISK_MIN_GB, DASH_MEDIA_SCRIPT
# (dashmedia.py; by default next to this script).

cfg="${DASH_CONFIG:-$HOME/media/config}"
state="${DASH_STATE:-$HOME/media/.state}"
media="${DASH_MEDIA:-$HOME/media}"
out="${DASH_OUT:-$cfg/nginx/www/status.json}"

key() { sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' "$cfg/$1/config.xml" 2>/dev/null; }
get() {  # url [header]
  if [ -n "${2:-}" ]; then curl -fsS -m 10 "$1" -H "$2" 2>/dev/null; else curl -fsS -m 10 "$1" 2>/dev/null; fi
}

# ─── Fast: every round ───────────────────────────────────────────

system_json() {
  local load ncpu memsize pagesize used boot now
  load=$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}' | awk '{print $1, $2, $3}')
  ncpu=$(sysctl -n hw.ncpu 2>/dev/null || echo 1)
  memsize=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
  pagesize=$(sysctl -n hw.pagesize 2>/dev/null || echo 4096)
  # In use = active + wired + compressed (what Activity Monitor calls "used")
  used=$(vm_stat 2>/dev/null | awk -v ps="$pagesize" '
    /Pages active/ {a=$3} /Pages wired/ {w=$4} /occupied by compressor/ {c=$5}
    END {gsub(/\./,"",a); gsub(/\./,"",w); gsub(/\./,"",c); print (a+w+c)*ps}')
  boot=$(sysctl -n kern.boottime 2>/dev/null | sed -n 's/^{ sec = \([0-9]*\),.*/\1/p')
  now=$(date +%s)
  jq -nc --arg load "$load" --argjson ncpu "${ncpu:-1}" --argjson mem "${memsize:-0}" --argjson used "${used:-0}" \
    --argjson up "$(( now - ${boot:-$now} ))" '
    ($load | split(" ") | map(tonumber? // 0)) as $l
    | {load: $l, cpus: $ncpu, cpu_pct: ((($l[0] // 0) / $ncpu * 100) | if . > 100 then 100 else . end | floor),
       mem_total: $mem, mem_used: $used, mem_pct: (if $mem > 0 then ($used / $mem * 100 | floor) else 0 end), uptime_s: $up}'
}

playing_json() {
  local jk
  jk=$(cat "$state/dashstatus/jellyfin-key" 2>/dev/null)
  [ -n "$jk" ] || { echo "[]"; return; }
  get "http://127.0.0.1:8096/Sessions?ActiveWithinSeconds=120" "Authorization: MediaBrowser Token=\"$jk\"" | jq -c '
    [.[]? | select(.NowPlayingItem) | . as $s | .NowPlayingItem as $i
     | {user: .UserName, client: (.Client // ""), device: (.DeviceName // ""), id: $i.Id,
        image: (if $i.SeriesPrimaryImageTag then $i.SeriesId else $i.Id end),
        tag: ($i.SeriesPrimaryImageTag // $i.ImageTags.Primary // null),
        title: (if $i.SeriesName then $i.SeriesName else $i.Name end),
        detail: (if $i.SeriesName then "S\($i.ParentIndexNumber // 0)E\($i.IndexNumber // 0) · \($i.Name)" else ($i.ProductionYear // "" | tostring) end),
        progress: (if ($i.RunTimeTicks // 0) > 0 then (($s.PlayState.PositionTicks // 0) / $i.RunTimeTicks * 100 | floor) else 0 end),
        paused: ($s.PlayState.IsPaused // false),
        method: ($s.PlayState.PlayMethod // "DirectPlay"),
        video_direct: ($s.TranscodingInfo.IsVideoDirect // true),
        hw: ($s.TranscodingInfo.HardwareAccelerationType // ""),
        reasons: ($s.TranscodingInfo.TranscodeReasons // [])}]' 2>/dev/null || echo "[]"
}

downloads_json() {
  local transfer torrents sab sk
  transfer=$(get "http://127.0.0.1:8081/api/v2/transfer/info" || echo '{}')
  torrents=$(get "http://127.0.0.1:8081/api/v2/torrents/info" || echo '[]')
  sk=$(sed -n 's/^api_key = *//p' "$cfg/sabnzbd/sabnzbd.ini" 2>/dev/null)
  sab=$([ -n "$sk" ] && get "http://127.0.0.1:8080/api?mode=queue&output=json&apikey=$sk" || echo '{}')
  jq -nc --argjson t "$transfer" --argjson ts "$torrents" --argjson sab "$sab" '
    {dl_speed: (($t.dl_info_speed // 0) + (($sab.queue.kbpersec // "0") | tonumber? // 0) * 1024 | floor),
     up_speed: ($t.up_info_speed // 0),
     downloading: ([$ts[] | select(.progress < 1 and (.state | test("downloading|forcedDL|metaDL|stalledDL|queuedDL")))] | length)
                  + ($sab.queue.noofslots // 0),
     stalled: ([$ts[] | select(.progress < 1 and (.state == "stalledDL" or .state == "metaDL"))] | length),
     seeding: ([$ts[] | select(.progress >= 1 and (.state | test("uploading|stalledUP|forcedUP")))] | length)}' 2>/dev/null || echo '{}'
}

# ─── Slow: every few minutes ─────────────────────────────────────

arr_json() {  # url key
  local queue missing health
  [ -n "$2" ] || { echo '{}'; return; }
  queue=$(get "$1/api/v3/queue/status" "X-Api-Key: $2" | jq '.totalCount // 0' 2>/dev/null || echo null)
  missing=$(get "$1/api/v3/wanted/missing?pageSize=1&monitored=true" "X-Api-Key: $2" | jq '.totalRecords // 0' 2>/dev/null || echo null)
  health=$(get "$1/api/v3/health" "X-Api-Key: $2" | jq -c '[.[] | select(.source != "UpdateCheck") | {type, message}]' 2>/dev/null || echo '[]')
  jq -nc --argjson q "${queue:-null}" --argjson m "${missing:-null}" --argjson h "$health" '{queue: $q, missing: $m, health: $h}'
}

slow_json() {
  local sk rk pk bk jk seerr_key indexers statuses counts badges requests free_kb total_kb
  sk=$(key sonarr) rk=$(key radarr) pk=$(key prowlarr)
  bk=$(sed -n '/^auth:/,/^[^ ]/{s/^  apikey: *//p;}' "$cfg/bazarr/config/config.yaml" 2>/dev/null | head -1 | tr -d "'")
  jk=$(cat "$state/dashstatus/jellyfin-key" 2>/dev/null)
  seerr_key=$(jq -r '.main.apiKey // empty' "$cfg/seerr/settings.json" 2>/dev/null)
  indexers=$(get "http://127.0.0.1:9696/api/v1/indexer" "X-Api-Key: $pk" || echo '[]')
  statuses=$(get "http://127.0.0.1:9696/api/v1/indexerstatus" "X-Api-Key: $pk" || echo '[]')
  counts=$([ -n "$jk" ] && get "http://127.0.0.1:8096/Items/Counts" "Authorization: MediaBrowser Token=\"$jk\"" || echo '{}')
  badges=$([ -n "$bk" ] && get "http://127.0.0.1:6767/api/badges?apikey=$bk" || echo '{}')
  requests=$([ -n "$seerr_key" ] && get "http://127.0.0.1:5055/api/v1/request/count" "X-Api-Key: $seerr_key" || echo '{}')
  read -r total_kb free_kb < <(df -Pk "$media" 2>/dev/null | awk 'NR == 2 {print $2, $4}')
  # Latest requests with their titles (Seerr's request list only has ids)
  local recent_requests="[]" req_list r
  if [ -n "$seerr_key" ] && req_list=$(get "http://127.0.0.1:5055/api/v1/request?take=8&sort=added" "X-Api-Key: $seerr_key"); then
    while IFS= read -r r; do
      [ -n "$r" ] || continue
      local kind tmdb details
      kind=$(jq -r '.type' <<< "$r"); tmdb=$(jq -r '.media.tmdbId' <<< "$r")
      details=$(get "http://127.0.0.1:5055/api/v1/$([ "$kind" = tv ] && echo tv || echo movie)/$tmdb" "X-Api-Key: $seerr_key" || echo '{}')
      recent_requests=$(jq -c --argjson r "$r" --argjson d "$details" '. + [{
        title: ($d.title // $d.name // "TMDB \($r.media.tmdbId)"),
        year: (($d.releaseDate // $d.firstAirDate // "")[0:4]), type: $r.type, id: $r.media.tmdbId,
        poster: ($d.posterPath // null), by: ($r.requestedBy.displayName // ""), date: $r.createdAt,
        status: (if $r.media.status == 5 then "available" elif $r.media.status == 4 then "partly available"
                 elif $r.status == 1 then "waiting for approval" elif $r.status == 3 then "declined"
                 elif $r.media.status == 3 then "in progress" else "approved" end)}]' <<< "$recent_requests" 2>/dev/null || echo "$recent_requests")
    done < <(jq -c '.results[]?' <<< "$req_list")
  fi
  # Recently watched: every user's last-played items (Jellyfin keeps a
  # last-played date per user; its activity log doesn't record playback)
  local watched="[]" users uid
  if [ -n "$jk" ] && users=$(get "http://127.0.0.1:8096/Users" "Authorization: MediaBrowser Token=\"$jk\""); then
    for uid in $(jq -r '.[].Id' <<< "$users"); do
      watched=$(jq -c --argjson acc "$watched" --arg user "$(jq -r --arg id "$uid" '.[] | select(.Id == $id) | .Name' <<< "$users")" '
        $acc + [.Items[]? | select(.UserData.LastPlayedDate) | {user: $user,
          title: (if .SeriesName then .SeriesName else .Name end),
          detail: (if .SeriesName then "S\(.ParentIndexNumber // 0)E\(.IndexNumber // 0) · \(.Name)" else (.ProductionYear // "" | tostring) end),
          played: .UserData.Played, progress: (.UserData.PlayedPercentage // 0 | floor), date: .UserData.LastPlayedDate}]' \
        <<< "$(get "http://127.0.0.1:8096/Users/$uid/Items?Recursive=true&SortBy=DatePlayed&SortOrder=Descending&Limit=8&IncludeItemTypes=Movie,Episode&Fields=UserData" "Authorization: MediaBrowser Token=\"$jk\"" || echo '{}')" 2>/dev/null || echo "$watched")
    done
    watched=$(jq -c 'sort_by(.date) | reverse | .[0:8]' <<< "$watched")
  fi
  # Prowlarr's last 24 hours: queries, grabs, failures
  local since stats ts_cli tailscale
  since=$(date -u -v-24H '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)
  stats=$(get "http://127.0.0.1:9696/api/v1/indexerstats?startDate=$since" "X-Api-Key: $pk" | jq -c '
    {queries: ([.indexers[]?.numberOfQueries] | add // 0), grabs: ([.indexers[]?.numberOfGrabs] | add // 0),
     failed: ([.indexers[]?.numberOfFailedQueries] | add // 0)}' 2>/dev/null || echo '{}')
  ts_cli=$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)
  tailscale=$([ -x "$ts_cli" ] && "$ts_cli" status --json 2>/dev/null | jq -c '
    {installed: true, state: .BackendState, online: (.Self.Online // false), name: (.Self.DNSName // "" | sub("\\.$"; "")),
     ip: ((.Self.TailscaleIPs // [])[0] // ""), peers: ((.Peer // {}) | length), key_expiry: (.Self.KeyExpiry // null)}' \
    || echo '{"installed": false}')
  jq -nc --argjson sonarr "$(arr_json http://127.0.0.1:8989 "$sk")" --argjson radarr "$(arr_json http://127.0.0.1:7878 "$rk")" \
    --argjson idx "$indexers" --argjson st "$statuses" --argjson counts "$counts" --argjson badges "$badges" --argjson req "$requests" \
    --argjson stats "${stats:-{\}}" --argjson ts "${tailscale:-{\}}" --argjson up "$(uptime_json)" --argjson watched "${watched:-[]}" --argjson rr "${recent_requests:-[]}" \
    --argjson total "${total_kb:-0}" --argjson free "${free_kb:-0}" --argjson warn "${DISK_WARN_GB:-50}" --argjson min "${DISK_MIN_GB:-10}" \
    --argjson media "$(python3 "${DASH_MEDIA_SCRIPT:-$(dirname "${BASH_SOURCE[0]}")/dashmedia.py}" 2>/dev/null || echo '{}')" '
    ($idx | map({key: (.id | tostring), value: .name}) | from_entries) as $names
    | [$idx[] | select(.enable) | .id] as $on
    | {sonarr: $sonarr, radarr: $radarr,
       prowlarr: {indexers: ($idx | length), enabled: ([$idx[] | select(.enable)] | length),
                  off: [$st[]? | select(.indexerId as $i | $on | index($i)) | select(.disabledTill != null and ((.disabledTill | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) > now))
                        | {name: ($names[.indexerId | tostring] // "indexer \(.indexerId)"), until: .disabledTill}]},
       library: {movies: ($counts.MovieCount // null), series: ($counts.SeriesCount // null), episodes: ($counts.EpisodeCount // null)},
       subtitles: {missing_episodes: ($badges.episodes // null), missing_movies: ($badges.movies // null)},
       indexer_stats: $stats, tailscale: $ts, uptime: $up, watched: $watched, recent_requests: $rr,
       requests: {pending: ($req.pending // null), processing: ($req.processing // null), available: ($req.available // null), total: ($req.total // null)},
       disk: {total_gb: ($total / 1048576 | floor), free_gb: ($free / 1048576 | floor), warn_gb: $warn, min_gb: $min}}
      + $media' 2>/dev/null || echo '{}'
}

# 24-hour availability per service: a sample every slow round (5 minutes),
# the last 288 kept in $state/dashstatus/uptime.json. Byparr is checked on
# /docs: its /health opens a browser and can take longer than the timeout.
UPTIME_CHECKS="Jellyfin|http://127.0.0.1:8096/health Seerr|http://127.0.0.1:5055/api/v1/status Sonarr|http://127.0.0.1:8989/ping Radarr|http://127.0.0.1:7878/ping Prowlarr|http://127.0.0.1:9696/ping Bazarr|http://127.0.0.1:6767 qBittorrent|http://127.0.0.1:8081 SABnzbd|http://127.0.0.1:8080 Cleanuparr|http://127.0.0.1:11011/health Byparr|http://127.0.0.1:8191/docs"
uptime_json() {
  local f="$state/dashstatus/uptime.json" now sample="{}" check name url code hist
  now=$(date +%s)
  for check in $UPTIME_CHECKS; do
    name=${check%%|*} url=${check#*|}
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url" 2>/dev/null || true)
    sample=$(jq -c --arg n "$name" --argjson up "$([[ "$code" =~ ^[23] ]] && echo true || echo false)" '. + {($n): $up}' <<< "$sample")
  done
  hist=$(jq -c --argjson s "$sample" --argjson now "$now" '(. + [{t: $now, up: $s}]) | .[-288:]' "$f" 2>/dev/null || jq -nc --argjson s "$sample" --argjson now "$now" '[{t: $now, up: $s}]')
  mkdir -p "$(dirname "$f")" && printf '%s\n' "$hist" > "$f.tmp.$$" && mv -f "$f.tmp.$$" "$f"
  # Per service: % of samples up, current state, and the last 48 samples (4h) for a sparkline
  jq -c '(.[0].up | keys) as $names
    | [$names[] as $n | {name: $n, up_now: (.[-1].up[$n] // false),
        pct: (([.[] | .up[$n] | select(. != null)] | (map(select(.)) | length) * 100 / (length | if . == 0 then 1 else . end)) | floor),
        recent: [.[-48:][] | .up[$n] // null]}]' <<< "$hist"
}

# What needs you, with what to do; facts only, from the data gathered
attention_json() {  # fast slow torrents
  jq -nc --argjson f "$1" --argjson s "$2" --argjson ts "$3" --arg conn "$(cat "$state/netwatch/connection" 2>/dev/null)" \
    --argjson pi "$(cat "$state/postimport/status.json" 2>/dev/null || echo '{}')" \
    --argjson paused "$([ -f "$state/e2e/paused-indexers.json" ] && echo true || echo false)" '
    [ (if $conn == "offline" then {level: "warn", text: "The Mac is offline: nothing can download, and Cleanuparr is paused", action: "It resumes on its own when the connection is back"} else empty end),
      (if ($s.disk.free_gb // 1e9) < ($s.disk.min_gb // 10) then {level: "error", text: "Only \($s.disk.free_gb) GB free: imports have stopped", action: "Delete something in Sonarr or Radarr"}
       elif ($s.disk.free_gb // 1e9) < ($s.disk.warn_gb // 50) then {level: "warn", text: "\($s.disk.free_gb) GB free on the media disk", action: "Imports stop below \($s.disk.min_gb) GB"} else empty end),
      (if (($s.prowlarr.off // []) | length) > 0 then {level: "warn", text: "Indexers switched off after failures: \([$s.prowlarr.off[].name] | join(", "))", action: "Usually temporary; Prowlarr → Indexers → Test All brings back the ones that work"} else empty end),
      ($s.sonarr.health // [] | .[] | select(.type == "error") | {level: "error", text: "Sonarr: \(.message)", action: "Sonarr → System → Status"}),
      ($s.radarr.health // [] | .[] | select(.type == "error") | {level: "error", text: "Radarr: \(.message)", action: "Radarr → System → Status"}),
      ($ts[]? | select(.progress < 1 and (.state == "stalledDL" or .state == "metaDL") and (now - .added_on) > 86400)
        | {level: "warn", text: "Not moving for over a day: \(.name[0:70]) (\(.num_seeds) seeders)", action: "Sonarr/Radarr → Activity: remove it with \"Blocklist release\" to try another"}),
      ($pi.looking[]? | {level: "info", text: "Rejected \(.title): \(.reason)", action: "A better release is being looked for"}),
      ($pi.kept[]? | {level: "warn", text: "Kept \(.title) although \(.reason)", action: "No better release found; Interactive Search to pick one"}),
      (if $paused then {level: "error", text: "An end-to-end test left Radarr indexers paused", action: "Run nix run .#install"} else empty end),
      ($f.playing[]? | select(.method == "Transcode" and (.reasons | index("SubtitleCodecNotSupported")))
        | {level: "info", text: "\(.user) is watching \(.title) with picture subtitles burned into the video (heavy on the CPU)", action: "Pick a text subtitle (External/SRT) in the player"})
    ]' 2>/dev/null || echo '[]'
}

dashstatus_round() {  # updates DASH_SLOW (cached slow data) every DASH_SLOW_EVERY rounds
  local fast torrents now_s
  if [ $(( DASH_ROUND % ${DASH_SLOW_EVERY:-20} )) -eq 0 ] || [ -z "${DASH_SLOW:-}" ]; then
    DASH_SLOW=$(slow_json)
    DASH_SLOW_AT=$(date +%s)
  fi
  DASH_ROUND=$((DASH_ROUND + 1))
  torrents=$(get "http://127.0.0.1:8081/api/v2/torrents/info" || echo '[]')
  fast=$(jq -nc --argjson system "$(system_json)" --argjson playing "$(playing_json)" --argjson downloads "$(downloads_json)" \
    --arg conn "$(cat "$state/netwatch/connection" 2>/dev/null || echo unknown)" \
    '{system: $system, playing: $playing, downloads: $downloads, connection: $conn}')
  now_s=$(date +%s)
  mkdir -p "$(dirname "$out")"
  jq -nc --argjson f "$fast" --argjson s "${DASH_SLOW:-{\}}" --argjson a "$(attention_json "$fast" "${DASH_SLOW:-{\}}" "$torrents")" \
    --argjson now "$now_s" --argjson slow_at "${DASH_SLOW_AT:-$now_s}" \
    '$f + $s + {attention: $a, updated: $now, slow_updated: $slow_at}' > "$out.tmp.$$" && mv -f "$out.tmp.$$" "$out"
  chmod 644 "$out" 2>/dev/null
  return 0
}

dashstatus_main() {
  DASH_ROUND=0 DASH_SLOW=""
  while :; do
    dashstatus_round
    sleep "${DASH_INTERVAL:-15}"
  done
}

[ "${DASHSTATUS_LIB:-}" = 1 ] || dashstatus_main
