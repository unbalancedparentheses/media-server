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
  trap - ERR
  set +e
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

echo "Failure-path tests"
for t in $(declare -F | awk '{print $3}' | grep '^test_'); do
  run_test "$t"
done
echo ""
echo "  $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
