"""Pinned versions (pins.json, shared with the bash steps until they're
migrated)."""
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
