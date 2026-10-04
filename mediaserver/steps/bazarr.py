"""Bazarr: subtitles for everything Sonarr and Radarr have, in
subtitles.languages."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from mediaserver import api, creds, launchd, logins
from mediaserver import common as c
from mediaserver.api import ApiError
from mediaserver.config import Config, Keys
from mediaserver.ui import info, ok, warn


def config_file(cfg: Config) -> Path | None:
    return next((f for f in (cfg.paths.config / "bazarr/config/config/config.yaml",
                             cfg.paths.config / "bazarr/config/config.yaml") if f.exists()), None)


def settings(current: dict, keys: Keys, providers: list, languages: list, bind: str) -> dict:
    """Bazarr's config.yaml as setup wants it, keeping everything else"""
    want = copy.deepcopy(current)
    general = want.setdefault("general", {})
    for app, key, port in (("sonarr", keys.sonarr, 8989), ("radarr", keys.radarr, 7878)):
        if key:
            # base_url "": Bazarr rewrites "/" to it, which would count as a change every run
            want.setdefault(app, {}).update(ip="localhost", port=port, base_url="", apikey=key, ssl=False)
            general[f"use_{app}"] = True
    if providers:
        general["enabled_providers"] = list(providers)
    if languages:
        general["serie_default_enabled"] = True
        general["movie_default_enabled"] = True
    general.update(
        # Minimum score filters out mislabeled subs
        minimum_score=70, minimum_score_movie=70,
        # Upgrade subs when a better match appears
        upgrade_subs=True, upgrade_frequency=12, days_to_upgrade_subs=7,
        # Prefer embedded subs (always correctly labeled)
        use_embedded_subs=True,
        # ...but not picture-based ones (Blu-ray PGS, DVD VobSub): browsers
        # can't show them, so Jellyfin burns them into the video, re-encoding
        # every frame on the CPU. Ignoring them makes Bazarr fetch a text
        # subtitle the player shows directly.
        ignore_pgs_subs=True, ignore_vobsub_subs=True,
        # Listen where the other admin UIs do (network.admin_bind)
        ip=bind,
    )
    return want


def language_profiles(existing: list, languages: list, want: str) -> list | None:
    """The profiles with "Default" carrying subtitles.languages (created, or
    updated when it differs); None when it's right already. Other profiles
    are kept as they are.

    want = "first": done once the first language is there (cutoff = its
    item id). Until then the others are fetched too, as a fallback, and
    Bazarr keeps looking for the first; so a file with only Spanish still
    gets English when it exists. (65535, "any", would stop at whatever
    language happens to be there.) "all": every one. Item ids start at 1:
    Bazarr checks the cutoff with "if cutoff", so an id of 0 would mean no
    cutoff at all. Bazarr compares the flags as the strings "True"/"False";
    booleans (written by older versions of this setup) never match."""
    items = [{"id": i + 1, "language": lang, "hi": "False", "forced": "False", "audio_exclude": "False",
              "audio_only_include": "False"} for i, lang in enumerate(languages)]
    cutoff = 1 if want == "first" else None
    default = next((p for p in existing if p.get("name") == "Default"), None)
    if default is not None:
        if default.get("items") == items and default.get("cutoff") == cutoff:
            return None
        return [dict(p, items=items, cutoff=cutoff) if p.get("name") == "Default" else p for p in existing]
    return existing + [{"profileId": max([p.get("profileId", 0) for p in existing] or [0]) + 1, "name": "Default",
                        "cutoff": cutoff, "items": items, "mustContain": [], "mustNotContain": [], "originalFormat": None}]


def login_settings(current: dict, user: str, password: str) -> dict | None:
    """Bazarr accepts only "form" or "basic" (anything else, like the
    "forms" an older version of this setup wrote, is reset to null = no
    login) and stores the password as an MD5 hash. None when it's set."""
    want = {"type": "form", "username": user, "password": hashlib.md5(password.encode()).hexdigest()}
    auth = current.get("auth") or {}
    if all(auth.get(k) == v for k, v in want.items()):
        return None
    updated = copy.deepcopy(current)
    updated.setdefault("auth", {}).update(want)
    return updated


def read_yaml(path: Path) -> dict:
    import yaml
    with open(path) as f:
        return yaml.safe_load(f) or {}


