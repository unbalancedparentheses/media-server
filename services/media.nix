{ pkgs, ... }:

let
  home = "/Users/claudiabottasera";
  configBase = "${home}/media/config";

  # Helper to reduce repetition across all media services
  mkService = {
    name,
    pkg,
    binName ? pkg.meta.mainProgram or name,
    args ? [],
    env ? {},
  }: {
    serviceConfig = {
      ProgramArguments = [ "${pkg}/bin/${binName}" ] ++ args;
      KeepAlive = true;
      RunAtLoad = true;
      WorkingDirectory = "${configBase}/${name}";
      StandardOutPath = "${configBase}/${name}/logs/stdout.log";
      StandardErrorPath = "${configBase}/${name}/logs/stderr.log";
      EnvironmentVariables = { HOME = home; } // env;
    };
  };

  # Recyclarr config generated in the nix store.
  # After first run of Sonarr/Radarr, replace YOUR_SONARR_API_KEY / YOUR_RADARR_API_KEY
  # with real keys from each app's Settings > General > API Key.
  recyclarrConfig = pkgs.writeText "recyclarr.yml" ''
    # Recyclarr configuration — syncs TRaSH Guides quality profiles
    # Docs: https://recyclarr.dev/wiki/yaml/config-reference/
    sonarr:
      main:
        base_url: http://localhost:8989
        api_key: YOUR_SONARR_API_KEY
        replace_existing_custom_formats: true
        quality_definition:
          type: series
        quality_profiles:
          - name: WEB-1080p
            reset_unmatched_scores:
              enabled: true
        custom_formats:
          - trash_ids:
              - 32b367365729d530ca1c124a0b180c64  # Bad Dual Groups
              - 82d40da2bc6923f41e14394075dd4b03  # No-RlsGroup
              - e1a997ddb54e3ecbfe06341ad323c458  # Obfuscated
              - 06d66ab109d4d2eddb2794d21526d140  # Retags
            assign_scores_to:
              - name: WEB-1080p
      anime:
        base_url: http://localhost:8990
        api_key: YOUR_SONARR_ANIME_API_KEY
        quality_definition:
          type: anime
        quality_profiles:
          - name: Remux-1080p - Anime
            reset_unmatched_scores:
              enabled: true

    radarr:
      main:
        base_url: http://localhost:7878
        api_key: YOUR_RADARR_API_KEY
        replace_existing_custom_formats: true
        quality_definition:
          type: movie
        quality_profiles:
          - name: HD Bluray + WEB
            reset_unmatched_scores:
              enabled: true
        custom_formats:
          - trash_ids:
              - ed38b889b31be83fda192888e2286d83  # BR-DISK
              - 90cedc1fea7ea5d11298bebd3d1d3223  # EVO (no WEBDL)
              - b8cd450cbfa689c0259a01d9e29ba3d6  # 3D
            assign_scores_to:
              - name: HD Bluray + WEB
  '';

in
{
  launchd.user.agents = {
    # --- Media Server ---
    media-jellyfin = mkService {
      name = "jellyfin";
      pkg = pkgs.jellyfin;
      args = [ "--datadir" "${configBase}/jellyfin" ];
    };

    # --- TV ---
    media-sonarr = mkService {
      name = "sonarr";
      pkg = pkgs.sonarr;
      binName = "Sonarr";
      args = [ "--data=${configBase}/sonarr" "--nobrowser" ];
    };

    media-sonarr-anime = mkService {
      name = "sonarr-anime";
      pkg = pkgs.sonarr;
      binName = "Sonarr";
      args = [ "--data=${configBase}/sonarr-anime" "--nobrowser" "--port=8990" ];
    };

    # --- Movies ---
    media-radarr = mkService {
      name = "radarr";
      pkg = pkgs.radarr;
      binName = "Radarr";
      args = [ "--data=${configBase}/radarr" "--nobrowser" ];
    };

    # --- Indexers ---
    media-prowlarr = mkService {
      name = "prowlarr";
      pkg = pkgs.prowlarr;
      binName = "Prowlarr";
      args = [ "--data=${configBase}/prowlarr" "--nobrowser" ];
    };

    # --- Subtitles ---
    media-bazarr = mkService {
      name = "bazarr";
      pkg = pkgs.bazarr;
      args = [ "--config" "${configBase}/bazarr" "--no-update" ];
    };

    # --- Downloaders ---
    media-sabnzbd = mkService {
      name = "sabnzbd";
      pkg = pkgs.sabnzbd;
      args = [ "-f" "${configBase}/sabnzbd/sabnzbd.ini" "-s" "127.0.0.1:8080" ];
    };

    media-qbittorrent = mkService {
      name = "qbittorrent";
      pkg = pkgs.qbittorrent-nox;
      binName = "qbittorrent-nox";
      args = [ "--webui-port=8081" "--profile=${configBase}/qbittorrent" ];
    };

    # --- Quality Profile Sync (runs daily at 3 AM) ---
    media-recyclarr = {
      serviceConfig = {
        ProgramArguments = [
          "${pkgs.recyclarr}/bin/recyclarr"
          "sync"
          "--config" "${recyclarrConfig}"
        ];
        WorkingDirectory = "${configBase}/recyclarr";
        StandardOutPath = "${configBase}/recyclarr/logs/stdout.log";
        StandardErrorPath = "${configBase}/recyclarr/logs/stderr.log";
        StartCalendarInterval = [{ Hour = 3; Minute = 0; }];
        EnvironmentVariables.HOME = home;
      };
    };

    # NOTE: jellyseerr and flaresolverr are Linux-only in nixpkgs.
    # For jellyseerr on macOS, run via Docker or use the Overseerr fork.
    # For flaresolverr, Docker is the recommended approach on macOS.
  };
}
