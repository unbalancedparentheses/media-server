# media-server

One-command self-hosted media server. 40+ Docker containers, fully automated, pre-wired, and verified. Request a movie and it's downloaded, organized, subtitled, and ready to watch.

```
bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)
```

## Services

### Streaming & Media

| Service | Description |
|---------|-------------|
| **Jellyfin** | Open-source media player — streams movies, TV, and anime via browser or native apps (iOS, Android, Apple TV, Fire TV, Roku) |
| **Navidrome** | Music streaming server with Subsonic API — works with DSub, Symfonium, etc. |
| **Kavita** | Digital library for ebooks, comics, and manga |
| **TubeArchivist** | YouTube archive — subscribe to channels, download videos, full-text search, offline playback |

### Library Automation

| Service | Description |
|---------|-------------|
| **Jellyseerr** | Netflix-like request portal — browse, request, and track movies/TV shows |
| **Sonarr** | TV show automation — monitors, downloads, renames, and organizes episodes |
| **Sonarr Anime** | Dedicated Sonarr instance with anime-specific indexers (Nyaa, SubsPlease, Mikan) |
| **Radarr** | Movie automation — same as Sonarr but for films |
| **Lidarr** | Music automation — monitors artists and downloads new releases |
| **LazyLibrarian** | Book/audiobook automation — ARM64-native replacement for Readarr |
| **Prowlarr** | Centralized indexer manager — configure once, synced to all *arr services |
| **Bazarr** | Automatic subtitle downloads — English always, Spanish when available |
| **Recyclarr** | Syncs TRaSH Guide quality profiles to Sonarr/Radarr weekly |
| **Janitorr** | Rule-based media cleanup — auto-removes unwatched content after a grace period |

### Downloads

| Service | Description |
|---------|-------------|
| **qBittorrent** | Torrent client, routed through Gluetun VPN |
| **SABnzbd** | Usenet client — faster and more private, requires paid provider |
| **Autobrr** | IRC/RSS automation — grabs releases from private trackers within seconds |
| **Unpackerr** | Auto-extracts compressed downloads for *arr import |
| **FlareSolverr** | Cloudflare bypass for protected indexers |
| **Gluetun** | VPN tunnel for torrent traffic — built-in kill switch (optional, disabled by default) |

### AI

| Service | Description |
|---------|-------------|
| **Ollama** | Local LLM runtime — run Llama, Mistral, Gemma, etc. on your machine |
| **Open WebUI** | ChatGPT-like web interface for Ollama models |

### Photos

| Service | Description |
|---------|-------------|
| **Immich** | Google Photos replacement with ML — face recognition, object detection, map view, mobile auto-upload |

### Transcoding

| Service | Description |
|---------|-------------|
| **Tdarr** | Distributed transcode automation — convert H.264 to H.265/HEVC, save 40-50% storage |

### Infrastructure

| Service | Description |
|---------|-------------|
| **Nginx** | Reverse proxy — maps `.media.local` domains, serves landing page with live widgets |
| **Watchtower** | Auto-updates all containers daily at 4 AM with rolling restarts |
| **Dozzle** | Live Docker log viewer — invaluable with 35+ containers |
| **CrowdSec** | Collaborative IPS — blocks malicious IPs hitting your Nginx |
| **Beszel** | Lightweight system monitoring — CPU, RAM, disk, per-container stats |
| **Scrutiny** | Hard drive S.M.A.R.T. monitoring |
| **Uptime Kuma** | Service uptime monitoring with push notifications |
| **Homepage** | Dashboard with live widgets — transfers, calendar, requests, disk space |
| **Gitea** | Local Git mirror and repository hosting |
| **Tailscale** | Mesh VPN for remote access with automatic HTTPS certificates |

### Note on LazyLibrarian vs Readarr

The official Readarr Docker image (`lscr.io/linuxserver/readarr:develop`) historically had no ARM64 builds, breaking setup on Apple Silicon Macs. ARM64 support was added in late 2024 via the `develop` tag, but it remains unstable. LazyLibrarian is the practical alternative — it has native ARM64 support, integrates with qBittorrent/SABnzbd, and works reliably. Both can coexist if you want to try Readarr alongside LazyLibrarian.

## Quick start

### One-liner

```
bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)
```

Clones or updates `~/media-server` and runs `./setup.sh --yes` for full non-interactive setup.

### Manual

```bash
git clone https://github.com/unbalancedparentheses/media-server.git
cd media-server
# Optional: copy/edit config.toml first (setup will auto-create it if missing)
./setup.sh --yes
```

### What setup.sh does

Fully idempotent — safe to re-run at any time.

1. **Installs prerequisites** — Docker + jq (uses Python TOML parsing, yq optional), Tailscale optional
2. **Checks Tailscale** — configures remote access (optional)
3. **Creates `~/media/` directory structure** — libraries, downloads, configs, backups
4. **Pre-seeds SABnzbd** — generates API key, skips first-run wizard
5. **Starts all containers** via Docker Compose
6. **Adds `.media.local` domains** to `/etc/hosts`
7. **Configures every service** — wires Jellyfin, Sonarr, Radarr, Prowlarr, Bazarr, Jellyseerr, qBittorrent, SABnzbd, and Nginx together
8. **Runs 90+ verification checks** — every service healthy, every API wired, every container running

## Configuration

Optional for first run: `setup.sh --yes` will auto-create `config.toml` with secure generated defaults.
You can still copy/edit `config.toml.example` manually for full control.

### Credentials

```toml
[jellyfin]
username = "admin"
password = "changeme"

[qbittorrent]
username = "admin"
password = "changeme"
```