def write_yaml(path: Path, data: dict) -> None:
    import yaml
    # API keys and provider passwords: private, whatever the umask
    c.write_atomic(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True), 0o600)


def restart(cfg: Config) -> None:
    launchd.restart(cfg.paths.config, "bazarr")
    api.wait_for("Bazarr", cfg.urls.bazarr)


def run(cfg: Config) -> None:
    info("Configuring Bazarr...")
    path = config_file(cfg)
    if path is None:
        warn("Bazarr config file not found")
        return
    ok(f"Config: {path}")
    # API keys and provider passwords: private, also when nothing changes
    # (Bazarr writes it itself too)
    if path.stat().st_mode & 0o077:
        path.chmod(0o600)
    url = cfg.urls.bazarr
    languages = [x for x in cfg.get("subtitles.languages", []) or [] if x]
    want_mode = cfg.get("subtitles.want", "first")

    # Edit with a YAML parser: line-based edits corrupted the file when a
    # value's shape changed (e.g. a one-line list becoming a block list)
    try:
        current = read_yaml(path)
        wanted = settings(current, Keys.read(cfg.paths.config), cfg.get("subtitles.providers", []) or [], languages,
                          cfg.get("network.admin_bind", "0.0.0.0"))
        ok("Sonarr + Radarr configured")
        # Only rewrite (and restart Bazarr) when something changed
        if wanted != current:
            write_yaml(path, wanted)
            restart(cfg)
            ok("Bazarr restarted with the new settings")
    except (OSError, ValueError, ImportError) as e:
        warn(f"Could not update Bazarr config ({e})")

    # Language profiles via the settings API (form data). Before the login
    # below: the settings API may rewrite config.yaml.
    key = c.bazarr_key(cfg.paths.config)
    if languages and key:
        api.wait_for("Bazarr", url)
        # Unreadable is not "none": saving would replace every other profile
        try:
            existing = api.get(f"{url}/api/system/languages/profiles?apikey={key}")
        except ApiError:
            existing = None
        shown = f"{' '.join(languages)}; {want_mode}"
        if existing is None:
            warn("Couldn't read Bazarr's language profiles; not changed (retried next run)")
        else:
            profiles = language_profiles(existing, languages, want_mode)
            if profiles is None:
                ok(f"Language profile: Default ({shown})")
            else:
                default_id = next(p["profileId"] for p in profiles if p["name"] == "Default")
                # Every language a profile uses has to be enabled
                enabled = sorted({i["language"] for p in profiles for i in p.get("items") or []})
                form = [("languages-enabled", lang) for lang in enabled] + [
                    ("languages-profiles", json.dumps(profiles, separators=(",", ":"))),
                    ("settings-general-serie_default_profile", str(default_id)),
                    ("settings-general-movie_default_profile", str(default_id))]
                if post_form(f"{url}/api/system/settings?apikey={key}", form):
                    ok(f"Language profile: Default ({' '.join(languages)}, updated)")
                else:
                    warn("Could not set the language profile")
                # Bazarr recomputes what's missing only when it re-indexes
                for task in ("series_full_scan_subtitles", "movies_full_scan_subtitles"):
                    c.request(f"{url}/api/system/tasks?apikey={key}", "POST", form={"taskid": task})

    # The login, in config.yaml; after the settings API (it would be overwritten)
    user, password = cfg.jellyfin_user, cfg.jellyfin_pass
    try:
        updated = login_settings(read_yaml(path), user, password)
        if updated is not None:
            write_yaml(path, updated)
            restart(cfg)
    except (OSError, ValueError, ImportError):
        warn("Could not set the Bazarr login")
        return
    if logins.bazarr(url, user, password):
        if not creds.match(cfg.paths.state, "bazarr", user, password):
            creds.record(cfg.paths.state, "bazarr", user, password)
        ok(f"Bazarr login: {user}")
    else:
        warn("Bazarr's login doesn't work yet (retried next run)")


def post_form(url: str, fields: list[tuple[str, str]], tries: int = 3) -> bool:
    """POST repeated form fields (Bazarr's settings form), with retries"""
    import time
    import urllib.parse
    body = urllib.parse.urlencode(fields).encode()
    for attempt in range(tries):
        r = c.request(url, "POST", {"Content-Type": "application/x-www-form-urlencoded"}, body=body)
        if r.ok:
            return True
        if attempt + 1 < tries:
            time.sleep(2)
    return False
