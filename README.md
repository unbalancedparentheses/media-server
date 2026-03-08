# media-server

One-command self-hosted media server. Request a movie or TV show and it's automatically downloaded, organized, subtitled, and ready to stream — like running your own Netflix.

```
bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)
```

## How it works

```
You ──> Jellyseerr (request) ──> Sonarr / Radarr ──> Prowlarr ──> Indexers
                                        │
                            qBittorrent / SABnzbd
                              (through VPN tunnel)
                                        │
                                 download completes
                                  ├── Bazarr (subtitles)
                                  └── Tdarr (transcode to H.265)
                                        │
                               Jellyfin (stream it)
                                        │
                          Janitorr (clean up unwatched)
```

1. Request a movie or show through **Jellyseerr** (Netflix-like browse and request UI)
2. **Sonarr** (TV/anime) or **Radarr** (movies) searches indexers via **Prowlarr**
3. Best match is sent to **qBittorrent** (through VPN) or **SABnzbd** (Usenet)
4. File is downloaded, renamed, and added to your **Jellyfin** library
5. **Bazarr** fetches subtitles automatically (English + Spanish by default)
6. **Tdarr** transcodes to H.265 in the background to save ~40-50% storage
7. **Janitorr** removes content nobody has watched after a grace period

## Quick start

### One-liner

```
bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)
```

Clones to `~/media-server`, prompts for credentials, and runs full setup.

### Manual

```bash
git clone https://github.com/unbalancedparentheses/media-server.git
cd media-server
cp config.toml.example config.toml  # optional — setup creates one if missing
./setup.sh
```

### What setup.sh does

Fully idempotent — safe to re-run at any time.

1. Installs Docker + dependencies
2. Prompts for Jellyfin and qBittorrent credentials (or generates them with `--yes`)
3. Creates `~/media/` directory structure (libraries, downloads, configs)
4. Starts 25+ containers via Docker Compose
5. Wires every service together (API keys, download clients, indexers, subtitles)
6. Sets authentication on all services (Jellyfin credentials shared across the stack)
7. Adds `.media.local` domains to `/etc/hosts`
8. Runs 90+ verification checks

```bash
./setup.sh                      # Full setup (interactive)
./setup.sh --yes                # Full setup (non-interactive, generates passwords)
./setup.sh --test               # Run verification checks only
./setup.sh --update             # Pull latest images + restart
./setup.sh --backup             # Backup all service configs
./setup.sh --restore <file>     # Restore from backup
./setup.sh --preflight          # Check prerequisites + config
./setup.sh --check-config       # Validate config.toml only
./setup.sh --dry-run            # Preview actions without changing anything
```

## Services

### Core — streaming and requests

