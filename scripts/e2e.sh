#!/usr/bin/env bash
# End-to-end import test: nix run .#e2e [-- --keep]
#
# Proves the automatic path works with no manual step, using Blender's
# Creative Commons film "Tears of Steel" (downloaded once from
# download.blender.org into ~/media/.state/e2e):
#
#   Movie: request in Seerr → Radarr adds it → a correctly named release is
#          handed to Radarr → qBittorrent → automatic import → Jellyfin →
#          Seerr shows it as available.
#   TV:    Sonarr gets the free series "Pioneer One" (no indexer search) →
#          a correctly named S01E01 release → qBittorrent → automatic import
#          → Jellyfin.
#
# The releases are torrents built here whose data is already in the download
# folder, so qBittorrent completes them instantly without peers or public
# indexers. Everything the test adds is removed afterwards unless --keep.
# Results go to ~/media/logs/e2e-<timestamp>.log.

E2E_MOVIE_TMDB=133701
E2E_SERIES_TVDB=170551
E2E_TAG="MEDIASERVERTEST"
E2E_SOURCE_URL="https://download.blender.org/demo/movies/ToS/tears_of_steel_720p.mov"
E2E_SOURCE_SIZE=372178639
E2E_PORT=18765

e2e_step() { printf '%s  %s\n' "$(date +%T)" "$*"; }
e2e_pass() { E2E_PASSED=$((E2E_PASSED + 1)); ok "$*"; }
e2e_fail() { E2E_FAILED=$((E2E_FAILED + 1)); printf "\033[1;31m   ✗ %s\033[0m\n" "$*"; }

# wait_until <seconds> <command...>: poll every 3s until the command succeeds
wait_until() {
  local max="$1" i=0
  shift
  until "$@" >/dev/null 2>&1; do
    i=$((i + 3))
    [ "$i" -ge "$max" ] && return 1
    sleep 3
  done
}

# Build a torrent for one folder containing one file; prints the info hash
make_torrent() {
  python3 - "$1" "$2" << 'PY'
import hashlib, os, sys
folder, out = sys.argv[1:]
name = os.path.basename(folder)
files = sorted(f for f in os.listdir(folder) if not f.startswith("."))
piece = 1 << 20
pieces, buf, entries = b"", b"", []
for f in files:
    path = os.path.join(folder, f)
    entries.append({"length": os.path.getsize(path), "path": [f]})
    with open(path, "rb") as fh:
        while chunk := fh.read(piece):
            buf += chunk
            while len(buf) >= piece:
                pieces += hashlib.sha1(buf[:piece]).digest()
                buf = buf[piece:]
if buf:
    pieces += hashlib.sha1(buf).digest()

def enc(x):
    if isinstance(x, int): return b"i%de" % x
    if isinstance(x, str): x = x.encode()
    if isinstance(x, bytes): return b"%d:%s" % (len(x), x)
    if isinstance(x, list): return b"l" + b"".join(map(enc, x)) + b"e"
    return b"d" + b"".join(enc(k) + enc(v) for k, v in sorted(x.items())) + b"e"

# A per-run nonce gives each run a new info hash: Sonarr/Radarr remember hashes
# they already imported and would silently ignore a repeat
info = {"name": name, "piece length": piece, "pieces": pieces, "files": entries, "private": 1,
        "x-e2e-run": os.urandom(8).hex()}
with open(out, "wb") as fh:
    fh.write(enc({"info": info, "created by": "media-server e2e"}))
print(hashlib.sha1(enc(info)).hexdigest())
PY
}

