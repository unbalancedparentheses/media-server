#!/usr/bin/env bash

render_template() {
  local template_file="$1" out_file="$2"
  python3 - "$template_file" "$out_file" << 'PY'
import os
import re
import sys

src, dst = sys.argv[1], sys.argv[2]
with open(src, "r", encoding="utf-8") as f:
    content = f.read()

def repl(match):
    key = match.group(1)
    return os.environ.get(key, "")

content = re.sub(r"\{\{([A-Za-z0-9_]+)\}\}", repl, content)
with open(dst, "w", encoding="utf-8") as f:
    f.write(content)
PY
}

write_recyclarr_config_from_template() {
  export SONARR_INTERNAL SONARR_KEY SONARR_PROFILE SONARR_ANIME_INTERNAL ANIME_KEY SONARR_ANIME_PROFILE RADARR_INTERNAL RADARR_KEY RADARR_PROFILE
  render_template "$SCRIPT_DIR/templates/recyclarr.yml.tpl" "$RECYCLARR_CONFIG"
}

write_janitorr_config_from_template() {
  export SONARR_KEY RADARR_KEY JELLYFIN_API_KEY JELLYFIN_USER JELLYFIN_PASS JELLYSEERR_KEY
  render_template "$SCRIPT_DIR/templates/janitorr.application.yml.tpl" "$JANITORR_CONFIG"
}

write_homepage_services_from_template() {
  local bazarr_widget_key=""
  if [ -f "$CONFIG_DIR/bazarr/config/config/config.yaml" ]; then
    bazarr_widget_key=$(sed -n 's/^apikey: *//p' "$CONFIG_DIR/bazarr/config/config/config.yaml" 2>/dev/null | head -1)
  fi

  export JELLYFIN_TOKEN JELLYSEERR_KEY SONARR_KEY SONARR_ANIME_KEY RADARR_KEY LIDARR_KEY PROWLARR_KEY
  export QBIT_USER QBIT_PASS SABNZBD_KEY
  export BAZARR_WIDGET_KEY="$bazarr_widget_key"
  render_template "$SCRIPT_DIR/templates/homepage.services.yaml.tpl" "$HP_DIR/services.yaml"
}

write_api_proxy_from_template() {
  export SONARR_KEY SONARR_ANIME_KEY RADARR_KEY JELLYFIN_API_KEY JELLYSEERR_KEY SABNZBD_KEY
  render_template "$SCRIPT_DIR/templates/nginx.api-proxy.conf.tpl" "$API_PROXY"
}
