# media-server

One-command setup for a self-hosted media server on macOS. Automates 13 Docker containers, wires them together, and verifies everything works.

```
bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)
```

## What you get

A fully automated media pipeline — request a movie or TV show, and it gets downloaded, organized, subtitled, and ready to watch. Every service below runs as a Docker container and is pre-configured to talk to the others:

- **Jellyfin** — open-source media player (like Plex, but no account or subscription needed). Streams your movies, TV shows, and anime from a browser or native apps (iOS, Android, Apple TV, Fire TV, Roku, etc.)
- **Jellyseerr** — request portal where you (or family/friends you share with) browse and request content. It has a clean Netflix-like UI with posters, ratings, and trailers. Requests automatically trigger downloads through Sonarr/Radarr
- **Sonarr** — monitors your TV show library, searches for new episodes as they air, grabs them from indexers, and imports them into the correct folder with proper naming. Handles season packs, quality upgrades, and episode renaming automatically
- **Sonarr Anime** — a second Sonarr instance dedicated to anime, with separate quality profiles and anime-specific indexers (Nyaa, SubsPlease, Mikan). Keeps anime organized separately from regular TV shows
- **Radarr** — same as Sonarr but for movies. Monitors your movie wishlist, searches for releases, and imports them with proper naming and metadata
- **Prowlarr** — centralized indexer manager. Instead of configuring indexers separately in Sonarr and Radarr, you add them once in Prowlarr and it syncs them everywhere. Supports both torrent indexers and usenet indexers
- **Bazarr** — automatic subtitle downloader. Watches your libraries and fetches subtitles from OpenSubtitles and other providers. Configured for English and Spanish by default, but you can add any language
- **qBittorrent** — torrent download client. Sonarr/Radarr send downloads here. Configured with seeding ratios and time limits from your config so torrents are cleaned up automatically
- **SABnzbd** — usenet download client. If you have a usenet provider (Newshosting, Eweka, etc.), Sonarr/Radarr will use this for NZB downloads. Faster and more private than torrents, but requires a paid provider subscription
- **Recyclarr** — syncs quality profiles from TRaSH Guides (community-maintained best practices for Sonarr/Radarr). Runs weekly to keep your quality preferences, custom formats, and release scoring up to date
- **FlareSolverr** — a headless browser that solves Cloudflare challenges. Some torrent indexers (like 1337x) use Cloudflare protection — FlareSolverr lets Prowlarr access them without manual intervention
- **Organizr** — unified dashboard with tabs for all services. One URL to access everything with a single login
- **Nginx** — reverse proxy that maps `.media.local` domains to each service (e.g., `jellyfin.media.local`). Also serves a landing page with links and live API widgets showing system status

## Quick start

### One-liner

The install script clones the repo to `~/media-server`, copies the example config, and tells you to edit it:

```
bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)
```

If `~/media-server` already exists, it runs `git pull` to update instead of cloning.

### Manual

```bash
git clone https://github.com/unbalancedparentheses/media-server.git
cd media-server
cp config.toml.example config.toml
# Edit config.toml with your credentials
./setup.sh
```

### What setup.sh does

The script is fully idempotent — safe to re-run at any time. Each step checks the current state and only makes changes if needed.

1. **Installs prerequisites** — Homebrew (macOS package manager), Docker Desktop (container runtime), jq (JSON processor), yq (TOML/YAML parser), and Tailscale (remote access VPN)
2. **Checks Tailscale** — verifies you're signed in for remote access. If not, it warns you and continues (local access still works)
3. **Creates the `~/media/` directory structure** — library folders for movies/tv/anime, download staging areas for torrents and usenet, config directories for each service, and a backups folder
4. **Pre-seeds SABnzbd config** — generates an API key and skips the first-run wizard so SABnzbd starts ready to use
5. **Starts all 13 containers** with Docker Compose — generates a `.env` file with your user/group IDs and timezone, then brings up the full stack
6. **Adds `.media.local` domains to `/etc/hosts`** — maps `media.local`, `jellyfin.media.local`, `sonarr.media.local`, etc. to `127.0.0.1` so you can use friendly URLs instead of `localhost:port`. Requires sudo
7. **Configures every service** — sets up Jellyfin (user account, libraries, metadata), qBittorrent (credentials, download paths, seeding rules), SABnzbd (usenet providers), Sonarr/Radarr (root folders, download clients, quality profiles), Prowlarr (indexers, app sync), Bazarr (subtitle languages, providers), Jellyseerr (Jellyfin integration, Sonarr/Radarr connections), Organizr (tabs, auth), and Nginx (reverse proxy, landing page)
8. **Runs ~77 verification checks** — tests every service is healthy, every API connection works, every integration is wired correctly, and every container is running

## Configuration

Copy `config.toml.example` to `config.toml` and edit it. This single file controls everything — credentials, download behavior, quality preferences, indexers, and usenet providers.

### Credentials

```toml
[jellyfin]
username = "admin"
password = "changeme"

[qbittorrent]
username = "admin"
password = "changeme"

[organizr]
username = "admin"
password = "changeme"
email = "admin@media.local"
```

These set the login credentials for each service. Change them from the defaults before running setup.

### Downloads

```toml
[downloads]
seeding_ratio = 2
seeding_time_minutes = 10080  # 7 days
```

Controls how long torrents seed after completing. `seeding_ratio = 2` means upload 2x what you downloaded. `seeding_time_minutes = 10080` means seed for at least 7 days. Whichever limit is hit first triggers cleanup.

### Subtitles

```toml
[subtitles]
languages = ["en", "es"]
providers = ["opensubtitlescom"]
```