### Downloads

```toml
[downloads]
seeding_ratio = 2
seeding_time_minutes = 10080  # 7 days
```

### Subtitles

```toml
[subtitles]
languages = ["en", "es"]
providers = ["opensubtitlescom"]
```

English subtitles are always downloaded. Spanish is fetched when available. Add any [ISO 639-1 codes](https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes) you need.

### Quality profiles

```toml
[quality]
sonarr_profile = "WEB-1080p"
sonarr_anime_profile = "Remux-1080p - Anime"
radarr_profile = "HD Bluray + WEB"
```

[TRaSH Guide](https://trash-guides.info/) profiles synced by Recyclarr. Defaults: 1080p web for TV, HD bluray for movies.

### Indexers

```toml
[[indexers]]
name = "1337x"
definitionName = "1337x"
enable = true
flaresolverr = true
```

Public torrent indexers, anime indexers (Nyaa, SubsPlease, Mikan), and optional usenet indexers (NZBgeek). Set `flaresolverr = true` for Cloudflare-protected sites.

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

Routes torrent traffic through [Gluetun](https://github.com/qdm12/gluetun). 30+ providers supported. Kill switch built in. Optional — disabled by default.

### YouTube archive

```toml
[tubearchivist]
username = "admin"
password = "changeme"
```

### Usenet providers

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

### Timezone

```toml
timezone = "America/New_York"
```

## Usage

```bash
./setup.sh --preflight          # Fast local prerequisite + config checks
./setup.sh --check-config       # Validate config.toml only
./setup.sh --yes                # Full setup, non-interactive
./setup.sh                      # Full setup (idempotent, interactive)
./setup.sh --test               # Verification checks only
./setup.sh --update             # Backup + pull latest images + restart
./setup.sh --backup             # Backup all service configs
./setup.sh --restore <file>     # Restore from backup
```

## Remote access

[Tailscale](https://tailscale.com) provides mesh VPN with automatic HTTPS:

- `https://<hostname>.ts.net:8096` — Jellyfin
- `https://<hostname>.ts.net:5055` — Jellyseerr
- `https://<hostname>.ts.net` — Landing page

Share access with family/friends by inviting them to your tailnet. Skip during setup if you only need local access.

## Ports

| Service | Port | URL |
|---------|------|-----|
| Nginx (landing) | 80 | http://media.local |
| Jellyfin | 8096 | http://jellyfin.media.local |
| Jellyseerr | 5055 | http://jellyseerr.media.local |
| Sonarr | 8989 | http://sonarr.media.local |
| Sonarr Anime | 8990 | http://sonarr-anime.media.local |
| Radarr | 7878 | http://radarr.media.local |
| Prowlarr | 9696 | http://prowlarr.media.local |
| Bazarr | 6767 | http://bazarr.media.local |
| qBittorrent | 8081 | http://qbittorrent.media.local |
| SABnzbd | 8080 | http://sabnzbd.media.local |
| Lidarr | 8686 | http://lidarr.media.local |
| LazyLibrarian | 5299 | http://lazylibrarian.media.local |
| Navidrome | 4533 | http://navidrome.media.local |
| Kavita | 5001 | http://kavita.media.local |
| TubeArchivist | 8000 | http://tubearchivist.media.local |
| Tdarr | 8265 | http://tdarr.media.local |
| Autobrr | 7474 | http://autobrr.media.local |
| Immich | 2283 | http://immich.media.local |
| Open WebUI | 3100 | http://open-webui.media.local |
| Ollama | 11434 | — |
| Dozzle | 9999 | http://dozzle.media.local |
| Beszel | 8090 | http://beszel.media.local |
| CrowdSec | 8180 | — |
| Scrutiny | 9091 | http://scrutiny.media.local |
| Gitea | 3000 | http://gitea.media.local |
| Uptime Kuma | 3001 | http://uptime-kuma.media.local |
| Homepage | 3002 | http://homepage.media.local |

## Directory structure

```
~/media/
├── movies/                     # Radarr
├── tv/                         # Sonarr
├── anime/                      # Sonarr Anime
├── music/                      # Lidarr
├── books/                      # LazyLibrarian
├── photos/                     # Immich
├── youtube/                    # TubeArchivist
├── transcode_cache/            # Tdarr working directory
├── downloads/
│   ├── torrents/{complete,incomplete}
│   └── usenet/{complete,incomplete}
├── config/                     # One directory per service
└── backups/                    # 10 retained, oldest pruned
```

## How it works

```
User ──> Jellyseerr ──> Sonarr/Radarr ──> Prowlarr ──> Indexers
                                │
                    qBittorrent/SABnzbd (via Gluetun VPN)
                                │
                         download completes
                          ├── Jellyfin (library scan)
                          ├── Bazarr (subtitles)
                          └── Tdarr (transcode queue)
                                │
                     User watches on Jellyfin
                                │
                     Janitorr cleans up unwatched content
```

1. You request a movie or show in Jellyseerr
2. Sonarr/Radarr searches indexers via Prowlarr
3. Best match sent to qBittorrent (through VPN) or SABnzbd
4. Downloaded, imported, renamed, and added to Jellyfin
5. Bazarr fetches subtitles in English (+ Spanish when available)
6. Tdarr transcodes to H.265 in the background to save storage
7. Janitorr removes content nobody watches after your configured grace period

## What else could you add

The stack is already at the enthusiast/completionist tier. Niche additions if you need them:

- **Audiobookshelf** — audiobook and podcast server
- **Mylar3 / Kapowarr** — comic book automation
