{ pkgs, ... }:

let
  home = "/Users/claudiabottasera";
  configBase = "${home}/media/config";

  healthCheck = pkgs.writeShellScript "media-health-check" ''
    failed=""

    check() {
      local name="$1" port="$2"
      if ! ${pkgs.curl}/bin/curl -sf -o /dev/null --connect-timeout 3 "http://localhost:$port"; then
        failed="$failed $name(:$port)"
      fi
    }

    check "Jellyfin"     8096
    check "Sonarr"       8989
    check "Sonarr-Anime" 8990
    check "Radarr"       7878
    check "Prowlarr"     9696
    check "Bazarr"       6767
    check "SABnzbd"      8080
    check "qBittorrent"  8081

    if [ -n "$failed" ]; then
      echo "DOWN:$failed"
      /usr/bin/osascript -e "display notification \"Services down:$failed\" with title \"Media Server\" sound name \"Basso\""
      exit 1
    else
      echo "All services healthy."
    fi
  '';

in
{
  # Runs every 5 minutes
  launchd.user.agents.media-health = {
    serviceConfig = {
      ProgramArguments = [ "${healthCheck}" ];
      StartInterval = 300;
      WorkingDirectory = home;
      StandardOutPath = "${configBase}/nginx/logs/health-stdout.log";
      StandardErrorPath = "${configBase}/nginx/logs/health-stderr.log";
      EnvironmentVariables.HOME = home;
    };
  };
}