# Indexers whose automatic search/RSS the test switched off, as JSON
E2E_PAUSED_INDEXERS=""
e2e_pause_radarr_indexers() {
  local H="X-Api-Key: $RADARR_KEY" idx
  E2E_PAUSED_INDEXERS=$(api GET "$RADARR_URL/api/v3/indexer" -H "$H" | \
    jq -c '[.[] | select(.enableAutomaticSearch or .enableRss) | {id, enableAutomaticSearch, enableRss}]')
  for idx in $(jq -r '.[].id' <<< "$E2E_PAUSED_INDEXERS"); do
    api PUT "$RADARR_URL/api/v3/indexer/$idx?forceSave=true" -H "$H" \
      -d "$(api GET "$RADARR_URL/api/v3/indexer/$idx" -H "$H" | jq -c '.enableAutomaticSearch = false | .enableRss = false')" >/dev/null
  done
  e2e_step "Paused automatic search on $(jq length <<< "$E2E_PAUSED_INDEXERS") Radarr indexers for the test"
}
e2e_resume_radarr_indexers() {
  [ -n "$E2E_PAUSED_INDEXERS" ] || return 0
  local H="X-Api-Key: $RADARR_KEY" entry idx
  while IFS= read -r entry; do
    idx=$(jq -r .id <<< "$entry")
    api PUT "$RADARR_URL/api/v3/indexer/$idx?forceSave=true" -H "$H" \
      -d "$(api GET "$RADARR_URL/api/v3/indexer/$idx" -H "$H" | jq -c --argjson e "$entry" \
        '.enableAutomaticSearch = $e.enableAutomaticSearch | .enableRss = $e.enableRss')" >/dev/null || \
      warn "Could not re-enable Radarr indexer $idx; turn its search back on in Radarr"
  done < <(jq -c '.[]' <<< "$E2E_PAUSED_INDEXERS")
  E2E_PAUSED_INDEXERS=""
  e2e_step "Radarr indexers restored"
}

