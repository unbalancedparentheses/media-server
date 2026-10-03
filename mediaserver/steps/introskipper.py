"""Intro Skipper: a Jellyfin plugin that detects intros and credits (by
audio fingerprint) and marks them as media segments, so players show
"Skip". Pinned in pins.json like Moonbase (the manifest's 12/ folder is
for Jellyfin 12)."""
from __future__ import annotations

import re
import time

from mediaserver.api import ApiError
from mediaserver.config import Config
from mediaserver.jellyfin import Jellyfin
from mediaserver.pins import INTRO_SKIPPER
from mediaserver.ui import info, ok, warn

NAME = "Intro Skipper"


def with_intro_skipper(options: dict) -> dict:
    """Library options with Intro Skipper first among the segment
    providers and not disabled"""
    want = dict(options)
    want["MediaSegmentProviderOrder"] = [NAME] + [p for p in options.get("MediaSegmentProviderOrder") or [] if p != NAME]
    want["DisabledMediaSegmentProviders"] = [p for p in options.get("DisabledMediaSegmentProviders") or [] if p != NAME]
    return want


def enable_libraries(jf: Jellyfin) -> None:
    """Jellyfin 12 runs a media-segment provider only for libraries that
    list it (library options → Media Segment Providers); enable Intro
    Skipper for the TV and Anime libraries so new episodes are analyzed too"""
    try:
        libraries = jf.get("Library/VirtualFolders") or []
    except ApiError:
        warn("Could not read Jellyfin's libraries")
        return
    for lib in libraries:
        if lib.get("CollectionType") != "tvshows":
            continue
        options = lib.get("LibraryOptions") or {}
        want = with_intro_skipper(options)
        if want != options:
            try:
                jf.post("Library/VirtualFolders/LibraryOptions", {"Id": lib["ItemId"], "LibraryOptions": want})
            except ApiError:
                warn(f"Could not turn on Intro Skipper for '{lib.get('Name')}'")
                continue
        ok(f"Intro Skipper on for '{lib.get('Name')}'")


def run(cfg: Config) -> None:
    info("Configuring Intro Skipper...")
    jf = Jellyfin(cfg)
    if not jf.login():
        warn("Skipping: not logged in to Jellyfin")
        return
    if not jf.pin_repository(f"{NAME} {INTRO_SKIPPER.version}", INTRO_SKIPPER.manifest, "intro-skipper"):
        return
    plugin = jf.plugin(INTRO_SKIPPER.guid)
    if not plugin:
        if not jf.install_plugin(NAME, INTRO_SKIPPER.guid, INTRO_SKIPPER.version, INTRO_SKIPPER.manifest):
            warn(f"Could not install Intro Skipper {INTRO_SKIPPER.version}")
            return
        # Installation is asynchronous; the plugin loads on the next restart
        plugins_dir = cfg.paths.config / "jellyfin/data/plugins"
        for _ in range(90):
            if any(plugins_dir.glob(f"{NAME}*")):
                break
            time.sleep(1)
        else:
            warn("Intro Skipper download didn't finish; re-run setup")
            return
        ok("Intro Skipper installed; restarting Jellyfin")
        if not jf.restart_ready():
            warn("Intro Skipper: Jellyfin isn't back yet; re-run setup")
            return
        plugin = jf.wait_plugin(INTRO_SKIPPER.guid)
        if not plugin:
            warn(f"Intro Skipper didn't load after restart (see {cfg.paths.logs}/jellyfin.log)")
            return
        # Analyze what's already in the library once; new episodes are
        # analyzed as they're added
        try:
            task = next((t for t in jf.get("ScheduledTasks") or []
                         if t.get("Key") == "CPBIntroSkipperDetectIntrosCredits" or re.search(r"Detect.*(Intro|Segment)", t.get("Name") or "", re.I)), None)
            if task:
                jf.post(f"ScheduledTasks/Running/{task['Id']}")
                ok("Analyzing the library for intros and credits (runs in the background)")
        except ApiError:
            pass
    enable_libraries(jf)
    plugin = jf.plugin(INTRO_SKIPPER.guid) or plugin
    if plugin.get("Version") == INTRO_SKIPPER.version:
        ok(f"Intro Skipper {plugin.get('Version')} loaded")
    else:
        warn(f"Intro Skipper {plugin.get('Version')} is installed; setup pins {INTRO_SKIPPER.version}")
