#!/usr/bin/env bash
set -eo pipefail

# ═══════════════════════════════════════════════════════════════════
# Media Server — Backup service configurations
# Creates a timestamped tarball of all service configs.
# Usage: ./backup.sh [--restore <backup-file>]
# ═══════════════════════════════════════════════════════════════════

MEDIA_DIR="$HOME/media"
CONFIG_DIR="$MEDIA_DIR/config"
BACKUP_DIR="$MEDIA_DIR/backups"
MAX_BACKUPS=10

info()  { printf "\n\033[1;34m=> %s\033[0m\n" "$*"; }
ok()    { printf "\033[1;32m   ✓ %s\033[0m\n" "$*"; }
warn()  { printf "\033[1;33m   ! %s\033[0m\n" "$*"; }
err()   { printf "\033[1;31m   ✗ %s\033[0m\n" "$*"; exit 1; }

# ─── Restore mode ────────────────────────────────────────────────
if [ "$1" = "--restore" ]; then
  BACKUP_FILE="$2"
  [ -z "$BACKUP_FILE" ] && err "Usage: ./backup.sh --restore <backup-file>"
  [ ! -f "$BACKUP_FILE" ] && err "Backup file not found: $BACKUP_FILE"

  info "Restoring from $BACKUP_FILE..."
  echo "  This will overwrite current configs in $CONFIG_DIR"
  echo ""
  read -r -p "  Continue? [y/N] " confirm
  [ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && { echo "  Aborted."; exit 0; }

  info "Stopping containers..."
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  docker compose -f "$SCRIPT_DIR/docker-compose.yml" down 2>/dev/null || true

  info "Extracting backup..."
  tar xzf "$BACKUP_FILE" -C "$MEDIA_DIR"
  ok "Configs restored"

  info "Starting containers..."
  docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d
  ok "All containers started"

  echo ""
  echo "  Restore complete. Run ./test.sh to verify."
  echo ""
  exit 0
fi

# ─── Backup mode (default) ──────────────────────────────────────
[ ! -d "$CONFIG_DIR" ] && err "Config directory not found: $CONFIG_DIR"
mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/media-server_${TIMESTAMP}.tar.gz"

info "Backing up service configs..."

# List what we're backing up
SERVICES=""
for dir in "$CONFIG_DIR"/*/; do
  [ -d "$dir" ] && SERVICES="$SERVICES $(basename "$dir")"
done
ok "Services:$SERVICES"

# Create tarball (relative to ~/media so restore is easy)
tar czf "$BACKUP_FILE" -C "$MEDIA_DIR" config/ 2>/dev/null
BACKUP_SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)
ok "Created: $BACKUP_FILE ($BACKUP_SIZE)"

# Prune old backups, keep MAX_BACKUPS most recent
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/media-server_*.tar.gz 2>/dev/null | wc -l | tr -d ' ')
if [ "$BACKUP_COUNT" -gt "$MAX_BACKUPS" ]; then
  REMOVE_COUNT=$((BACKUP_COUNT - MAX_BACKUPS))
  ls -1t "$BACKUP_DIR"/media-server_*.tar.gz | tail -n "$REMOVE_COUNT" | while read -r old; do
    rm -f "$old"
    ok "Pruned: $(basename "$old")"
  done
fi

echo ""
echo "  Backups in $BACKUP_DIR ($BACKUP_COUNT total, keeping last $MAX_BACKUPS)"
echo "  Restore with: ./backup.sh --restore $BACKUP_FILE"
echo ""
