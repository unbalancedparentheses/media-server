{ pkgs, ... }:

{
  home.stateVersion = "25.05";

  home.packages = with pkgs; [
    # Media tools
    ffmpeg
    mediainfo
    yt-dlp

    # CLI essentials
    ripgrep
    fd
    bat
    eza
    fzf
  ];

  programs.git = {
    enable = true;
    userName = "Claudia Bottasera";
    extraConfig = {
      init.defaultBranch = "main";
      pull.rebase = true;
    };
  };

  programs.zsh = {
    enable = true;
    shellAliases = {
      ms = "darwin-rebuild switch --flake ~/media-server";
      mstatus = "launchctl list | grep media";
      mlog = "tail -f ~/media/config/*/logs/*.log";
      ls = "eza";
      ll = "eza -la";
      cat = "bat --plain";
    };
  };
}
