# Release filters

setup.sh creates every custom format in this folder in Sonarr (`sonarr/`) or
Radarr (`radarr/`) and scores it -10000 in every quality profile, whose
minimum score is kept at 0 or above, so matching releases are never grabbed.

From [TRaSH Guides](https://github.com/TRaSH-Guides/Guides) (MIT, commit edb8ff81ae63),
copied unchanged from `docs/json/{radarr,sonarr}/cf/`:

- **BR-DISK**: full Blu-ray disc images, which Jellyfin can't play directly
- **LQ / LQ (Release Title)**: groups and tags known for bad or fake releases
- **Upscaled**: SD/HD sources upscaled to a higher resolution
- **Extras**: bonus-content-only releases mislabeled as the movie or episode
- **3D** (Radarr): 3D releases

Ours:

- **Foreign Subtitles** (`foreign-subs.json`): releases tagged with Chinese,
  French, Portuguese, Italian or Russian subtitles (VOSTFR, BIG5, CHS/CHT,
  简繁, Legendado, …), or from Chinese fansub groups (KissSub, LoliHouse,
  ANi, …), common with anime. Releases marked English or multi-sub, or not
  marked at all, are unaffected.

To update the TRaSH ones, copy newer versions of those files from the TRaSH
repo. Any other JSON file in the TRaSH custom-format format dropped here is
applied the same way on the next `nix run .#install`.
