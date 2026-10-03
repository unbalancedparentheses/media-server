#!/usr/bin/env bash
# Non-setup modes: backup, restore, update, uninstall, status, logs,
# restart, preflight and config check.

# ─── Backup ──────────────────────────────────────────────────────
# Archives ~/media/config, ~/media/config.toml and setup's own records in
# ~/media/.state (applied logins, migration marker, e2e test ownership),
# which describe these databases and must travel with them. Services are
# stopped while archiving so their SQLite databases are consistent.
# An array: setup splits words only on newlines and tabs (IFS)
STATE_RECORDS=(
  .state/credentials.json
  .state/sonarr-anime-migrated
  .state/sonarr-anime-migration.json
  .state/e2e/owned.json
  .state/e2e/paused-indexers.json
  .state/tailscale-routes.json
  .state/renamed-sonarr
  .state/renamed-radarr
  .state/postimport/state.json
)
do_backup() {
  [ -d "$CONFIG_DIR" ] || err "Config directory not found: $CONFIG_DIR"
  mkdir -p "$BACKUP_DIR"

  local backup_file backup_size backup_count f name
  backup_file="$BACKUP_DIR/media-server_$(date +%Y%m%d_%H%M%S).tar.gz"

  info "Backing up service configs..."
  # Stop every running service so no database is copied mid-write, and put
  # back exactly the ones that were running — also if tar fails or you
  # interrupt the backup
  BACKUP_WAS_RUNNING=""
  for name in $SERVICE_NAMES; do
    svc_loaded "$name" && BACKUP_WAS_RUNNING="$BACKUP_WAS_RUNNING$name"$'\n'
  done
  if [ -n "$BACKUP_WAS_RUNNING" ]; then
    info "Stopping services for a consistent snapshot..."
    trap 'restart_backed_up_services; exit 130' INT TERM
    for name in $BACKUP_WAS_RUNNING; do
      svc_stop "$name" || { restart_backed_up_services; err "Could not stop $name; backup aborted (nothing was archived)"; }
    done
  fi

  local records=() record
  for record in "${STATE_RECORDS[@]}"; do [ -e "$MEDIA_DIR/$record" ] && records+=("$record"); done
  # Logs and caches are large and recreated on start
  if ! (umask 077 && tar czf "$backup_file" \
      --exclude='config/*/logs' --exclude='config/jellyfin/log' --exclude='config/jellyfin/cache' \
      --exclude='config/nginx/temp' \
      -C "$MEDIA_DIR" config "$(basename "$CONFIG_FILE")" ${records[@]+"${records[@]}"}); then
    restart_backed_up_services
    rm -f "$backup_file"
    err "Backup failed"
  fi
  restart_backed_up_services

  backup_size=$(du -sh "$backup_file" | cut -f1)
  ok "Created: $backup_file ($backup_size)"

  local backup_files=()
  while IFS= read -r f; do
    backup_files+=("$f")
  done < <(ls -1t "$BACKUP_DIR"/media-server_*.tar.gz 2>/dev/null)
  backup_count="${#backup_files[@]}"
  if [ "$backup_count" -gt "$MAX_BACKUPS" ]; then
    for f in "${backup_files[@]:$MAX_BACKUPS}"; do
      rm -f "$f"
      ok "Pruned: $(basename "$f")"
    done
  fi

  echo ""
  echo "  Backups in $BACKUP_DIR ($backup_count total, keeping last $MAX_BACKUPS)"
  echo "  Backups contain passwords and API keys; keep a copy on another disk."
  echo "  Restore with: nix run .#restore -- $backup_file"
  echo ""
}

# Start the services that were running before do_backup stopped them
restart_backed_up_services() {
  local name
  trap - INT TERM
  [ -n "${BACKUP_WAS_RUNNING:-}" ] || return 0
  for name in $BACKUP_WAS_RUNNING; do
    svc_loaded "$name" || launchctl bootstrap "$LAUNCHD_DOMAIN" "$(svc_plist "$name")" 2>/dev/null || \
      warn "Could not restart $name (nix run .#restart -- $name)"
  done
  ok "Services restarted: $(printf '%s ' $BACKUP_WAS_RUNNING)"
  BACKUP_WAS_RUNNING=""
}

