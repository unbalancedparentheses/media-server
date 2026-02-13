#!/usr/bin/env bash
set -eo pipefail

# ═══════════════════════════════════════════════════════════════════
# Media Server — One-command bootstrap for a fresh Mac
# Usage: ./bootstrap.sh
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MEDIA_DIR="$HOME/media"

info()  { printf "\n\033[1;34m=> %s\033[0m\n" "$*"; }
ok()    { printf "\033[1;32m   ✓ %s\033[0m\n" "$*"; }
warn()  { printf "\033[1;33m   ! %s\033[0m\n" "$*"; }
err()   { printf "\033[1;31m   ✗ %s\033[0m\n" "$*"; exit 1; }

# ─── 1. Prerequisites ────────────────────────────────────────────
info "Checking prerequisites..."

# Homebrew
if ! command -v brew &>/dev/null; then
  info "Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
  ok "Homebrew installed"
else
  ok "Homebrew"
fi

# Docker
if ! command -v docker &>/dev/null; then
  info "Installing Docker Desktop..."
  brew install --cask docker
  ok "Docker Desktop installed — please open it from Applications and wait for it to start"
  echo ""
  echo "  After Docker Desktop is running, re-run this script."
  echo ""
  exit 0
elif ! docker info &>/dev/null; then
  err "Docker is installed but not running. Start Docker Desktop and re-run this script."
fi
ok "Docker"

# jq (used by setup.sh)
if ! command -v jq &>/dev/null; then
  brew install jq
  ok "jq installed"
else
  ok "jq"
fi

# ─── 2. Directory structure ──────────────────────────────────────
info "Creating directory structure..."

mkdir -p "$MEDIA_DIR"/{movies,tv,anime}
mkdir -p "$MEDIA_DIR"/downloads/torrents/{complete,incomplete}
mkdir -p "$MEDIA_DIR"/downloads/usenet/{complete,incomplete}
mkdir -p "$MEDIA_DIR"/backups
mkdir -p "$MEDIA_DIR"/config/{jellyfin,sonarr,sonarr-anime,radarr,prowlarr,bazarr,sabnzbd,qbittorrent,jellyseerr,recyclarr,flaresolverr,nginx}/logs

ok "~/media/ directory tree created"

# ─── 3. Docker Compose ───────────────────────────────────────────
info "Starting containers..."

docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d

ok "All containers started"

# ─── 4. /etc/hosts ───────────────────────────────────────────────
info "Checking /etc/hosts..."

DOMAINS="media.local jellyfin.media.local jellyseerr.media.local sonarr.media.local sonarr-anime.media.local radarr.media.local prowlarr.media.local bazarr.media.local sabnzbd.media.local qbittorrent.media.local"

if ! grep -q "media.local" /etc/hosts 2>/dev/null; then
  echo ""
  echo "  Adding .media.local domains to /etc/hosts (requires sudo)..."
  echo ""
  sudo bash -c "echo '' >> /etc/hosts && echo '# Media Server' >> /etc/hosts && echo '127.0.0.1 $DOMAINS' >> /etc/hosts"
  ok "Hosts entries added"
else
  ok "Hosts entries already present"
fi

# ─── 5. Run setup (connects all services) ────────────────────────
info "Running service setup..."

bash "$SCRIPT_DIR/setup.sh"

# ─── 6. Done ─────────────────────────────────────────────────────
echo ""
echo "  ┌──────────────────────────────────────────────────────────┐"
echo "  │                   Setup Complete!                        │"
echo "  ├──────────────────────────────────────────────────────────┤"
echo "  │                                                          │"
echo "  │  Dashboard:    http://media.local                        │"
echo "  │  Request:      http://jellyseerr.media.local             │"
echo "  │  Watch:        http://jellyfin.media.local               │"
echo "  │                                                          │"
echo "  │  All services: http://media.local (links to everything)  │"
echo "  │                                                          │"
echo "  └──────────────────────────────────────────────────────────┘"
echo ""
echo "  Quick start:"
echo "    1. Go to http://jellyseerr.media.local"
echo "    2. Search for a series or movie"
echo "    3. Click Request"
echo "    4. Watch at http://jellyfin.media.local"
echo ""
