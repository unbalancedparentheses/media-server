{
  description = "Self-hosted media server for macOS: Jellyfin, Seerr, Sonarr, Radarr, Prowlarr, Bazarr, qBittorrent, SABnzbd, run as launchd agents";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    byparr = {
      url = "github:ThePhaseless/Byparr/v3.0.4";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      byparr,
    }:
    let
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f (pkgsFor system));

      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          # SABnzbd needs unrar to extract most usenet downloads
          config.allowUnfreePredicate = p: builtins.elem (nixpkgs.lib.getName p) [ "unrar" ];
        };

      # nixpkgs marks these Linux-only, but they build and run on macOS
      onDarwinToo =
        pkgs: p:
        p.overrideAttrs (old: {
          meta = old.meta // {
            platforms = old.meta.platforms ++ pkgs.lib.platforms.darwin;
          };
        });

      mkStack =
        pkgs:
        let
          inherit (pkgs) lib;
          exe = lib.getExe;
          # node-gyp (for Seerr's SQLite module) calls xcodebuild on macOS
          seerr = (onDarwinToo pkgs pkgs.seerr).overrideAttrs (old: {
            nativeBuildInputs = old.nativeBuildInputs ++ [
              pkgs.xcbuild
              pkgs.cctools
            ];
          });
          # On macOS SABnzbd keeps the Mac awake while downloading through
          # PyObjC, which the Linux-oriented package doesn't include
          sabnzbd = onDarwinToo pkgs (
            pkgs.sabnzbd.override {
              python3 = pkgs.python3 // {
                withPackages =
                  f:
                  pkgs.python3.withPackages (
                    ps:
                    f ps
                    ++ [
                      ps.pyobjc-core
                      ps.pyobjc-framework-Cocoa
                    ]
                  );
              };
            }
          );

          # Byparr isn't in nixpkgs. Run the pinned source with uv, as upstream
          # documents; the venv, Python and patched Firefox live in @STATE@.
          byparrStart = pkgs.writeShellScript "byparr-start" ''
            set -eu
            state="$BYPARR_STATE"
            mkdir -p "$state"
            export UV_PROJECT_ENVIRONMENT="$state/venv"
            export UV_CACHE_DIR="$state/uv-cache"
            export UV_PYTHON_INSTALL_DIR="$state/python"
            export PYTHONDONTWRITEBYTECODE=1
            cd ${byparr}
            ${exe pkgs.uv} sync --frozen --no-dev --quiet
            if [ ! -e "$state/browser-fetched" ]; then
              ${exe pkgs.uv} run --frozen --no-dev python -m invisible_playwright fetch
              touch "$state/browser-fetched"
            fi
            exec ${exe pkgs.uv} run --frozen --no-dev python main.py
          '';

          # Placeholders filled in by setup.sh when it writes the launchd agents:
          # @CONFIG@ (~/media/config), @STATE@ (~/media/.state), @ADMIN_BIND@
          services = {
            jellyfin.args = [
              (exe pkgs.jellyfin)
              "--datadir=@CONFIG@/jellyfin/data"
              "--configdir=@CONFIG@/jellyfin/config"
              "--cachedir=@CONFIG@/jellyfin/cache"
              "--logdir=@CONFIG@/jellyfin/log"
            ];
            sonarr.args = [
              (exe pkgs.sonarr)
              "-nobrowser"
              "-data=@CONFIG@/sonarr"
            ];
            radarr.args = [
              (exe pkgs.radarr)
              "-nobrowser"
              "-data=@CONFIG@/radarr"
            ];
            prowlarr.args = [
              (exe pkgs.prowlarr)
              "-nobrowser"
              "-data=@CONFIG@/prowlarr"
            ];
            bazarr.args = [
              (exe pkgs.bazarr)
              "--config"
              "@CONFIG@/bazarr"
              "--port"
              "6767"
              "--no-update"
              "True"
            ];
            qbittorrent.args = [
              (exe pkgs.qbittorrent-nox)
              "--profile=@CONFIG@/qbittorrent"
              "--webui-port=8081"
              "--confirm-legal-notice"
            ];
            sabnzbd.args = [
              (exe sabnzbd)
              "--config-file"
              "@CONFIG@/sabnzbd/sabnzbd.ini"
              "--server"
              "@ADMIN_BIND@:8080"
              "--browser"
              "0"
            ];
            unpackerr.args = [
              (exe pkgs.unpackerr)
              "--config"
              "@CONFIG@/unpackerr/unpackerr.conf"
            ];
            seerr = {
              args = [ (exe seerr) ];
              env = {
                PORT = "5055";
                CONFIG_DIRECTORY = "@CONFIG@/seerr";
              };
            };
            byparr = {
              args = [ "${byparrStart}" ];
              env = {
                HOST = "127.0.0.1";
                PORT = "8191";
                BYPARR_STATE = "@STATE@/byparr";
              };
            };
            nginx.args = [
              (exe pkgs.nginx)
              "-p"
              "@CONFIG@/nginx"
              "-c"
              "@CONFIG@/nginx/nginx.conf"
              "-e"
              "stderr"
              "-g"
              "daemon off;"
            ];
          };

          manifest = pkgs.writeText "media-server-services.json" (
            builtins.toJSON {
              inherit services;
              nginxMimeTypes = "${pkgs.nginx}/conf/mime.types";
            }
          );

          # setup.sh and the tools it needs; also what `./setup.sh` re-execs into
          cli = pkgs.writeShellApplication {
            name = "media-server";
            runtimeInputs = with pkgs; [
              bash
              coreutils
              curl
              gnused
              gnugrep
              gnutar
              gzip
              gawk
              jq
              openssl
              (python3.withPackages (ps: [ ps.pyyaml ]))
            ];
            text = ''
              export MEDIA_SERVICES_JSON=${manifest}
              export MEDIA_SERVER_SRC=${self}
              exec bash ${self}/setup.sh "$@"
            '';
          };
        in
        {
          inherit
            cli
            manifest
            seerr
            sabnzbd
            ;
        };

      app = pkgs: args: {
        type = "app";
        program = toString (
          pkgs.writeShellScript "media-server-app" ''
            exec ${nixpkgs.lib.getExe (mkStack pkgs).cli} ${nixpkgs.lib.escapeShellArgs args} "$@"
          ''
        );
      };
    in
    {
      packages = forAllSystems (
        pkgs:
        let
          stack = mkStack pkgs;
        in
        {
          default = stack.cli;
          inherit (stack) manifest seerr sabnzbd;
        }
      );

      apps = forAllSystems (pkgs: {
        default = app pkgs [ ];
        install = app pkgs [ ];
        uninstall = app pkgs [ "--uninstall" ];
        status = app pkgs [ "--status" ];
        logs = app pkgs [ "--logs" ];
        restart = app pkgs [ "--restart" ];
        test = app pkgs [ "--test" ];
        e2e = app pkgs [ "--e2e" ];
        backup = app pkgs [ "--backup" ];
        restore = app pkgs [ "--restore" ];
        update = app pkgs [ "--update" ];
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            shellcheck
            jq
            nginx
            nixfmt
            python3
          ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt);
    };
}
