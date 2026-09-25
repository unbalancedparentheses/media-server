#!/usr/bin/env bash
set -eo pipefail

# media-server installer (macOS)
# Usage: bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)

REPO="https://github.com/unbalancedparentheses/media-server.git"
DEST="$HOME/media-server"

info() { printf "\n\033[1;34m=> %s\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m   ✓ %s\033[0m\n" "$*"; }
err()  { printf "\033[1;31m   ✗ %s\033[0m\n" "$*"; exit 1; }

[ "$(uname -s)" = "Darwin" ] || err "media-server runs on macOS"
if ! command -v nix >/dev/null 2>&1; then
  [ -x /nix/var/nix/profiles/default/bin/nix ] && export PATH="/nix/var/nix/profiles/default/bin:$PATH"
fi
command -v nix >/dev/null 2>&1 || err "Nix is required. Install it with: curl -fsSL https://install.determinate.systems/nix | sh -s -- install"
command -v git >/dev/null 2>&1 || err "git is required (xcode-select --install)"

if [ -d "$DEST/.git" ]; then
  info "Updating existing checkout..."
  git -C "$DEST" pull --ff-only
  ok "Updated $DEST"
else
  info "Cloning media-server..."
  git clone "$REPO" "$DEST"
  ok "Cloned to $DEST"
fi

info "Running setup (asks for your passwords)..."
cd "$DEST"
exec nix run .#install
