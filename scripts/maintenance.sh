#!/usr/bin/env bash
# Non-setup modes: backup, restore, update, preflight, config check.

# ─── Backup function ────────────────────────────────────────────
# The archive holds ~/media/config plus the repo's .env and config.toml
# (under repo-files/), which carry the Immich DB password and all
# credentials — without them a restore on a new machine can't open the
# Immich database. Containers are stopped while archiving so the SQLite
# and Postgres files are consistent.
do_backup() {
  [ ! -d "$CONFIG_DIR" ] && err "Config directory not found: $CONFIG_DIR"
  mkdir -p "$BACKUP_DIR"

  local timestamp backup_file backup_size backup_count staging running=0 f
  timestamp=$(date +%Y%m%d_%H%M%S)
  backup_file="$BACKUP_DIR/media-server_${timestamp}.tar.gz"
  staging="$TMPDIR_SETUP/backup"

  info "Backing up service configs..."

  mkdir -p "$staging/repo-files"
  for f in .env config.toml; do
    [ -f "$SCRIPT_DIR/$f" ] && cp "$SCRIPT_DIR/$f" "$staging/repo-files/"
  done

  if has_cmd docker && docker info >/dev/null 2>&1 && has_docker_compose; then
    running=$(dc ps -q --status running 2>/dev/null | grep -c . || true)
  fi
  if [ "$running" -gt 0 ]; then
    info "Stopping containers for a consistent snapshot..."
    dc stop >/dev/null 2>&1
  fi

  # Logs and Immich's ML model cache are large and re-created on start
  if ! (umask 077 && tar czf "$backup_file" \
      --exclude='config/*/logs' --exclude='config/jellyfin/log' --exclude='config/immich-ml' \
      -C "$MEDIA_DIR" config -C "$staging" repo-files); then
    [ "$running" -gt 0 ] && dc start >/dev/null 2>&1
    rm -f "$backup_file"
    err "Backup failed"
  fi

  if [ "$running" -gt 0 ]; then
    dc start >/dev/null 2>&1
    ok "Containers restarted"
  fi

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
  echo "  Restore with: ./setup.sh --restore $backup_file"
  echo ""
}

