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

  LIDARR_URL="http://$admin:8686"
  NAVIDROME_URL="http://localhost:4533"
  IMMICH_URL="http://localhost:2283"
  TDARR_URL="http://$admin:8265"
  DOZZLE_URL="http://$admin:9999"
  BESZEL_URL="http://$admin:8090"
  SCRUTINY_URL="http://$admin:9091"
  UPTIME_KUMA_URL="http://$admin:3001"

  SONARR_INTERNAL="http://sonarr:8989"
  SONARR_ANIME_INTERNAL="http://sonarr-anime:8989"
  RADARR_INTERNAL="http://radarr:7878"
  PROWLARR_INTERNAL="http://prowlarr:9696"
  LIDARR_INTERNAL="http://lidarr:8686"

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
  SERVICE_HEALTH_ENDPOINTS+=$'Lidarr|'"$LIDARR_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Navidrome|'"$NAVIDROME_URL"$'/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Immich|'"$IMMICH_URL"$'/api/server/ping\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Tdarr|'"$TDARR_URL"$'|auth\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Scrutiny|'"$SCRUTINY_URL"$'/api/health|auth\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Uptime Kuma|'"$UPTIME_KUMA_URL"$'\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Dozzle|'"$DOZZLE_URL"$'|auth\n'
  SERVICE_HEALTH_ENDPOINTS+=$'Beszel|'"$BESZEL_URL"$'/api/health\n'
  SERVICE_HEALTH_ENDPOINTS="${SERVICE_HEALTH_ENDPOINTS%$'\n'}"

  # One per line: setup.sh sets IFS to newline/tab, so spaces don't split
  CONTAINER_LIST=$'jellyfin\nnavidrome\nsonarr\nsonarr-anime\nradarr\nlidarr\nprowlarr\nbazarr\nsabnzbd\nqbittorrent\njellyseerr\nflaresolverr\nmedia-nginx\nrecyclarr\nunpackerr\ntdarr\njanitorr\ndozzle\nbeszel\nbeszel-agent\nimmich\nimmich-machine-learning\nimmich-redis\nimmich-postgres\nscrutiny\nuptime-kuma'
}
