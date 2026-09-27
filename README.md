# media-server

A self-hosted media server for macOS, set up with one command. Request a movie or show, and it's found, downloaded, organized, subtitled and ready to watch in a Netflix-style app on your TV, phone and browser.

```
bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)
```

Everything runs natively from [Nix](https://nixos.org): no Docker, no VM. Each service is a macOS launchd agent, and one command removes them all.

**What you get**

- **Watch:** Jellyfin with the Moonfin app (profiles, a featured banner, requests built in), on Apple TV, Android TV, Fire TV, phones, tablets and the browser. "Skip intro" and "Skip credits" buttons, and hardware video conversion on Apple silicon.
- **Request:** browse and request in Moonfin or Seerr. Sonarr (TV, anime) and Radarr (movies) find a good release, qBittorrent downloads it, and it shows up in your library on its own.
- **Subtitles:** Bazarr gets English subtitles, or Spanish when there's no English, from free providers.
- **Sensible defaults:** junk releases (fakes, disc images, upscales, foreign-subtitle releases) are never grabbed, x265 is preferred for smaller files, anime comes with Japanese audio, and stuck downloads are replaced automatically.
- **Looked after:** a disk-space warning, backups and restore, remote access over Tailscale, and about 120 automatic checks after every install.

## Contents

- [Install](#install)
- [First steps](#first-steps)
- [Everyday use](#everyday-use)
- [Changing settings](#changing-settings)
- [Updates, backups, uninstall](#updates-backups-uninstall)
- [Troubleshooting](#troubleshooting)
- [Reference](#reference): commands, configuration, services, security, testing, files

## Install

**You need:** a Mac (Apple silicon recommended) that stays on, about 10 GB free for the apps (they take about 5 GB once built) plus space for your library, and an internet connection.

1. **Install Nix** (skip if you have it):

   ```bash
   curl -fsSL https://install.determinate.systems/nix | sh -s -- install
   ```

   Open a new terminal afterwards.

2. **Install media-server.** Either run the one-liner above, which clones the repo into `~/media-server` and starts setup, or do it by hand:

   ```bash
   git clone https://github.com/unbalancedparentheses/media-server.git
   cd media-server
   nix run .#install
   ```

3. **Choose passwords.** Setup asks for a Jellyfin username and password (also used for every admin page) and a qBittorrent password. Run `nix run .#install -- --yes` instead to have strong ones generated and printed. They're saved in `~/media/config.toml`.

4. **Wait.** The first run takes a while, often half an hour or more: it builds a few services that have no prebuilt macOS version and downloads the browser Byparr uses. Later runs take a minute or two. Setup then:
   - writes each service's settings under `~/media/config`;
   - starts every service, and sets them to start again at login;
   - connects them to each other (downloads, indexers, subtitles, requests, libraries);
   - installs the Moonfin web app and Intro Skipper into Jellyfin;
   - runs its checks, and prints the addresses to open.

   If the macOS firewall asks whether a service may accept connections, allow at least Jellyfin and Seerr.

5. **Keep the Mac awake:** System Settings → Energy → turn on "Prevent automatic sleeping when the display is off".

You can run `nix run .#install` again at any time: it only changes what differs from `config.toml`.

## First steps

1. **Open Moonfin** at `http://localhost:8096/Moonfin/Web/` on the Mac, or `http://<mac-ip>:8096/Moonfin/Web/` from another device. Log in with the Jellyfin username and password you chose.
2. **Request something.** Search for a movie or show in Moonfin (or in Seerr at `http://<mac-ip>:5055`) and press Request. It usually appears in the library within minutes to an hour, depending on how many people are sharing the release.
3. **Install the apps.** Get **Moonfin** from the App Store, Google Play or Amazon on your TV, phone or tablet, enter `http://<mac-ip>:8096` as the server, and log in. LG and Samsung TVs can sideload Moonfin, or use Litefin.
4. **Add your family.** In Jellyfin (`http://<mac-ip>:8096` → Dashboard → Users), create a user per person. Moonfin shows them as profiles, each with its own watch history. They can request too.
5. **Look around the dashboard** at `http://localhost` (or `http://<mac-ip>`): current downloads, the upcoming-episodes calendar, recent requests and links to every service. `http://localhost/admin.html` lists the admin pages.

To find your Mac's address, run `ipconfig getifaddr en0` or look in System Settings → Wi-Fi → Details.

## Everyday use

- **Watching:** open Moonfin. Subtitles are on by default, in English, or Spanish when there's no English. Anime plays in Japanese when the file has it. "Skip intro" and "Skip credits" appear on episodes once Intro Skipper has analyzed them, which it does shortly after they arrive.
- **Requesting:** request in Moonfin or Seerr. Anime goes to its own library with anime-specific rules (Japanese audio, well-rated release groups). Nothing needs to be approved; the request starts right away.
- **What happens on its own:**
  - Sonarr and Radarr search for the best release that passes the filters, and keep watching for anything not found yet.
  - A download that stalls for about 30 minutes, or never starts, is removed and replaced with another release (Cleanuparr).
  - Imported files are renamed (`Show - S01E01 - Title`, `Movie (Year)`) and added to Jellyfin.
  - Bazarr fetches subtitles; new episodes of monitored shows are grabbed as they air.
- **Seeing progress:** the dashboard shows downloads in progress. Seerr shows each request's status. For details, open Sonarr (`:8989`) or Radarr (`:7878`) → Activity.
- **Something is wrong with a file** (wrong language, bad quality): in Sonarr or Radarr, open the title, use the interactive search (the person icon), and pick another release. Unwanted downloads can be removed with "Blocklist release" so they're never picked again.
- **Running out of space:** you get a macOS notification when the disk drops below 50 GB free. Imports stop below 10 GB so the disk never fills completely. Delete things in Sonarr or Radarr, with "Delete files" ticked.
- **Away from home:** install [Tailscale](https://tailscale.com) on the Mac and your devices. Setup then publishes Moonfin, Seerr and the dashboard over HTTPS on your private tailnet (for example `https://<mac-name>.<tailnet>.ts.net:8096`). Invite family to the tailnet to share.

## Changing settings

All settings live in **`~/media/config.toml`**. Edit it, then run:

```bash
nix run .#install
```

Common changes:

| To… | Set in `config.toml` |
|---|---|
| Change the shared password | `[jellyfin] password`. Every service gets it, Jellyfin included. |
| Get 4K instead of 1080p | `[quality] sonarr_profile` / `radarr_profile` = `"Ultra-HD"` |
| Change subtitle languages | `[subtitles] languages = ["en", "es"]`, and `want = "first"` (first one found) or `"all"` |
| Allow English dubs for anime | `[quality] anime_block_dubs = false` |
| Turn subtitles off by default | `[playback] subtitle_mode = "Smart"` (only when the audio isn't your language) or `"None"` |
| Upload faster (seed more) | `[downloads] upload_limit_kib = 0` (no limit) |
| Add a subtitle provider with an account | add its login in Bazarr (Settings → Providers), then uncomment it under `[subtitles] providers` |
| Use Usenet | enable a provider under `[[usenet_providers]]` and an indexer under `[[indexers]]` (see [Usenet](#configuration)) |
| Keep the admin pages on this Mac only | `[network] admin_bind = "127.0.0.1"` |
| Stop publishing over Tailscale | `[network] tailscale_https = false` |

Every setting is explained in [`config.toml.example`](config.toml.example) and under [Configuration](#configuration).

## Updates, backups, uninstall

- **Update:** `nix run .#update` backs up, pulls the latest version of this repo and re-runs setup. Service versions are pinned in `flake.lock`, and Renovate opens a pull request when there are updates.
- **Back up:** `nix run .#backup` saves all settings, accounts and watch history (not the media itself) to `~/media/backups`, keeping the last 10. The services are stopped for a moment so the databases are consistent. Backups contain passwords, so keep a copy on another disk.
- **Restore:** `nix run .#restore -- ~/media/backups/<file>.tar.gz`, then `nix run .#install`. The current settings are kept next to it (`*.pre-restore-<time>`).
- **Uninstall:** `nix run .#uninstall` stops and removes every service. `nix run .#uninstall -- --purge` also deletes settings, logs and state. Your movies, shows, downloads and backups are never deleted. Afterwards, `nix-collect-garbage` frees the disk space used by the apps.

## Troubleshooting

Start with these three commands:

```bash
nix run .#status              # which services are running, and free disk space
nix run .#test                # about 120 checks, with what failed
nix run .#logs -- sonarr      # follow one service's log (also ~/media/logs)
```

- **A request never downloads.** Public torrents are sometimes dead or thinly shared. Check Sonarr/Radarr → Activity: Cleanuparr replaces stalled downloads automatically, and Wanted → Missing lists titles still being searched for. Old or obscure titles may simply have no release that passes the filters. Usenet (paid) helps a lot here.
- **No subtitles.** The free providers don't have everything. Adding a free [OpenSubtitles.com](https://www.opensubtitles.com) account (in Bazarr, then in `config.toml`) covers most gaps. Also check that the file isn't already carrying subtitles in the player's subtitle menu.
- **Can't reach it from another device.** Use the Mac's address, not `localhost`. Check the macOS firewall allowed Jellyfin and Seerr (System Settings → Network → Firewall → Options), and that the Mac isn't asleep.
- **Setup stopped with an error.** It says what failed and where to look. Fix it and run `nix run .#install` again; it picks up where things stand.
- **The dashboard doesn't load on port 80.** Something else uses the port: set `[network] dashboard_port` to another one, such as 8088.
- **After a reboot nothing works.** The services start when you log in. For recovery after a power cut without anyone logging in, turn on automatic login (System Settings → Users & Groups).
- **Starting over.** `nix run .#uninstall -- --purge`, then `nix run .#install`. Your media stays.

## Reference

### Commands

```bash
nix run .#install                 # Set up, or apply config.toml changes (--yes: no prompts)
nix run .#status                  # Services, health, free disk space
nix run .#logs -- <service>       # Follow a service's log
nix run .#restart [-- <service>]  # Restart one or all services
nix run .#test                    # Verification checks
nix run .#e2e [-- --keep]         # End-to-end test (see Testing)
nix run .#unit                    # Failure-path tests (no services needed)
nix run .#backup                  # Back up settings and config.toml
nix run .#restore -- <file>       # Restore a backup
nix run .#update                  # Back up, git pull, re-run setup
nix run .#uninstall [-- --purge]  # Remove the services (--purge: and their data)
```

`./setup.sh` does the same as `nix run .#install` (it runs itself through Nix). Other flags: `--check-config`, `--preflight`, `--dry-run`.

### How it works

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

1. Browse and request in **Moonfin** (or **Seerr** directly).
2. **Sonarr** (TV, anime) or **Radarr** (movies) searches indexers through **Prowlarr**, skipping junk releases.
3. The best match goes to **qBittorrent**, or **SABnzbd** for Usenet. If a torrent stalls, **Cleanuparr** removes it and the release is blocklisted, so Sonarr/Radarr try another.
4. The file is renamed and added to your **Jellyfin** library.
5. **Bazarr** fetches subtitles.

### Services

| Service | Port | What it does |
|---------|------|-------------|
| **[Jellyfin](https://jellyfin.org)** + **[Moonbase](https://github.com/Moonfin-Client/Plugin)** + **[Intro Skipper](https://github.com/intro-skipper/intro-skipper)** | 8096 | Media server, with hardware video conversion (VideoToolbox); Moonbase serves the Moonfin web app and links it to Seerr; Intro Skipper finds intros and credits so players show "Skip" |
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
| **[nginx](https://nginx.org)** | 80 | Dashboard with live download and calendar widgets |
| **diskwatch** | — | Warns (macOS notification) when the media disk runs low |

### Configuration

`~/media/config.toml`, created from [`config.toml.example`](config.toml.example) on the first run. Re-run `nix run .#install` after editing.

- **Credentials:** `[jellyfin]` and `[qbittorrent]`. The Jellyfin login is shared by Seerr, Sonarr, Radarr, Prowlarr, Bazarr, SABnzbd and Cleanuparr. To change a password, edit it here and re-run install: every service gets it, Jellyfin included. Short passwords work everywhere (Cleanuparr and qBittorrent refuse them through their APIs, so setup writes them directly). Setup records each service's login in `~/media/.state/credentials.json` only after logging in with it, so a change that fails part-way is retried on the next run. Renaming the Jellyfin user has to be done in Jellyfin itself.
- **Quality:** `[quality]` picks the Sonarr/Radarr profiles requests use (built-in profiles: `HD-1080p`, `Ultra-HD`, …). Anime requests go to `~/media/anime` (Jellyfin's Anime library) with Sonarr's **Anime** profile, a copy of `sonarr_anime_profile` that setup creates.
- **Audio:** anime comes with Japanese audio and English subtitles: English-dub-only releases are never grabbed for anime (`anime_block_dubs`; dual audio is fine). Movies and TV prefer releases with English audio (`prefer_english_audio`), without blocking films that have no English release.
- **Release filters:** BR-DISK images, known-bad release groups, upscales, extras-only, 3D, and releases tagged with non-English subtitles (VOSTFR, BIG5, CHS, …; common with anime) are scored -10000 in every profile, so they're never grabbed. See [`custom-formats/`](custom-formats/README.md). HEVC (x265) releases get +100, so the smaller file wins when several are acceptable (`prefer_h265 = false` to turn off).
- **Anime release groups:** anime releases are ranked by the TRaSH Guides tiers, Blu-ray groups above web groups (`anime_release_groups = false` turns the ranking off). Raw releases without subtitles and low-quality anime groups are always blocked. Matching goes by release name and group, so it makes Japanese audio and full subtitles very likely, not certain.
- **File names:** `rename_files` (on by default) has Sonarr/Radarr name imports `Show - S01E01 - Title` and `Movie (Year)`, so Jellyfin always gets season and episode numbers (release names like `Show E01` leave it without). Turning it on renames the existing library once; library files are hard links, so seeding is unaffected.
- **Subtitles:** `[subtitles]` languages and providers. With `want = "first"` (default) a title gets the first language found in list order (English, or Spanish when there's no English); `want = "all"` gets every language. The defaults need no account (including SubtitulamosTV and Subtis for Spanish); OpenSubtitles.com, SubDL, Jimaku and AnimeTosho (anime), SubX (Spanish) and Addic7ed are listed commented out, to enable after adding their login or API key in Bazarr. Most anime releases already carry English subtitles in the file, which the `embeddedsubtitles` provider recognizes.
- **Playback:** `[playback]` sets the Jellyfin user's defaults: subtitles always on in English (Jellyfin falls back to another language when a file has no English), and Japanese audio when a file has it, so dual-audio anime plays in Japanese; everything else plays its default track. Setup re-applies these on every install. `hardware_acceleration` (on by default) has Jellyfin convert video with Apple's VideoToolbox when a TV or phone can't play a file directly, including 10-bit HEVC and AV1, instead of the CPU.
- **Skip intro/credits:** Intro Skipper is turned on for the TV and Anime libraries (Jellyfin 12 enables segment providers per library). It analyzes the existing library once after install, then new episodes, and marks intros and credits; Jellyfin players that support media segments show a "Skip" button.
- **Indexers:** `[[indexers]]` public torrent and anime indexers (Nyaa, SubsPlease, Mikan, Bangumi, …); `flaresolverr = true` routes one through Byparr. All of them sync to Sonarr and Radarr. `enable = false` disables one that's already added.
- **Usenet:** public torrent sites are often thin (few or no seeders). Usenet is faster and more reliable, but needs two paid accounts: a **provider** (e.g. Newshosting, Eweka, Frugal Usenet) under `[[usenet_providers]]`, and an **indexer** (e.g. NZBgeek, DrunkenSlug, NZBFinder) under `[[indexers]]` with its API key (see the NZBgeek example in `config.toml.example`). Set `enable = true` on both and re-run install; Sonarr and Radarr then use SABnzbd automatically.
- **Stuck downloads:** `[cleanuparr]` Cleanuparr checks the queue every 5 minutes. A public torrent with no progress for `stalled_strikes` checks (default 6, about 30 minutes), one that never gets metadata, or one that fails to import 3 times is removed, blocklisted, and searched for again. `enabled = false` turns it off.
- **Uploads:** `[downloads] upload_limit_kib` caps qBittorrent's upload speed (default 100 KiB/s; 0 = no limit). `seeding_ratio` and `seeding_time_minutes` decide when finished torrents stop seeding.
- **Disk space:** `[disk] warn_free_gb` (default 50) shows a macOS notification when the media disk runs low; below `min_free_gb` (default 10) Sonarr and Radarr stop importing so the disk never fills completely. `nix run .#status` shows free space.
- **Network:** `[network] admin_bind` restricts where the admin UIs listen (`"0.0.0.0"` = every interface, `"127.0.0.1"` = this Mac only); `dashboard_port` moves the dashboard off port 80; `tailscale_https` publishes over Tailscale.

There's no built-in VPN. If you use one, run its Mac app; torrent traffic follows the system connection.

### Access and security

- **Remote access:** if [Tailscale](https://tailscale.com) is installed and signed in, setup publishes Jellyfin/Moonfin (`:8096`), Seerr (`:5055`) and the dashboard over HTTPS on your tailnet. `tailscale_https = false` takes them down again.
- **Logins:** every admin UI requires a login, checked by `nix run .#test` with the `config.toml` password.
- **Dashboard:** it needs no login and shows downloads, calendars, requests and recently added items (read-only) to anyone who can reach it. The API keys stay in nginx, which only allows the read-only endpoints the widgets use, and only `GET`. With `admin_bind = "127.0.0.1"`, admin cards say "Only on the Mac" when the dashboard is opened from another device.
- **qBittorrent:** it skips its login only for requests from this Mac, which is how setup, the *arr apps and the dashboard reach it.
- **Byparr:** it has no login, so it only listens on `127.0.0.1`.
- **Plugins:** Moonbase and Intro Skipper are pinned to a version, from their manifests at a fixed commit (Jellyfin verifies each download's checksum).
- **Sleep and login:** the services are launchd *user* agents, so they run while you're logged in (a locked screen is fine) and start again at login.

### Testing

- **`nix run .#test`** checks every service, connection, folder, profile, login and filter (about 120 checks). Setup runs it at the end and exits non-zero if any check fails.
- **`nix run .#e2e`** proves the integration end to end, with no manual step and no public indexers:
  1. **Movie:** requests Blender's Creative Commons film *Tears of Steel* in Seerr. Radarr gets a correctly named release, qBittorrent completes it, Radarr imports it, Jellyfin adds it, Bazarr downloads English subtitles, and Seerr marks it available.
  2. **TV:** gives Sonarr an episode of the free series *Pioneer One*, which is imported and appears in Jellyfin.

  The film (372 MB) is downloaded once into `~/media/.state/e2e`. The test builds its own torrents with the data already in place, so they complete instantly (the TV episode is the same file under an episode name). It proves request → grab → import → library → subtitles → Seerr, not indexer searches or downloading from peers. Radarr's automatic search is paused while it runs, with the settings saved first so they're restored even after a crash. The test records what it creates and only ever removes that, refuses to run if the test titles are already in your library, and keeps its record when a service doesn't answer during cleanup, retrying next time. `--keep` leaves everything in place for inspection. Logs: `~/media/logs/e2e-*.log`.
- **`nix run .#unit`** runs the failure-path tests, which need no services and run with setup's own shell settings: interrupted password changes, backup and restore through `setup.sh`, an interrupted anime migration, Cleanuparr's login safeguard, Tailscale route removal, plugin repositories that can't be read, and e2e cleanup and indexer restore when a service doesn't answer. CI runs them on every push.

### Files

```
~/media/
├── config.toml          # Your settings
├── movies/  tv/  anime/ # Library
├── downloads/           # torrents/ and usenet/
├── config/              # Per-service settings and databases
├── logs/                # One log per service
├── backups/             # Keeps the last 10
└── .state/              # Setup's records (applied logins, migration, test
                         # ownership, Tailscale routes), Byparr, Nix GC root
```

### Extending

Some things you could add alongside this stack:

- **[Kavita](https://www.kavitareader.com)**: ebooks, comics, manga
- **[Audiobookshelf](https://www.audiobookshelf.org)**: audiobooks and podcasts
- **[Autobrr](https://autobrr.com)**: IRC/RSS automation for private trackers
