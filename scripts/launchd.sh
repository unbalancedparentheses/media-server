#!/usr/bin/env bash
# Services run as launchd user agents (~/Library/LaunchAgents), one per
# service. Their command lines come from the Nix manifest ($MEDIA_SERVICES_JSON),
# with @CONFIG@/@STATE@/@ADMIN_BIND@ filled in here.

LAUNCHD_DOMAIN="gui/$(id -u)"

# Write every agent's plist; print the names whose plist changed
write_launch_agents() {
  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
  python3 - "$MEDIA_SERVICES_JSON" "$LABEL_PREFIX" "$HOME/Library/LaunchAgents" \
    "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR" "${ADMIN_BIND:-0.0.0.0}" "${TZ_VALUE:-}" << 'PY'
import json, os, plistlib, sys

manifest, prefix, agents_dir, config_dir, state_dir, log_dir, admin_bind, tz = sys.argv[1:]
subst = {"@CONFIG@": config_dir, "@STATE@": state_dir, "@ADMIN_BIND@": admin_bind}

def fill(s):
    for k, v in subst.items():
        s = s.replace(k, v)
    return s

services = json.load(open(manifest))["services"]
for name, svc in services.items():
    label = f"{prefix}.{name}"
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    if tz:
        env["TZ"] = tz
    env.update({k: fill(v) for k, v in svc.get("env", {}).items()})
    plist = {
        "Label": label,
        "ProgramArguments": [fill(a) for a in svc["args"]],
        "EnvironmentVariables": env,
        "WorkingDirectory": os.path.join(config_dir, name),
        "RunAtLoad": True,
        "KeepAlive": True,
        # Don't restart a crash-looping service more than every 10s
        "ThrottleInterval": 10,
        # Jellyfin and the *arr apps can take a while to shut down cleanly
        "ExitTimeOut": 30,
        "StandardOutPath": os.path.join(log_dir, f"{name}.log"),
        "StandardErrorPath": os.path.join(log_dir, f"{name}.log"),
    }
    os.makedirs(plist["WorkingDirectory"], exist_ok=True)
    path = os.path.join(agents_dir, f"{label}.plist")
    new = plistlib.dumps(plist)
    try:
        old = open(path, "rb").read()
    except FileNotFoundError:
        old = None
    if new != old:
        with open(path, "wb") as f:
            f.write(new)
        print(name)
PY
}

svc_loaded() { launchctl print "$LAUNCHD_DOMAIN/$(svc_label "$1")" >/dev/null 2>&1; }

# Load agents that aren't running; reload the ones whose plist changed
start_services() {
  local changed="$1" name
  for name in $SERVICE_NAMES; do
    if svc_loaded "$name"; then
      if printf '%s\n' "$changed" | grep -qx "$name"; then
        svc_stop "$name" || err "Could not stop $name to apply its new settings"
        launchctl bootstrap "$LAUNCHD_DOMAIN" "$(svc_plist "$name")"
        ok "$name (restarted with new settings)"
      else
        ok "$name (running)"
      fi
    else
      launchctl bootstrap "$LAUNCHD_DOMAIN" "$(svc_plist "$name")"
      ok "$name (started)"
    fi
  done
}

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

# Start agents that already exist (after stop_services), without rewriting them
resume_services() {
  local name
  for name in $SERVICE_NAMES; do
    [ -f "$(svc_plist "$name")" ] && ! svc_loaded "$name" && \
      launchctl bootstrap "$LAUNCHD_DOMAIN" "$(svc_plist "$name")"
  done
  return 0
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
