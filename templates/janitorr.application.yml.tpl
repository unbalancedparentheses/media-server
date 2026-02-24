logging:
  level:
    com.github.schaka: INFO
  file:
    name: "/logs/janitorr.log"

file-system:
  access: true
  validate-seeding: true
  leaving-soon-dir: "/media/leaving-soon"
  media-server-leaving-soon-dir: "/media/leaving-soon"
  from-scratch: true
  free-space-check-dir: "/"

application:
  dry-run: true
  run-once: false
  whole-tv-show: false
  whole-show-seeding-check: false
  leaving-soon: 14d
  leaving-soon-threshold-offset-percent: 5
  exclusion-tags:
    - "janitorr_keep"

  media-deletion:
    enabled: true
    movie-expiration:
      5: 15d
      10: 30d
      15: 60d
      20: 90d
    season-expiration:
      5: 15d
      10: 30d
      15: 60d
      20: 120d

  tag-based-deletion:
    enabled: false
    minimum-free-disk-percent: 100
    schedules: []

  episode-deletion:
    enabled: false
    clean-older-seasons: false
    tag: janitorr_daily
    max-episodes: 10
    max-age: 30d

clients:
  sonarr:
    enabled: true
    url: "http://sonarr:8989"
    api-key: "{{SONARR_KEY}}"
    delete-empty-shows: true
    import-exclusions: false
  radarr:
    enabled: true
    url: "http://radarr:7878"
    api-key: "{{RADARR_KEY}}"
    only-delete-files: false
    import-exclusions: false
  bazarr:
    enabled: false
    url: "http://bazarr:6767"
    api-key: ""
  jellyfin:
    enabled: true
    url: "http://jellyfin:8096"
    api-key: "{{JELLYFIN_API_KEY}}"
    username: "{{JELLYFIN_USER}}"
    password: "{{JELLYFIN_PASS}}"
    delete: true
    exclude-favorited: false
    leaving-soon-tv: "Shows (Leaving Soon)"
    leaving-soon-movies: "Movies (Leaving Soon)"
    leaving-soon-type: MOVIES_AND_TV
  emby:
    enabled: false
    url: ""
    api-key: ""
    username: ""
    password: ""
    delete: false
    exclude-favorited: false
    leaving-soon-tv: ""
    leaving-soon-movies: ""
    leaving-soon-type: NONE
  jellyseerr:
    enabled: true
    url: "http://jellyseerr:5055"
    api-key: "{{JELLYSEERR_KEY}}"
    match-server: false
  jellystat:
    enabled: false
    whole-tv-show: false
    url: ""
    api-key: ""
  streamystats:
    enabled: false
    whole-tv-show: false
    url: ""
    api-key: ""
