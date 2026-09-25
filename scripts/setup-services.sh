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
    val = os.environ.get(key, "")
    # Escape backslashes and double-quotes for safe embedding in YAML/nginx strings
    return val.replace("\\", "\\\\").replace('"', '\\"')

content = re.sub(r"\{\{([A-Za-z0-9_]+)\}\}", repl, content)
with open(dst, "w", encoding="utf-8") as f:
    f.write(content)
PY
}


write_api_proxy_from_template() {
  export SONARR_KEY SONARR_ANIME_KEY RADARR_KEY JELLYFIN_API_KEY JELLYSEERR_KEY SABNZBD_KEY
  render_template "$SCRIPT_DIR/templates/nginx.api-proxy.conf.tpl" "$API_PROXY"
}
