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
  validate_required_config

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
  init_service_registry
}

configure_tailscale() {
  TS_IP=""
  TS_HOSTNAME=""
  if [ -z "$TS_CLI" ]; then
    return 0
  elif [ "$(cfg '.network.tailscale_https // true')" != "true" ]; then
    ok "Tailscale HTTPS disabled (network.tailscale_https = false)"
  elif ! "$TS_CLI" status &>/dev/null; then
    warn "Tailscale is not connected; open it from the menu bar to enable remote access"
  else
    TS_IP=$("$TS_CLI" ip -4 2>/dev/null || echo "")
    TS_HOSTNAME=$("$TS_CLI" status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//' || true)
    [ -n "$TS_IP" ] && ok "Tailscale connected ($TS_IP)"
    if [ -n "$TS_HOSTNAME" ]; then
      SERVE_STATUS=$("$TS_CLI" serve status 2>/dev/null || echo "")
      if echo "$SERVE_STATUS" | grep -q "https.*443" && echo "$SERVE_STATUS" | grep -q "https.*8096" && echo "$SERVE_STATUS" | grep -q "https.*5055"; then
        ok "Tailscale HTTPS already configured"
      else
        info "Configuring Tailscale HTTPS..."
        run_timeout 10 "$TS_CLI" serve --bg --yes --https=443 "http://127.0.0.1:$DASHBOARD_PORT" </dev/null 2>/dev/null && ok "HTTPS :443 → dashboard" || warn "Failed to configure HTTPS :443"
        run_timeout 10 "$TS_CLI" serve --bg --yes --https=8096 http://127.0.0.1:8096 </dev/null 2>/dev/null && ok "HTTPS :8096 → Jellyfin" || warn "Failed to configure HTTPS :8096"
        run_timeout 10 "$TS_CLI" serve --bg --yes --https=5055 http://127.0.0.1:5055 </dev/null 2>/dev/null && ok "HTTPS :5055 → Seerr" || warn "Failed to configure HTTPS :5055"
      fi
    fi
  fi
  return 0
}

create_directories() {
  info "Creating directory structure..."
  mkdir -p "$MOVIES_DIR" "$TV_DIR" "$ANIME_DIR"
  # Jellyfin doesn't watch an empty library folder for new files, so the
  # first show in an empty TV library would only appear at the next
  # scheduled scan; a hidden file keeps each folder non-empty
  touch "$MOVIES_DIR/.jellyfin-watch" "$TV_DIR/.jellyfin-watch" "$ANIME_DIR/.jellyfin-watch"
  # Per-category folders too: Sonarr/Radarr flag a download client whose
  # folder doesn't exist yet (qBittorrent only creates it on first download)
  mkdir -p "$DOWNLOADS_DIR"/{torrents,usenet}/incomplete \
    "$DOWNLOADS_DIR"/{torrents,usenet}/complete/{sonarr,radarr}
  mkdir -p "$BACKUP_DIR" "$LOG_DIR" "$STATE_DIR"
  mkdir -p "$CONFIG_DIR"/{jellyfin,sonarr,radarr,prowlarr,bazarr,sabnzbd,qbittorrent,seerr,unpackerr,byparr}
  mkdir -p "$CONFIG_DIR"/nginx/{www,temp}
  ok "$MEDIA_DIR directory tree ready"
}

# Where the admin UIs listen, as an address to connect to
admin_host() {
  if [ "${ADMIN_BIND:-0.0.0.0}" = "0.0.0.0" ]; then echo "127.0.0.1"; else echo "$ADMIN_BIND"; fi
}

# *arr config.xml: API key, port and bind address are set before first start
# so setup knows the key and the second Sonarr gets its own port. Existing
# files keep their key; port and bind address follow config.toml.
seed_arr_config() {
  local name="$1" port="$2" file="$CONFIG_DIR/$1/config.xml" bind="${ADMIN_BIND:-0.0.0.0}"
  [ "$bind" = "0.0.0.0" ] && bind="*"
  if [ ! -f "$file" ]; then
    cat > "$file" << XML
<Config>
  <BindAddress>$bind</BindAddress>
  <Port>$port</Port>
  <ApiKey>$(openssl rand -hex 16)</ApiKey>
  <LaunchBrowser>False</LaunchBrowser>
  <UpdateMechanism>External</UpdateMechanism>
  <AuthenticationMethod>Forms</AuthenticationMethod>
  <AuthenticationRequired>Enabled</AuthenticationRequired>
</Config>
XML
    chmod 600 "$file"
    ok "$name: config.xml (port $port)"
  else
    local before
    before=$(cat "$file")
    sed_inplace "s|<Port>[^<]*</Port>|<Port>$port</Port>|; s|<BindAddress>[^<]*</BindAddress>|<BindAddress>$bind</BindAddress>|" "$file"
    # A running service only reads config.xml at start
    [ "$(cat "$file")" != "$before" ] && CONFIG_CHANGED="$CONFIG_CHANGED$name"$'\n'
  fi
  return 0
}

# qBittorrent stores the Web UI password as PBKDF2-SHA512; writing it up
# front avoids the random first-run password that's only printed to the log.
# On macOS qBittorrent reads qBittorrent.ini (qBittorrent.conf on Linux).
# An existing file keeps its password; one without a password gets ours.
seed_qbittorrent_config() {
  local dir="$CONFIG_DIR/qbittorrent/qBittorrent/config" file bind="${ADMIN_BIND:-0.0.0.0}"
  file="$dir/qBittorrent.ini"
  [ -f "$file" ] && grep -q '^WebUI\\Password_PBKDF2=' "$file" && return 0
  mkdir -p "$dir"
  [ "$bind" = "0.0.0.0" ] && bind="*"
  python3 - "$file" "$(cfg '.qbittorrent.username')" "$(cfg '.qbittorrent.password')" "$bind" << 'PY'
import base64, hashlib, os, sys

path, user, password, bind = sys.argv[1:]
salt = os.urandom(16)
key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, 64)
pbkdf2 = f"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(key).decode()})"
prefs = {
    "WebUI\\Address": bind,
    "WebUI\\Port": "8081",
    "WebUI\\Username": user,
    "WebUI\\Password_PBKDF2": f'"{pbkdf2}"',
}

lines = open(path).read().splitlines() if os.path.exists(path) else []
if not any(l.strip() == "[LegalNotice]" for l in lines):
    lines = ["[LegalNotice]", "Accepted=true", ""] + lines
if not any(l.strip() == "[Preferences]" for l in lines):
    lines += ["", "[Preferences]"]
# Drop existing values for these keys, then add ours under [Preferences]
lines = [l for l in lines if l.split("=", 1)[0] not in prefs]
i = lines.index("[Preferences]") + 1
lines[i:i] = [f"{k}={v}" for k, v in prefs.items()]
with open(path, "w") as f:
    f.write("\n".join(lines) + "\n")
os.chmod(path, 0o600)
PY
  ok "qBittorrent: qBittorrent.ini"
}

