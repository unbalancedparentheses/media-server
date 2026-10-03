"""Pinned versions of the Jellyfin plugins (pins.json)."""
from __future__ import annotations

import json
from pathlib import Path

_PINS = json.loads((Path(__file__).with_name("pins.json")).read_text())


class Plugin:
    def __init__(self, data: dict):
        self.commit: str = data["commit"]
        self.version: str = data["version"]
        self.guid: str = data["guid"]
        self.manifest: str = data["manifest"].format(commit=self.commit)


MOONBASE = Plugin(_PINS["moonbase"])
INTRO_SKIPPER = Plugin(_PINS["intro_skipper"])
# The anime quality profile setup creates (a copy of quality.sonarr_anime_profile)
ANIME_PROFILE = "Anime"


def fallback_name(profile: str, resolution: str) -> str:
    """The copy of a profile that also allows a resolution (stuck.py's
    fallback), e.g. Anime (+720p)"""
    return f"{profile} (+{resolution})"


def profile_origin(name: str) -> str:
    """The profile a fallback copy was made from ("Anime (+720p)" → "Anime");
    other names as they are. Setup treats a copy like its origin, so a
    reinstall keeps the Anime copy's anime scores (dub blocking, groups)."""
    for resolution in ("720p", "480p"):
        suffix = f" (+{resolution})"
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name
