#!/usr/bin/env bash
set -Eeo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_FILE="$ROOT_DIR/config.toml"
COMPOSE_FILE="$ROOT_DIR/docker-compose.yml"

pass() { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m[FAIL]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }

has_cmd() { command -v "$1" >/dev/null 2>&1; }
cfg() { echo "$CONFIG_JSON" | jq -r "$1"; }

FAILED=0

check_cmd() {
  local name="$1"
  if has_cmd "$name"; then
    pass "$name installed"
  else
    fail "$name is missing"
    FAILED=1
  fi
}

echo "Preflight checks for media-server"
echo ""

check_cmd bash
check_cmd curl
check_cmd jq
check_cmd yq
check_cmd python3

if has_cmd docker; then
  pass "docker installed"
  if docker info >/dev/null 2>&1; then
    pass "docker daemon is running"
  else
    fail "docker is installed but daemon is not running"
    FAILED=1
  fi
else
  fail "docker is missing"
  FAILED=1
fi

if [ -f "$CONFIG_FILE" ]; then
  pass "config.toml exists"
  if has_cmd yq && has_cmd jq; then
    if CONFIG_JSON=$(yq -p toml -o json '.' "$CONFIG_FILE" 2>/dev/null); then
      pass "config.toml parses as valid TOML"
      required_paths=(
        ".jellyfin.username"
        ".jellyfin.password"
        ".qbittorrent.username"
        ".qbittorrent.password"
        ".downloads.complete"
        ".downloads.incomplete"
        ".quality.sonarr_profile"
        ".quality.sonarr_anime_profile"
        ".quality.radarr_profile"
      )
      for path in "${required_paths[@]}"; do
        value="$(cfg "$path // empty")"
        if [ -n "$value" ] && [ "$value" != "null" ]; then
          pass "required config present: $path"
        else
          fail "required config missing: $path"
          FAILED=1
        fi
      done
    else
      fail "config.toml is invalid TOML"
      FAILED=1
    fi
  else
    warn "skipping config content validation (jq/yq unavailable)"
  fi
else
  warn "config.toml is missing (copy config.toml.example first)"
  FAILED=1
fi

if [ -f "$COMPOSE_FILE" ]; then
  pass "docker-compose.yml exists"
  if has_cmd docker && docker info >/dev/null 2>&1; then
    if docker compose -f "$COMPOSE_FILE" config -q >/dev/null 2>&1; then
      pass "docker compose config is valid"
    else
      fail "docker compose config is invalid"
      FAILED=1
    fi
  else
    warn "skipping docker compose validation (docker unavailable)"
  fi
else
  fail "docker-compose.yml is missing"
  FAILED=1
fi

echo ""
if [ "$FAILED" -eq 0 ]; then
  pass "preflight passed"
else
  fail "preflight failed"
fi

exit "$FAILED"
