#!/usr/bin/env bash
# Where everything lives: URLs, library/download paths, launchd services.

init_service_registry() {
  # Admin UIs listen on network.admin_bind; reach them there unless it's all interfaces
  local admin="${ADMIN_BIND:-0.0.0.0}"
  [ "$admin" = "0.0.0.0" ] && admin="localhost"

  QBIT_URL="http://$admin:8081"
  JELLYFIN_URL="http://localhost:8096"
  SONARR_URL="http://$admin:8989"
  RADARR_URL="http://$admin:7878"
  PROWLARR_URL="http://$admin:9696"
  BAZARR_URL="http://$admin:6767"
  SABNZBD_URL="http://$admin:8080"
  SEERR_URL="http://localhost:5055"
  BYPARR_URL="http://127.0.0.1:8191"
  DASHBOARD_PORT="${DASHBOARD_PORT:-80}"
  DASHBOARD_URL="http://localhost:$DASHBOARD_PORT"

  # How the services reach each other (all on this machine)
  SONARR_INTERNAL="http://localhost:8989"
  RADARR_INTERNAL="http://localhost:7878"
  PROWLARR_INTERNAL="http://localhost:9696"

  MOVIES_DIR="$MEDIA_DIR/movies"
  TV_DIR="$MEDIA_DIR/tv"
  ANIME_DIR="$MEDIA_DIR/anime"
  DOWNLOADS_DIR="$MEDIA_DIR/downloads"

  # name|url
  SERVICE_HEALTH_ENDPOINTS=$'Jellyfin|'"$JELLYFIN_URL"$'/health\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Sonarr|'"$SONARR_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Radarr|'"$RADARR_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Prowlarr|'"$PROWLARR_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Bazarr|'"$BAZARR_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'SABnzbd|'"$SABNZBD_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'qBittorrent|'"$QBIT_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Seerr|'"$SEERR_URL"$'/api/v1/status\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Byparr|'"$BYPARR_URL"$'/health\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Dashboard|'"$DASHBOARD_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS="${SERVICE_HEALTH_ENDPOINTS%$'\n'}"
}

# launchd agents, one per service (names match flake.nix `services`).
# One per line: setup.sh sets IFS to newline/tab, so spaces don't split.
SERVICE_NAMES=$'jellyfin\nsonarr\nradarr\nprowlarr\nbazarr\nqbittorrent\nsabnzbd\nunpackerr\nseerr\nbyparr\nnginx\ndiskwatch'
LABEL_PREFIX="${MEDIA_LABEL_PREFIX:-org.media-server}"
svc_label() { printf '%s.%s' "$LABEL_PREFIX" "$1"; }
svc_plist() { printf '%s/Library/LaunchAgents/%s.plist' "$HOME" "$(svc_label "$1")"; }
