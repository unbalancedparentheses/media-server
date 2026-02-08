{ pkgs, ... }:

let
  home = "/Users/claudiabottasera";
  mediaBase = "${home}/media";
  configBase = "${mediaBase}/config";
in
{
  launchd.user.agents = {
    media-jellyfin = {
      serviceConfig = {
        ProgramArguments = [
          "${pkgs.jellyfin}/bin/jellyfin"
          "--datadir" "${configBase}/jellyfin"
        ];
        KeepAlive = true;
        RunAtLoad = true;
        WorkingDirectory = "${configBase}/jellyfin";
        StandardOutPath = "${configBase}/jellyfin/logs/stdout.log";
        StandardErrorPath = "${configBase}/jellyfin/logs/stderr.log";
        EnvironmentVariables.HOME = home;
      };
    };

    media-sonarr = {
      serviceConfig = {
        ProgramArguments = [
          "${pkgs.sonarr}/bin/Sonarr"
          "--data=${configBase}/sonarr"
          "--nobrowser"
        ];
        KeepAlive = true;
        RunAtLoad = true;
        WorkingDirectory = "${configBase}/sonarr";
        StandardOutPath = "${configBase}/sonarr/logs/stdout.log";
        StandardErrorPath = "${configBase}/sonarr/logs/stderr.log";
        EnvironmentVariables.HOME = home;
      };
    };

    media-sonarr-anime = {
      serviceConfig = {
        ProgramArguments = [
          "${pkgs.sonarr}/bin/Sonarr"
          "--data=${configBase}/sonarr-anime"
          "--nobrowser"
          "--port=8990"
        ];
        KeepAlive = true;
        RunAtLoad = true;
        WorkingDirectory = "${configBase}/sonarr-anime";
        StandardOutPath = "${configBase}/sonarr-anime/logs/stdout.log";
        StandardErrorPath = "${configBase}/sonarr-anime/logs/stderr.log";
        EnvironmentVariables.HOME = home;
      };
    };

    media-radarr = {
      serviceConfig = {
        ProgramArguments = [
          "${pkgs.radarr}/bin/Radarr"
          "--data=${configBase}/radarr"
          "--nobrowser"
        ];
        KeepAlive = true;
        RunAtLoad = true;
        WorkingDirectory = "${configBase}/radarr";
        StandardOutPath = "${configBase}/radarr/logs/stdout.log";
        StandardErrorPath = "${configBase}/radarr/logs/stderr.log";
        EnvironmentVariables.HOME = home;
      };
    };

    media-prowlarr = {
      serviceConfig = {
        ProgramArguments = [
          "${pkgs.prowlarr}/bin/Prowlarr"
          "--data=${configBase}/prowlarr"
          "--nobrowser"
        ];
        KeepAlive = true;
        RunAtLoad = true;
        WorkingDirectory = "${configBase}/prowlarr";
        StandardOutPath = "${configBase}/prowlarr/logs/stdout.log";
        StandardErrorPath = "${configBase}/prowlarr/logs/stderr.log";
        EnvironmentVariables.HOME = home;
      };
    };

    media-bazarr = {
      serviceConfig = {
        ProgramArguments = [
          "${pkgs.bazarr}/bin/bazarr"
          "--config" "${configBase}/bazarr"
          "--no-update"
        ];
        KeepAlive = true;
        RunAtLoad = true;
        WorkingDirectory = "${configBase}/bazarr";
        StandardOutPath = "${configBase}/bazarr/logs/stdout.log";
        StandardErrorPath = "${configBase}/bazarr/logs/stderr.log";
        EnvironmentVariables.HOME = home;
      };
    };

    media-sabnzbd = {
      serviceConfig = {
        ProgramArguments = [
          "${pkgs.sabnzbd}/bin/sabnzbd"
          "-f" "${configBase}/sabnzbd/sabnzbd.ini"
          "-s" "127.0.0.1:8080"
        ];
        KeepAlive = true;
        RunAtLoad = true;
        WorkingDirectory = "${configBase}/sabnzbd";
        StandardOutPath = "${configBase}/sabnzbd/logs/stdout.log";
        StandardErrorPath = "${configBase}/sabnzbd/logs/stderr.log";
        EnvironmentVariables.HOME = home;
      };
    };

    media-qbittorrent = {
      serviceConfig = {
        ProgramArguments = [
          "${pkgs.qbittorrent-nox}/bin/qbittorrent-nox"
          "--webui-port=8081"
          "--profile=${configBase}/qbittorrent"
        ];
        KeepAlive = true;
        RunAtLoad = true;
        WorkingDirectory = "${configBase}/qbittorrent";
        StandardOutPath = "${configBase}/qbittorrent/logs/stdout.log";
        StandardErrorPath = "${configBase}/qbittorrent/logs/stderr.log";
        EnvironmentVariables.HOME = home;
      };
    };

    media-jellyseerr = {
      serviceConfig = {
        ProgramArguments = [
          "${pkgs.jellyseerr}/bin/jellyseerr"
        ];
        KeepAlive = true;
        RunAtLoad = true;
        WorkingDirectory = "${configBase}/jellyseerr";
        StandardOutPath = "${configBase}/jellyseerr/logs/stdout.log";
        StandardErrorPath = "${configBase}/jellyseerr/logs/stderr.log";
        EnvironmentVariables = {
          HOME = home;
          CONFIG_DIRECTORY = "${configBase}/jellyseerr";
          PORT = "5055";
        };
      };
    };
  };
}