do_e2e() {
  local keep="${E2E_KEEP:-false}" log dir src torrents server_pid="" qbit_cookie started
  mkdir -p "$LOG_DIR"
  log="$LOG_DIR/e2e-$(date +%Y%m%d_%H%M%S).log"
  exec > >(tee "$log") 2>&1
  E2E_PASSED=0
  E2E_FAILED=0
  started=$(date +%s)

  info "End-to-end import test"
  dir="$STATE_DIR/e2e"
  torrents="$dir/torrents"
  src="$dir/tears_of_steel_720p.mov"
  mkdir -p "$torrents"
  if [ "$(stat -f %z "$src" 2>/dev/null || echo 0)" != "$E2E_SOURCE_SIZE" ]; then
    e2e_step "Downloading Tears of Steel (CC BY, 372 MB) from download.blender.org..."
    curl -fL --progress-bar -o "$src" "$E2E_SOURCE_URL" || err "Download failed"
  fi

  local H_RADARR="X-Api-Key: $RADARR_KEY" H_SONARR="X-Api-Key: $SONARR_KEY" H_SEERR="X-Api-Key: $SEERR_KEY"
  jellyfin_login
  [ -n "$JELLYFIN_TOKEN" ] || err "Could not log in to Jellyfin"
  local JA
  JA=$(jf_auth "$JELLYFIN_TOKEN")
  qbit_cookie=$(curl -sf -c - "$QBIT_URL/api/v2/auth/login" \
    --data-urlencode "username=$QBIT_USER" --data-urlencode "password=$QBIT_PASS" | extract_qbit_cookie || true)

  # Serve the test torrents to Radarr/Sonarr
  python3 -m http.server "$E2E_PORT" --bind 127.0.0.1 --directory "$torrents" >/dev/null 2>&1 &
  server_pid=$!
  E2E_SERVER_PID=$server_pid
  trap 'kill "$E2E_SERVER_PID" 2>/dev/null || true; e2e_resume_radarr_indexers; cleanup' EXIT

  # ── Movie ──────────────────────────────────────────────────────
  info "Movie: Tears of Steel (2012)"
  local movie_name="Tears.of.Steel.2012.1080p.WEB-DL.x264-$E2E_TAG" movie_hash movie_id=""
  mkdir -p "$DL_COMPLETE/radarr/$movie_name"
  ln -f "$src" "$DL_COMPLETE/radarr/$movie_name/$movie_name.mov"
  movie_hash=$(make_torrent "$DL_COMPLETE/radarr/$movie_name" "$torrents/$movie_name.torrent")

  # Seerr has Radarr search as soon as the movie is added, and a public
  # release could win the race against the test release. Pause automatic
  # search and RSS on Radarr's indexers during the test; restored below and
  # on exit, whatever happens.
  e2e_pause_radarr_indexers
  e2e_step "Requesting it in Seerr"
  if api POST "$SEERR_URL/api/v1/request" -H "$H_SEERR" -d "{\"mediaType\":\"movie\",\"mediaId\":$E2E_MOVIE_TMDB}" >/dev/null; then
    e2e_pass "Seerr accepted the request"
  else
    e2e_fail "Seerr rejected the request (already in the library? run with a clean state)"
  fi
  radarr_movie_id() { api GET "$RADARR_URL/api/v3/movie?tmdbId=$E2E_MOVIE_TMDB" -H "$H_RADARR" | jq -er '.[0].id'; }
  if wait_until 90 radarr_movie_id; then
    movie_id=$(radarr_movie_id)
    e2e_pass "Seerr → Radarr: movie added ($(api GET "$RADARR_URL/api/v3/movie/$movie_id" -H "$H_RADARR" | jq -r '.rootFolderPath'))"
  else
    e2e_fail "Seerr → Radarr: movie never appeared in Radarr"
  fi

  if [ -n "$movie_id" ]; then
    e2e_step "Handing Radarr a correctly named release"
    local pushed
    pushed=$(api POST "$RADARR_URL/api/v3/release/push" -H "$H_RADARR" -d "$(jq -nc --arg t "$movie_name" \
      --arg u "http://127.0.0.1:$E2E_PORT/$movie_name.torrent" --argjson s "$E2E_SOURCE_SIZE" \
      '{title:$t, downloadUrl:$u, protocol:"torrent", publishDate:(now|todate), size:$s, indexer:"e2e test"}')" | \
      jq -r 'if type == "array" then .[0] else . end | if .approved then "approved" else "rejected: \(.rejections // [] | join("; "))" end' || echo "error")
    [ "$pushed" = "approved" ] && e2e_pass "Radarr approved the release" || e2e_fail "Radarr: $pushed"

    movie_has_file() { [ "$(api GET "$RADARR_URL/api/v3/movie/$movie_id" -H "$H_RADARR" | jq -r .hasFile)" = "true" ]; }
    if wait_until 300 movie_has_file; then
      e2e_pass "qBittorrent → Radarr: imported automatically ($(api GET "$RADARR_URL/api/v3/movie/$movie_id" -H "$H_RADARR" | jq -r '.movieFile.relativePath'))"
    else
      e2e_fail "Radarr did not import it within 5 minutes: $(api GET "$RADARR_URL/api/v3/queue" -H "$H_RADARR" | jq -c '[.records[]? | {trackedDownloadState, msg: [.statusMessages[]?.messages[]?]}]')"
    fi

    jellyfin_has_movie() {
      api GET "$JELLYFIN_URL/Items?Recursive=true&IncludeItemTypes=Movie&Fields=ProviderIds" -H "$JA" | \
        jq -e --arg t "$E2E_MOVIE_TMDB" 'any(.Items[]; .ProviderIds.Tmdb == $t)'
    }
    if wait_until 180 jellyfin_has_movie; then
      e2e_pass "Radarr → Jellyfin: in the library"
    else
      e2e_fail "Jellyfin didn't pick it up within 3 minutes"
    fi

    seerr_available() {
      api POST "$SEERR_URL/api/v1/settings/jobs/jellyfin-recently-added-scan/run" -H "$H_SEERR" >/dev/null
      [ "$(api GET "$SEERR_URL/api/v1/movie/$E2E_MOVIE_TMDB" -H "$H_SEERR" | jq -r '.mediaInfo.status')" = "5" ]
    }
    if wait_until 180 seerr_available; then
      e2e_pass "Jellyfin → Seerr: shown as available"
    else
      e2e_fail "Seerr doesn't show it as available within 3 minutes"
    fi
  fi

  # ── TV ─────────────────────────────────────────────────────────
  info "TV: Pioneer One S01E01"
  local tv_name="Pioneer.One.S01E01.1080p.WEB-DL.x264-$E2E_TAG" tv_hash series_id="" episode_id="" profile_id lookup
  mkdir -p "$DL_COMPLETE/sonarr/$tv_name"
  ln -f "$src" "$DL_COMPLETE/sonarr/$tv_name/$tv_name.mov"
  tv_hash=$(make_torrent "$DL_COMPLETE/sonarr/$tv_name" "$torrents/$tv_name.torrent")

  profile_id=$(api GET "$SONARR_URL/api/v3/qualityprofile" -H "$H_SONARR" | jq -r --arg n "$SONARR_PROFILE" '.[] | select(.name == $n) | .id')
  lookup=$(api GET "$SONARR_URL/api/v3/series/lookup?term=tvdb:$E2E_SERIES_TVDB" -H "$H_SONARR" | jq -c '.[0]')
  if [ -n "$profile_id" ] && [ "$lookup" != "null" ]; then
    series_id=$(api POST "$SONARR_URL/api/v3/series" -H "$H_SONARR" -d "$(jq -c --argjson p "$profile_id" --arg root "$TV_DIR" \
      '. + {qualityProfileId:$p, rootFolderPath:$root, monitored:true, seasonFolder:true,
            addOptions:{monitor:"none", searchForMissingEpisodes:false}}' <<< "$lookup")" | jq -r '.id // empty' || true)
  fi
  if [ -n "$series_id" ]; then
    e2e_pass "Sonarr: series added (no indexer search)"
    s01e01_id() { api GET "$SONARR_URL/api/v3/episode?seriesId=$series_id" -H "$H_SONARR" | jq -er '.[] | select(.seasonNumber == 1 and .episodeNumber == 1) | .id'; }
    wait_until 60 s01e01_id && episode_id=$(s01e01_id)
  else
    e2e_fail "Sonarr: could not add Pioneer One"
  fi

  if [ -n "$episode_id" ]; then
    # Added with monitor "none" (so Sonarr searches nothing), which also
    # unmonitors the series; monitor the series and just this episode
    api PUT "$SONARR_URL/api/v3/series/$series_id" -H "$H_SONARR" \
      -d "$(api GET "$SONARR_URL/api/v3/series/$series_id" -H "$H_SONARR" | jq -c '.monitored = true')" >/dev/null
    api PUT "$SONARR_URL/api/v3/episode/monitor" -H "$H_SONARR" -d "{\"episodeIds\":[$episode_id],\"monitored\":true}" >/dev/null
    local pushed
    pushed=$(api POST "$SONARR_URL/api/v3/release/push" -H "$H_SONARR" -d "$(jq -nc --arg t "$tv_name" \
      --arg u "http://127.0.0.1:$E2E_PORT/$tv_name.torrent" --argjson s "$E2E_SOURCE_SIZE" \
      '{title:$t, downloadUrl:$u, protocol:"torrent", publishDate:(now|todate), size:$s, indexer:"e2e test"}')" | \
      jq -r 'if type == "array" then .[0] else . end | if .approved then "approved" else "rejected: \(.rejections // [] | join("; "))" end' || echo "error")
    [ "$pushed" = "approved" ] && e2e_pass "Sonarr approved the release" || e2e_fail "Sonarr: $pushed"

    episode_has_file() { [ "$(api GET "$SONARR_URL/api/v3/episode/$episode_id" -H "$H_SONARR" | jq -r .hasFile)" = "true" ]; }
    if wait_until 300 episode_has_file; then
      e2e_pass "qBittorrent → Sonarr: imported automatically ($(api GET "$SONARR_URL/api/v3/episodefile?seriesId=$series_id" -H "$H_SONARR" | jq -r '.[0].relativePath'))"
    else
      e2e_fail "Sonarr did not import it within 5 minutes: $(api GET "$SONARR_URL/api/v3/queue" -H "$H_SONARR" | jq -c '[.records[]? | {trackedDownloadState, msg: [.statusMessages[]?.messages[]?]}]')"
    fi

    # Episodes are titled by name ("Earthfall"), so match on the file path
    jellyfin_has_episode() {
      api GET "$JELLYFIN_URL/Items?Recursive=true&IncludeItemTypes=Episode&Fields=Path" -H "$JA" | \
        jq -e --arg tag "$E2E_TAG" 'any(.Items[]; (.Path // "") | contains($tag))'
    }
    if wait_until 180 jellyfin_has_episode; then
      e2e_pass "Sonarr → Jellyfin: episode in the library"
    else
      e2e_fail "Jellyfin didn't pick the episode up within 3 minutes"
    fi
  fi

  e2e_resume_radarr_indexers

  # ── Cleanup ────────────────────────────────────────────────────
  if [ "$keep" = "true" ]; then
    warn "--keep: leaving the test movie, series and torrents in place"
  else
    info "Removing what the test added..."
    # Queued downloads for the test titles, including any Radarr/Sonarr
    # grabbed on their own, so none is left behind in qBittorrent
    local qid
    [ -n "$movie_id" ] && for qid in $(api GET "$RADARR_URL/api/v3/queue?movieIds=$movie_id" -H "$H_RADARR" | jq -r '.records[]?.id'); do
      api DELETE "$RADARR_URL/api/v3/queue/$qid?removeFromClient=true&blocklist=false" -H "$H_RADARR" >/dev/null || true
    done
    [ -n "$series_id" ] && for qid in $(api GET "$SONARR_URL/api/v3/queue?seriesIds=$series_id" -H "$H_SONARR" | jq -r '.records[]?.id'); do
      api DELETE "$SONARR_URL/api/v3/queue/$qid?removeFromClient=true&blocklist=false" -H "$H_SONARR" >/dev/null || true
    done
    local media_id
    media_id=$(api GET "$SEERR_URL/api/v1/movie/$E2E_MOVIE_TMDB" -H "$H_SEERR" | jq -r '.mediaInfo.id // empty' || true)
    [ -n "$media_id" ] && api DELETE "$SEERR_URL/api/v1/media/$media_id" -H "$H_SEERR" >/dev/null
    [ -n "$movie_id" ] && api DELETE "$RADARR_URL/api/v3/movie/$movie_id?deleteFiles=true&addImportExclusion=false" -H "$H_RADARR" >/dev/null
    [ -n "$series_id" ] && api DELETE "$SONARR_URL/api/v3/series/$series_id?deleteFiles=true" -H "$H_SONARR" >/dev/null
    [ -n "$qbit_cookie" ] && curl -sf -o /dev/null "$QBIT_URL/api/v2/torrents/delete" -b "$qbit_cookie" \
      --data-urlencode "hashes=$movie_hash|$tv_hash" --data-urlencode "deleteFiles=true"
    rm -rf "${DL_COMPLETE:?}/radarr/$movie_name" "${DL_COMPLETE:?}/sonarr/$tv_name" "$torrents"
    api POST "$JELLYFIN_URL/Library/Refresh" -H "$JA" >/dev/null || true
    ok "Test movie, series, request and torrents removed (the source file stays cached in $dir)"
  fi

  kill "$server_pid" 2>/dev/null || true

  echo ""
  if [ "$E2E_FAILED" -eq 0 ]; then
    printf "\033[1;32m   End-to-end test passed: %d steps in %ds\033[0m\n" "$E2E_PASSED" "$(( $(date +%s) - started ))"
  else
    printf "\033[1;31m   End-to-end test: %d of %d steps failed\033[0m\n" "$E2E_FAILED" "$((E2E_PASSED + E2E_FAILED))"
  fi
  echo "   Log: $log"
  return "$E2E_FAILED"
}