# ─── Restore ─────────────────────────────────────────────────────
# The current config directory and config.toml are kept alongside with a
# .pre-restore-<timestamp> suffix rather than overwritten.
do_restore() {
  [ -n "$RESTORE_FILE" ] || err "Usage: nix run .#restore -- <backup-file>"
  [ -f "$RESTORE_FILE" ] || err "Backup file not found: $RESTORE_FILE"

  local timestamp extract
  timestamp=$(date +%Y%m%d_%H%M%S)
  extract="$MEDIA_DIR/.restore-$timestamp"

  info "Restoring from $RESTORE_FILE..."
  echo "  Current configs move to $CONFIG_DIR.pre-restore-$timestamp"
  if [ "$NON_INTERACTIVE" != "true" ]; then
    local confirm
    read -r -p "  Continue? [y/N] " confirm
    [ "$confirm" = "y" ] || [ "$confirm" = "Y" ] || { echo "  Aborted."; exit 0; }
  fi

  mkdir -p "$extract"
  tar xzf "$RESTORE_FILE" -C "$extract"
  [ -d "$extract/config" ] || { rm -rf "$extract"; err "Not a media-server backup (no config/ inside)"; }

  info "Stopping services..."
  stop_services || { rm -rf "$extract"; err "Some services didn't stop; restore aborted (nothing was changed)"; }

  [ -d "$CONFIG_DIR" ] && mv "$CONFIG_DIR" "$CONFIG_DIR.pre-restore-$timestamp"
  mv "$extract/config" "$CONFIG_DIR"
  if [ -f "$extract/$(basename "$CONFIG_FILE")" ]; then
    [ -f "$CONFIG_FILE" ] && mv "$CONFIG_FILE" "$CONFIG_FILE.pre-restore-$timestamp"
    mv "$extract/$(basename "$CONFIG_FILE")" "$CONFIG_FILE"
  fi
  restore_state_records "$extract" "$timestamp"
  rm -rf "$extract"
  ok "Configs restored"

  echo ""
  echo "  Now run 'nix run .#install' to start the services with the restored configs."
  echo "  Previous configs: $CONFIG_DIR.pre-restore-$timestamp (delete once you're happy)"
  echo ""
}

