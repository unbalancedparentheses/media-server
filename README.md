# media-server

One-command setup for a self-hosted media server on macOS. Automates 13 Docker containers, wires them together, and verifies everything works.

```
bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)
```

## What you get

- **Jellyfin** — watch your movies and TV shows
- **Jellyseerr** — request new content with a clean UI
- **Sonarr** + **Sonarr Anime** — automatic TV show and anime downloads
- **Radarr** — automatic movie downloads
- **Prowlarr** — centralized indexer management (torrent + usenet)
- **Bazarr** — automatic subtitles (English, Spanish, configurable)
- **qBittorrent** — torrent client
- **SABnzbd** — usenet client
- **Recyclarr** — TRaSH Guide quality profiles, synced weekly
- **FlareSolverr** — Cloudflare bypass for protected indexers
- **Organizr** — unified dashboard with tabs for all services
- **Nginx** — reverse proxy with landing page and API widgets

## Quick start

### One-liner

The install script clones the repo, copies the example config, and runs setup:

```
bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)
```

### Manual

```bash
git clone https://github.com/unbalancedparentheses/media-server.git
cd media-server
cp config.toml.example config.toml
# Edit config.toml with your credentials
./setup.sh
```

### What setup.sh does

1. Installs prerequisites (Homebrew, Docker, jq, yq)
2. Creates the `~/media/` directory structure
3. Starts all 13 containers with Docker Compose
4. Adds `.media.local` domains to `/etc/hosts`
5. Configures every service (credentials, connections, libraries, indexers, auth)
6. Runs ~77 verification checks to confirm everything works

## Configuration

Copy `config.toml.example` to `config.toml` and edit it:

```toml
[jellyfin]
username = "admin"
password = "changeme"

[qbittorrent]
username = "admin"
password = "changeme"

[downloads]
seeding_ratio = 2
seeding_time_minutes = 10080  # 7 days

[subtitles]
languages = ["en", "es"]

[quality]
sonarr_profile = "WEB-1080p"
radarr_profile = "HD Bluray + WEB"
```

Indexers and usenet providers are also configured in `config.toml` — see the example file for all options.

## Usage

```bash
./setup.sh            # Full setup (idempotent, safe to re-run)
./setup.sh --test     # Run verification checks only
./update.sh           # Pull latest images and restart
./backup.sh           # Backup all service configs
./backup.sh --restore ~/media/backups/media-server_20240101_120000.tar.gz
```

## Ports

| Service | Port | URL |
|---------|------|-----|
| Nginx (landing page) | 80 | http://media.local |
| Jellyfin | 8096 | http://jellyfin.media.local |
| Jellyseerr | 5055 | http://jellyseerr.media.local |
| Sonarr | 8989 | http://sonarr.media.local |
| Sonarr Anime | 8990 | http://sonarr-anime.media.local |
| Radarr | 7878 | http://radarr.media.local |
| Prowlarr | 9696 | http://prowlarr.media.local |
| Bazarr | 6767 | http://bazarr.media.local |
| qBittorrent | 8081 | http://qbittorrent.media.local |
| SABnzbd | 8080 | http://sabnzbd.media.local |
| Organizr | 9090 | http://organizr.media.local |

## Directory structure

```
~/media/
├── movies/                     # Movie library
├── tv/                         # TV show library
├── anime/                      # Anime library
├── downloads/
│   ├── torrents/{complete,incomplete}
│   └── usenet/{complete,incomplete}
├── config/                     # All service configs
│   ├── jellyfin/
│   ├── sonarr/
│   ├── radarr/
│   └── ...
└── backups/                    # Config backups (10 retained)
```

## How it works

```
User                Jellyseerr              Sonarr/Radarr           Prowlarr
 │                      │                        │                      │
 ├── request movie ────>│                        │                      │
 │                      ├── add to library ─────>│                      │
 │                      │                        ├── search indexers ──>│
 │                      │                        │<── results ─────────┤
 │                      │                        ├── send to ──> qBittorrent/SABnzbd
 │                      │                        │                      │
 │                      │                    download completes         │
 │                      │                        ├── notify ──> Jellyfin (library scan)
 │                      │                        ├── notify ──> Bazarr (subtitles)
 │                      │                        │
 │<── watch on Jellyfin ┤                        │
```
