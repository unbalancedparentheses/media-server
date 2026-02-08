{ pkgs, ... }:

let
  home = "/Users/claudiabottasera";
  mediaBase = "${home}/media";
  configBase = "${mediaBase}/config";

  allServices = [
    "jellyfin" "sonarr" "sonarr-anime" "radarr" "prowlarr"
    "bazarr" "qbittorrent" "recyclarr" "nginx"
  ];

  mediaDomains = [
    "jellyfin" "sonarr" "sonarr-anime" "radarr" "prowlarr"
    "bazarr" "qbittorrent"
  ];
in
{
  nixpkgs.hostPlatform = "aarch64-darwin";

  nix.settings.experimental-features = [ "nix-command" "flakes" ];

  system.primaryUser = "claudiabottasera";

  environment.systemPackages = with pkgs; [
    git
    curl
    htop
    jq
  ];

  # Create all config/log directories on activation
  system.activationScripts.postActivation.text = ''
    echo "Creating media server directories..."
    mkdir -p ${mediaBase}/{movies,tv,anime}
    mkdir -p ${mediaBase}/downloads/torrents/{complete,incomplete}
    mkdir -p ${mediaBase}/downloads/usenet/{complete,incomplete}
    mkdir -p ${mediaBase}/backups
    ${builtins.concatStringsSep "\n" (map (s: "mkdir -p ${configBase}/${s}/logs") allServices)}

    # Add media server hosts entries
    if ! grep -q "media.local" /etc/hosts 2>/dev/null; then
      echo "" >> /etc/hosts
      echo "# Media server local domains (managed by nix-darwin)" >> /etc/hosts
      echo "127.0.0.1 ${builtins.concatStringsSep " " (map (d: "${d}.media.local") mediaDomains)}" >> /etc/hosts
    fi
  '';

  # Homebrew cask integration for GUI apps
  homebrew = {
    enable = true;
    onActivation.cleanup = "zap";
    casks = [
      "iina"             # media player
      "firefox"
    ];
  };

  system.stateVersion = 6;
}
