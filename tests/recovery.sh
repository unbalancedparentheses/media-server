#!/usr/bin/env bash
# Failure-path tests: interrupted password changes, restoring older backups,
# and e2e cleanup when services don't answer. They run the real setup
# functions against a scratch ~/media, with the HTTP and launchd helpers
# replaced by fakes, so no services are needed.
#
# Run: nix run .#unit   (or: bash tests/recovery.sh, with jq and python3)
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASSED=0
FAILED=0

# Each test runs in a subshell with a fresh scratch install
run_test() {
  local name="$1" out
  if out=$( (sandbox; "$name") 2>&1 ); then
    printf '\033[1;32m  ✓ %s\033[0m\n' "$name"
    PASSED=$((PASSED + 1))
  else
    printf '\033[1;31m  ✗ %s\033[0m\n%s\n' "$name" "$(sed 's/^/      /' <<< "$out")"
    FAILED=$((FAILED + 1))
  fi
}

sandbox() {
  MEDIA_DIR=$(mktemp -d)
  trap 'rm -rf "$MEDIA_DIR"' EXIT
  CONFIG_FILE="$MEDIA_DIR/config.toml" CONFIG_DIR="$MEDIA_DIR/config" STATE_DIR="$MEDIA_DIR/.state"
  LOG_DIR="$MEDIA_DIR/logs" BACKUP_DIR="$MEDIA_DIR/backups" MAX_BACKUPS=10
  SCRIPT_DIR="$ROOT" DRY_RUN=false NON_INTERACTIVE=true PURGE=false
  mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR"
  # shellcheck source=/dev/null
  for f in lib.sh service_registry.sh launchd.sh maintenance.sh; do . "$ROOT/scripts/$f"; done
  # shellcheck source=/dev/null
  for f in "$ROOT"/scripts/steps/*.sh; do . "$f"; done
  # The shell settings setup.sh runs with (its ERR trap comes from lib.sh):
  # word splitting on newlines/tabs only, stop on errors
  set -Eeo pipefail
  IFS=$'\n\t'
  init_service_registry
  FAKE="$MEDIA_DIR/fake"
  mkdir -p "$FAKE"
  # launchd: nothing is running
  svc_loaded() { return 1; }
  svc_stop() { return 0; }
  stop_services() { return 0; }
  launchctl() { return 0; }
  sleep() { :; }
}

# fail "<message>": ends the test
fail() { echo "$*"; exit 1; }
expect_eq() { [ "$1" = "$2" ] || fail "expected '$2', got '$1' ($3)"; }

# ─── Restoring backups ───────────────────────────────────────────

make_state_records() {
  echo '{"version":2,"services":{"jellyfin":{"username":"admin","password":"current"}}}' > "$STATE_DIR/credentials.json"
  touch "$STATE_DIR/sonarr-anime-migrated"
  mkdir -p "$STATE_DIR/e2e"
  echo '{"movie_id":"42"}' > "$STATE_DIR/e2e/owned.json"
}

# A backup from before setup's records were included: the current records
# (which describe the newer databases) are set aside, not kept
test_restore_old_backup_sets_records_aside() {
  echo old-db > "$CONFIG_DIR/sonarr.db"
  echo 'timezone = "UTC"' > "$CONFIG_FILE"
  tar czf "$MEDIA_DIR/old.tar.gz" -C "$MEDIA_DIR" config config.toml
  echo new-db > "$CONFIG_DIR/sonarr.db"
  make_state_records

  RESTORE_FILE="$MEDIA_DIR/old.tar.gz" do_restore >/dev/null
  expect_eq "$(cat "$CONFIG_DIR/sonarr.db")" old-db "restored database"
  [ ! -e "$STATE_DIR/sonarr-anime-migrated" ] || fail "migration marker kept: the migration would be skipped"
  [ ! -e "$STATE_DIR/e2e/owned.json" ] || fail "e2e ownership record kept"
  [ ! -e "$STATE_DIR/credentials.json" ] || fail "credentials record kept"
  compgen -G "$STATE_DIR/pre-restore-*/credentials.json" >/dev/null || fail "previous records not set aside"
}

