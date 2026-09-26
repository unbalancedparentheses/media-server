# Release filters

setup.sh creates every custom format in this folder in Sonarr (`sonarr/`) or
Radarr (`radarr/`) and scores it in the quality profiles: -10000 unless the
file sets `"mediaServerScore"`. Profiles' minimum score is kept at 0 or
above, so -10000 means the release is never grabbed. A file with
`"mediaServerProfiles": "anime"` applies only to Sonarr's Anime profile
(which setup creates for anime series); `"standard"` only to the others.

From [TRaSH Guides](https://github.com/TRaSH-Guides/Guides) (MIT, commit edb8ff81ae63),
copied unchanged from `docs/json/{radarr,sonarr}/cf/`:

- **BR-DISK**: full Blu-ray disc images, which Jellyfin can't play directly
- **LQ / LQ (Release Title)**: groups and tags known for bad or fake releases
- **Upscaled**: SD/HD sources upscaled to a higher resolution
- **Extras**: bonus-content-only releases mislabeled as the movie or episode
- **3D** (Radarr): 3D releases
- **Dubs Only** (Sonarr, `dubs-only.json`, Anime profile only): releases with
  only an English dub (tagged Dub/Dubbed, or from dub-only groups such as
  Yameii), so anime comes with Japanese audio. Dual-audio releases pass.
  Turn off with `anime_block_dubs = false`.

Ours:

- **Prefer HEVC** (`prefer-hevc.json`, scored **+100**, not -10000): x265/HEVC
  releases win over otherwise equal ones, for smaller files. Turn off with
  `prefer_h265 = false` under `[quality]`.

- **Prefer English Audio** (`prefer-english.json`, **+50**, TV and movie
  profiles only): releases whose audio includes English win over otherwise
  equal ones. Only a preference: a film with no English release still
  downloads. Turn off with `prefer_english_audio = false`.

- **Foreign Subtitles** (`foreign-subs.json`): releases tagged with Chinese,
  French, Portuguese, Italian or Russian subtitles (VOSTFR, BIG5, CHS/CHT,
  简繁, Legendado, …), or from Chinese fansub groups (KissSub, LoliHouse,
  ANi, …), common with anime. Releases marked English or multi-sub, or not
  marked at all, are unaffected.

To update the TRaSH ones, copy newer versions of those files from the TRaSH
repo. Any other JSON file in the TRaSH custom-format format dropped here is
applied the same way on the next `nix run .#install`.