| Service | What it does |
|---------|-------------|
| **[Jellyfin](https://jellyfin.org)** | Stream your library — browser, iOS, Android, Apple TV, Fire TV, Roku, Chromecast |
| **[Jellyseerr](https://github.com/Fallenbagel/jellyseerr)** | Request movies and shows — browse trending, search, track requests |

### TV, anime, and movies

| Service | What it does |
|---------|-------------|
| **[Sonarr](https://sonarr.tv)** | Automatically downloads and organizes TV shows |
| **[Sonarr Anime](https://sonarr.tv)** | Dedicated instance with anime indexers (Nyaa, SubsPlease, Mikan, Bangumi) |
| **[Radarr](https://radarr.video)** | Automatically downloads and organizes movies |
| **[Bazarr](https://www.bazarr.media)** | Automatic subtitle downloads for everything |
| **[Prowlarr](https://prowlarr.com)** | Manages all your indexers in one place, syncs to Sonarr/Radarr |

### Downloads

| Service | What it does |
|---------|-------------|
| **[qBittorrent](https://www.qbittorrent.org)** | Torrent client, routed through VPN |
| **[SABnzbd](https://sabnzbd.org)** | Usenet client (optional — requires a paid provider) |
| **[Gluetun](https://github.com/qdm12/gluetun)** | VPN tunnel for torrent traffic with kill switch (optional) |
| **[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)** | Bypasses Cloudflare on protected indexers |
| **[Unpackerr](https://unpackerr.zip)** | Extracts compressed downloads so Sonarr/Radarr can import them |

### Background automation

| Service | What it does |
|---------|-------------|
| **[Recyclarr](https://recyclarr.dev)** | Syncs [TRaSH Guide](https://trash-guides.info/) quality profiles to Sonarr/Radarr weekly |
| **[Janitorr](https://github.com/Schaka/Janitorr)** | Removes unwatched content after a configurable grace period (starts in dry-run mode) |
| **[Tdarr](https://home.tdarr.io)** | Transcodes media to H.265/HEVC to save storage |

### Also included

| Service | What it does |
|---------|-------------|
| **[Navidrome](https://www.navidrome.org)** | Music streaming (works with DSub, Symfonium, etc.) |
| **[Lidarr](https://lidarr.audio)** | Music automation — monitors artists and downloads releases |
| **[Immich](https://immich.app)** | Self-hosted Google Photos replacement with face/object recognition |
| **[Nginx](https://nginx.org)** | Reverse proxy — `.media.local` domains + landing page with live widgets |
| **[Dozzle](https://dozzle.dev)** | Live Docker log viewer (SSE streaming) |
| **[Beszel](https://beszel.dev)** | System monitoring (CPU, RAM, disk, per-container) |
| **[Scrutiny](https://github.com/AnalogJ/scrutiny)** | Hard drive S.M.A.R.T. health monitoring |
| **[Uptime Kuma](https://uptime.kuma.pet)** | Service uptime monitoring with notifications |
| **[Tailscale](https://tailscale.com)** | Mesh VPN for remote access (optional) |

## Configuration

Edit `config.toml` before running setup, or let setup prompt you interactively. See `config.toml.example` for all options.

### Credentials

```toml
[jellyfin]
username = "admin"
password = "changeme"

[qbittorrent]
username = "admin"
password = "changeme"
```

Setup prompts for real passwords on first run. With `--yes`, secure random passwords are generated automatically. Jellyfin credentials are shared across most services (Sonarr, Radarr, Prowlarr, Bazarr, SABnzbd, Navidrome, Immich).

### Quality profiles

```toml
[quality]
sonarr_profile = "WEB-1080p"
sonarr_anime_profile = "Remux-1080p - Anime"
radarr_profile = "HD Bluray + WEB"
```

[TRaSH Guide](https://trash-guides.info/) profiles synced by Recyclarr. Defaults: 1080p web for TV, remux for anime, HD bluray for movies.

### Subtitles

```toml
[subtitles]
languages = ["en", "es"]
providers = ["opensubtitlescom", "podnapisi", "yifysubtitles"]
```

English always downloaded, additional languages when available. Nine providers pre-configured — some need free accounts (OpenSubtitles, Addic7ed), configurable in the Bazarr UI after setup.

### Indexers

Pre-configured with public torrent indexers and anime indexers:

```toml
[[indexers]]
name = "1337x"
definitionName = "1337x"
enable = true
flaresolverr = true      # needs FlareSolverr for Cloudflare bypass

[[indexers]]
name = "Nyaa.si"
definitionName = "nyaasi"
enable = true
anime = true             # routes to Sonarr Anime only
```

Included by default: 1337x, EZTV, The Pirate Bay, YTS, Knaben, LimeTorrents, BitSearch, MegaPeer, Nyaa.si, SubsPlease, Mikan, Bangumi Moe. Add private trackers or usenet indexers as needed.

### VPN

```toml
[vpn]
enable = false
provider = "mullvad"
type = "wireguard"
wireguard_private_key = ""
wireguard_addresses = ""
server_countries = "Switzerland"
```

Routes torrent traffic through [Gluetun](https://github.com/qdm12/gluetun). 30+ providers supported (Mullvad, ProtonVPN, NordVPN, Surfshark, etc.). Kill switch built in. Optional — disabled by default.

### Usenet

```toml
[[usenet_providers]]
name = "usenet"
enable = false
host = ""
port = 563
ssl = true
username = ""
password = ""
connections = 20
```

Optional. Faster and more private than torrents, but requires a paid Usenet provider (Newshosting, Eweka, Frugal Usenet, etc.).

## Access

All services are available at `http://<service>.media.local` after setup.

| Service | URL |
|---------|-----|
| Landing page | http://media.local |
| Jellyfin | http://jellyfin.media.local |
| Jellyseerr | http://jellyseerr.media.local |
| Sonarr | http://sonarr.media.local |
| Sonarr Anime | http://sonarr-anime.media.local |
| Radarr | http://radarr.media.local |
| Prowlarr | http://prowlarr.media.local |
| Bazarr | http://bazarr.media.local |
| qBittorrent | http://qbittorrent.media.local |
| SABnzbd | http://sabnzbd.media.local |
| Lidarr | http://lidarr.media.local |
| Navidrome | http://navidrome.media.local |
| Immich | http://immich.media.local |
| Tdarr | http://tdarr.media.local |

For remote access, [Tailscale](https://tailscale.com) provides mesh VPN with automatic HTTPS. Share with family/friends by inviting them to your tailnet.

## Directory structure

```
~/media/
├── movies/              # Radarr
├── tv/                  # Sonarr
├── anime/               # Sonarr Anime
├── music/               # Lidarr / Navidrome
├── photos/              # Immich
├── downloads/
│   ├── torrents/
│   └── usenet/
├── config/              # Per-service config directories
├── transcode_cache/     # Tdarr working directory
├── leaving-soon/        # Janitorr staging area
└── backups/             # Auto-pruned, keeps last 10
```

## Extending

Some things you could add alongside this stack:

- **[Kavita](https://www.kavitareader.com)** — ebooks, comics, manga
- **[Readarr](https://readarr.com)** — book/audiobook automation
- **[Audiobookshelf](https://www.audiobookshelf.org)** — audiobook and podcast server
- **[Mylar3](https://github.com/mylar3/mylar3)** — comic book automation
- **[TubeArchivist](https://www.tubearchivist.com)** — YouTube archive
- **[Autobrr](https://autobrr.com)** — IRC/RSS automation for private trackers
