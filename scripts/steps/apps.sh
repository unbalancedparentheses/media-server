#!/usr/bin/env bash
# Dashboard API proxy.

write_api_proxy() {
  info "Generating API proxy config for the dashboard..."

  # Re-read the Seerr key (created during Seerr setup above)
  [ -z "$SEERR_KEY" ] && [ -f "$CONFIG_DIR/seerr/settings.json" ] && \
    SEERR_KEY=$(jq -r '.main.apiKey // empty' "$CONFIG_DIR/seerr/settings.json" 2>/dev/null)

  API_PROXY="$CONFIG_DIR/nginx/api-proxy.conf"
  ADMIN_HOST=$(admin_host)
  write_api_proxy_from_template
  chmod 600 "$API_PROXY"
  ok "api-proxy.conf written"

  svc_restart nginx && ok "nginx reloaded" || warn "Could not restart nginx"
}
