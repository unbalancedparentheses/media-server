#!/usr/bin/env bash

init_service_registry() {
  QBIT_URL="http://localhost:8081"
  JELLYFIN_URL="http://localhost:8096"
  SONARR_URL="http://localhost:8989"
  SONARR_ANIME_URL="http://localhost:8990"
  RADARR_URL="http://localhost:7878"
  PROWLARR_URL="http://localhost:9696"
  BAZARR_URL="http://localhost:6767"
  SABNZBD_URL="http://localhost:8080"
  JELLYSEERR_URL="http://localhost:5055"

  LIDARR_URL="http://localhost:8686"
  NAVIDROME_URL="http://localhost:4533"
  IMMICH_URL="http://localhost:2283"
  TDARR_URL="http://localhost:8265"
  DOZZLE_URL="http://localhost:9999"
  BESZEL_URL="http://localhost:8090"
  SCRUTINY_URL="http://localhost:9091"
  UPTIME_KUMA_URL="http://localhost:3001"

  SONARR_INTERNAL="http://sonarr:8989"
  SONARR_ANIME_INTERNAL="http://sonarr-anime:8989"
  RADARR_INTERNAL="http://radarr:7878"
  PROWLARR_INTERNAL="http://prowlarr:9696"
  LIDARR_INTERNAL="http://lidarr:8686"

  SERVICE_HEALTH_ENDPOINTS=$'Jellyfin|'"$JELLYFIN_URL"$'/health\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Sonarr|'"$SONARR_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Sonarr Anime|'"$SONARR_ANIME_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Radarr|'"$RADARR_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Prowlarr|'"$PROWLARR_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Bazarr|'"$BAZARR_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'SABnzbd|'"$SABNZBD_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'qBittorrent|'"$QBIT_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Jellyseerr|'"$JELLYSEERR_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Lidarr|'"$LIDARR_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Navidrome|'"$NAVIDROME_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Immich|'"$IMMICH_URL"$'/api/server/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Tdarr|'"$TDARR_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Scrutiny|'"$SCRUTINY_URL"$'/api/health\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Uptime Kuma|'"$UPTIME_KUMA_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Dozzle|'"$DOZZLE_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Beszel|'"$BESZEL_URL"$'/api/health\n'
  SERVICE_HEALTH_ENDPOINTS="${SERVICE_HEALTH_ENDPOINTS%$'\n'}"

  CONTAINER_LIST="jellyfin navidrome sonarr sonarr-anime radarr lidarr prowlarr bazarr sabnzbd qbittorrent jellyseerr flaresolverr media-nginx recyclarr unpackerr tdarr janitorr dozzle beszel immich immich-machine-learning immich-redis immich-postgres scrutiny uptime-kuma"
}
