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
  for f in lib.sh service_registry.sh launchd.sh maintenance.sh e2e.sh; do . "$ROOT/scripts/$f"; done
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

# ─── Interrupted password changes ────────────────────────────────

# Sonarr's login change fails, then applies but doesn't work, then works:
# the record only advances at the end
test_arr_login_advances_only_when_verified() {
  JELLYFIN_USER=admin JELLYFIN_PASS=new
  creds_load
  creds_set sonarr admin old
  api() {
    case "$1 $2" in
      "GET "*/config/host) echo '{"id":1,"username":"admin","authenticationMethod":"forms"}' ;;
      "PUT "*/config/host/1) [ -f "$FAKE/put_ok" ] ;;
      *) return 22 ;;
    esac
  }
  # The login form: redirect to the app only once the fake says it works
  curl() {
    if [ -f "$FAKE/login_ok" ]; then echo "http://localhost:8989/"; else echo "http://localhost:8989/login?loginFailed=true"; fi
  }

  set_arr_login Sonarr http://localhost:8989 key v3 sonarr >/dev/null
  expect_eq "$(creds_get sonarr password)" old "PUT failed"

  touch "$FAKE/put_ok"
  set_arr_login Sonarr http://localhost:8989 key v3 sonarr >/dev/null
  expect_eq "$(creds_get sonarr password)" old "applied, but the new login doesn't work"

  touch "$FAKE/login_ok"
  set_arr_login Sonarr http://localhost:8989 key v3 sonarr >/dev/null
  expect_eq "$(creds_get sonarr password)" new "applied and verified"
  # And it survives a reload from disk
  creds_load
  expect_eq "$(creds_get sonarr password)" new "after reload"
}

# Jellyfin's password change is interrupted: the old password stays
# recorded (Jellyfin needs it to change the password later), and the next
# run finishes the change
test_jellyfin_change_retried_after_interruption() {
  JELLYFIN_USER=admin JELLYFIN_PASS=new MOVIES_DIR=/m TV_DIR=/t ANIME_DIR=/a
  creds_load
  creds_set jellyfin admin old
  echo old > "$FAKE/jf_pass"
  curl() { :; }  # the startup wizard is done
  api() {
    local method="$1" url="$2"; shift 2
    case "$method $url" in
      "POST "*/Users/AuthenticateByName)
        local body="${*: -1}"
        [ "$(jq -r .Pw <<< "$body")" = "$(cat "$FAKE/jf_pass")" ] && echo '{"AccessToken":"tok"}' || return 22 ;;
      "GET "*/Users/Me) echo '{"Id":"u1"}' ;;
      "POST "*/Users/u1/Password)
        [ -f "$FAKE/change_ok" ] || return 22
        echo new > "$FAKE/jf_pass" ;;
      "GET "*/Library/VirtualFolders) echo '[]' ;;
      *) return 22 ;;
    esac
  }

  configure_jellyfin >/dev/null
  expect_eq "$(creds_get jellyfin password)" old "change failed"

  touch "$FAKE/change_ok"
  configure_jellyfin >/dev/null
  expect_eq "$(cat "$FAKE/jf_pass")" new "Jellyfin's password"
  expect_eq "$(creds_get jellyfin password)" new "record after the retry"
}

# The older credentials file (one shared login) upgrades per service,
# without guessing Cleanuparr's password
test_credentials_upgrade_from_shared_record() {
  jq -n '{jellyfin: {username: "admin", password: "pw"}, qbittorrent: {username: "q", password: "qp"}}' > "$STATE_DIR/credentials.json"
  creds_load
  expect_eq "$(creds_get sonarr password)" pw "sonarr"
  expect_eq "$(creds_get bazarr username)" admin "bazarr"
  expect_eq "$(creds_get qbittorrent password)" qp "qbittorrent"
  expect_eq "$(creds_get cleanuparr password)" "" "cleanuparr"
}

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

# ─── e2e cleanup and pre-checks ──────────────────────────────────

