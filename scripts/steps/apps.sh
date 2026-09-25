#!/usr/bin/env bash
# Landing-page API proxy.

write_api_proxy() {
  info "Generating API proxy config for landing page..."

  # Re-read Jellyseerr key (may have been created during Jellyseerr setup above)
  [ -z "$JELLYSEERR_KEY" ] && [ -f "$CONFIG_DIR/jellyseerr/settings.json" ] && \
    JELLYSEERR_KEY=$(jq -r '.main.apiKey // empty' "$CONFIG_DIR/jellyseerr/settings.json" 2>/dev/null)

  API_PROXY="$CONFIG_DIR/nginx/api-proxy.conf"
  write_api_proxy_from_template

  ok "api-proxy.conf written"

  # Reload nginx to pick up the new proxy config
  if docker exec media-nginx nginx -t >/dev/null 2>&1; then
    docker exec media-nginx nginx -s reload >/dev/null 2>&1 && \
      ok "nginx reloaded" || warn "Could not reload nginx (will apply on next restart)"
  else
    warn "nginx config test failed — skipping reload"
  fi
}
