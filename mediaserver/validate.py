"""Validate config.toml before setup changes anything.

Rejects unknown keys (with a "did you mean" suggestion), wrong types,
out-of-range values and incompatible combinations, and prints every problem
at once. Free-form keys are allowed only where they're meant to be: an
indexer's `fields` (passed to Prowlarr as-is). Exit status 1 on problems.
"""
from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

from mediaserver.config import default_paths, load_toml

BUILTIN_PROFILES = ["Any", "SD", "HD-720p", "HD-1080p", "Ultra-HD", "HD - 720p/1080p"]


def string(nonempty=True):
    def check(v):
        if not isinstance(v, str):
            return "must be text (in quotes)"
        if nonempty and not v.strip():
            return "must not be empty"
    return check


def boolean(v):
    if not isinstance(v, bool):
        return "must be true or false (no quotes)"


def integer(lo=None, hi=None):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int):
            return "must be a whole number (no quotes)"
        if lo is not None and v < lo:
            return f"must be at least {lo}"
        if hi is not None and v > hi:
            return f"must be at most {hi}"
    return check


def number(lo=None):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "must be a number (no quotes)"
        if lo is not None and v < lo:
            return f"must be at least {lo}"
    return check


def one_of(*choices):
    def check(v):
        if v not in choices:
            return "must be one of: " + ", ".join(json.dumps(c) for c in choices)
    return check


def list_of(item, nonempty=False):
    def check(v):
        if not isinstance(v, list):
            return "must be a list, like [\"a\", \"b\"]"
        if nonempty and not v:
            return "must not be empty"
        for x in v:
            problem = item(x)
            if problem:
                return f"entry {json.dumps(x)} {problem}"
    return check


def free_table(v):
    if not isinstance(v, dict):
        return "must be a table, like { apiKey = \"…\" }"


# section → key → check; REQUIRED lists keys that must be present
SCHEMA = {
    "": {"timezone": string()},
    "jellyfin": {"username": string(), "password": string()},
    # timezone here is accepted from older example files (setup warns)
    "qbittorrent": {"username": string(), "password": string(), "timezone": string()},
    "downloads": {
        "seeding_ratio": number(lo=0),
        "seeding_time_minutes": integer(lo=0),
        "upload_limit_kib": integer(lo=0),
    },
    "subtitles": {
        "languages": list_of(string(), nonempty=True),
        "want": one_of("first", "all"),
        "providers": list_of(string()),
    },
    "quality": {
        "sonarr_profile": one_of(*BUILTIN_PROFILES),
        "sonarr_anime_profile": one_of(*BUILTIN_PROFILES),
        "radarr_profile": one_of(*BUILTIN_PROFILES),
        "prefer_h265": boolean,
        "rename_files": boolean,
        "prefer_english_audio": boolean,
        "anime_block_dubs": boolean,
        "anime_release_groups": boolean,
        "fallback_profile": one_of("", *BUILTIN_PROFILES),
    },
    "playback": {
        "subtitle_mode": one_of("Always", "Smart", "OnlyForced", "Default", "None"),
        "subtitle_language": string(),
        "audio_language": string(nonempty=False),
        "anime_audio_language": string(nonempty=False),
        "hardware_acceleration": boolean,
        "allow_remux": boolean,
    },
    "requests": {"auto_approve": boolean},
    "network": {
        "admin_bind": one_of("0.0.0.0", "127.0.0.1"),
        "dashboard_port": integer(lo=1, hi=65535),
        "tailscale_https": boolean,
    },
    "disk": {"warn_free_gb": integer(lo=0), "min_free_gb": integer(lo=0)},
    "cleanuparr": {"enabled": boolean, "stalled_strikes": integer(lo=3)},
    "library": {
        "check_downloads": boolean,
        "stereo_audio": boolean,
        "ocr_subtitles": boolean,
        "drop_picture_subtitles": boolean,
        "default_tracks": boolean,
        "max_replacements": integer(lo=0, hi=20),
        "search_missing": boolean,
    },
}
INDEXER = {
    "name": string(),
    "definitionName": string(),
    "enable": boolean,
    "flaresolverr": boolean,
    "fields": free_table,
}
USENET = {
    "name": string(),
    "enable": boolean,
    "host": string(nonempty=False),
    "port": integer(lo=1, hi=65535),
    "ssl": boolean,
    "username": string(nonempty=False),
    "password": string(nonempty=False),
    "connections": integer(lo=1),
}
REQUIRED = {
    "": ["timezone"],
    "jellyfin": ["username", "password"],
    "qbittorrent": ["username", "password"],
    "quality": ["sonarr_profile", "sonarr_anime_profile", "radarr_profile"],
}
ARRAYS = {"indexers": (INDEXER, ["name", "definitionName"]), "usenet_providers": (USENET, ["name"])}