# Setup's records must match the restored databases: take the backup's, or,
# for a backup made before they were included, set the current ones aside
# (they describe the newer databases). Setup then re-checks every login,
# re-runs the anime migration if needed (it skips series already there),
# and the e2e test forgets items that aren't in the restored databases.
restore_state_records() {  # extract-dir timestamp
  local extract="$1" aside="$STATE_DIR/pre-restore-$2" record moved=false
  for record in "${STATE_RECORDS[@]}"; do
    if [ -e "$MEDIA_DIR/$record" ]; then
      mkdir -p "$aside/$(dirname "${record#.state/}")"
      mv "$MEDIA_DIR/$record" "$aside/${record#.state/}"
      moved=true
    fi
    if [ -e "$extract/$record" ]; then
      mkdir -p "$MEDIA_DIR/$(dirname "$record")"
      mv "$extract/$record" "$MEDIA_DIR/$record"
    fi
  done
  [ "$moved" = true ] && ok "Setup's previous records set aside in $aside"
  [ -e "$extract/.state" ] || warn "This backup predates setup's records; the next install re-checks every login and migration"
  return 0
}

# ─── Update ──────────────────────────────────────────────────────
# Versions are pinned in flake.lock, so updating means pulling this repo and
# re-running setup, which rewrites the agents to the new Nix store paths.
do_update() {
  local repo="${MEDIA_SERVER_REPO:-$PWD}"
  [ -f "$repo/flake.nix" ] && git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1 || \
    err "Run this from your media-server checkout (or set MEDIA_SERVER_REPO)"

  if [ -d "$CONFIG_DIR" ]; then
    info "Creating pre-update backup..."
    do_backup
  fi

  info "Updating $repo..."
  if [ -n "$(git -C "$repo" status --porcelain --untracked-files=no)" ]; then
    warn "Local changes in $repo; not pulling (commit or stash them to update)"
  else
    git -C "$repo" pull --ff-only || warn "git pull failed; continuing with the current version"
  fi

  info "Re-running setup..."
  local setup_args=()
  [ "$NON_INTERACTIVE" = "true" ] && setup_args+=(--yes)
  # The re-run takes the lock itself
  release_lock
  exec nix --extra-experimental-features "nix-command flakes" run "path:$repo#install" -- ${setup_args[@]+"${setup_args[@]}"}
}

# ─── Uninstall ───────────────────────────────────────────────────
# Stops the services and removes their launchd agents and Nix GC root.
# --purge also deletes configs, logs, state and config.toml. The library
# (movies, tv, anime), downloads and backups are never deleted.
do_uninstall() {
  info "Stopping and removing services..."
  local plist name failed=""
  for plist in "$HOME/Library/LaunchAgents/$LABEL_PREFIX".*.plist; do
    [ -e "$plist" ] || continue
    name=$(basename "$plist" .plist)
    name="${name#"$LABEL_PREFIX".}"
    if svc_stop "$name"; then
      rm -f "$plist"
      ok "$name removed"
    else
      failed="$failed $name"
    fi
  done
  [ -z "$failed" ] || err "Could not stop:$failed (the others were removed). Configs and Tailscale were left alone; check 'nix run .#status' and re-run uninstall"
  rm -f "$STATE_DIR/gcroot"

  remove_tailscale_serve || warn "Some Tailscale HTTPS routes are still published (see above)"

  if [ "$PURGE" = "true" ]; then
    echo ""
    echo "  --purge deletes all service settings, accounts, watch history and API"
    echo "  keys: $CONFIG_DIR, $CONFIG_FILE, $LOG_DIR, $STATE_DIR,"
    echo "  and Byparr's browser in ~/Library/Caches/invisible-playwright."
    if [ "$NON_INTERACTIVE" != "true" ]; then
      local confirm
      read -r -p "  Delete them? [y/N] " confirm
      [ "$confirm" = "y" ] || [ "$confirm" = "Y" ] || { echo "  Kept configs."; PURGE=false; }
    fi
  fi
  if [ "$PURGE" = "true" ]; then
    rm -rf "$CONFIG_DIR" "$LOG_DIR" "$STATE_DIR" "$CONFIG_FILE" "$HOME/Library/Caches/invisible-playwright"
    ok "Configs, logs and state deleted"
  fi

  echo ""
  echo "  Uninstalled. Not touched:"
  echo "    Library and downloads: $MOVIES_DIR, $TV_DIR, $ANIME_DIR, $DOWNLOADS_DIR"
  echo "    Backups: $BACKUP_DIR"
  [ "$PURGE" = "true" ] || echo "    Configs (reinstall picks them up): $CONFIG_DIR, $CONFIG_FILE"
  echo "  Free the Nix store space with: nix-collect-garbage"
  echo ""
}

# Undo the `tailscale serve` entries setup adds: HTTPS ports whose handler
# proxies to what setup published there, either recorded when publishing
# (tailscale-routes.json, e.g. an old dashboard port) or what the current
# config would publish. Returns non-zero if a removal failed.
remove_tailscale_serve() {
  local ts_cli port dash="${DASHBOARD_PORT:-80}" routes ours failed=0
  ts_cli="$(detect_tailscale_cli)"
  [ -n "$ts_cli" ] || return 0
  [ -f "$CONFIG_FILE" ] && dash=$(load_config_json "$CONFIG_FILE" | jq -r '.network.dashboard_port // 80' 2>/dev/null || echo 80)
  routes="$STATE_DIR/tailscale-routes.json"
  # port → list of targets that count as ours
  ours=$(jq -nc --arg dash "$dash" --argjson rec "$(cat "$routes" 2>/dev/null || echo '{}')" '
    {"443": ["http://127.0.0.1:\($dash)"], "8096": ["http://127.0.0.1:8096"], "5055": ["http://127.0.0.1:5055"]} as $now
    | reduce ($rec | to_entries[]) as $r ($now; .[$r.key] += [$r.value])')
  local status
  # Can't tell what's published: keep the record and report it
  status=$("$ts_cli" serve status --json 2>/dev/null) && jq -e 'type == "object"' <<< "$status" >/dev/null 2>&1 || {
    warn "Couldn't read Tailscale's published routes; nothing removed (record kept in $routes)"
    return 1
  }
  for port in $(jq -r --argjson ours "$ours" '
      .Web // {} | to_entries[]
      | (.key | split(":") | last) as $port
      | select($ours[$port] != null and any(.value.Handlers[]?; .Proxy as $p | $ours[$port] | index($p)))
      | $port' <<< "$status"); do
    if run_timeout 10 "$ts_cli" serve --https="$port" off </dev/null >/dev/null 2>&1; then
      ok "Tailscale HTTPS :$port removed"
    else
      warn "Couldn't remove Tailscale HTTPS :$port (remove it with: tailscale serve --https=$port off)"
      failed=1
    fi
  done
  [ "$failed" = 0 ] && rm -f "$routes"
  return "$failed"
}

# Remember a route setup published, so it can be removed even after the
# config changes
record_tailscale_route() {  # port target
  local routes="$STATE_DIR/tailscale-routes.json" updated
  updated=$(jq -c --arg p "$1" --arg t "$2" '.[$p] = $t' "$routes" 2>/dev/null) || updated=$(jq -nc --arg p "$1" --arg t "$2" '{($p): $t}')
  write_atomic "$routes" "$updated"
}

# ─── Status / logs / restart ─────────────────────────────────────
do_status() {
  local name state
  info "Services"
  for name in $SERVICE_NAMES; do
    state=$(svc_state "$name")
    case "$state" in
      running*) ok "$(printf '%-13s %s' "$name" "$state")" ;;
      *) warn "$(printf '%-13s %s' "$name" "$state")" ;;
    esac
  done
  info "Health"
  local svc_name svc_url code
  while IFS='|' read -r svc_name svc_url; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 2 "$svc_url" 2>/dev/null || true)
    case "$code" in
      2*|3*) ok "$(printf '%-13s %s' "$svc_name" "$svc_url")" ;;
      *) warn "$(printf '%-13s %s (HTTP %s)' "$svc_name" "$svc_url" "${code:-none}")" ;;
    esac
  done <<< "$SERVICE_HEALTH_ENDPOINTS"
  info "Disk"
  local free_gb
  free_gb=$(( $(df -Pk "$MEDIA_DIR" | awk 'NR == 2 { print $4 }') / 1024 / 1024 ))
  if [ "$free_gb" -lt "${DISK_WARN_GB:-50}" ]; then
    warn "$free_gb GB free on the media disk (warning below ${DISK_WARN_GB:-50} GB, imports stop below ${DISK_MIN_GB:-10} GB)"
  else
    ok "$free_gb GB free on the media disk"
  fi
  echo ""
  echo "  Logs: $LOG_DIR (nix run .#logs -- <service>)"
}

