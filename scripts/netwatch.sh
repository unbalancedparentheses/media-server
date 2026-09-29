#!/usr/bin/env bash
# netwatch: keeps the stack sensible when the Mac goes offline (e.g. on a
# flight) and comes back. Runs as a launchd agent (see flake.nix).
#
# Every NETWATCH_INTERVAL seconds (60) it checks the connection and then
# reconciles, rather than acting once per transition, so a failed step is
# simply retried on the next round:
#   - offline (3 failed checks in a row, so blips don't count): Cleanuparr's
#     queue cleaner must be off; otherwise it takes every download for
#     stalled, removes it and blocklists the release.
#   - online: the queue cleaner must be what config.toml says, which setup
#     writes to $NETWATCH_STATE/cleanuparr-wanted ("true"/"false").
#   - before the first successful check nothing is changed, so a restart
#     while offline never switches the cleaner back on.
# Coming back online also re-tests the indexers (Prowlarr backs off for up
# to a day after failures, and Sonarr/Radarr then can't search) and clears
# Bazarr's provider throttling.
# The current state is written to $NETWATCH_STATE/connection for setup.
#
# Environment: NETWATCH_CONFIG (~/media/config), NETWATCH_STATE,
# NETWATCH_INTERVAL, NETWATCH_SIMULATE_OFFLINE (a file whose presence means
# offline, for tests), NETWATCH_CLEANUPARR_URL.

cfg="${NETWATCH_CONFIG:-$HOME/media/config}"
state_dir="${NETWATCH_STATE:-$HOME/media/.state/netwatch}"
cleanuparr_url="${NETWATCH_CLEANUPARR_URL:-http://127.0.0.1:11011}"

log() { echo "$(date '+%F %T') $*"; }
key() { sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' "$cfg/$1/config.xml" 2>/dev/null; }

probe() {
  if [ -n "${NETWATCH_SIMULATE_OFFLINE:-}" ]; then
    [ ! -e "$NETWATCH_SIMULATE_OFFLINE" ]
    return
  fi
  curl -fsS -o /dev/null --connect-timeout 5 -m 10 https://www.gstatic.com/generate_204 2>/dev/null ||
    curl -fsS -o /dev/null --connect-timeout 5 -m 10 https://cloudflare.com/cdn-cgi/trace 2>/dev/null
}

cleanuparr_key() { sqlite3 "$cfg/cleanuparr/users.db" 'SELECT api_key FROM users LIMIT 1' 2>/dev/null; }

# What the queue cleaner should be while online (config.toml, via setup)
wanted() {
  local w
  w=$(cat "$state_dir/cleanuparr-wanted" 2>/dev/null)
  [ "$w" = false ] && echo false || echo true
}

# Make Cleanuparr's queue cleaner enabled = $1; non-zero if it couldn't be
# read or set (retried next round)
set_cleaner() {
  local k conf
  k=$(cleanuparr_key)
  [ -n "$k" ] || return 1
  conf=$(curl -fsS -m 15 "$cleanuparr_url/api/configuration/queue_cleaner" -H "X-Api-Key: $k") || return 1
  [ "$(jq -r .enabled <<< "$conf")" = "$1" ] && return 0
  curl -fsS -m 15 -o /dev/null -X PUT "$cleanuparr_url/api/configuration/queue_cleaner" \
    -H "X-Api-Key: $k" -H "Content-Type: application/json" -d "$(jq -c --argjson e "$1" '.enabled = $e' <<< "$conf")" || return 1
  if [ "$1" = true ]; then log "resumed Cleanuparr's queue cleaner"; else log "paused Cleanuparr's queue cleaner"; fi
}

# Back online: re-test indexers (can take minutes, so in the background;
# the apps answer 400 when some indexer still fails, which is still a
# completed test) and clear Bazarr's provider throttling
after_reconnect() {
  retest() {  # name url key
    [ -n "$3" ] || return 0
    if curl -sS -m 900 -o /dev/null -X POST "$2" -H "X-Api-Key: $3"; then
      log "re-tested $1's indexers"
    else
      log "couldn't re-test $1's indexers"
    fi
  }
  (
    retest Prowlarr http://127.0.0.1:9696/api/v1/indexer/testall "$(key prowlarr)"
    retest Sonarr http://127.0.0.1:8989/api/v3/indexer/testall "$(key sonarr)" &
    retest Radarr http://127.0.0.1:7878/api/v3/indexer/testall "$(key radarr)" &
    wait
  ) &
  local k
  k=$(sed -n '/^auth:/,/^[^ ]/{s/^  apikey: *//p;}' "$cfg/bazarr/config/config.yaml" 2>/dev/null | head -1 | tr -d "'")
  [ -n "$k" ] && curl -fsS -m 30 -o /dev/null -X POST "http://127.0.0.1:6767/api/providers?apikey=$k" -d action=reset &&
    log "cleared Bazarr's provider throttling"
  return 0
}

# One round. Globals: NW_STATE (unknown|online|offline), NW_MISSES
netwatch_round() {
  local before="$NW_STATE"
  if probe; then
    NW_STATE=online NW_MISSES=0
  else
    NW_MISSES=$((NW_MISSES + 1))
    [ "$NW_MISSES" -ge 3 ] && NW_STATE=offline
  fi
  [ "$NW_STATE" != "$before" ] && {
    log "connection: $NW_STATE"
    mkdir -p "$state_dir" && printf '%s\n' "$NW_STATE" > "$state_dir/connection"
  }
  case "$NW_STATE" in
    offline) set_cleaner false || log "couldn't pause Cleanuparr's queue cleaner (retrying)" ;;
    online)
      set_cleaner "$(wanted)" || log "couldn't set Cleanuparr's queue cleaner (retrying)"
      [ "$before" = offline ] && after_reconnect
      ;;
  esac
  return 0
}

netwatch_main() {
  mkdir -p "$state_dir"
  rm -f "$state_dir/cleanuparr-paused"  # older versions' pause flag
  NW_STATE=unknown NW_MISSES=0
  while :; do
    netwatch_round
    sleep "${NETWATCH_INTERVAL:-60}"
  done
}

# Sourced by the tests with NETWATCH_LIB=1
[ "${NETWATCH_LIB:-}" = 1 ] || netwatch_main