e2e_fakes() {
  RADARR_KEY=r SONARR_KEY=s SEERR_KEY=k JELLYFIN_TOKEN=""
  DL_COMPLETE="$MEDIA_DIR/downloads"
  mkdir -p "$STATE_DIR/e2e"
  echo '{"movie_id":"42"}' > "$STATE_DIR/e2e/owned.json"
  api() {
    case "$1 $2" in
      "GET "*/queue*) echo '{"records":[]}' ;;
      "DELETE "*) touch "$FAKE/deleted" ;;
      *) return 22 ;;
    esac
  }
}

# Radarr not answering during cleanup: nothing counts as removed, the
# record is kept for the next run
test_cleanup_keeps_record_when_radarr_is_down() {
  e2e_fakes
  api_status() { echo 000; }
  e2e_cleanup >/dev/null && fail "cleanup reported success"
  [ -f "$STATE_DIR/e2e/owned.json" ] || fail "ownership record dropped"
}

# Movie already gone (404): cleanup succeeds and forgets it
test_cleanup_treats_404_as_gone() {
  e2e_fakes
  api_status() { echo 404; }
  e2e_cleanup >/dev/null || fail "cleanup failed"
  [ ! -f "$STATE_DIR/e2e/owned.json" ] || fail "record kept after a clean run"
  [ ! -f "$FAKE/deleted" ] || fail "tried to delete a movie that's gone"
}

# Movie still there (200): it's deleted
test_cleanup_deletes_existing_movie() {
  e2e_fakes
  api_status() { echo 200; }
  e2e_cleanup >/dev/null || fail "cleanup failed"
  [ -f "$FAKE/deleted" ] || fail "movie not deleted"
}

# The pre-check can't reach Radarr: it must stop, not assume the test
# titles are absent
test_clean_slate_stops_when_radarr_is_down() {
  RADARR_KEY=r SONARR_KEY=s SEERR_KEY=k
  api() { return 22; }
  local out
  out=$(e2e_require_clean_slate 2>&1) && fail "went ahead without checking Radarr"
  grep -q "Couldn't reach Radarr" <<< "$out" || fail "unexpected message: $out"
}

# ─── Interrupted anime migration ─────────────────────────────────

# A series is added, but copying its monitoring fails: the migration isn't
# marked complete, and the next run finishes that series (not skips it)
test_migration_finishes_interrupted_series() {
  SONARR_KEY=s SONARR_ANIME_PROFILE=HD-1080p
  mkdir -p "$CONFIG_DIR/sonarr-anime"
  command -v sqlite3 >/dev/null || fail "sqlite3 is needed"
  sqlite3 "$CONFIG_DIR/sonarr-anime/sonarr.db" "CREATE TABLE Series (Id, TvdbId, Path, Monitored, Seasons); INSERT INTO Series VALUES (7, 111, '/a/Show', 1, '[]');"
  echo '[]' > "$FAKE/series"
  api() {
    case "$1 $2" in
      "GET "*/api/v3/series) cat "$FAKE/series" ;;
      "GET "*/qualityprofile) echo '[{"id":4,"name":"Anime"}]' ;;
      "GET "*/series/lookup*) echo '[{"title":"Show","tvdbId":111}]' ;;
      "POST "*/api/v3/series) echo '[{"id":50,"tvdbId":111}]' > "$FAKE/series"; echo '{"id":50}' ;;
      "POST "*/command) ;;
      *) return 22 ;;
    esac
  }
  # Copying monitoring fails the first time, works the second
  migrate_monitoring() { [ -f "$FAKE/monitoring_ok" ] && echo "$3" > "$FAKE/finished"; }

  migrate_anime_sonarr >/dev/null
  [ ! -f "$STATE_DIR/sonarr-anime-migrated" ] || fail "marked complete after a failure"
  expect_eq "$(jq -r '.added["111"]' "$STATE_DIR/sonarr-anime-migration.json")" 50 "series recorded as added"

  touch "$FAKE/monitoring_ok"
  migrate_anime_sonarr >/dev/null
  expect_eq "$(cat "$FAKE/finished" 2>/dev/null)" 50 "monitoring copied for the series added earlier"
  [ -f "$STATE_DIR/sonarr-anime-migrated" ] || fail "not marked complete after finishing"
}

