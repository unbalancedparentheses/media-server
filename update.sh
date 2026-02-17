#!/usr/bin/env bash
set -eo pipefail

# ═══════════════════════════════════════════════════════════════════
# Media Server — Update all containers to latest images
# Usage: ./update.sh
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

info()  { printf "\n\033[1;34m=> %s\033[0m\n" "$*"; }
ok()    { printf "\033[1;32m   ✓ %s\033[0m\n" "$*"; }
warn()  { printf "\033[1;33m   ! %s\033[0m\n" "$*"; }

[ ! -f "$COMPOSE_FILE" ] && { echo "docker-compose.yml not found"; exit 1; }
docker info &>/dev/null || { echo "Docker is not running"; exit 1; }

# Back up configs first
if [ -f "$SCRIPT_DIR/backup.sh" ]; then
  info "Creating pre-update backup..."
  bash "$SCRIPT_DIR/backup.sh"
fi

info "Pulling latest images..."
docker compose -f "$COMPOSE_FILE" pull

info "Restarting containers with new images..."
docker compose -f "$COMPOSE_FILE" up -d

# Show what changed
info "Current image versions..."
docker compose -f "$COMPOSE_FILE" images --format "table {{.Service}}\t{{.Tag}}\t{{.Size}}"

# Clean up old images
OLD_IMAGES=$(docker images --filter "dangling=true" -q 2>/dev/null | wc -l | tr -d ' ')
if [ "$OLD_IMAGES" -gt 0 ]; then
  info "Cleaning up $OLD_IMAGES old image(s)..."
  docker image prune -f >/dev/null 2>&1
  ok "Old images removed"
fi

echo ""
echo "  Update complete. Run ./test.sh to verify."
echo ""
