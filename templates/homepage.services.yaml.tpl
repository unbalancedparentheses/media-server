- Media:
    - Jellyfin:
        icon: jellyfin.svg
        href: http://jellyfin.media.local
        description: Stream your library
        server: local
        container: jellyfin
        widget:
          type: jellyfin
          url: http://jellyfin:8096
          key: {{JELLYFIN_TOKEN}}
    - Jellyseerr:
        icon: jellyseerr.svg
        href: http://jellyseerr.media.local
        description: Request movies & TV
        server: local
        container: jellyseerr
        widget:
          type: jellyseerr
          url: http://jellyseerr:5055
          key: {{JELLYSEERR_KEY}}
    - Navidrome:
        icon: navidrome.svg
        href: http://navidrome.media.local
        description: Stream your music
        server: local
        container: navidrome
    - Kavita:
        icon: kavita.svg
        href: http://kavita.media.local
        description: Read books & comics
        server: local
        container: kavita
- Library Management:
    - Sonarr:
        icon: sonarr.svg
        href: http://sonarr.media.local
        description: TV show automation
        server: local
        container: sonarr
        widget:
          type: sonarr
          url: http://sonarr:8989
          key: {{SONARR_KEY}}
    - Sonarr Anime:
        icon: sonarr.svg
        href: http://sonarr-anime.media.local
        description: Anime series
        server: local
        container: sonarr-anime
        widget:
          type: sonarr
          url: http://sonarr-anime:8989
          key: {{SONARR_ANIME_KEY}}
    - Radarr:
        icon: radarr.svg
        href: http://radarr.media.local
        description: Movie automation
        server: local
        container: radarr
        widget:
          type: radarr
          url: http://radarr:7878
          key: {{RADARR_KEY}}
    - Lidarr:
        icon: lidarr.svg
        href: http://lidarr.media.local
        description: Music automation
        server: local
        container: lidarr
        widget:
          type: lidarr
          url: http://lidarr:8686
          key: {{LIDARR_KEY}}
    - Prowlarr:
        icon: prowlarr.svg
        href: http://prowlarr.media.local
        description: Indexer management
        server: local
        container: prowlarr
        widget:
          type: prowlarr
          url: http://prowlarr:9696
          key: {{PROWLARR_KEY}}
    - Bazarr:
        icon: bazarr.svg
        href: http://bazarr.media.local
        description: Subtitle downloads
        server: local
        container: bazarr
        widget:
          type: bazarr
          url: http://bazarr:6767
          key: {{BAZARR_WIDGET_KEY}}
- Downloads:
    - qBittorrent:
        icon: qbittorrent.svg
        href: http://qbittorrent.media.local
        description: Torrent client
        server: local
        container: qbittorrent
        widget:
          type: qbittorrent
          url: http://qbittorrent:8081
          username: {{QBIT_USER}}
          password: {{QBIT_PASS}}
    - SABnzbd:
        icon: sabnzbd.svg
        href: http://sabnzbd.media.local
        description: Usenet client
        server: local
        container: sabnzbd
        widget:
          type: sabnzbd
          url: http://sabnzbd:8080
          key: {{SABNZBD_KEY}}
- Tools:
    - Immich:
        icon: immich.svg
        href: http://immich.media.local
        description: Photo management
        server: local
        container: immich
    - Gitea:
        icon: gitea.svg
        href: http://gitea.media.local
        description: Git mirror & hosting
        server: local
        container: gitea
    - Uptime Kuma:
        icon: uptime-kuma.svg
        href: http://uptime-kuma.media.local
        description: Service uptime monitoring
        server: local
        container: uptime-kuma
        widget:
          type: uptimekuma
          url: http://uptime-kuma:3001
          slug: default
    - Scrutiny:
        icon: scrutiny.svg
        href: http://scrutiny.media.local
        description: Disk health monitoring
        server: local
        container: scrutiny