seed_sabnzbd_config() {
  local file="$CONFIG_DIR/sabnzbd/sabnzbd.ini"
  [ -f "$file" ] && return 0
  cat > "$file" << SABEOF
__version__ = 19
__encoding__ = utf-8
[misc]
api_key = $(openssl rand -hex 16)
download_dir = $DOWNLOADS_DIR/usenet/incomplete
complete_dir = $DOWNLOADS_DIR/usenet/complete
SABEOF
  chmod 600 "$file"
  ok "SABnzbd: sabnzbd.ini (wizard skipped)"
}

write_nginx_config() {
  local conf="$CONFIG_DIR/nginx/nginx.conf" proxy="$CONFIG_DIR/nginx/api-proxy.conf"
  NGINX_MIME_TYPES=$(jq -r '.nginxMimeTypes' "$MEDIA_SERVICES_JSON")
  ADMIN_HOST=$(admin_host)
  export NGINX_MIME_TYPES DASHBOARD_PORT ADMIN_HOST
  render_template "$SCRIPT_DIR/templates/nginx.conf.tpl" "$conf"
  # Placeholder until the API keys are known (write_api_proxy)
  [ -f "$proxy" ] || : > "$proxy"
  cp "$SCRIPT_DIR/landing.html" "$CONFIG_DIR/nginx/www/index.html"
  cp "$SCRIPT_DIR/admin.html" "$CONFIG_DIR/nginx/www/admin.html"
  chmod 644 "$CONFIG_DIR/nginx/www/"*.html
}

write_service_configs() {
  info "Writing service configs..."
  CONFIG_CHANGED=""
  seed_arr_config sonarr 8989
  seed_arr_config radarr 7878
  seed_arr_config prowlarr 9696
  seed_qbittorrent_config
  seed_sabnzbd_config
  write_nginx_config
  ok "Service configs ready"
}