# A current backup carries the records and restores them with the databases
test_backup_and_restore_keep_records_together() {
  echo 'timezone = "UTC"' > "$CONFIG_FILE"
  make_state_records
  do_backup >/dev/null
  local backup
  backup=$(ls "$BACKUP_DIR"/media-server_*.tar.gz)
  tar tzf "$backup" | grep -qx '.state/credentials.json' || fail "credentials record not in the backup"

  rm -f "$STATE_DIR/sonarr-anime-migrated"
  echo '{"version":2,"services":{}}' > "$STATE_DIR/credentials.json"
  RESTORE_FILE="$backup" do_restore >/dev/null
  [ -e "$STATE_DIR/sonarr-anime-migrated" ] || fail "migration marker not restored"
  expect_eq "$(jq -r .services.jellyfin.password "$STATE_DIR/credentials.json")" current "restored credentials"
}

# Backup and restore through setup.sh itself (its real shell settings and
# argument handling), against a scratch ~/media and launchd names that
# match no real service
test_entry_point_backup_and_restore() {
  local env=(MEDIA_DIR="$MEDIA_DIR" MEDIA_SERVICES_JSON="$FAKE/services.json" MEDIA_LABEL_PREFIX="test.media-server.$$")
  echo '{}' > "$FAKE/services.json"
  echo 'timezone = "UTC"' > "$CONFIG_FILE"
  echo db > "$CONFIG_DIR/sonarr.db"
  make_state_records
  env "${env[@]}" bash "$ROOT/setup.sh" --backup >/dev/null 2>&1 || fail "setup.sh --backup failed"
  local backup
  backup=$(ls "$BACKUP_DIR"/media-server_*.tar.gz)
  for record in credentials.json sonarr-anime-migrated e2e/owned.json; do
    tar tzf "$backup" | grep -qx ".state/$record" || fail "backup is missing .state/$record"
  done

  rm -f "$STATE_DIR/sonarr-anime-migrated"
  echo '{"version":2,"services":{}}' > "$STATE_DIR/credentials.json"
  env "${env[@]}" bash "$ROOT/setup.sh" --restore "$backup" --yes >/dev/null 2>&1 || fail "setup.sh --restore failed"
  [ -e "$STATE_DIR/sonarr-anime-migrated" ] || fail "migration marker not restored"
  expect_eq "$(jq -r .services.jellyfin.password "$STATE_DIR/credentials.json")" current "restored credentials"
}

# ─── Tailscale ───────────────────────────────────────────────────

# A route published for an old dashboard port is still removed
test_tailscale_removes_route_for_old_dashboard_port() {
  echo 'timezone = "UTC"
[network]
dashboard_port = 80' > "$CONFIG_FILE"
  echo '{"443": "http://127.0.0.1:8080"}' > "$STATE_DIR/tailscale-routes.json"
  detect_tailscale_cli() { echo fake_tailscale; }
  fake_tailscale() {
    local IFS=' '  # "$*" joins with IFS's first character (setup's is a newline)
    case "$*" in
      "serve status --json") echo '{"Web":{"mac.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8080"}}}}}' ;;
      "serve --https=443 off") touch "$FAKE/removed-443" ;;
    esac
  }
  run_timeout() { shift; "$@"; }
  remove_tailscale_serve >/dev/null || fail "reported a failure"
  [ -f "$FAKE/removed-443" ] || fail "old dashboard route left published"
  [ ! -f "$STATE_DIR/tailscale-routes.json" ] || fail "route record kept after removal"
}

# A removal that fails is reported and the record kept
test_tailscale_removal_failure_is_reported() {
  echo 'timezone = "UTC"' > "$CONFIG_FILE"
  echo '{"8096": "http://127.0.0.1:8096"}' > "$STATE_DIR/tailscale-routes.json"
  detect_tailscale_cli() { echo fake_tailscale; }
  fake_tailscale() {
    local IFS=' '  # "$*" joins with IFS's first character (setup's is a newline)
    case "$*" in
      "serve status --json") echo '{"Web":{"mac.ts.net:8096":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8096"}}}}}' ;;
      *) return 1 ;;
    esac
  }
  run_timeout() { shift; "$@"; }
  remove_tailscale_serve >/dev/null 2>&1 && fail "a failed removal was reported as success"
  [ -f "$STATE_DIR/tailscale-routes.json" ] || fail "record dropped after a failed removal"
}

