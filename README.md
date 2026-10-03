# media-server

A self-hosted media server for macOS, set up with one command. Request a movie or show, and it's found, downloaded, organized, subtitled and ready to watch in a Netflix-style app on your TV, phone and browser.

```
bash <(curl -fsSL https://raw.githubusercontent.com/unbalancedparentheses/media-server/main/install.sh)
```

It needs [Nix](https://determinate.systems/nix-installer/) first (see [Install](#install)). Everything runs natively from Nix: no Docker, no VM. Each service is a macOS launchd agent, and one command removes them all.

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

**You need:** a Mac that stays on (Apple silicon recommended; developed and tested on an M-series Mac with macOS 26), about 10 GB free for the apps (they take about 5 GB once built) plus space for your library, and an internet connection.

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

**All `nix run .#…` commands run from the media-server folder**: `cd ~/media-server` if you used the one-liner, or wherever you cloned it.

## First steps

1. **Open Moonfin** at `http://localhost:8096/Moonfin/Web/` on the Mac, or `http://<mac-ip>:8096/Moonfin/Web/` from another device. Log in with the Jellyfin username and password you chose.
2. **Request something.** Search for a movie or show in Moonfin (or in Seerr at `http://<mac-ip>:5055`) and press Request. It usually appears in the library within minutes to an hour, depending on how many people are sharing the release.
3. **Install the apps.** Get **Moonfin** from the App Store, Google Play or Amazon on your TV, phone or tablet, enter `http://<mac-ip>:8096` as the server, and log in. LG and Samsung TVs can sideload Moonfin, or use Litefin.
4. **Add your family.** In Jellyfin (`http://<mac-ip>:8096` → Dashboard → Users), create a user per person. Moonfin shows them as profiles, each with its own watch history. They can request too, and their requests download right away (`[requests] auto_approve`; set it to `false` to approve them yourself in Seerr). The playback defaults (subtitles on, Japanese audio for anime) are set for your user only; others choose theirs in the player.
5. **Look around the dashboard** at `http://localhost` (or `http://<mac-ip>`). On one page:
   - **Search** (press `/`): your library ("watch") and anything else to request, in one box.
   - **What needs attention**, with what to do, or "Everything is working"; an offline banner when the Mac has no connection.
   - **At a glance**: now playing, transfer speeds, library size, requests, missing items, missing subtitles, indexer health (with the last 24 hours of searches and grabs), CPU and memory, disk, Tailscale.
   - **Now playing**, including *why* Jellyfin is converting a video if it is (for example "picture subtitles drawn into the video"), **service uptime** over 24 hours, and **recently watched**.
   - Downloads in progress, the upcoming calendar, recently added, and recent requests with posters.

   `http://localhost/admin.html` lists the admin pages.

To find your Mac's address, run `ipconfig getifaddr en0` or look in System Settings → Wi-Fi → Details.

## Everyday use

- **Watching:** open Moonfin. Subtitles are on by default, in English, or Spanish when there's no English. Anime plays in Japanese when the file has it. "Skip intro" and "Skip credits" appear on episodes once Intro Skipper has analyzed them, which it does shortly after they arrive.
- **Requesting:** request in Moonfin or Seerr. Anime goes to its own library with anime-specific rules (Japanese audio, well-rated release groups). Requests start right away, yours and (by default) everyone else's.
- **What happens on its own:**
  - Sonarr and Radarr search for the best release that passes the filters, and keep watching for anything not found yet.
  - A download that stalls for about 30 minutes, or never starts, is removed and replaced with another release (Cleanuparr).
  - Imported files are renamed (`Show - S01E01 - Title`, `Movie (Year)`) and added to Jellyfin.
  - Each new file is checked. One that won't play, is far too short (a sample or a fake) or is a dub of Japanese anime is deleted and its release blocklisted, and another is fetched. You're notified only if nothing better turns up.
  - Files are made to play directly in browsers: a stereo track is added when the audio is only Dolby/DTS, and Blu-ray picture subtitles are read into text. Files already in your library get the same, a few at a time.
  - Bazarr fetches subtitles; new episodes of monitored shows are grabbed as they air.
- **Seeing progress:** the dashboard shows downloads in progress. Seerr shows each request's status. For details, open Sonarr (`:8989`) or Radarr (`:7878`) → Activity.
- **Something is wrong with a file** that the checks didn't catch (bad quality, wrong edition): in Sonarr or Radarr, open the title, use the interactive search (the person icon), and pick another release. Unwanted downloads can be removed with "Blocklist release" so they're never picked again.
- **Deleting something:** in Sonarr (shows) or Radarr (movies), open the title → Delete, with "Delete files" ticked. Jellyfin and Seerr follow. Deleting only in Jellyfin would leave Sonarr/Radarr to download it again.
- **Running out of space:** you get a macOS notification when the disk drops below 50 GB free. Sonarr and Radarr stop importing below 10 GB free; downloads already in progress can still use more, so free space before it gets that far. Delete things in Sonarr or Radarr, with "Delete files" ticked.
- **Offline (on a flight):** everything plays from the Mac itself without internet. Open **`http://localhost:8096/web/`**, Jellyfin's own player (verified offline: login, libraries, artwork, playback, subtitles). Moonfin's web app is patched so it doesn't need the internet either (setup serves the renderer and player files it would download), but use the Jellyfin player if it doesn't load. Requests, new downloads, subtitle searches and Seerr need a connection. Going offline doesn't cost you downloads: netwatch pauses Cleanuparr while there's no connection, and when you're back it re-tests the indexers and subtitle providers so downloads resume on their own.
- **On a phone or tablet, offline:** download before you leave. In the Moonfin app (iPhone, iPad, Android), open a movie or episode → Download; choose a smaller quality to save space (the Mac converts it while downloading, so do it at home on Wi-Fi). [Infuse](https://firecore.com/infuse) (Apple devices) and [Findroid](https://github.com/jarnedemeulemeester/Findroid) (Android) can download from Jellyfin too. Downloads are allowed for every user.
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
| Approve family members' requests yourself | `[requests] auto_approve = false` |

Every setting is explained in [`config.toml.example`](config.toml.example) and under [Configuration](#configuration).

## Updates, backups, uninstall

- **Update:** `nix run .#update` backs up, pulls the latest version of this repo and re-runs setup. Service versions are pinned in `flake.lock`, and Renovate opens a pull request when there are updates, once the [Renovate app](https://github.com/apps/renovate) is installed on the repository.
- **Back up:** `nix run .#backup` saves all settings, accounts and watch history (not the media itself) to `~/media/backups`, keeping the last 10. The services are stopped for a moment so the databases are consistent. Backups contain passwords, so keep a copy on another disk.
- **Restore:** `nix run .#restore -- ~/media/backups/<file>.tar.gz`, then `nix run .#install`. The current settings are kept next to it (`*.pre-restore-<time>`).
- **Uninstall:** `nix run .#uninstall` stops and removes every service. `nix run .#uninstall -- --purge` also deletes settings, logs and state. Your movies, shows, downloads and backups are never deleted. Afterwards, `nix-collect-garbage` frees the disk space used by the apps.

## Troubleshooting

Start with these:

```bash
nix run .#open                # open the dashboard (the first install does it for you)
nix run .#doctor              # is it working? findings with evidence and what to do
nix run .#status              # which services are running, and free disk space
nix run .#test                # about 120 checks that the setup is correct
nix run .#logs -- sonarr      # follow one service's log (also ~/media/logs)
```

`doctor` is read-only. It reports what it observes (indexers switched off after failures, downloads that haven't moved in a day, what Sonarr/Radarr's health checks say, titles still without subtitles, anime made in Japanese whose files have no Japanese audio, subtitles only available as pictures, downloads rejected by the checks after import, disk space, work an interrupted operation left unfinished) and suggests what to do; it never changes anything.

- **A request never downloads.** Public torrents are sometimes dead or thinly shared. Check Sonarr/Radarr → Activity: Cleanuparr replaces stalled downloads automatically, and Wanted → Missing lists titles still being searched for. Old or obscure titles may simply have no release that passes the filters. Usenet (paid) helps a lot here.
- **No subtitles.** The free providers don't have everything. Adding a free [OpenSubtitles.com](https://www.opensubtitles.com) account (in Bazarr, then in `config.toml`) covers most gaps. Also check that the file isn't already carrying subtitles in the player's subtitle menu.
- **Playback in Brave/Chrome stops at the same minute.** With `allow_remux = false` (the default) browsers get the video converted with regular keyframes; if you turned it on, turn it off again. Once a file has its stereo track (`[library] stereo_audio`), browsers usually play it directly, with no conversion at all; the dashboard's Now Playing says which. Safari plays most files directly.
- **Can't reach it from another device.** Use the Mac's address, not `localhost`. Check the macOS firewall allowed Jellyfin and Seerr (System Settings → Network → Firewall → Options), and that the Mac isn't asleep.
- **Setup stopped with an error.** It says what failed and where to look. Fix it and run `nix run .#install` again; it picks up where things stand.
- **The dashboard doesn't load on port 80.** Something else uses the port: set `[network] dashboard_port` to another one, such as 8088.
- **After a reboot nothing works.** The services start when you log in. For recovery after a power cut without anyone logging in, turn on automatic login (System Settings → Users & Groups).
- **Forgot a password.** They're in `~/media/config.toml` (`[jellyfin]` is the login for Moonfin, Jellyfin and every admin page; `[qbittorrent]` for qBittorrent).
- **Starting over.** `nix run .#uninstall -- --purge`, then `nix run .#install`. Your media stays.

## Reference

### Commands

```bash
nix run .#install                 # Set up, or apply config.toml changes (--yes: no prompts)
nix run .#status                  # Services, health, free disk space
nix run .#doctor                  # Is it working? Findings and what to do (read-only)
nix run .#logs -- <service>       # Follow a service's log
nix run .#restart [-- <service>]  # Restart one or all services
nix run .#test                    # Verification checks
nix run .#e2e [-- --keep]         # End-to-end test (see Testing)
nix run .#unit                    # Unit and failure-path tests (no services needed)
nix run .#backup                  # Back up settings and config.toml
nix run .#restore -- <file>       # Restore a backup
nix run .#update                  # Back up, git pull, re-run setup
nix run .#uninstall [-- --purge]  # Remove the services (--purge: and their data)
```

`./setup.sh` does the same as `nix run .#install` (it runs itself through Nix). Other flags: `--check-config`, `--preflight`, `--dry-run`.

Only one operation that changes the services (install, update, restore, backup, uninstall, restart, e2e) runs at a time; a second one says what's running and stops. `config.toml` is checked in full before anything changes: unknown or misspelled settings (with a "did you mean"), wrong types and out-of-range values are all listed, and nothing is touched until they're fixed. Each install ends with how long it took and its slowest steps.

### How it works

```
You ──> Moonfin / Seerr (request) ──> Sonarr / Radarr ──> Prowlarr ──> Indexers
                                             │                (Byparr for Cloudflare)
                                   qBittorrent / SABnzbd  <── Cleanuparr (clears stuck downloads)
                                             │
                                      download completes
                                             │
                          postimport (checks the file, fixes audio/subtitles)
                                             │
                                      Bazarr (subtitles)
                                             │
                                  Jellyfin ──> Moonfin (watch it)
```

1. Browse and request in **Moonfin** (or **Seerr** directly).
2. **Sonarr** (TV, anime) or **Radarr** (movies) searches indexers through **Prowlarr**, skipping junk releases.
3. The best match goes to **qBittorrent**, or **SABnzbd** for Usenet. If a torrent stalls, **Cleanuparr** removes it and the release is blocklisted, so Sonarr/Radarr try another.
4. The file is renamed and added to your **Jellyfin** library.
5. **postimport** checks the file (replacing it if it's broken, a fake or a dub) and makes it play directly everywhere.
6. **Bazarr** fetches subtitles.

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
| **[nginx](https://nginx.org)** | 80 | The dashboard (see below) |
| **dashstatus** | — | Gathers the dashboard's live data every 15 s into one file the page reads; the API keys stay on the server |
| **postimport** | — | Checks each imported file and fixes it so it plays directly (see `[library]`): rejects ones that won't play, are far too short or are dubbed anime (Sonarr/Radarr blocklist the release and fetch another); adds a stereo AAC track (Apple's encoder) when the audio is only Dolby/DTS/TrueHD; reads Blu-ray picture subtitles into a `.srt` with OCR ([pgsrip](https://github.com/ratoaq2/pgsrip), Tesseract). Rewrites go to a new file that's checked before replacing the old one. Low priority, so it doesn't slow anything. A file still seeding is only rewritten when there's plenty of space, since the torrent keeps its own copy until it's done. Log: `~/media/logs/postimport.log` |
| **diskwatch** | — | Warns (macOS notification) when the media disk runs low |
| **netwatch** | — | Checks the connection every minute (offline after 3 failed checks in a row) and keeps things in line each time, so a step that fails is retried: offline, Cleanuparr's queue cleaner is paused (it would otherwise remove every download as stalled); online, it's whatever `cleanuparr.enabled` says. Coming back also re-tests the indexers (Prowlarr backs off for up to a day after failures) and clears Bazarr's provider throttling. Nothing changes before its first successful check. Log: `~/media/logs/netwatch.log` |

### Configuration

`~/media/config.toml`, created from [`config.toml.example`](config.toml.example) on the first run (every setting is commented there too). Re-run `nix run .#install` after editing.

**General**

| Setting | Default | What it does |
|---|---|---|
| `timezone` | `"America/New_York"` | Time zone for the services' schedules and logs. Must come before the first `[section]`. |

**`[jellyfin]` and `[qbittorrent]`: logins**

| Setting | Default | What it does |
|---|---|---|
| `jellyfin.username` / `password` | `admin` / asked at setup | The login for Moonfin and Jellyfin, shared by Seerr, Sonarr, Radarr, Prowlarr, Bazarr, SABnzbd and Cleanuparr |
| `qbittorrent.username` / `password` | `admin` / asked at setup | qBittorrent's own login |

To change a password, edit it here and re-run install: every service gets it, Jellyfin included. Short passwords work everywhere (Cleanuparr and qBittorrent refuse them through their APIs, so setup writes them directly). Each service's login is recorded in `~/media/.state/credentials.json` only after setup has logged in with it, so a change that fails part-way is retried next time. Renaming the Jellyfin user has to be done in Jellyfin itself.

**`[downloads]`**

| Setting | Default | What it does |
|---|---|---|
| `seeding_ratio` | `2` | Stop seeding a finished torrent after uploading this many times its size… |
| `seeding_time_minutes` | `10080` (7 days) | …or after this long, whichever comes first |
| `upload_limit_kib` | `100` | qBittorrent's upload speed cap in KiB/s (`0` = no limit) |

**`[subtitles]`**

| Setting | Default | What it does |
|---|---|---|
| `languages` | `["en", "es"]` | Subtitle languages, in order of preference |
| `want` | `"first"` | `"first"`: English (the first language); until English is found the others are fetched too as a fallback, and Bazarr keeps looking for English. `"all"`: every language. |
| `providers` | 8 free providers | Where Bazarr looks. The defaults need no account (including SubtitulamosTV and Subtis for Spanish, and `embeddedsubtitles` for subtitles already inside the file). OpenSubtitles.com, SubDL, Jimaku and AnimeTosho (anime), SubX (Spanish) and Addic7ed are listed commented out: add their login or API key in Bazarr first, then uncomment. |

Picture-based subtitles inside files (Blu-ray PGS, DVD VobSub) don't count: browsers can't show them, so Jellyfin would burn them into the video, re-encoding every frame on the CPU. Bazarr fetches a text subtitle for those files instead, which Jellyfin picks first.

**`[quality]`**

| Setting | Default | What it does |
|---|---|---|
| `sonarr_profile` / `radarr_profile` | `"HD-1080p"` | Quality profile for TV and movie requests (built in: `SD`, `HD-720p`, `HD-1080p`, `Ultra-HD`, `HD - 720p/1080p`, `Any`) |
| `sonarr_anime_profile` | `"HD-1080p"` | Base of Sonarr's **Anime** profile, which setup creates for anime: anime requests go to `~/media/anime` with it |
| `prefer_h265` | `true` | Prefer x265/HEVC releases (+100), so the smaller file wins when several are acceptable |
| `prefer_english_audio` | `true` | Movies and TV: prefer releases with English audio (+50). A preference, not a block. |
| `anime_block_dubs` | `true` | Anime: never grab English-dub-only releases (dual audio is fine) |
| `anime_release_groups` | `true` | Anime: rank releases by the TRaSH Guides tiers, Blu-ray groups above web groups |
| `fallback_resolution` | `""` | Off unless set (`"720p"` or `"480p"`): a title stuck for a week whose releases are all turned down only for their quality gets a copy of its profile that also allows this resolution; nothing else in the profile changes (Sonarr sets profiles per series, so for a series it's the whole series) |
| `rename_files` | `true` | Name imports `Show - S01E01 - Title` and `Movie (Year)`, so Jellyfin always gets season and episode numbers. Turning it on renames the existing library once; library files are hard links, so seeding is unaffected. |

Always on: BR-DISK images, known-bad groups, upscales, extras-only, 3D, and releases tagged with non-English subtitles (VOSTFR, BIG5, CHS, …) are scored -10000, so they're never grabbed; for anime, raw releases without subtitles and low-quality groups too. See [`custom-formats/`](custom-formats/README.md). Release names and groups make Japanese audio and full subtitles very likely for anime, not certain.

**`[playback]`: Jellyfin defaults for your user**

| Setting | Default | What it does |
|---|---|---|
| `subtitle_mode` | `"Always"` | `"Always"`, `"Smart"` (only when the audio isn't your language), `"OnlyForced"`, `"Default"` or `"None"` |
| `subtitle_language` | `"eng"` | Preferred subtitle language (Jellyfin falls back to another when a file has none in it) |
| `audio_language` | `""` | Audio for everything but anime: `""` = the file's own default track (usually the original language); a language (e.g. `"eng"`) = that track when the file has it |
| `anime_audio_language` | `"jpn"` | Audio for anime (the anime library) when the file has it: dual-audio anime plays in Japanese |
| `allow_remux` | `false` | When a device can't play a file directly, convert the video (hardware, a keyframe every 3 s) instead of copying it into a stream. Copied Blu-ray video can have keyframes 10 s apart, and browser players stall on it (playback stopping at the same minute). Devices that play the file directly (Safari, most TV apps) aren't affected. |
| `hardware_acceleration` | `true` | Convert video with Apple's VideoToolbox (H.264, HEVC, VP9, AV1 including 10-bit; HDR tone mapping) when a device can't play a file directly |

Setup re-applies these on every install. Intro Skipper is always on for the TV and Anime libraries.

**`[requests]`**

| Setting | Default | What it does |
|---|---|---|
| `auto_approve` | `true` | Requests from other users (family members with their own Jellyfin account) download right away; `false`: they wait for an admin's approval in Seerr. Admins' requests are always approved. |

**`[network]`**

| Setting | Default | What it does |
|---|---|---|
| `admin_bind` | `"0.0.0.0"` | Where the admin pages listen: `"0.0.0.0"` (every interface) or `"127.0.0.1"` (this Mac only). Moonfin, Seerr and the dashboard are always reachable. |
| `dashboard_port` | `80` | The dashboard's port |
| `tailscale_https` | `true` | If Tailscale is signed in, publish Moonfin, Seerr and the dashboard over HTTPS on your tailnet; `false` takes them down |

There's no built-in VPN. If you use one, run its Mac app; torrent traffic follows the system connection.

**`[disk]`**

| Setting | Default | What it does |
|---|---|---|
| `warn_free_gb` | `50` | Show a macOS notification (at most every 6 hours) when free space drops below this |
| `min_free_gb` | `10` | Sonarr and Radarr stop importing below this (downloads in progress can still use more space) |

**`[cleanuparr]`: stuck downloads**

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `true` | Check the queue every 5 minutes; remove public torrents that stalled, never got metadata (3 checks) or failed to import (3 times), blocklist them and search again |
| `stalled_strikes` | `6` | Checks without progress before a torrent counts as stalled (6 × 5 minutes ≈ 30 minutes; at least 3) |

**`[library]`: checks after each download**

| Setting | Default | What it does |
|---|---|---|
| `check_downloads` | `true` | Reject a new file that won't play, is under half the expected length (a sample or a fake), or is anime made in Japanese without Japanese audio (with `quality.anime_block_dubs`). Sonarr/Radarr blocklist the release and fetch another |
| `max_replacements` | `3` | Releases rejected for the same episode or movie before the file is kept anyway (you get a notification) |
| `search_missing` | `true` | Search again for what's still missing (Sonarr and Radarr only search once, when it's added): at most 3 search commands an hour (a season search counts as one, though Sonarr may ask the indexers several times for it), each title waiting longer each time (1 h up to a day), within your profiles and filters. A title missing for a day also gets up to 2 look-only searches an hour, to show on the dashboard why nothing was taken (nothing found, quality, language, size, filters, no seeders, indexers down). A title that's downloading keeps its schedule. `false`: none of it, no indexer queries at all |
| `stereo_audio` | `true` | Add a stereo AAC track, first, when the audio is only in formats browsers can't play (Dolby Digital/Atmos, DTS, TrueHD); the original tracks stay |
| `ocr_subtitles` | `true` | Read picture subtitles (Blu-ray PGS) into a text `.srt` next to the file, for the `subtitles.languages` that have no text subtitles yet (only the first with `want = "first"`) |
| `drop_picture_subtitles` | `true` | Remove a picture subtitle track (Blu-ray PGS) when its language is also there as text. Moonfin picks picture subtitles over text ones whatever the file says, and Jellyfin has to burn them into the video (two subtitles at once, heavy on the CPU). Picture tracks in other languages, and forced ones, stay |
| `default_tracks` | `true` | Each file's default tracks follow your preferences, so every player picks them: the audio above, and subtitles in the first of `subtitles.languages` that's there as text (English, else Spanish). Text wins over picture subtitles in the same language (a default picture track would be burned into the video while the player shows the text one) |

By hand: `nix run .#postimport -- --check FILE` says what it would do; `--fix FILE` does it now.

**`[[indexers]]`: where to search**

Each entry is one Prowlarr indexer, synced to Sonarr and Radarr:

```toml
[[indexers]]
name = "Nyaa.si"            # shown in Prowlarr
definitionName = "nyaasi"   # Prowlarr's indexer definition
enable = true               # false disables one that's already added
flaresolverr = false        # true: go through Byparr (Cloudflare-protected sites)
fields = { apiKey = "…" }   # for indexers that need an account or API key
```

The defaults are 15 public ones: general (1337x, EZTV, The Pirate Bay, YTS, Knaben, LimeTorrents, MegaPeer, KickassTorrents, Uindex) and anime (Nyaa, SubsPlease, Mikan, Bangumi Moe, Tokyo Toshokan, nekoBT). NZBgeek is included, disabled, as a Usenet example.

**`[[usenet_providers]]`: Usenet (optional, paid)**

Public torrents are often thinly shared; Usenet is faster and more reliable, but needs two paid accounts: a **provider** here (e.g. Newshosting, Eweka, Frugal Usenet) and an **indexer** under `[[indexers]]` with its API key (e.g. NZBgeek, DrunkenSlug, NZBFinder). Fill in `host`, `port`, `ssl`, `username`, `password`, `connections`, set `enable = true` on both, and re-run install; Sonarr and Radarr then use SABnzbd automatically. `enable = false` switches a provider off again.

### Access and security

- **Remote access:** if [Tailscale](https://tailscale.com) is installed and signed in, setup publishes Jellyfin/Moonfin (`:8096`), Seerr (`:5055`) and the dashboard over HTTPS on your tailnet. `tailscale_https = false` takes them down again.
- **Logins:** every admin UI requires a login. `nix run .#test` logs in to each with the `config.toml` password, except qBittorrent, which skips its login for this Mac: its stored password hash is checked instead.
- **Dashboard:** it needs no login and shows downloads, calendars, requests and recently added items to anyone who can reach it. The API keys stay in nginx, which only allows the read-only endpoints the widgets use, and only `GET`. The one change it can make is the speed limit (the ⇅ button: qBittorrent's alternative speed limits and SABnzbd's limit; "Normal limits" goes back to the ones in `config.toml`, which aren't touched): that endpoint answers only this Mac, private home-network addresses and Tailscale, and only JSON requests with the dashboard's header, so another website can't make your browser change it. That isn't a login: anyone on your home network or tailnet can change the speed limit. With `admin_bind = "127.0.0.1"`, admin cards say "Only on the Mac" when the dashboard is opened from another device.
- **qBittorrent:** it skips its login only for requests from this Mac, which is how setup, the *arr apps and the dashboard reach it.
- **Byparr:** it has no login, so it only listens on `127.0.0.1`. Its Firefox runs truly headless (no window), so solving Cloudflare challenges doesn't switch Spaces or move your windows.
- **Plugins:** Moonbase and Intro Skipper are pinned to a version, from their manifests at a fixed commit (Jellyfin verifies each download's checksum).
- **Sleep and login:** the services are launchd *user* agents, so they run while you're logged in (a locked screen is fine) and start again at login.

### Testing

- **`nix run .#test`** checks every service, connection, folder, profile, login and filter (about 120 checks). Setup runs it at the end and exits non-zero if any check fails.
- **`nix run .#e2e`** proves the integration end to end, with no manual step and no public indexers:
  1. **Movie:** requests Blender's Creative Commons film *Tears of Steel* in Seerr. Radarr gets a correctly named release, qBittorrent completes it, Radarr imports it, Jellyfin adds it, Bazarr downloads English subtitles, and Seerr marks it available.
  2. **TV:** gives Sonarr an episode of the free series *Pioneer One*, which is imported and appears in Jellyfin.

  The film (372 MB) is downloaded once into `~/media/.state/e2e`. The test builds its own torrents with the data already in place, so they complete instantly (the TV episode is the same file under an episode name). It proves request → grab → import → library → subtitles → Seerr, not indexer searches or downloading from peers. Radarr's automatic search is paused while it runs, with the settings saved first so they're restored even after a crash. The test records what it creates and only ever removes that, refuses to run if the test titles are already in your library, and keeps its record when a service doesn't answer during cleanup, retrying next time. `--keep` leaves everything in place for inspection. Logs: `~/media/logs/e2e-*.log`.
- **`nix run .#unit`** runs about 310 tests that need no real services: every setup step against fake versions of the services (a fresh install, a re-run that changes nothing, a changed config), the post-import checks on real media files, the dashboard's data, and the failure paths: interrupted password changes, backup and restore through `setup.sh`, an older install's unmerged anime Sonarr, Cleanuparr's login safeguard, Tailscale route removal, plugin repositories that can't be read, the operation lock, and the whole e2e test, including its cleanup and indexer restore when a service doesn't answer. It fails if test coverage drops below 80%. CI runs it on every push.
- **Fresh install** ([`.github/workflows/fresh-install.yml`](.github/workflows/fresh-install.yml)), on every push to main on a clean GitHub Mac (only the Nix store is reused between runs, so the services aren't rebuilt each time): install → checks → a re-run that must change nothing → a config change → an invalid config that must be rejected → e2e → doctor → backup and restore → uninstall, which must leave nothing behind.

### Files

```
~/media/
├── config.toml          # Your settings
├── movies/  tv/  anime/ # Library
├── downloads/           # torrents/ and usenet/
├── config/              # Per-service settings and databases
├── logs/                # One log per service
├── backups/             # Keeps the last 10
└── .state/              # Setup's records (applied logins, renames, test
                         # ownership, Tailscale routes), Byparr, Nix GC root
```

### Legal

This sets up software; what it downloads is up to you. Only download and share content you have the right to, and check the rules where you live. The automatic test uses Creative Commons works (*Tears of Steel*, *Pioneer One*).

### Extending

Some things you could add alongside this stack:

- **[Kavita](https://www.kavitareader.com)**: ebooks, comics, manga
- **[Audiobookshelf](https://www.audiobookshelf.org)**: audiobooks and podcasts
- **[Autobrr](https://autobrr.com)**: IRC/RSS automation for private trackers