start_stack() {
  info "Starting services..."
  remove_retired_services
  local changed
  changed=$(write_launch_agents)
  # Also restart services whose config file changed (e.g. a new admin_bind)
  start_services "$changed"$'\n'"${CONFIG_CHANGED:-}"
  # Keep this version's Nix store paths from being garbage-collected while
  # the agents point at them
  nix-store --add-root "$STATE_DIR/gcroot" --realise "$MEDIA_SERVICES_JSON" >/dev/null 2>&1 || \
    warn "Could not register a Nix GC root; 'nix-collect-garbage' may remove the running services"
}

# Stop and remove launchd agents for services this version no longer runs
# (e.g. the separate anime Sonarr, merged into Sonarr). Their config folders
# are left in place.
remove_retired_services() {
  local plist name
  for plist in "$HOME/Library/LaunchAgents/$LABEL_PREFIX".*.plist; do
    [ -e "$plist" ] || continue
    name=$(basename "$plist" .plist)
    name="${name#"$LABEL_PREFIX".}"
    printf '%s\n' "$SERVICE_NAMES" | grep -qx "$name" && continue
    svc_stop "$name" && rm -f "$plist" && \
      ok "$name: no longer used, stopped and removed (its data stays in $CONFIG_DIR/$name)"
  done
  return 0
}

read_setup_config() {
  JELLYFIN_USER=$(cfg '.jellyfin.username')
  JELLYFIN_PASS=$(cfg '.jellyfin.password')
  QBIT_USER=$(cfg '.qbittorrent.username')
  QBIT_PASS=$(cfg '.qbittorrent.password')
  DL_COMPLETE="$DOWNLOADS_DIR/torrents/complete"
  DL_INCOMPLETE="$DOWNLOADS_DIR/torrents/incomplete"
  SEED_RATIO=$(cfg '.downloads.seeding_ratio')
  SEED_TIME=$(cfg '.downloads.seeding_time_minutes')
  SUBTITLE_LANGS=$(cfg '[.subtitles.languages[]] | join(",")')
  SUBTITLE_PROVIDERS=$(cfg '[.subtitles.providers[]] | join(",")')
  SONARR_PROFILE=$(cfg '.quality.sonarr_profile')
  SONARR_ANIME_PROFILE=$(cfg '.quality.sonarr_anime_profile')
  RADARR_PROFILE=$(cfg '.quality.radarr_profile')
}

wait_for_services() {
  info "Waiting for all services (the first start downloads Byparr's browser)..."
  local svc_name svc_url
  while IFS='|' read -r svc_name svc_url; do
    [ -z "$svc_name" ] && continue
    if [ "$svc_name" = "Byparr" ]; then
      WAIT_MAX=600 wait_for "$svc_name" "$svc_url"
    else
      wait_for "$svc_name" "$svc_url"
    fi
  done <<< "$SERVICE_HEALTH_ENDPOINTS"
}

# Read service API keys from their config files (no output; used by --test too)
read_api_keys() {
  SONARR_KEY=$(get_api_key "sonarr")
  RADARR_KEY=$(get_api_key "radarr")
  PROWLARR_KEY=$(get_api_key "prowlarr")
  SABNZBD_KEY=""
  [ -f "$CONFIG_DIR/sabnzbd/sabnzbd.ini" ] && SABNZBD_KEY=$(sed -n 's/^api_key = *//p' "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null || echo "")
  SEERR_KEY=""
  [ -f "$CONFIG_DIR/seerr/settings.json" ] && SEERR_KEY=$(jq -r '.main.apiKey // empty' "$CONFIG_DIR/seerr/settings.json" 2>/dev/null)
  return 0
}

load_api_keys() {
  info "Reading API keys..."
  read_api_keys
  [ -n "$SONARR_KEY" ]       && ok "Sonarr:       $(mask "$SONARR_KEY")"       || err "Sonarr key not found"
  [ -n "$RADARR_KEY" ]       && ok "Radarr:       $(mask "$RADARR_KEY")"       || err "Radarr key not found"
  [ -n "$PROWLARR_KEY" ]     && ok "Prowlarr:     $(mask "$PROWLARR_KEY")"     || err "Prowlarr key not found"
  [ -n "$SABNZBD_KEY" ]      && ok "SABnzbd:      $(mask "$SABNZBD_KEY")"      || warn "SABnzbd key not found"
  [ -n "$SEERR_KEY" ]        && ok "Seerr:        $(mask "$SEERR_KEY")"        || warn "Seerr key not found (will read after setup)"
  ok "qBittorrent:  user $QBIT_USER"
}