# ─── Restore function ───────────────────────────────────────────
# The current config directory and repo files are kept alongside with a
# .pre-restore-<timestamp> suffix rather than overwritten.
do_restore() {
  [ -z "$RESTORE_FILE" ] && err "Usage: ./setup.sh --restore <backup-file>"
  [ ! -f "$RESTORE_FILE" ] && err "Backup file not found: $RESTORE_FILE"
  ensure_compose_ready

  local timestamp extract f
  timestamp=$(date +%Y%m%d_%H%M%S)
  extract="$MEDIA_DIR/.restore-$timestamp"

  info "Restoring from $RESTORE_FILE..."
  echo "  Current configs move to $CONFIG_DIR.pre-restore-$timestamp"
  if [ "$NON_INTERACTIVE" = "true" ]; then
    ok "Non-interactive mode: restore confirmation auto-accepted"
  else
    echo ""
    read -r -p "  Continue? [y/N] " confirm
    [ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && { echo "  Aborted."; exit 0; }
  fi

  info "Extracting backup..."
  mkdir -p "$extract"
  tar xzf "$RESTORE_FILE" -C "$extract"
  [ -d "$extract/config" ] || { rm -rf "$extract"; err "Not a media-server backup (no config/ inside)"; }

  info "Stopping containers..."
  dc down 2>/dev/null || true

  [ -d "$CONFIG_DIR" ] && mv "$CONFIG_DIR" "$CONFIG_DIR.pre-restore-$timestamp"
  mv "$extract/config" "$CONFIG_DIR"
  ok "Configs restored"

  if [ -d "$extract/repo-files" ]; then
    for f in .env config.toml; do
      [ -f "$extract/repo-files/$f" ] || continue
      [ -f "$SCRIPT_DIR/$f" ] && mv "$SCRIPT_DIR/$f" "$SCRIPT_DIR/$f.pre-restore-$timestamp"
      mv "$extract/repo-files/$f" "$SCRIPT_DIR/$f"
      ok "Restored $f"
    done
  else
    warn "Backup predates .env/config.toml backups; keeping the current ones"
    [ -f "$CONFIG_DIR/immich-postgres/immich_dump.sql" ] && \
      warn "If Immich can't open its database, restore config/immich-postgres/immich_dump.sql manually"
  fi
  rm -rf "$extract"

  info "Starting containers..."
  dc up -d
  ok "All containers started"

  echo ""
  echo "  Restore complete. Run ./setup.sh to re-apply config, or ./setup.sh --test to verify."
  echo "  Previous configs: $CONFIG_DIR.pre-restore-$timestamp (delete once you're happy)"
  echo ""
}

# ─── Update function ────────────────────────────────────────────
# Image versions are pinned in docker-compose.yml, so updating means pulling
# this repo and re-running setup, which pulls the new images and applies any
# config changes that came with them.
do_update() {
  [ ! -f "$COMPOSE_FILE" ] && err "docker-compose.yml not found"
  ensure_compose_ready

  info "Creating pre-update backup..."
  do_backup

  info "Updating media-server..."
  if git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [ -n "$(git -C "$SCRIPT_DIR" status --porcelain --untracked-files=no)" ]; then
      warn "Local changes in $SCRIPT_DIR; not pulling (commit or stash them to update)"
    elif git -C "$SCRIPT_DIR" pull --ff-only; then
      ok "Repository updated"
    else
      warn "git pull failed; continuing with the current version"
    fi
  else
    warn "$SCRIPT_DIR is not a git checkout; skipping repository update"
  fi

  info "Pulling images..."
  dc pull

  info "Re-running setup..."
  local setup_args=()
  [ "$NON_INTERACTIVE" = "true" ] && setup_args+=(--yes)
  "$SCRIPT_DIR/setup.sh" ${setup_args[@]+"${setup_args[@]}"}

  local old_images
  old_images=$(docker images --filter "dangling=true" -q 2>/dev/null | wc -l | tr -d ' ')
  if [ "$old_images" -gt 0 ]; then
    info "Cleaning up $old_images old image(s)..."
    docker image prune -f >/dev/null 2>&1
    ok "Old images removed"
  fi
}

do_preflight() {
  local failed=0
  local config_json=""
  local value=""

  pf_ok() { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
  pf_fail() { printf "\033[1;31m[FAIL]\033[0m %s\n" "$*"; failed=1; }
  pf_warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }

  preflight_check_cmd() {
    local name="$1"
    if has_cmd "$name"; then
      pf_ok "$name installed"
    else
      pf_fail "$name is missing"
    fi
  }

  echo "Preflight checks for media-server"
  echo ""

  preflight_check_cmd bash
  preflight_check_cmd curl
  preflight_check_cmd jq
  preflight_check_cmd python3

  if has_cmd docker; then
    pf_ok "docker installed"
    if docker info >/dev/null 2>&1; then
      pf_ok "docker daemon is running"
      if has_docker_compose; then
        pf_ok "docker compose plugin available"
      else
        pf_fail "docker compose plugin is missing"
      fi
    else
      pf_fail "docker is installed but daemon is not running"
    fi
  else
    pf_fail "docker is missing"
  fi

  if [ -f "$CONFIG_FILE" ]; then
    pf_ok "config.toml exists"
    if has_cmd jq; then
      if config_json=$(try_load_config_json "$CONFIG_FILE" 2>/dev/null); then
        pf_ok "config.toml parses as valid TOML"
        for path in \
          ".jellyfin.username" \
          ".jellyfin.password" \
          ".qbittorrent.username" \
          ".qbittorrent.password" \
          ".downloads.complete" \
          ".downloads.incomplete" \
          ".quality.sonarr_profile" \
          ".quality.sonarr_anime_profile" \
          ".quality.radarr_profile"; do
          value=$(echo "$config_json" | jq -r "$path // empty")
          if [ -n "$value" ] && [ "$value" != "null" ]; then
            pf_ok "required config present: $path"
          else
            pf_fail "required config missing: $path"
          fi
        done

        value=$(echo "$config_json" | jq -r '.downloads.complete // empty')
        [[ "$value" == /* ]] && pf_ok "downloads.complete is absolute" || pf_fail "downloads.complete must be an absolute path"
        value=$(echo "$config_json" | jq -r '.downloads.incomplete // empty')
        [[ "$value" == /* ]] && pf_ok "downloads.incomplete is absolute" || pf_fail "downloads.incomplete must be an absolute path"
        value=$(echo "$config_json" | jq -r '.downloads.seeding_ratio // empty')
        is_non_negative_number "$value" && pf_ok "downloads.seeding_ratio is valid" || pf_fail "downloads.seeding_ratio must be a non-negative number"
        value=$(echo "$config_json" | jq -r '.downloads.seeding_time_minutes // empty')
        is_non_negative_int "$value" && pf_ok "downloads.seeding_time_minutes is valid" || pf_fail "downloads.seeding_time_minutes must be a non-negative integer"
        value=$(echo "$config_json" | jq -r "$TIMEZONE_PATH // empty")
        [ -n "$value" ] && pf_ok "timezone is set" || pf_fail "timezone must be set"
      else
        pf_fail "config.toml is invalid TOML"
      fi
    else
      pf_warn "skipping config content validation (jq unavailable)"
    fi
  else
    pf_warn "config.toml is missing (copy config.toml.example first)"
    failed=1
  fi

  if [ -f "$COMPOSE_FILE" ]; then
    pf_ok "docker-compose.yml exists"
    if has_cmd docker && docker info >/dev/null 2>&1 && has_docker_compose; then
      if [ -f "$OVERRIDE_FILE" ]; then
        docker compose -f "$COMPOSE_FILE" -f "$OVERRIDE_FILE" config -q >/dev/null 2>&1 && \
          pf_ok "docker compose config is valid (base + override)" || pf_fail "docker compose config is invalid"
      else
        docker compose -f "$COMPOSE_FILE" config -q >/dev/null 2>&1 && \
          pf_ok "docker compose config is valid" || pf_fail "docker compose config is invalid"
      fi
    else
      pf_warn "skipping docker compose validation (docker/compose unavailable)"
    fi
  else
    pf_fail "docker-compose.yml is missing"
  fi

  echo ""
  if [ "$failed" -eq 0 ]; then
    pf_ok "preflight passed"
  else
    pf_fail "preflight failed"
  fi
  return "$failed"
}
do_check_config() {
  require_cmd jq
  [ -f "$CONFIG_FILE" ] || err "config.toml not found"
  CONFIG_JSON=$(load_config_json "$CONFIG_FILE")
  validate_required_config
  validate_config_semantics

  info "Config validation passed"
  ok "Credentials and required fields are present"
  ok "Download paths and numeric values are valid"
  ok "Timezone is set"
}
smoke_check_generated_files() {
  local missing=0
  local f

  info "Running generated-file smoke checks..."
  for f in "$SCRIPT_DIR/.env" "$SCRIPT_DIR/docker-compose.override.yml"; do
    if [ -s "$f" ]; then
      ok "Present: $f"
    else
      warn "Missing/empty: $f"
      missing=1
    fi
  done

  if [ "$missing" -ne 0 ]; then
    err "Generated-file smoke checks failed"
  fi

  dc config -q >/dev/null 2>&1 || err "Docker Compose config validation failed"
  ok "Docker Compose config validates"
}
