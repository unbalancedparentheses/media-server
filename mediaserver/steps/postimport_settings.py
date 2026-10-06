"""The settings of the checks and fixes after each download (the postimport
service reads them every round, so a change needs no restart)."""
from __future__ import annotations

from mediaserver import common as c
from mediaserver.config import Config
from mediaserver.ui import info, ok


def settings(cfg: Config) -> dict:
    p = cfg.paths
    return {
        "check_downloads": cfg.flag("library.check_downloads", True),
        "stereo_audio": cfg.flag("library.stereo_audio", True),
        "ocr_subtitles": cfg.flag("library.ocr_subtitles", True),
        "drop_picture_subtitles": cfg.flag("library.drop_picture_subtitles", True),
        "default_tracks": cfg.flag("library.default_tracks", True),
        "max_replacements": cfg.get("library.max_replacements", 3),
        "block_dubs": cfg.flag("quality.anime_block_dubs", True),
        "audio_language": cfg.get("playback.audio_language", ""),
        "anime_audio_language": cfg.get("playback.anime_audio_language", "jpn"),
        "anime_dir": str(p.anime),
        "subtitle_languages": cfg.get("subtitles.languages", ["en"]),
        "want": cfg.get("subtitles.want", "first"),
        "min_free_gb": cfg.disk_min_gb,
        "warn_free_gb": cfg.disk_warn_gb,
        "library_dirs": [str(p.movies), str(p.tv), str(p.anime)],
        "search_missing": cfg.flag("library.search_missing", True),
        "fallback_resolution": cfg.get("quality.fallback_resolution", ""),
        "repackage_mp4": cfg.flag("library.repackage_mp4", True),
        "searches_per_hour": cfg.get("library.searches_per_hour", 4),
        "search_upgrades": cfg.flag("library.search_upgrades", True),
    }


def run(cfg: Config) -> None:
    info("Configuring the checks after each download...")
    path = cfg.paths.state / "postimport/settings.json"
    new = settings(cfg)
    if c.read_json(path) != new:
        c.write_json(path, new, mode=0o600)
        ok("Settings written")
    on = [what for flag, what in (("check_downloads", "bad downloads replaced"), ("stereo_audio", "stereo audio added"),
                                  ("ocr_subtitles", "picture subtitles read into text"),
                                  ("default_tracks", "default tracks set to your preferences"),
                                  ("drop_picture_subtitles", "picture subtitles removed when there as text")) if new[flag]]
    ok("After each download: " + (", ".join(on) if on else "nothing (all off in [library])"))
