{
  description = "Self-hosted media server for macOS: Jellyfin, Seerr, Sonarr, Radarr, Prowlarr, Bazarr, qBittorrent, SABnzbd, Cleanuparr, run as launchd agents";

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


          # Cleanuparr isn't in nixpkgs; upstream publishes self-contained
          # macOS builds. It keeps its data in CLEANUPARR_CONFIG_PATH.
          cleanuparr =
            let
              version = "2.10.8";
              arch = if pkgs.stdenv.hostPlatform.isAarch64 then "arm64" else "amd64";
            in
            pkgs.stdenvNoCC.mkDerivation {
              pname = "cleanuparr";
              inherit version;
              src = pkgs.fetchzip {
                url = "https://github.com/Cleanuparr/Cleanuparr/releases/download/v${version}/Cleanuparr-${version}-osx-${arch}.zip";
                hash =
                  {
                    arm64 = "sha256-CLl/FvWOAtpvT4k0x19AasCrt+ZG1m0d+tfSLRa3l7M=";
                    amd64 = "sha256-Xbu/GehuffwzDMiFF0AZeCkAvY6obupNUeY0IytPmRg=";
                  }
                  .${arch};
              };
              # A signed single-file .NET app: leave the binaries untouched
              dontFixup = true;
              installPhase = ''
                mkdir -p $out/lib/cleanuparr $out/bin
                cp -R . $out/lib/cleanuparr
                ln -s $out/lib/cleanuparr/Cleanuparr $out/bin/cleanuparr
              '';
              meta = {
                description = "Removes stalled and failed downloads from the *arr apps";
                homepage = "https://github.com/Cleanuparr/Cleanuparr";
                license = lib.licenses.gpl3Only;
                mainProgram = "cleanuparr";
                platforms = lib.platforms.darwin;
              };
            };

          # The Python package: setup and the background services below.
          # Copied on its own so the services restart only when the Python
          # code changes.
          mediaserverPkg = pkgs.runCommand "mediaserver-python" { } ''
            mkdir -p $out
            cp -r ${./mediaserver} $out/mediaserver
          '';
          python = "${pkgs.python3}/bin/python3";
          # A service: python3 -m mediaserver.<module>, with extra tools on PATH
          pyService =
            name: tools:
            pkgs.writeShellScript name ''
              export PATH=${lib.makeBinPath tools}:/usr/bin:/bin:/usr/sbin
              export PYTHONPATH=${mediaserverPkg} PYTHONDONTWRITEBYTECODE=1
              exec ${python} -m mediaserver.${name} "$@"
            '';

          # Gets indexers past Cloudflare; Byparr isn't in nixpkgs, so its
          # pinned source runs with uv (mediaserver/byparr.py)
          byparrStart = pyService "byparr" [ pkgs.uv ];
          # Free space on the media disk; a notification when it runs low
          diskwatchStart = pyService "diskwatch" [ ];
          # Keeps the stack sensible when the Mac goes offline and comes back
          netwatchStart = pyService "netwatch" [ ];
          # Gathers the dashboard's live data into one file the page reads
          dashstatusStart = pyService "dashstatus" [ ];
          # Checks and fixes every imported file: replaces broken or dubbed
          # downloads, adds stereo audio, reads picture subtitles into text.
          # Jellyfin's ffmpeg has Apple's AAC encoder; pgsrip does the OCR.
          postimportStart = pyService "postimport" [
            pkgs.jellyfin-ffmpeg
            pkgs.pgsrip
          ];
          # (each module's docstring in mediaserver/ explains what it does)

          # Placeholders filled in by setup.sh when it writes the launchd agents:
          # @MEDIA@ (~/media), @CONFIG@ (~/media/config), @STATE@ (~/media/.state),
          # @ADMIN_BIND@, @DISK_WARN_GB@, @DISK_MIN_GB@, @DISK_RESERVE_GB@, @DISK_PAUSE@
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
                BYPARR_SRC = "${byparr}";
                BYPARR_UV = exe pkgs.uv;
                # Its Firefox runs with no window at all; the default opens
                # real windows, which made macOS switch Spaces and move windows
                INVPW_TRUE_HEADLESS = "1";
              };
            };
            cleanuparr = {
              # The real binary, not the bin/ symlink: it finds wwwroot next to itself
              args = [ "${cleanuparr}/lib/cleanuparr/Cleanuparr" ];
              env = {
                PORT = "11011";
                BIND_ADDRESS = "@ADMIN_BIND@";
                CLEANUPARR_CONFIG_PATH = "@CONFIG@/cleanuparr";
              };
            };
            dashstatus = {
              args = [ "${dashstatusStart}" ];
              env = {
                DASH_CONFIG = "@CONFIG@";
                DASH_STATE = "@STATE@";
                DASH_MEDIA = "@MEDIA@";
                DISK_WARN_GB = "@DISK_WARN_GB@";
                DISK_MIN_GB = "@DISK_MIN_GB@";
              };
            };
            netwatch = {
              args = [ "${netwatchStart}" ];
              env = {
                NETWATCH_CONFIG = "@CONFIG@";
                NETWATCH_STATE = "@STATE@/netwatch";
              };
            };
            postimport = {
              args = [ "${postimportStart}" ];
              env = {
                POSTIMPORT_CONFIG = "@CONFIG@";
                POSTIMPORT_STATE = "@STATE@/postimport";
              };
            };
            diskwatch = {
              args = [ "${diskwatchStart}" ];
              env = {
                MEDIA_DIR = "@MEDIA@";
                DISKWATCH_STATE = "@STATE@/diskwatch";
                DISK_WARN_GB = "@DISK_WARN_GB@";
                DISK_MIN_GB = "@DISK_MIN_GB@";
                DISK_RESERVE_GB = "@DISK_RESERVE_GB@";
                DISK_PAUSE = "@DISK_PAUSE@";
              };
            };
            # Keeps the Mac from sleeping while it's plugged in (-s holds only
            # on AC power: on battery it sleeps as usual; the display still can)
            awake.args = [
              "/usr/bin/caffeinate"
              "-s"
            ];
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

          # Moonfin's web app loads hls.js from a CDN; setup serves this pinned
          # copy instead, so it works without internet (e.g. on a flight)
          moonfinHlsJs = pkgs.fetchurl {
            url = "https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js";
            hash = "sha256-SEBU6M0D0/bReB+39AK9wxjYpMUn+TOpXGJOJ8yalHA=";
          };

          manifest = pkgs.writeText "media-server-services.json" (
            builtins.toJSON {
              inherit services;
              nginxMimeTypes = "${pkgs.nginx}/conf/mime.types";
              moonfinHlsJs = "${moonfinHlsJs}";
            }
          );

          # setup.sh and the tools it needs; also what `./setup.sh` re-execs into
          cli = pkgs.writeShellApplication {
            name = "media-server";
            runtimeInputs = with pkgs; [
              bash
              coreutils
              curl
              (python3.withPackages (ps: [
                ps.pyyaml
                ps.bcrypt
              ]))
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
            postimportStart
            manifest
            seerr
            sabnzbd
            cleanuparr
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
          inherit (stack)
            manifest
            seerr
            sabnzbd
            cleanuparr
            ;
        }
      );

      apps = forAllSystems (pkgs: {
        default = app pkgs [ ];
        install = app pkgs [ ];
        uninstall = app pkgs [ "--uninstall" ];
        status = app pkgs [ "--status" ];
        doctor = app pkgs [ "--doctor" ];
        open = app pkgs [ "--open" ];
        logs = app pkgs [ "--logs" ];
        restart = app pkgs [ "--restart" ];
        test = app pkgs [ "--test" ];
        e2e = app pkgs [ "--e2e" ];
        backup = app pkgs [ "--backup" ];
        restore = app pkgs [ "--restore" ];
        update = app pkgs [ "--update" ];
        # --check FILE / --fix FILE: the post-import checks and fixes by hand
        postimport = {
          type = "app";
          program = toString (mkStack pkgs).postimportStart;
        };
        # Unit and failure-path tests against fake services (no real services needed)
        unit = {
          type = "app";
          program = nixpkgs.lib.getExe (
            pkgs.writeShellApplication {
              name = "media-server-unit-tests";
              runtimeInputs = with pkgs; [
                bash
                coreutils
                (python3.withPackages (ps: [
                  ps.pyyaml
                  ps.bcrypt
                  ps.coverage
                ]))
                jellyfin-ffmpeg
                nginx
              ];
              text = ''
                # With coverage: the report lists what isn't tested yet, and
                # the run fails if coverage drops below the floor
                COVERAGE_FILE=$(mktemp -d)/coverage
                export COVERAGE_FILE PYTHONDONTWRITEBYTECODE=1
                cd ${self}
                python3 -m coverage run --source=mediaserver -m unittest discover -q -s tests -t .
                python3 -m coverage report --sort=cover --skip-covered --fail-under=85
              '';
            }
          );
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            shellcheck
            jq
            nginx
            nixfmt
            pyright
            ruff
            # The tests on real media files (postimport, repackaging) need it
            jellyfin-ffmpeg
            (python3.withPackages (ps: [
              ps.pyyaml
              ps.bcrypt
            ]))
          ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt);
    };
}