def suggest(key, known):
    close = difflib.get_close_matches(key, known, n=1, cutoff=0.6)
    return f' (did you mean "{close[0]}"?)' if close else ""


def check_table(where, table, schema, required, problems):
    for key, value in table.items():
        if key not in schema:
            problems.append(f'{where}{key}: unknown setting{suggest(key, list(schema))}')
            continue
        problem = schema[key](value)
        if problem:
            problems.append(f"{where}{key} {problem}")
    for key in required:
        if key not in table:
            problems.append(f"{where}{key}: missing")


def validate(config: dict) -> list[str]:
    """Every problem with config.toml (parsed), all at once"""
    problems: list[str] = []
    known_sections = [s for s in SCHEMA if s] + list(ARRAYS)
    top = {k: v for k, v in config.items() if not isinstance(v, (dict, list))}
    check_table("", top, SCHEMA[""], REQUIRED[""], problems)
    for section, value in config.items():
        if not isinstance(value, (dict, list)):
            continue
        if section in ARRAYS:
            schema, required = ARRAYS[section]
            if not isinstance(value, list):
                problems.append(f"[[{section}]] must be written as [[{section}]] entries")
                continue
            for i, entry in enumerate(value):
                label = entry.get("name", i + 1) if isinstance(entry, dict) else i + 1
                check_table(f"[[{section}]] {label}: ", entry, schema, required, problems)
        elif section in SCHEMA and section:
            check_table(f"{section}.", value, SCHEMA[section], REQUIRED.get(section, []), problems)
        else:
            problems.append(f"[{section}]: unknown section{suggest(section, known_sections)}")
    for section in REQUIRED:
        if section and section not in config:
            problems.append(f"[{section}]: missing section")

    # Combinations
    disk = config.get("disk", {})
    if isinstance(disk.get("warn_free_gb"), int) and isinstance(disk.get("min_free_gb"), int) \
            and disk["warn_free_gb"] < disk["min_free_gb"]:
        problems.append("disk.warn_free_gb must be at least disk.min_free_gb (warn before imports stop)")
    for section in ("jellyfin", "qbittorrent"):
        if config.get(section, {}).get("password") == "changeme":
            problems.append(f"{section}.password is still the example's \"changeme\"; set a real password")
    names = [e.get("name") for e in config.get("indexers", []) if isinstance(e, dict)]
    for dup in sorted({n for n in names if names.count(n) > 1 and n}):
        problems.append(f'[[indexers]] "{dup}" appears more than once')
    return problems


def check_file(path: Path) -> list[str]:
    """Problems with the file, including not being valid TOML"""
    try:
        return validate(load_toml(path))
    except OSError as e:
        return [f"can't be read: {e.strerror}"]
    except ValueError as e:  # tomllib.TOMLDecodeError
        return [f"isn't valid TOML: {e}"]


def main() -> int:
    """setup.sh's config check: prints every problem in setup's style;
    exit status 1 when there are any"""
    path = default_paths().config_file
    problems = check_file(path)
    if problems:
        print(f"\033[1;31m   ✗ {path} has problems; nothing was changed:\033[0m", file=sys.stderr)
        for line in problems:
            print(f"       {line}", file=sys.stderr)
        return 1
    return 0
