#!/usr/bin/env bash
# Services run as launchd user agents (~/Library/LaunchAgents), one per
# service. Their command lines come from the Nix manifest ($MEDIA_SERVICES_JSON),
# with @CONFIG@/@STATE@/@ADMIN_BIND@ filled in here.

LAUNCHD_DOMAIN="gui/$(id -u)"

svc_loaded() { launchctl print "$LAUNCHD_DOMAIN/$(svc_label "$1")" >/dev/null 2>&1; }

wait_for_bootout() {
  local i=0
  while svc_loaded "$1" && [ "$i" -lt 30 ]; do
    sleep 1
    i=$((i + 1))
  done
}

# A full stop (including leftover children) and start, rather than
# `launchctl kickstart -k`, which would leave Bazarr's server running
svc_restart() {
  svc_loaded "$1" || return 1
  svc_stop "$1" || return 1
  launchctl bootstrap "$LAUNCHD_DOMAIN" "$(svc_plist "$1")"
}

# Returns non-zero if the service is still loaded or its processes survive
svc_stop() {
  if svc_loaded "$1"; then
    launchctl bootout "$LAUNCHD_DOMAIN/$(svc_label "$1")" 2>/dev/null || true
    wait_for_bootout "$1"
  fi
  kill_leftovers "$1"
  if svc_loaded "$1" || pgrep -f "$CONFIG_DIR/$1([/ ]|\$)" >/dev/null 2>&1; then
    warn "$1 did not stop"
    return 1
  fi
  return 0
}

# Some services (Bazarr) run their server as a child that outlives the
# launchd job. Every service's command line names its config directory, so
# stop whatever still does.
kill_leftovers() {
  local pattern="$CONFIG_DIR/$1([/ ]|$)" i=0
  pkill -f "$pattern" 2>/dev/null || return 0
  while pgrep -f "$pattern" >/dev/null 2>&1 && [ "$i" -lt 10 ]; do
    sleep 1
    i=$((i + 1))
  done
  pkill -9 -f "$pattern" 2>/dev/null || true
  sleep 1
  return 0
}

# Stops every service; returns non-zero if any of them didn't stop
stop_services() {
  local name failed=0
  for name in $SERVICE_NAMES; do svc_stop "$name" || failed=1; done
  return "$failed"
}

# "running (pid 123)", "stopped (last exit 1)" or "not installed"
svc_state() {
  local info pid code
  info=$(launchctl print "$LAUNCHD_DOMAIN/$(svc_label "$1")" 2>/dev/null) || { echo "not installed"; return; }
  pid=$(printf '%s\n' "$info" | sed -n 's/^[[:space:]]*pid = \([0-9]*\)$/\1/p' | head -1)
  code=$(printf '%s\n' "$info" | sed -n 's/^[[:space:]]*last exit code = \(.*\)$/\1/p' | head -1)
  if [ -n "$pid" ]; then
    echo "running (pid $pid)"
  else
    echo "stopped (last exit ${code:-?})"
  fi
}
