#!/usr/bin/env bash
set -eo pipefail

# media-server installer
# Usage: bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)

REPO="https://github.com/unbalancedparentheses/media-server.git"
DEST="$HOME/media-server"

info() { printf "\n\033[1;34m=> %s\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m   ✓ %s\033[0m\n" "$*"; }
err()  { printf "\033[1;31m   ✗ %s\033[0m\n" "$*"; exit 1; }

if [ -d "$DEST/.git" ]; then
  info "Updating existing install..."
  git -C "$DEST" pull --ff-only
  ok "Updated $DEST"
else
  info "Cloning media-server..."
  git clone "$REPO" "$DEST"
  ok "Cloned to $DEST"
fi

cd "$DEST"

if [ ! -f config.toml ]; then
  cp config.toml.example config.toml
  ok "Created config.toml from example"
  echo ""
  echo "  Edit ~/media-server/config.toml with your credentials, then run:"
  echo ""
  echo "    cd ~/media-server && ./setup.sh"
  echo ""
else
  ok "config.toml already exists"
  echo ""
  echo "  Ready to run:"
  echo ""
  echo "    cd ~/media-server && ./setup.sh"
  echo ""
fi
