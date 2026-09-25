#!/usr/bin/env bash
# Host preparation: prerequisites, config.toml, Tailscale, directories,
# compose env files, container start and /etc/hosts.

install_prerequisites() {
  info "Checking prerequisites..."

  OS_NAME="$(uname -s)"
  if [ "$OS_NAME" = "Darwin" ]; then
    # Homebrew (macOS package manager)
    if ! has_cmd brew; then
      info "Installing Homebrew..."
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
      ok "Homebrew installed"
    else
      ok "Homebrew"
    fi
  fi

  # Docker
  if ! has_cmd docker; then
    if [ "$OS_NAME" = "Darwin" ]; then
      info "Installing Docker Desktop..."
      brew install --cask docker
      ok "Docker Desktop installed — please open it from Applications and wait for it to start"
      echo ""
      echo "  After Docker Desktop is running, re-run this script."
      echo ""
      exit 0
    elif [ "$OS_NAME" = "Linux" ]; then
      info "Installing Docker Engine (Linux)..."
      require_cmd curl
      curl -fsSL https://get.docker.com | as_root sh
      if has_cmd systemctl; then
        as_root systemctl enable --now docker >/dev/null 2>&1 || true
      fi
      ok "Docker installed"
    else
      err "Unsupported OS for automatic Docker install: $OS_NAME"
    fi
  fi
  if ! docker info >/dev/null 2>&1; then
    if [ "$OS_NAME" = "Linux" ] && docker info 2>&1 | grep -qi "permission denied"; then
      warn "Docker permission denied for user '$USER'."
      warn "Run: sudo usermod -aG docker $USER"
      warn "Then log out/in and re-run setup."
    fi
    err "Docker is installed but not usable. Start Docker (or fix permissions) and re-run this script."
  fi
  ok "Docker"
  require_docker_compose
  ok "Docker Compose"

  if [ "$OS_NAME" = "Linux" ] && has_cmd getent && getent group docker >/dev/null 2>&1; then
    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
      as_root usermod -aG docker "$USER" >/dev/null 2>&1 || true
      warn "Added $USER to docker group (or attempted to). You may need to log out/in."
    fi
  fi

  # python3
  if ! has_cmd python3; then
    err "python3 is required. Install Xcode Command Line Tools or Homebrew Python."
  else
    ok "python3"
  fi

  # jq
  if ! has_cmd jq; then
    info "Installing jq..."
    install_package jq || err "Could not install jq automatically. Please install jq and re-run."
    ok "jq installed"
  else
    ok "jq"
  fi

  # yq is optional (python3 TOML parser is preferred fallback)
  if has_cmd yq; then
    ok "yq"
  else
    warn "yq not found (using python TOML parser fallback)"
  fi

  # Tailscale (remote access)
  TS_CLI="$(detect_tailscale_cli)"
  if [ -n "$TS_CLI" ]; then
    ok "Tailscale"
  elif [ "$OS_NAME" = "Darwin" ] && has_cmd brew; then
    info "Installing Tailscale..."
    brew install --cask tailscale
    TS_CLI="$(detect_tailscale_cli)"
    [ -n "$TS_CLI" ] && ok "Tailscale installed" || warn "Could not detect tailscale CLI after install"
  else
    warn "Tailscale not installed (remote access step will be skipped)"
  fi
}

ensure_config() {
  # config.toml
  if [ ! -f "$CONFIG_FILE" ]; then
    cp "$SCRIPT_DIR/config.toml.example" "$CONFIG_FILE"
    if [ "$NON_INTERACTIVE" = "true" ]; then
      write_secure_defaults_to_config "$CONFIG_FILE"
      warn "config.toml not found — created with secure generated defaults"
    else
      warn "config.toml not found — created from config.toml.example"
      prompt_credentials "$CONFIG_FILE"
    fi
  fi
  CONFIG_JSON=$(load_config_json "$CONFIG_FILE")
  validate_required_config

  # Prompt for credentials if still using defaults (interactive mode only)
  needs_credentials=false
  jf_pass_check=$(cfg '.jellyfin.password // ""')
  qbit_pass_check=$(cfg '.qbittorrent.password // ""')
  if [ "$jf_pass_check" = "changeme" ] || [ "$qbit_pass_check" = "changeme" ]; then
    needs_credentials=true
  fi
  if [ "$needs_credentials" = "true" ]; then
    if [ "$NON_INTERACTIVE" = "true" ]; then
      write_secure_defaults_to_config "$CONFIG_FILE"
    else
      prompt_credentials "$CONFIG_FILE"
    fi
    CONFIG_JSON=$(load_config_json "$CONFIG_FILE")
  fi

  validate_config_semantics
}

