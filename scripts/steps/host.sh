#!/usr/bin/env bash
# Host preparation: config.toml, Tailscale, directories, service config files
# written before first start, and the launchd agents.

check_platform() {
  info "Checking prerequisites..."
  [ "$(uname -s)" = "Darwin" ] || err "This setup runs services as launchd agents and supports macOS only"
  [ -n "${MEDIA_SERVICES_JSON:-}" ] && [ -f "$MEDIA_SERVICES_JSON" ] || \
    err "Run through Nix: 'nix run .#install' (or ./setup.sh, which does that for you)"
  ok "macOS $(sw_vers -productVersion 2>/dev/null || true), services from Nix"
  TS_CLI="$(detect_tailscale_cli)"
  [ -n "$TS_CLI" ] && ok "Tailscale" || warn "Tailscale not installed (remote access step will be skipped)"
}

ensure_config() {
  mkdir -p "$MEDIA_DIR"
  if [ ! -f "$CONFIG_FILE" ]; then
    cp "$SCRIPT_DIR/config.toml.example" "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
    if [ "$NON_INTERACTIVE" = "true" ]; then
      write_secure_defaults_to_config "$CONFIG_FILE"
      warn "$CONFIG_FILE not found — created with secure generated defaults"
    else
      warn "$CONFIG_FILE not found — created from config.toml.example"
      prompt_credentials "$CONFIG_FILE"
    fi
  fi
  CONFIG_JSON=$(load_config_json "$CONFIG_FILE")

  # Prompt for credentials if still using defaults (interactive mode only)
  local jf_pass_check qbit_pass_check
  jf_pass_check=$(cfg '.jellyfin.password // ""')
  qbit_pass_check=$(cfg '.qbittorrent.password // ""')
  if [ "$jf_pass_check" = "changeme" ] || [ "$qbit_pass_check" = "changeme" ]; then
    if [ "$NON_INTERACTIVE" = "true" ]; then
      write_secure_defaults_to_config "$CONFIG_FILE"
    else
      prompt_credentials "$CONFIG_FILE"
    fi
    CONFIG_JSON=$(load_config_json "$CONFIG_FILE")
  fi

  validate_config_semantics
  load_runtime_settings
}

# Settings every mode needs (also used by --test and --status)
load_runtime_settings() {
  TZ_VALUE=$(cfg "$TIMEZONE_PATH // \"\"")
  ADMIN_BIND=$(cfg '.network.admin_bind // "0.0.0.0"')
  DASHBOARD_PORT=$(cfg '.network.dashboard_port // 80')
  DISK_WARN_GB=$(cfg '.disk.warn_free_gb // 50')
  DISK_MIN_GB=$(cfg '.disk.min_free_gb // 10')
  init_service_registry
}

configure_tailscale() {
  TS_IP=""
  TS_HOSTNAME=""
  if [ -z "$TS_CLI" ]; then
    return 0
  elif [ "$(cfg_bool .network.tailscale_https true)" != "true" ]; then
    # Take down what an earlier run published
    if remove_tailscale_serve; then
      ok "Tailscale HTTPS disabled (network.tailscale_https = false)"
    else
      warn "Tailscale HTTPS is disabled in config.toml, but some routes are still published (see above)"
    fi
  elif ! "$TS_CLI" status &>/dev/null; then
    warn "Tailscale is not connected; open it from the menu bar to enable remote access"
  else
    TS_IP=$("$TS_CLI" ip -4 2>/dev/null || echo "")
    TS_HOSTNAME=$("$TS_CLI" status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//' || true)
    [ -n "$TS_IP" ] && ok "Tailscale connected ($TS_IP)"
    if [ -n "$TS_HOSTNAME" ]; then
      local serve port target label
      serve=$("$TS_CLI" serve status --json 2>/dev/null || echo "{}")
      for route in "443|http://127.0.0.1:$DASHBOARD_PORT|dashboard" "8096|http://127.0.0.1:8096|Jellyfin" "5055|http://127.0.0.1:5055|Seerr"; do
        IFS='|' read -r port target label <<< "$route"
        # Already published to the same place (not just the same port)?
        if jq -e --arg p ":$port" --arg t "$target" \
            'any(.Web // {} | to_entries[]; (.key | endswith($p)) and .value.Handlers["/"].Proxy == $t)' <<< "$serve" >/dev/null 2>&1; then
          record_tailscale_route "$port" "$target"
          ok "HTTPS :$port → $label"
        elif run_timeout 10 "$TS_CLI" serve --bg --yes --https="$port" "$target" </dev/null >/dev/null 2>&1; then
          record_tailscale_route "$port" "$target"
          ok "HTTPS :$port → $label (published)"
        else
          warn "Failed to publish HTTPS :$port"
        fi
      done
    fi
  fi
  return 0
}

