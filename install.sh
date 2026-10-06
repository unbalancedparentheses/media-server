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
# Flakes may not be enabled (e.g. Nix from the official installer)
NIX=(nix --extra-experimental-features "nix-command flakes")

# /usr/bin/git is only a stub until Apple's command-line tools are
# installed; without them, use git from Nix
git() {
  if xcode-select -p >/dev/null 2>&1; then
    command git "$@"
  else
    "${NIX[@]}" shell nixpkgs#git -c git "$@"
  fi
}

if [ -d "$DEST/.git" ]; then
  info "Updating existing checkout..."
  git -C "$DEST" pull --ff-only
  ok "Updated $DEST"
else
  info "Cloning media-server..."
  git clone "$REPO" "$DEST"
  ok "Cloned to $DEST"
fi

# Prebuilt services: when the repo names a binary cache (nix-cache.conf,
# filled by CI), Nix is told about it once, so the first install downloads
# what it would otherwise build (Seerr, SABnzbd) instead of taking half an hour
if [ -f "$DEST/nix-cache.conf" ]; then
  custom=/etc/nix/nix.custom.conf
  [ -f "$custom" ] || custom=/etc/nix/nix.conf
  substituter=$(sed -n 's/^extra-substituters *= *//p' "$DEST/nix-cache.conf")
  if [ -n "$substituter" ] && ! grep -qF "$substituter" "$custom" 2>/dev/null; then
    info "Adding the prebuilt services cache to Nix (asks for your Mac password)..."
    if sudo sh -c "cat '$DEST/nix-cache.conf' >> '$custom'"; then
      sudo launchctl kickstart -k system/systems.determinate.nix-daemon 2>/dev/null \
        || sudo launchctl kickstart -k system/org.nixos.nix-daemon 2>/dev/null || true
      ok "Prebuilt services from $substituter"
    fi
  fi
fi

info "Running setup (asks for your passwords)..."
cd "$DEST"
exec "${NIX[@]}" run .#install