configure_tailscale() {
  # Check Tailscale connection
  TS_IP=""
  TS_HOSTNAME=""
  if [ -z "$TS_CLI" ]; then
    warn "Skipping Tailscale setup (CLI not found)"
  elif ! "$TS_CLI" status &>/dev/null; then
    warn "Tailscale is not connected"
    echo "  Open Tailscale from the menu bar and sign in to enable remote access."
    echo "  You can skip this — local access will still work."
    if [ "$NON_INTERACTIVE" != "true" ]; then
      echo ""
      read -r -p "  Press Enter to continue..."
    fi
  else
    TS_IP=$("$TS_CLI" ip -4 2>/dev/null || echo "")
    TS_HOSTNAME=$("$TS_CLI" status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//')
    if [ -n "$TS_IP" ]; then
      ok "Tailscale connected ($TS_IP)"
    fi
    if [ -n "$TS_HOSTNAME" ]; then
      SERVE_STATUS=$("$TS_CLI" serve status 2>/dev/null || echo "")
      if echo "$SERVE_STATUS" | grep -q "https.*443" && echo "$SERVE_STATUS" | grep -q "https.*8096" && echo "$SERVE_STATUS" | grep -q "https.*5055"; then
        ok "Tailscale HTTPS already configured"
      else
        info "Configuring Tailscale HTTPS..."
        run_timeout 10 "$TS_CLI" serve --bg --yes --https=443 http://127.0.0.1:80 </dev/null 2>/dev/null && ok "HTTPS :443 → Nginx" || warn "Failed to configure HTTPS :443"
        run_timeout 10 "$TS_CLI" serve --bg --yes --https=8096 http://127.0.0.1:8096 </dev/null 2>/dev/null && ok "HTTPS :8096 → Jellyfin" || warn "Failed to configure HTTPS :8096"
        run_timeout 10 "$TS_CLI" serve --bg --yes --https=5055 http://127.0.0.1:5055 </dev/null 2>/dev/null && ok "HTTPS :5055 → Jellyseerr" || warn "Failed to configure HTTPS :5055"
      fi
    fi
  fi
}

create_directories() {
  info "Creating directory structure..."

  mkdir -p "$MEDIA_DIR"/{movies,tv,anime,music,photos}
  mkdir -p "$MEDIA_DIR"/downloads/torrents/{complete,incomplete}
  mkdir -p "$MEDIA_DIR"/downloads/usenet/{complete,incomplete}
  mkdir -p "$MEDIA_DIR"/backups
  mkdir -p "$MEDIA_DIR"/{transcode_cache,leaving-soon}
  mkdir -p "$MEDIA_DIR"/config/{jellyfin,sonarr,sonarr-anime,radarr,prowlarr,bazarr,sabnzbd,qbittorrent,jellyseerr,recyclarr,flaresolverr,nginx,lidarr,navidrome,unpackerr,gluetun,janitorr,beszel,immich-ml,scrutiny,uptime-kuma}/logs
  mkdir -p "$MEDIA_DIR"/config/tdarr/{server,configs,logs}
  mkdir -p "$MEDIA_DIR"/config/immich-postgres

  # Ensure api-proxy.conf exists as a file (Docker would create it as a directory)
  [ -f "$CONFIG_DIR/nginx/api-proxy.conf" ] || touch "$CONFIG_DIR/nginx/api-proxy.conf"

  ok "$MEDIA_DIR directory tree ready"

  # Pre-seed SABnzbd config to skip the first-run wizard
  if [ ! -f "$CONFIG_DIR/sabnzbd/sabnzbd.ini" ]; then
    SAB_GEN_KEY=$(openssl rand -hex 16)
    cat > "$CONFIG_DIR/sabnzbd/sabnzbd.ini" << SABEOF
__version__ = 19
__encoding__ = utf-8
[misc]
api_key = $SAB_GEN_KEY
download_dir = /downloads/usenet/incomplete
complete_dir = /downloads/usenet/complete
host_whitelist = sabnzbd
SABEOF
    ok "SABnzbd: pre-seeded config (wizard skipped)"
  fi
}

write_compose_env() {
  info "Generating .env for Docker Compose..."

  TZ_VALUE=$(cfg "$TIMEZONE_PATH // \"America/New_York\"")

  # Generate a stable Immich DB password (reuse existing if present)
  if [ -f "$SCRIPT_DIR/.env" ] && grep -q "^IMMICH_DB_PASSWORD=" "$SCRIPT_DIR/.env" 2>/dev/null; then
    IMMICH_DB_PASS=$(sed -n 's/^IMMICH_DB_PASSWORD=//p' "$SCRIPT_DIR/.env" | tr -d '"')
  else
    IMMICH_DB_PASS=$(openssl rand -hex 16)
  fi

  # VPN settings (optional)
  VPN_ENABLE=$(cfg '.vpn.enable // false')
  VPN_PROVIDER=$(cfg '.vpn.provider // "mullvad"')
  VPN_TYPE=$(cfg '.vpn.type // "wireguard"')
  VPN_WG_KEY=$(cfg '.vpn.wireguard_private_key // ""')
  VPN_WG_ADDR=$(cfg '.vpn.wireguard_addresses // ""')
  VPN_COUNTRIES=$(cfg '.vpn.server_countries // ""')

  COMPOSE_PROFILES_VALUE=""
  [ "$VPN_ENABLE" = "true" ] && COMPOSE_PROFILES_VALUE="vpn"

  ADMIN_BIND=$(cfg '.network.admin_bind // "0.0.0.0"')
  DOCKER_SUBNET=$(cfg '.network.docker_subnet // "172.29.84.0/24"')
  # nginx gets .10; other containers get addresses from the upper half
  read -r NGINX_IP DOCKER_IP_RANGE < <(python3 -c '
import ipaddress, sys
net = ipaddress.ip_network(sys.argv[1])
assert net.version == 4 and net.prefixlen <= 26
print(net[10], list(net.subnets(prefixlen_diff=1))[1], sep="\t")' "$DOCKER_SUBNET" 2>/dev/null) || true
  [ -n "${DOCKER_IP_RANGE:-}" ] || err "network.docker_subnet must be an IPv4 subnet of /26 or larger: $DOCKER_SUBNET"

  cat > "$SCRIPT_DIR/.env" << EOF
PUID=$(id -u)
PGID=$(id -g)
TZ="$TZ_VALUE"
IMMICH_DB_PASSWORD="$IMMICH_DB_PASS"
VPN_SERVICE_PROVIDER="$VPN_PROVIDER"
VPN_TYPE="$VPN_TYPE"
WIREGUARD_PRIVATE_KEY="$VPN_WG_KEY"
WIREGUARD_ADDRESSES="$VPN_WG_ADDR"
VPN_SERVER_COUNTRIES="$VPN_COUNTRIES"
BESZEL_AGENT_KEY="$(cfg '.beszel.agent_key // ""')"
ADMIN_BIND="$ADMIN_BIND"
DOCKER_SUBNET="$DOCKER_SUBNET"
NGINX_IP="$NGINX_IP"
DOCKER_IP_RANGE="$DOCKER_IP_RANGE"
COMPOSE_PROFILES=$COMPOSE_PROFILES_VALUE
EOF
  chmod 600 "$SCRIPT_DIR/.env"
  ok ".env (PUID=$(id -u), PGID=$(id -g), TZ=$TZ_VALUE, admin ports on $ADMIN_BIND)"

  # Generate docker-compose.override.yml for qBittorrent VPN routing
  info "Generating docker-compose.override.yml..."
  if [ "$VPN_ENABLE" = "true" ]; then
    # qBittorrent shares gluetun's network namespace, so it isn't on the
    # compose network itself; the alias keeps http://qbittorrent:8081 working
    # for the *arr apps, Prowlarr and nginx.
    OVERRIDE_CONTENT='services:
  gluetun:
    ports:
      - "${ADMIN_BIND:-0.0.0.0}:8081:8081"
    networks:
      default:
        aliases:
          - qbittorrent
  qbittorrent:
    network_mode: "service:gluetun"
    depends_on:
      gluetun:
        condition: service_healthy'
    OVERRIDE_MSG="VPN enabled: qBittorrent routed through gluetun"
  else
    OVERRIDE_CONTENT='services:
  qbittorrent:
    ports:
      - "${ADMIN_BIND:-0.0.0.0}:8081:8081"
      - "6881:6881"
      - "6881:6881/udp"'
    OVERRIDE_MSG="VPN disabled: qBittorrent ports exposed directly"
  fi
  # Only write if content changed (avoids unnecessary container recreation)
  if [ ! -f "$OVERRIDE_FILE" ] || [ "$(cat "$OVERRIDE_FILE")" != "$OVERRIDE_CONTENT" ]; then
    printf '%s\n' "$OVERRIDE_CONTENT" > "$OVERRIDE_FILE"
  fi
  ok "$OVERRIDE_MSG"

  write_htpasswd "$CONFIG_DIR/nginx/htpasswd" "$(cfg '.jellyfin.username')" "$(cfg '.jellyfin.password')"
}

# nginx basic auth for the UIs without a login (Tdarr, Dozzle, Scrutiny).
# Rewritten only when the credentials change, reusing the existing salt.
write_htpasswd() {
  local file="$1" user="$2" pass="$3" salt="" line
  if [ -f "$file" ]; then
    salt=$(sed -n "s/^[^:]*:\$apr1\$\([^\$]*\)\$.*/\1/p" "$file" | head -1)
  fi
  [ -n "$salt" ] || salt=$(openssl rand -hex 4)
  line="$user:$(printf '%s\n' "$pass" | openssl passwd -apr1 -salt "$salt" -stdin)"
  if [ ! -f "$file" ] || [ "$(cat "$file")" != "$line" ]; then
    printf '%s\n' "$line" > "$file"
    chmod 644 "$file"
    docker exec media-nginx nginx -s reload >/dev/null 2>&1 || true
    ok "nginx basic auth: $user (Tdarr, Dozzle, Scrutiny)"
  fi
}

# The compose network now has a fixed subnet (nginx needs a fixed IP).
# A network created by an older version without it has to be recreated.
ensure_compose_network() {
  local net current
  net=$(dc config --format json 2>/dev/null | jq -r '.networks.default.name // empty' 2>/dev/null || true)
  [ -n "$net" ] || return 0
  current=$(docker network inspect "$net" --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null || true)
  if [ -n "$current" ] && [ "$current" != "$DOCKER_SUBNET" ]; then
    warn "Docker network $net uses $current; recreating it with $DOCKER_SUBNET"
    dc down --remove-orphans
    if docker network inspect "$net" >/dev/null 2>&1 && ! docker network rm "$net" >/dev/null 2>&1; then
      err "Could not remove network $net; disconnect whatever still uses it ('docker network inspect $net') and re-run setup"
    fi
    ok "Network recreated on next start"
  fi
  return 0
}

start_stack() {
  info "Starting containers..."
  smoke_check_generated_files
  ensure_compose_network
  # Before Immich v3 starts, so it never sees an unmigrated database
  migrate_immich_vectors

  dc up -d

  ok "All containers started"
}

update_hosts_file() {
  info "Checking /etc/hosts..."

  DOMAINS="media.local jellyfin.media.local jellyseerr.media.local sonarr.media.local sonarr-anime.media.local radarr.media.local prowlarr.media.local bazarr.media.local sabnzbd.media.local qbittorrent.media.local lidarr.media.local navidrome.media.local immich.media.local tdarr.media.local dozzle.media.local beszel.media.local scrutiny.media.local uptime-kuma.media.local"

  if grep -qE "^[[:space:]]*127\.0\.0\.1[[:space:]].*\bmedia\.local\b" /etc/hosts 2>/dev/null; then
    ok "Hosts entries already present"
  else
    echo ""
    echo "  Adding .media.local domains to /etc/hosts (requires sudo)..."
    echo ""
    if sudo -n true 2>/dev/null; then
      sudo bash -c "echo '' >> /etc/hosts && echo '# Media Server' >> /etc/hosts && echo '127.0.0.1 $DOMAINS' >> /etc/hosts"
      ok "Hosts entries added"
    elif [ "$NON_INTERACTIVE" = "true" ]; then
      warn "Could not update /etc/hosts in non-interactive mode (no passwordless sudo)."
      echo "    echo '127.0.0.1 $DOMAINS' | sudo tee -a /etc/hosts"
    elif sudo bash -c "echo '' >> /etc/hosts && echo '# Media Server' >> /etc/hosts && echo '127.0.0.1 $DOMAINS' >> /etc/hosts"; then
      ok "Hosts entries added"
    else
      warn "Could not update /etc/hosts (no sudo). Run manually:"
      echo "    echo '127.0.0.1 $DOMAINS' | sudo tee -a /etc/hosts"
    fi
  fi
}

read_setup_config() {
  info "Reading config..."
  init_service_registry

  JELLYFIN_USER=$(cfg '.jellyfin.username')
  JELLYFIN_PASS=$(cfg '.jellyfin.password')
  QBIT_USER=$(cfg '.qbittorrent.username')
  QBIT_CONFIGURED_PASS=$(cfg '.qbittorrent.password')
  DL_COMPLETE=$(cfg '.downloads.complete')
  DL_INCOMPLETE=$(cfg '.downloads.incomplete')
  SEED_RATIO=$(cfg '.downloads.seeding_ratio')
  SEED_TIME=$(cfg '.downloads.seeding_time_minutes')
  SUBTITLE_LANGS=$(cfg '[.subtitles.languages[]] | join(",")')
  SUBTITLE_PROVIDERS=$(cfg '[.subtitles.providers[]] | join(",")')
  SONARR_PROFILE=$(normalize_profile_name "$(cfg '.quality.sonarr_profile')")
  SONARR_ANIME_PROFILE=$(normalize_profile_name "$(cfg '.quality.sonarr_anime_profile')")
  RADARR_PROFILE=$(normalize_profile_name "$(cfg '.quality.radarr_profile')")
  SONARR_PROFILE_ID=$(trash_profile_id sonarr "$SONARR_PROFILE")
  SONARR_ANIME_PROFILE_ID=$(trash_profile_id sonarr-anime "$SONARR_ANIME_PROFILE")
  RADARR_PROFILE_ID=$(trash_profile_id radarr "$RADARR_PROFILE")
}

wait_for_services() {
  info "Waiting for all services..."
  while IFS='|' read -r svc_name svc_url svc_auth; do
    [ -z "$svc_name" ] && continue
    wait_for "$svc_name" "$svc_url" "$svc_auth"
  done <<< "$SERVICE_HEALTH_ENDPOINTS"
}

# Read service API keys from their config files (no output; used by --test too)
read_api_keys() {
  SONARR_KEY=$(get_api_key "sonarr")
  SONARR_ANIME_KEY=$(get_api_key "sonarr-anime")
  RADARR_KEY=$(get_api_key "radarr")
  LIDARR_KEY=$(get_api_key "lidarr")
  PROWLARR_KEY=$(get_api_key "prowlarr")
  SABNZBD_KEY=""
  [ -f "$CONFIG_DIR/sabnzbd/sabnzbd.ini" ] && SABNZBD_KEY=$(sed -n 's/^api_key = *//p' "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null || echo "")
  JELLYSEERR_KEY=""
  [ -f "$CONFIG_DIR/jellyseerr/settings.json" ] && JELLYSEERR_KEY=$(jq -r '.main.apiKey // empty' "$CONFIG_DIR/jellyseerr/settings.json" 2>/dev/null)
  return 0
}

load_api_keys() {
  info "Reading API keys..."
  read_api_keys

  # Ensure Docker hostname is in the SABnzbd whitelist (prevents 403 from Sonarr/Radarr)
  if [ -n "$SABNZBD_KEY" ] && ! grep -q "^host_whitelist.*sabnzbd" "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null; then
    sed_inplace 's/^host_whitelist = .*/& sabnzbd/' "$CONFIG_DIR/sabnzbd/sabnzbd.ini" 2>/dev/null
    docker restart sabnzbd >/dev/null 2>&1 && ok "SABnzbd: added Docker hostname to whitelist" || true
    wait_for "SABnzbd" "$SABNZBD_URL"
  fi

  # Try configured password first, fall back to temp password from logs
  QBIT_TEMP_PASS=$(docker logs qbittorrent 2>&1 | sed -n 's/.*A temporary password is provided for this session: *//p' | tail -1 || echo "")
  [ -z "$QBIT_TEMP_PASS" ] && QBIT_TEMP_PASS="adminadmin"
  QBIT_PASS="$QBIT_CONFIGURED_PASS"

  [ -n "$SONARR_KEY" ]       && ok "Sonarr:       $(mask "$SONARR_KEY")"       || err "Sonarr key not found"
  [ -n "$SONARR_ANIME_KEY" ] && ok "Sonarr Anime: $(mask "$SONARR_ANIME_KEY")" || err "Sonarr Anime key not found"
  [ -n "$RADARR_KEY" ]       && ok "Radarr:       $(mask "$RADARR_KEY")"       || err "Radarr key not found"
  [ -n "$LIDARR_KEY" ]       && ok "Lidarr:       $(mask "$LIDARR_KEY")"       || err "Lidarr key not found"
  [ -n "$PROWLARR_KEY" ]     && ok "Prowlarr:     $(mask "$PROWLARR_KEY")"     || err "Prowlarr key not found"
  [ -n "$SABNZBD_KEY" ]      && ok "SABnzbd:      $(mask "$SABNZBD_KEY")"      || warn "SABnzbd key not found"
  [ -n "$JELLYSEERR_KEY" ]   && ok "Jellyseerr:   $(mask "$JELLYSEERR_KEY")"   || warn "Jellyseerr key not found (will read after setup)"
  ok "qBittorrent:  user $QBIT_USER"
}
