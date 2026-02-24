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
else
  ok "config.toml already exists"
fi

info "Running setup in non-interactive mode..."
./setup.sh --yes