Languages Bazarr will fetch subtitles for. Add any [ISO 639-1 codes](https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes) you need. The provider is which subtitle source to search.

### Quality profiles

```toml
[quality]
sonarr_profile = "WEB-1080p"
sonarr_anime_profile = "Remux-1080p - Anime"
radarr_profile = "HD Bluray + WEB"
```

These are [TRaSH Guide](https://trash-guides.info/) quality profile names, synced by Recyclarr. They control which releases Sonarr/Radarr prefer — resolution, source (web vs bluray), codec, etc. The defaults prefer 1080p web releases for TV and HD bluray for movies.

### Indexers

```toml
[[indexers]]
name = "1337x"
definitionName = "1337x"
enable = true
flaresolverr = true
```

Each `[[indexers]]` block adds a search source to Prowlarr. The `definitionName` must match Prowlarr's built-in definitions. Set `flaresolverr = true` for sites behind Cloudflare protection. Indexers are synced to Sonarr and Radarr automatically.

The example config includes public torrent indexers (1337x, EZTV, The Pirate Bay, YTS, etc.) and anime-specific indexers (Nyaa.si, SubsPlease, Mikan, Bangumi Moe). If you have a private usenet indexer like NZBgeek, add your API key and set `enable = true`.

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

If you have a usenet provider subscription, fill in the connection details and set `enable = true`. SABnzbd will use this to download NZBs that Sonarr/Radarr find. Common providers include Newshosting, Eweka, Frugal Usenet, UsenetExpress, and Easynews — all use port 563 with SSL.

### Other

```toml
timezone = "America/New_York"
```

Sets the timezone for all containers. Affects scheduled tasks (Recyclarr syncs, episode air times, etc.).

## Usage

```bash
./setup.sh                      # Full setup (idempotent, safe to re-run)
./setup.sh --test               # Run verification checks only
./setup.sh --update             # Backup + pull latest images + restart
./setup.sh --backup             # Backup all service configs
./setup.sh --restore <file>     # Restore configs from backup
```

- **Full setup** — runs everything from prerequisites to verification. Safe to re-run: it skips steps that are already done and only changes what's needed
- **Test** — runs only the ~77 verification checks. Useful to confirm everything is healthy without modifying anything
- **Update** — creates a backup first, then pulls the latest Docker images for all services, restarts containers, and cleans up old images. Run this periodically to stay on the latest versions
- **Backup** — archives all service configs from `~/media/config/` into a timestamped `.tar.gz` in `~/media/backups/`. Keeps the last 10 backups and prunes older ones automatically
- **Restore** — stops all containers, extracts a backup archive over the config directory, and restarts. Prompts for confirmation before overwriting

## Remote access

The setup installs [Tailscale](https://tailscale.com) and configures HTTPS via Tailscale Serve. After signing in, your media server is accessible from any device on your Tailscale network with valid HTTPS certificates:

- `https://<hostname>.ts.net:8096` — Jellyfin
- `https://<hostname>.ts.net:5055` — Jellyseerr
- `https://<hostname>.ts.net` — landing page

Your hostname is shown in the setup completion banner. You can also find it with:

    /Applications/Tailscale.app/Contents/MacOS/Tailscale status --json | jq -r '.Self.DNSName'

Tailscale Serve provides real HTTPS with Let's Encrypt certificates via Tailscale's ACME integration — no Nginx changes or manual cert management needed. TLS is terminated by Tailscale and proxied to local HTTP services.

You can also share access with family or friends by inviting them to your tailnet. They'll be able to reach Jellyfin and Jellyseerr from their own devices without being on your local network.

If you skip Tailscale sign-in during setup, everything still works locally — you can sign in later at any time.

## Ports

All services are accessible on localhost and via `.media.local` domains (added to `/etc/hosts` during setup).

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

All media and configuration lives under `~/media/`:

```
~/media/
├── movies/                     # Movie library (Radarr imports here)
├── tv/                         # TV show library (Sonarr imports here)
├── anime/                      # Anime library (Sonarr Anime imports here)
├── downloads/
│   ├── torrents/
│   │   ├── complete/           # Finished torrent downloads (qBittorrent)
│   │   └── incomplete/         # In-progress torrent downloads
│   └── usenet/
│       ├── complete/           # Finished usenet downloads (SABnzbd)
│       └── incomplete/         # In-progress usenet downloads
├── config/                     # All service configuration and databases
│   ├── jellyfin/
│   ├── sonarr/
│   ├── radarr/
│   └── ...                     # One directory per service
└── backups/                    # Config backups (10 retained, oldest pruned)
```

Sonarr/Radarr download to the `downloads/` staging area, then hardlink or move files to the appropriate library folder (`movies/`, `tv/`, or `anime/`). This means files aren't duplicated — they exist once on disk even though they appear in both the download client and the library.

## How it works

The full request-to-watch pipeline:

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

1. You open Jellyseerr and request a movie or TV show
2. Jellyseerr tells Radarr (for movies) or Sonarr (for TV) to add it
3. Sonarr/Radarr asks Prowlarr to search all configured indexers for the best release matching your quality profile
4. Prowlarr searches torrent sites and usenet indexers, returns results ranked by quality, size, and seeders
5. Sonarr/Radarr picks the best match and sends it to qBittorrent (torrents) or SABnzbd (usenet) for download
6. Once the download completes, Sonarr/Radarr imports it to the correct library folder with proper naming
7. Jellyfin detects the new file via a library scan and makes it available to stream
8. Bazarr detects the new file and fetches subtitles in your configured languages
9. You watch on any Jellyfin client — browser, phone, TV app, etc.
