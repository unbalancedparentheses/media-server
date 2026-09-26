# media-server

One-command self-hosted media server for macOS. Request a movie or TV show and it's automatically downloaded, organized, subtitled, and ready to stream in a Netflix-style app on every screen.

```
bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)
```

Everything runs natively from [Nix](https://nixos.org): no Docker, no VM. Each service is a launchd agent, and `nix run .#uninstall` removes them all.

## How it works

```
You ──> Moonfin / Seerr (request) ──> Sonarr / Radarr ──> Prowlarr ──> Indexers
                                             │                (Byparr for Cloudflare)
                                   qBittorrent / SABnzbd  <── Cleanuparr (clears stuck downloads)
                                             │
                                      download completes
                                             │
                                      Bazarr (subtitles)
                                             │
                                  Jellyfin ──> Moonfin (watch it)
```

1. Browse and request in **Moonfin** (or **Seerr** directly)
2. **Sonarr** (TV/anime) or **Radarr** (movies) searches indexers via **Prowlarr**, skipping junk releases
3. The best match goes to **qBittorrent** or **SABnzbd** (Usenet). If a torrent stalls, **Cleanuparr** removes it and the release is blocklisted so Sonarr/Radarr try another
4. The file is downloaded, renamed, and added to your **Jellyfin** library
5. **Bazarr** fetches subtitles (English + Spanish by default)

## Quick start

Needs macOS and [Nix](https://determinate.systems/nix-installer/) (`curl -fsSL https://install.determinate.systems/nix | sh -s -- install`).

```bash
git clone https://github.com/unbalancedparentheses/media-server.git
cd media-server
nix run .#install        # or ./setup.sh
```

The first run builds a few services that aren't prebuilt for macOS (Seerr, Sonarr) and downloads Byparr's browser, so it takes a while. Setup asks for a Jellyfin and a qBittorrent password (`--yes` generates them), then:

1. Writes each service's config (ports, API keys, passwords) under `~/media/config`
2. Starts every service as a launchd agent (`~/Library/LaunchAgents/org.media-server.*`)
3. Wires them together: download clients, indexers, subtitles, Seerr, Jellyfin libraries
4. Installs the Moonbase plugin so Jellyfin serves the Moonfin web app and connects it to Seerr
5. Blocks junk releases in Sonarr/Radarr
6. Runs the verification checks

Re-running is safe; it only changes what differs.

### Commands

```bash
nix run .#install                 # Full setup (re-run any time)
nix run .#status                  # Is everything running and healthy?
nix run .#logs -- sonarr          # Follow a service's log (~/media/logs)
nix run .#restart [-- sonarr]     # Restart one or all services
nix run .#test                    # Run the verification checks
nix run .#e2e [-- --keep]         # Real download → import → Jellyfin test (see below)
nix run .#backup                  # Back up configs and config.toml
nix run .#restore -- <file>       # Restore a backup
nix run .#update                  # Back up, git pull, re-run setup
nix run .#uninstall               # Stop and remove all services
nix run .#uninstall -- --purge    # ...and delete configs, logs and state
```

## Watching

**[Moonfin](https://github.com/Moonfin-Client/Moonfin-Core)** is the app for everyone who watches: one Netflix-style interface with profiles, a featured banner, and requests built in (via Seerr).

- **Browser:** `http://<mac-ip>:8096/Moonfin/Web/` (installable as an app)
- **Phones, tablets, Apple TV, Android TV, Fire TV:** install Moonfin from the App Store, Google Play or Amazon, and point it at `http://<mac-ip>:8096`
- **LG/Samsung TVs:** Moonfin can be sideloaded; Litefin is another option

Log in with a Jellyfin user. Create one per family member in Jellyfin (Dashboard → Users); Moonfin shows them as profiles.

## Services

| Service | Port | What it does |
|---------|------|-------------|
| **[Jellyfin](https://jellyfin.org)** + **[Moonbase](https://github.com/Moonfin-Client/Plugin)** | 8096 | Media server; Moonbase serves the Moonfin web app and links it to Seerr |
| **[Seerr](https://github.com/seerr-team/seerr)** | 5055 | Browse and request movies and shows |
| **[Sonarr](https://sonarr.tv)** | 8989 | Downloads and organizes TV shows and anime (anime goes to `~/media/anime`) |
| **[Radarr](https://radarr.video)** | 7878 | Downloads and organizes movies |
| **[Prowlarr](https://prowlarr.com)** | 9696 | Manages indexers, syncs them to Sonarr/Radarr |
| **[Bazarr](https://www.bazarr.media)** | 6767 | Subtitles |
| **[qBittorrent](https://www.qbittorrent.org)** | 8081 | Torrent client |
| **[SABnzbd](https://sabnzbd.org)** | 8080 | Usenet client (optional; needs a paid provider) |
| **[Byparr](https://github.com/ThePhaseless/Byparr)** | 8191 (local only) | Gets past Cloudflare on protected indexers |
| **[Unpackerr](https://unpackerr.zip)** | — | Extracts archived downloads for import |
| **[Cleanuparr](https://github.com/Cleanuparr/Cleanuparr)** | 11011 | Removes stalled, metadata-stuck and failed-import downloads; Sonarr/Radarr blocklist them and search again |
| **[nginx](https://nginx.org)** | 80 | Dashboard with live download/calendar widgets |
| **diskwatch** | — | Warns (macOS notification) when the media disk runs low |

The dashboard at `http://localhost` (or `http://<mac-ip>`) links to everything; `admin.html` lists the admin UIs.

## Configuration

`~/media/config.toml` (created from `config.toml.example` on first run). Re-run `nix run .#install` after editing.

- **Credentials:** `[jellyfin]` and `[qbittorrent]`. The Jellyfin login is shared by Seerr, Sonarr, Radarr, Prowlarr, Bazarr, SABnzbd and Cleanuparr. To change a password, edit it here and re-run install: setup remembers the last applied one (in `~/media/.state/credentials.json`) and changes it everywhere, Jellyfin included. Renaming the Jellyfin user has to be done in Jellyfin itself.
- **Quality:** `[quality]` picks the Sonarr/Radarr profiles Seerr requests use (built-in profiles: `HD-1080p`, `Ultra-HD`, …). Anime requests use `sonarr_anime_profile` and go to `~/media/anime`, so they show up in Jellyfin's Anime library.
- **Release filters:** BR-DISK images, known-bad release groups, upscales, extras-only, 3D, and releases tagged with non-English subtitles (VOSTFR, BIG5, CHS, …; common with anime) are scored -10000 in every profile, so they're never grabbed. See [`custom-formats/`](custom-formats/README.md). HEVC (x265) releases get +100, so the smaller file wins when several are acceptable (`prefer_h265 = false` to turn off).
- **Subtitles:** `[subtitles]` languages and providers. The defaults need no account; OpenSubtitles.com, SubDL, Jimaku (anime) and Addic7ed are listed commented out, to enable after adding their login or API key in Bazarr. Most anime releases already carry English subtitles in the file, which the `embeddedsubtitles` provider recognizes.
- **Indexers:** `[[indexers]]` public torrent and anime indexers (Nyaa, SubsPlease, Mikan, Bangumi); `flaresolverr = true` routes one through Byparr. All of them sync to Sonarr and Radarr.
- **Usenet:** public torrent sites are often thin (few or no seeders). Usenet is faster and more reliable, but needs two paid accounts: a **provider** (e.g. Newshosting, Eweka, Frugal Usenet) under `[[usenet_providers]]`, and an **indexer** (e.g. NZBgeek, DrunkenSlug, NZBFinder) under `[[indexers]]` with its API key (see the NZBgeek example in `config.toml.example`). Set `enable = true` on both and re-run install; Sonarr and Radarr then use SABnzbd automatically.
- **Stuck downloads:** `[cleanuparr]` Cleanuparr checks the queue every 5 minutes. A public torrent with no progress for `stalled_strikes` checks (default 6, about 30 minutes), one that never gets metadata, or one that fails to import 3 times is removed, blocklisted, and searched for again. `enabled = false` turns it off.
- **Disk space:** `[disk] warn_free_gb` (default 50) shows a macOS notification when the media disk runs low; below `min_free_gb` (default 10) Sonarr and Radarr stop importing so the disk never fills completely. `nix run .#status` shows free space.
- **Network:** `[network] admin_bind` restricts where the admin UIs listen (`"0.0.0.0"` = every interface, `"127.0.0.1"` = this Mac only); `dashboard_port` moves the dashboard off port 80.

There's no built-in VPN. If you use one, run its Mac app; torrent traffic follows the system connection.

## Access and security

- **Remote access:** if [Tailscale](https://tailscale.com) is installed and signed in, setup publishes Jellyfin/Moonfin (`:8096`), Seerr (`:5055`) and the dashboard over HTTPS on your tailnet. Invite family to the tailnet to watch away from home.
- **Logins:** every admin UI requires a login.
- **qBittorrent:** it skips its login only for requests from this Mac, which is how the dashboard reads it. Requests from the network log in.
- **Dashboard widgets:** they go through nginx, which adds the API keys, but only for the read-only endpoints the widgets use, and only for `GET`.
- **Byparr:** it has no login, so it only listens on `127.0.0.1`.
- **Firewall prompts:** with the macOS firewall on, the first start may ask whether each service may accept incoming connections. Allow Jellyfin and Seerr at least.
- **Sleep and login:** the Mac must be awake to serve. Enable System Settings → Energy → "Prevent automatic sleeping when the display is off". The services are launchd *user* agents, so after a reboot they start when you log in. A locked screen is fine, but for unattended recovery after power loss, turn on automatic login (System Settings → Users & Groups).

## Testing

`nix run .#test` checks every service, connection, folder, profile, login and filter (about 90 checks). Setup runs it at the end and exits non-zero if any check fails.

`nix run .#e2e` proves the automatic path end to end, with no manual step and no public indexers:

1. **Movie:** requests Blender's Creative Commons film *Tears of Steel* in Seerr. Radarr gets a correctly named release, qBittorrent completes it, Radarr imports it, Jellyfin adds it, Bazarr downloads English subtitles, and Seerr marks it available.
2. **TV:** gives Sonarr an episode of the free series *Pioneer One*, which is imported and appears in Jellyfin.

The film (372 MB) is downloaded once from download.blender.org into `~/media/.state/e2e`. The test builds its own torrents with the data already in place, so they complete instantly. Radarr's automatic indexer search is paused for the minute the test runs, so a public release can't race it. Everything it adds is removed afterwards (`--keep` leaves it for inspection). Results are logged to `~/media/logs/e2e-*.log`.

## Updates, backups, uninstall

- **Updates:** versions are pinned in `flake.lock`; Renovate opens a weekly PR to refresh it. `nix run .#update` backs up, pulls this repo and re-runs setup, which points the services at the new versions.
- **Backups:** `nix run .#backup` stops the running services for a consistent snapshot of `~/media/config` and `config.toml`, then starts exactly those again, also if the backup fails or is interrupted. It keeps the last 10 in `~/media/backups`. Backups contain passwords and API keys, so keep a copy on another disk.
- **Uninstall:** `nix run .#uninstall` stops the services and removes their launchd agents, the Tailscale HTTPS entries setup added, and the Nix GC root. `--purge` also deletes configs, logs and state (including Byparr's browser in `~/Library/Caches/invisible-playwright`). Your library, downloads and backups are never deleted. Run `nix-collect-garbage` afterwards to free the Nix store.

## Directory structure

```
~/media/
├── config.toml          # Your settings
├── movies/  tv/  anime/ # Library
├── downloads/           # torrents/ and usenet/
├── config/              # Per-service configs and databases
├── logs/                # One log per service
├── backups/             # Keeps the last 10
└── .state/              # Byparr's venv, Nix GC root
```

## Extending

Some things you could add alongside this stack:

- **[Kavita](https://www.kavitareader.com)** — ebooks, comics, manga
- **[Audiobookshelf](https://www.audiobookshelf.org)** — audiobook and podcast server
- **[Autobrr](https://autobrr.com)** — IRC/RSS automation for private trackers
