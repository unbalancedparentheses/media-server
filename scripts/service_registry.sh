#!/usr/bin/env bash

init_service_registry() {
  # Admin UIs listen on network.admin_bind; reach them there unless it's all interfaces
  local admin="${ADMIN_BIND:-0.0.0.0}"
  [ "$admin" = "0.0.0.0" ] && admin="localhost"

  QBIT_URL="http://$admin:8081"
  JELLYFIN_URL="http://localhost:8096"
  SONARR_URL="http://$admin:8989"
  SONARR_ANIME_URL="http://$admin:8990"
  RADARR_URL="http://$admin:7878"
  PROWLARR_URL="http://$admin:9696"
  BAZARR_URL="http://$admin:6767"
  SABNZBD_URL="http://$admin:8080"
  JELLYSEERR_URL="http://localhost:5055"

  DOZZLE_URL="http://$admin:9999"

  SONARR_INTERNAL="http://sonarr:8989"
  SONARR_ANIME_INTERNAL="http://sonarr-anime:8989"
  RADARR_INTERNAL="http://radarr:7878"
  PROWLARR_INTERNAL="http://prowlarr:9696"

  # name|url[|auth] — "auth" = behind nginx basic auth (Jellyfin credentials)
  SERVICE_HEALTH_ENDPOINTS=$'Jellyfin|'"$JELLYFIN_URL"$'/health\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Sonarr|'"$SONARR_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Sonarr Anime|'"$SONARR_ANIME_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Radarr|'"$RADARR_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Prowlarr|'"$PROWLARR_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Bazarr|'"$BAZARR_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'SABnzbd|'"$SABNZBD_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'qBittorrent|'"$QBIT_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Jellyseerr|'"$JELLYSEERR_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Dozzle|'"$DOZZLE_URL"$'|auth\n'
  SERVICE_HEALTH_ENDPOINTS="${SERVICE_HEALTH_ENDPOINTS%$'\n'}"

  # One per line: setup.sh sets IFS to newline/tab, so spaces don't split
  CONTAINER_LIST=$'jellyfin\nsonarr\nsonarr-anime\nradarr\nprowlarr\nbazarr\nsabnzbd\nqbittorrent\njellyseerr\nflaresolverr\nmedia-nginx\nunpackerr\ndozzle'
}