do_logs() {
  local name="$1"
  printf '%s\n' "$SERVICE_NAMES" | grep -qx "$name" || \
    err "Unknown service '$name'. One of: $(printf '%s ' $SERVICE_NAMES)"
  [ -f "$LOG_DIR/$name.log" ] || err "No log yet: $LOG_DIR/$name.log"
  exec tail -n 100 -F "$LOG_DIR/$name.log"
}

do_restart() {
  local name="$1" names
  if [ -n "$name" ]; then
    printf '%s\n' "$SERVICE_NAMES" | grep -qx "$name" || \
      err "Unknown service '$name'. One of: $(printf '%s ' $SERVICE_NAMES)"
    names="$name"
  else
    names="$SERVICE_NAMES"
  fi
  for name in $names; do
    if svc_restart "$name"; then ok "$name restarted"; else warn "$name is not installed"; fi
  done
}

# ─── Preflight / config check ────────────────────────────────────
do_preflight() {
  local failed=0 cmd
  pf_ok() { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
  pf_fail() { printf "\033[1;31m[FAIL]\033[0m %s\n" "$*"; failed=1; }

  echo "Preflight checks for media-server"
  echo ""
  [ "$(uname -s)" = "Darwin" ] && pf_ok "macOS" || pf_fail "macOS is required (services run as launchd agents)"
  for cmd in curl jq python3 openssl launchctl; do
    has_cmd "$cmd" && pf_ok "$cmd" || pf_fail "$cmd is missing"
  done
  [ -f "${MEDIA_SERVICES_JSON:-}" ] && pf_ok "Nix service manifest" || pf_fail "not running through Nix (use 'nix run .#install')"
  if [ -f "$CONFIG_FILE" ]; then
    if (CONFIG_JSON=$(load_config_json "$CONFIG_FILE") && validate_config_semantics) >/dev/null 2>&1; then
      pf_ok "$CONFIG_FILE is valid"
    else
      pf_fail "$CONFIG_FILE is invalid (run with --check-config for details)"
    fi
  else
    pf_ok "no $CONFIG_FILE yet (setup creates it)"
  fi
  echo ""
  if [ "$failed" -eq 0 ]; then pf_ok "preflight passed"; else pf_fail "preflight failed"; fi
  return "$failed"
}

do_check_config() {
  [ -f "$CONFIG_FILE" ] || err "$CONFIG_FILE not found"
  CONFIG_JSON=$(load_config_json "$CONFIG_FILE")
  validate_config_semantics
  info "Config validation passed"
  ok "Credentials, quality profiles, network settings and timezone are valid"
}
