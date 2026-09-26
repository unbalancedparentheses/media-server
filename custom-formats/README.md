# Release filters

setup.sh creates every custom format in this folder in Sonarr (`sonarr/`) or
Radarr (`radarr/`) and scores it in every quality profile: -10000 unless the
file sets `"mediaServerScore"`. Profiles' minimum score is kept at 0 or
above, so -10000 means the release is never grabbed.

From [TRaSH Guides](https://github.com/TRaSH-Guides/Guides) (MIT, commit edb8ff81ae63),
copied unchanged from `docs/json/{radarr,sonarr}/cf/`:

- **BR-DISK**: full Blu-ray disc images, which Jellyfin can't play directly
- **LQ / LQ (Release Title)**: groups and tags known for bad or fake releases
- **Upscaled**: SD/HD sources upscaled to a higher resolution
- **Extras**: bonus-content-only releases mislabeled as the movie or episode
- **3D** (Radarr): 3D releases

Ours:

- **Prefer HEVC** (`prefer-hevc.json`, scored **+100**, not -10000): x265/HEVC
  releases win over otherwise equal ones, for smaller files. Turn off with
  `prefer_h265 = false` under `[quality]`.

- **Foreign Subtitles** (`foreign-subs.json`): releases tagged with Chinese,
  French, Portuguese, Italian or Russian subtitles (VOSTFR, BIG5, CHS/CHT,
  简繁, Legendado, …), or from Chinese fansub groups (KissSub, LoliHouse,
  ANi, …), common with anime. Releases marked English or multi-sub, or not
  marked at all, are unaffected.

To update the TRaSH ones, copy newer versions of those files from the TRaSH
repo. Any other JSON file in the TRaSH custom-format format dropped here is
applied the same way on the next `nix run .#install`.