# A series that was already in Sonarr before the migration is left alone
test_migration_leaves_existing_series_alone() {
  SONARR_KEY=s SONARR_ANIME_PROFILE=HD-1080p
  mkdir -p "$CONFIG_DIR/sonarr-anime"
  sqlite3 "$CONFIG_DIR/sonarr-anime/sonarr.db" "CREATE TABLE Series (Id, TvdbId, Path, Monitored, Seasons); INSERT INTO Series VALUES (7, 111, '/a/Show', 1, '[]');"
  api() {
    case "$1 $2" in
      "GET "*/api/v3/series) echo '[{"id":9,"tvdbId":111}]' ;;
      "GET "*/qualityprofile) echo '[{"id":4,"name":"Anime"}]' ;;
      *) return 22 ;;
    esac
  }
  migrate_monitoring() { touch "$FAKE/touched"; }
  migrate_anime_sonarr >/dev/null
  [ ! -f "$FAKE/touched" ] || fail "changed a series that was already in Sonarr"
  [ -f "$STATE_DIR/sonarr-anime-migrated" ] || fail "not marked complete"
}

# ─── Cleanuparr's login safeguard ────────────────────────────────

cleanuparr_db() {
  mkdir -p "$CONFIG_DIR/cleanuparr"
  sqlite3 "$CONFIG_DIR/cleanuparr/cleanuparr.db" "CREATE TABLE general_configs (auth_disable_auth_for_local_addresses INTEGER); INSERT INTO general_configs VALUES (1);"
  sqlite3 "$CONFIG_DIR/cleanuparr/users.db" "CREATE TABLE users (api_key TEXT); INSERT INTO users VALUES ('k');"
}

# Not running: the login requirement is turned on in its database
test_cleanuparr_login_turned_on_while_stopped() {
  cleanuparr_db
  cleanuparr_require_login >/dev/null
  expect_eq "$(sqlite3 "$CONFIG_DIR/cleanuparr/cleanuparr.db" 'SELECT auth_disable_auth_for_local_addresses FROM general_configs')" 0 "bypass"
}

# Running, not answering, and won't stop: setup must stop, not carry on
test_cleanuparr_safeguard_stops_setup() {
  cleanuparr_db
  svc_loaded() { [ "$1" = cleanuparr ]; }
  svc_stop() { return 1; }
  api() { return 22; }
  ( cleanuparr_require_login >/dev/null 2>&1; echo "carried on" ) | grep -q "carried on" && fail "setup carried on with the login bypass on"
  return 0
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

# ─── e2e: Radarr indexers ────────────────────────────────────────

# Restoring fails for one indexer: it stays recorded for the next run
test_paused_indexers_kept_until_restored() {
  RADARR_KEY=r
  mkdir -p "$STATE_DIR/e2e"
  echo '[{"id":1,"enableAutomaticSearch":true,"enableRss":true},{"id":2,"enableAutomaticSearch":true,"enableRss":false}]' > "$(e2e_paused_file)"
  api_status() { echo 200; }
  api() {
    case "$1 $2" in
      "GET "*/indexer/*) echo '{"enableAutomaticSearch":false,"enableRss":false}' ;;
      "PUT "*/indexer/1*) return 0 ;;
      "PUT "*/indexer/2*) [ -f "$FAKE/radarr_ok" ] ;;
      *) return 22 ;;
    esac
  }
  e2e_resume_radarr_indexers >/dev/null 2>&1 && fail "reported restored while one indexer failed"
  expect_eq "$(jq -c '[.[].id]' "$(e2e_paused_file)")" "[2]" "still-paused record"
  touch "$FAKE/radarr_ok"
  e2e_resume_radarr_indexers >/dev/null || fail "restore failed"
  [ ! -f "$(e2e_paused_file)" ] || fail "record kept after restoring"
}

echo "Failure-path tests"
for t in $(declare -F | awk '{print $3}' | grep '^test_'); do
  run_test "$t"
done
echo ""
echo "  $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