# ─── Recovery edge cases ─────────────────────────────────────────

# Tailscale's status can't be read: nothing counts as removed, record kept
test_tailscale_status_failure_keeps_record() {
  echo 'timezone = "UTC"' > "$CONFIG_FILE"
  echo '{"8096": "http://127.0.0.1:8096"}' > "$STATE_DIR/tailscale-routes.json"
  detect_tailscale_cli() { echo fake_tailscale; }
  fake_tailscale() { return 1; }
  remove_tailscale_serve >/dev/null 2>&1 && fail "reported success without reading the routes"
  [ -f "$STATE_DIR/tailscale-routes.json" ] || fail "route record dropped"
}

# ─── Smaller recovery cases ──────────────────────────────────────

# ─── Operation lock ──────────────────────────────────────────────

# A second operation is refused while the first is running
test_lock_refuses_second_operation() {
  command sleep 30 & local owner=$!
  mkdir -p "$STATE_DIR/lock"; echo "$owner" > "$STATE_DIR/lock/pid"
  local out
  out=$( (acquire_lock) 2>&1 ) && { kill "$owner"; fail "took the lock from a running operation"; }
  kill "$owner" 2>/dev/null
  grep -q "Another media-server operation is running" <<< "$out" || fail "unexpected message: $out"
}

# A lock left by an operation that's gone is taken over
test_lock_taken_over_when_stale() {
  mkdir -p "$STATE_DIR/lock"; echo 999999 > "$STATE_DIR/lock/pid"
  acquire_lock >/dev/null 2>&1 || fail "stale lock not taken over"
  expect_eq "$(cat "$STATE_DIR/lock/pid")" "$$" "lock owner"
  release_lock
  [ ! -e "$STATE_DIR/lock" ] || fail "lock not released"
}

# Through setup.sh: a backup is refused while another operation runs
test_entry_point_refuses_concurrent_operation() {
  echo '{}' > "$FAKE/services.json"
  command sleep 30 & local owner=$!
  mkdir -p "$STATE_DIR/lock"; echo "$owner" > "$STATE_DIR/lock/pid"
  env MEDIA_DIR="$MEDIA_DIR" MEDIA_SERVICES_JSON="$FAKE/services.json" MEDIA_LABEL_PREFIX="test.media-server.$$" \
    bash "$ROOT/setup.sh" --backup >/dev/null 2>&1 && { kill "$owner"; fail "backup ran during another operation"; }
  kill "$owner" 2>/dev/null
  [ ! -e "$BACKUP_DIR" ] || [ -z "$(ls "$BACKUP_DIR" 2>/dev/null)" ] || fail "a backup was written"
  expect_eq "$(cat "$STATE_DIR/lock/pid")" "$owner" "lock still the first operation's"
}

# ─── Config validation ───────────────────────────────────────────

# A typo is caught before anything changes, with a suggestion
test_config_typo_rejected_with_suggestion() {
  echo '{}' > "$FAKE/services.json"
  sed 's/"changeme"/"real-password"/; s/^prefer_h265 = true/prefer_h256 = true/' "$ROOT/config.toml.example" > "$CONFIG_FILE"
  local out
  out=$(env MEDIA_DIR="$MEDIA_DIR" MEDIA_SERVICES_JSON="$FAKE/services.json" bash "$ROOT/setup.sh" --check-config 2>&1) && \
    fail "accepted an unknown setting"
  grep -q 'quality.prefer_h256: unknown setting (did you mean "prefer_h265"?)' <<< "$out" || fail "unexpected output: $out"
  sed 's/"changeme"/"real-password"/' "$ROOT/config.toml.example" > "$CONFIG_FILE"
  env MEDIA_DIR="$MEDIA_DIR" MEDIA_SERVICES_JSON="$FAKE/services.json" bash "$ROOT/setup.sh" --check-config >/dev/null 2>&1 || \
    fail "the example config (with real passwords) was rejected"
}

echo "Failure-path tests"
for t in $(declare -F | awk '{print $3}' | grep '^test_'); do
  run_test "$t"
done
echo ""
echo "  $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
