{ pkgs, lib, ... }:

let
  home = "/Users/claudiabottasera";
  configBase = "${home}/media/config";

  services = {
    jellyfin       = { port = 8096; ws = true; };
    sonarr         = { port = 8989; ws = false; };
    sonarr-anime   = { port = 8990; ws = false; };
    radarr         = { port = 7878; ws = false; };
    prowlarr       = { port = 9696; ws = false; };
    bazarr         = { port = 6767; ws = false; };
    sabnzbd        = { port = 8080; ws = false; };
    qbittorrent    = { port = 8081; ws = false; };
  };

  mkServerBlock = name: { port, ws }: ''
    server {
        listen 80;
        server_name ${name}.media.local;

        client_max_body_size 0;

        location / {
            proxy_pass http://127.0.0.1:${toString port};
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            ${lib.optionalString ws ''
            # WebSocket support
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            ''}
        }
    }
  '';

  nginxConf = pkgs.writeText "media-nginx.conf" ''
    worker_processes 1;
    pid ${configBase}/nginx/nginx.pid;
    error_log ${configBase}/nginx/logs/error.log;

    events {
        worker_connections 1024;
    }

    http {
        access_log ${configBase}/nginx/logs/access.log;

        # temp paths (writable by the daemon user)
        client_body_temp_path ${configBase}/nginx/tmp/client_body;
        proxy_temp_path ${configBase}/nginx/tmp/proxy;
        fastcgi_temp_path ${configBase}/nginx/tmp/fastcgi;
        uwsgi_temp_path ${configBase}/nginx/tmp/uwsgi;
        scgi_temp_path ${configBase}/nginx/tmp/scgi;

        proxy_connect_timeout 10s;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;

        ${lib.concatStringsSep "\n" (lib.mapAttrsToList mkServerBlock services)}

        # Catch-all: show a simple dashboard
        server {
            listen 80 default_server;
            server_name _;

            location / {
                default_type text/html;
                return 200 '${builtins.replaceStrings ["'"] ["\\'"] (dashboardHtml)}';
            }
        }
    }
  '';

  dashboardHtml = ''
    <!DOCTYPE html>
    <html>
    <head><title>Media Server</title>
    <style>
      body { font-family: system-ui; background: #1a1a2e; color: #eee; padding: 2rem; }
      h1 { color: #e94560; }
      .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 1rem; }
      a { display: block; padding: 1.5rem; background: #16213e; border-radius: 8px;
          color: #eee; text-decoration: none; text-align: center; transition: background 0.2s; }
      a:hover { background: #0f3460; }
      .port { color: #e94560; font-size: 0.8rem; }
    </style></head>
    <body>
      <h1>Media Server</h1>
      <div class="grid">
        <a href="http://jellyfin.media.local">Jellyfin<br><span class="port">:8096</span></a>
        <a href="http://sonarr.media.local">Sonarr<br><span class="port">:8989</span></a>
        <a href="http://sonarr-anime.media.local">Sonarr Anime<br><span class="port">:8990</span></a>
        <a href="http://radarr.media.local">Radarr<br><span class="port">:7878</span></a>
        <a href="http://prowlarr.media.local">Prowlarr<br><span class="port">:9696</span></a>
        <a href="http://bazarr.media.local">Bazarr<br><span class="port">:6767</span></a>
        <a href="http://sabnzbd.media.local">SABnzbd<br><span class="port">:8080</span></a>
        <a href="http://qbittorrent.media.local">qBittorrent<br><span class="port">:8081</span></a>
      </div>
    </body></html>
  '';

in
{
  # nginx runs as a system daemon so it can bind port 80
  launchd.daemons.media-nginx = {
    serviceConfig = {
      ProgramArguments = [
        "${pkgs.nginx}/bin/nginx"
        "-c" "${nginxConf}"
        "-g" "daemon off;"
      ];
      KeepAlive = true;
      RunAtLoad = true;
      WorkingDirectory = "${configBase}/nginx";
      StandardOutPath = "${configBase}/nginx/logs/stdout.log";
      StandardErrorPath = "${configBase}/nginx/logs/stderr.log";
    };
  };

  # Create nginx temp directories on activation
  system.activationScripts.postActivation.text = ''
    mkdir -p ${configBase}/nginx/tmp/{client_body,proxy,fastcgi,uwsgi,scgi}
  '';
}
