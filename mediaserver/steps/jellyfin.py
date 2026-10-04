"""Jellyfin: the admin user (and its password, kept in line with
config.toml), the Movies / TV Shows / Anime libraries, setup's own API key,
playback defaults and hardware video conversion."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from mediaserver import api, creds
from mediaserver import common as c
from mediaserver.api import ApiError
from mediaserver.config import Config
from mediaserver.jellyfin import CLIENT, Jellyfin
from mediaserver.ui import err, info, mask, ok, warn

KEY_APP = "MediaServer"


def key_file(cfg: Config):
    """Setup's Jellyfin API key, for the dashboard and the later steps"""
    return cfg.paths.state / "dashstatus/jellyfin-key"


def libraries(cfg: Config) -> list[tuple[str, str, str]]:
    p = cfg.paths
    return [("Movies", str(p.movies), "movies"), ("TV Shows", str(p.tv), "tvshows"), ("Anime", str(p.anime), "tvshows")]


def playback_config(conf: dict, cfg: Config) -> dict:
    """The user's playback defaults ([playback]): subtitles always on in the
    preferred language, and the preferred audio language when a file has it
    (none: each file's default track, which postimport sets)"""
    audio = cfg.get("playback.audio_language", "")
    want = {k: v for k, v in conf.items() if k != "AudioLanguagePreference"}
    if audio:  # Jellyfin leaves an unset preference out (null isn't stored back)
        want["AudioLanguagePreference"] = audio
    return dict(want, SubtitleMode=cfg.get("playback.subtitle_mode", "Always"),
                SubtitleLanguagePreference=cfg.get("playback.subtitle_language", "eng"),
                # With a preferred audio language, pick by language, not the default flag
                PlayDefaultAudioTrack=audio == "",
                # The files' default tracks carry the preferences; a remembered
                # pick (by track number) goes stale when a file is rewritten
                RememberAudioSelections=False, RememberSubtitleSelections=False)


def encoding_config(conf: dict, on: bool) -> dict:
    """Hardware video conversion ([playback] hardware_acceleration): Apple's
    VideoToolbox encodes and decodes (H.264, HEVC, VP9, AV1) when a TV or
    phone can't play a file directly, instead of the CPU; it also does
    HDR-to-SDR tone mapping. Off: Jellyfin's default (software)."""
    if not on:
        return dict(conf, HardwareAccelerationType="none")
    return dict(conf, HardwareAccelerationType="videotoolbox", EnableHardwareEncoding=True,
                HardwareDecodingCodecs=["h264", "hevc", "vp9", "av1"], EnableDecodingColorDepth10Hevc=True,
                EnableDecodingColorDepth10Vp9=True, EnableVideoToolboxTonemapping=True)


def policy_config(policy: dict, allow_remux: bool) -> dict:
    """[playback] allow_remux (default false): when a device can't play a
    file directly, Jellyfin either copies the video into a stream ("remux")
    or converts it. Copying keeps the file's own keyframes, which in Blu-ray
    encodes can be 10 s apart, and browsers' players can stall on that
    (playback stopping at a fixed minute). Off: such devices get the video
    converted by the hardware encoder with a keyframe every 3 s. Downloading
    to devices (to watch offline, e.g. on a flight) stays allowed."""
    return dict(policy, EnablePlaybackRemuxing=allow_remux, EnableContentDownloading=True)


def startup_wizard(cfg: Config, url: str) -> None:
    """A fresh Jellyfin: run its first-start wizard with config.toml's login"""
    h = {"Authorization": CLIENT}
    startup = c.request(f"{url}/Startup/Configuration")
    if b"UICulture" not in startup.body:
        ok("Already configured")
        return
    c.request(f"{url}/Startup/Configuration", "POST", h, body={"UICulture": "en-US", "MetadataCountryCode": "US",
                                                                "PreferredMetadataLanguage": "en"})
    # GET creates the first user; POST renames it and sets its password.
    # Only finish the wizard once that worked, or Jellyfin ends up with no
    # usable admin and the wizard can't be re-run.
    c.request(f"{url}/Startup/User", headers=h)
    if not c.request(f"{url}/Startup/User", "POST", h, body={"Name": cfg.jellyfin_user, "Password": cfg.jellyfin_pass}).ok:
        raise err(f"Could not create the Jellyfin admin user; finish the wizard at {url} and re-run setup")
    c.request(f"{url}/Startup/Complete", "POST", h, body=b"")
    ok(f"Admin user '{cfg.jellyfin_user}' created")


def sign_in(cfg: Config) -> Jellyfin | None:
    """Logged in with config.toml's login. When it changed since it was
    last applied: log in with the recorded one and change the password
    (Jellyfin requires the current one). Only a login that works is
    recorded; otherwise the old one is kept, so the next run retries."""
    state = cfg.paths.state
    jf = Jellyfin(cfg)
    if not jf.login():
        old_user, old_pass = creds.get(state, "jellyfin", "username"), creds.get(state, "jellyfin", "password")
        if old_pass and old_pass != cfg.jellyfin_pass:
            old = Jellyfin(cfg, old_user or cfg.jellyfin_user, old_pass)
            if old.login():
                try:
                    uid = old.get("Users/Me")["Id"]
                    old.post(f"Users/{uid}/Password", {"CurrentPw": old_pass, "NewPw": cfg.jellyfin_pass})
                    ok("Jellyfin password changed to the one in config.toml")
                except (ApiError, KeyError, TypeError):
                    warn("Could not change the Jellyfin password (retried next run)")
                if old_user and old_user != cfg.jellyfin_user:
                    warn(f"Renaming the Jellyfin user isn't automatic: rename '{old_user}' to '{cfg.jellyfin_user}' in Jellyfin (Dashboard → Users)")
                jf.login()
    if not jf.token:
        return None
    creds.record(state, "jellyfin", cfg.jellyfin_user, cfg.jellyfin_pass)
    return jf


def ensure_libraries(cfg: Config, jf: Jellyfin) -> bool:
    """Each library, with its folder; True if anything was added"""
    changed = False
    try:
        existing = {lib["Name"]: lib for lib in jf.get("Library/VirtualFolders") or []}
    except ApiError:
        warn("Could not read Jellyfin's libraries")
        return False
    for name, path, kind in libraries(cfg):
        if name not in existing:
            try:
                jf.post(f"Library/VirtualFolders?name={quote(name)}&collectionType={kind}&refreshLibrary=false", {"LibraryOptions": {}})
                ok(f"Created library: {name}")
                changed = True
            except ApiError:
                warn(f"Could not create: {name}")
                continue
        # Ensure the folder is attached (creating the library doesn't always set it)
        try:
            folders = {lib["Name"]: lib for lib in jf.get("Library/VirtualFolders") or []}
        except ApiError:
            folders = existing
        if path in (folders.get(name) or {}).get("Locations", []):
            ok(f"Library '{name}' → {path}")
            continue
        try:
            jf.post("Library/VirtualFolders/Paths?refreshLibrary=true", {"Name": name, "PathInfo": {"Path": path}})
            ok(f"Library '{name}' → {path}")
            changed = True
        except ApiError:
            warn(f"Could not add path to {name}")
    return changed


def api_key(jf: Jellyfin) -> str:
    """Setup's own key (named MediaServer), not whichever key happens to be
    listed last; created when missing"""
    def find() -> str | None:
        """The key; "" when there's none; None when the list couldn't be read
        (not "none": creating one then would make a second)"""
        try:
            return next((k["AccessToken"] for k in (jf.get("Auth/Keys") or {}).get("Items") or [] if k.get("AppName") == KEY_APP), "")
        except ApiError:
            return None
    key = find()
    if key is None:
        warn("Jellyfin: couldn't read its API keys; not creating one (retried next run)")
        return ""
    if not key:
        try:
            jf.post(f"Auth/Keys?app={KEY_APP}")
        except ApiError:
            pass
        key = find() or ""
    return key


def update(jf: Jellyfin, path: str, current: Any, want: Any, done: str, failed: str) -> None:
    if want == current:
        ok(done)
        return
    try:
        jf.post(path, want)
        ok(f"{done} (updated)")
    except ApiError:
        warn(failed)


def run(cfg: Config) -> None:
    info("Configuring Jellyfin...")
    url = cfg.urls.jellyfin
    startup_wizard(cfg, url)
    jf = sign_in(cfg)
    if jf is None:
        warn("Could not authenticate")
        return
    ok("Authenticated")
    changed = ensure_libraries(cfg, jf)

    key = api_key(jf)
    if key:
        c.write_atomic(key_file(cfg), key + "\n", mode=0o600)
        ok(f"API key: {mask(key)}")

    # Real-time monitoring and a daily scan on every library
    try:
        for lib in jf.get("Library/VirtualFolders") or []:
            options = lib.get("LibraryOptions")
            if not options:
                continue
            want = dict(options, EnableRealtimeMonitor=True, AutomaticRefreshIntervalDays=1)
            if want != options:
                try:
                    jf.post("Library/VirtualFolders/LibraryOptions", {"Id": lib["ItemId"], "LibraryOptions": want})
                except ApiError:
                    warn(f"Could not update '{lib.get('Name')}' options")
                    continue
            ok(f"Library '{lib.get('Name')}': real-time monitoring + daily scan")
    except ApiError:
        warn("Could not read Jellyfin's libraries")

    # Playback defaults and the user's remux/download policy
    try:
        me = jf.get("Users/Me")
        uid, conf = me["Id"], me.get("Configuration") or {}
        want = playback_config(conf, cfg)
        shown = f"subtitles {want['SubtitleMode']} ({want['SubtitleLanguagePreference']}), audio {want.get('AudioLanguagePreference') or 'default track'}"
        update(jf, f"Users/Configuration?userId={uid}", conf, want, f"Playback: {shown}", "Could not set the Jellyfin user's playback settings")
        policy = (jf.get(f"Users/{uid}") or {}).get("Policy")
        if policy:
            allow = cfg.flag("playback.allow_remux", False)
            want_policy = policy_config(policy, allow)
            if want_policy != policy:
                try:
                    jf.post(f"Users/{uid}/Policy", want_policy)
                    ok("Playback: remuxing " + ("allowed" if allow else "off (devices that can't play a file directly get it converted)")
                       + "; downloads to devices allowed")
                except ApiError:
                    warn("Could not change the Jellyfin user's playback settings")
    except (ApiError, KeyError, TypeError):
        warn("Could not read the Jellyfin user's settings")

    on = cfg.flag("playback.hardware_acceleration", True)
    try:
        conf = jf.get("System/Configuration/encoding")
        update(jf, "System/Configuration/encoding", conf, encoding_config(conf, on),
               "Transcoding: " + ("hardware (VideoToolbox)" if on else "software"), "Could not set Jellyfin's transcoding settings")
    except ApiError:
        warn("Could not read Jellyfin's transcoding settings")

    # Library monitor delay 15 s, for faster detection of new files
    try:
        conf = jf.get("System/Configuration")
        if conf and conf.get("LibraryMonitorDelay") != 15:
            jf.post("System/Configuration", dict(conf, LibraryMonitorDelay=15))
        ok("Library monitor delay: 15s")
    except ApiError:
        warn("Could not set monitor delay")

    # Jellyfin only starts watching a library for new files after it has
    # been scanned once: scan when a library or folder was just added (the
    # daily scan and real-time monitoring cover the rest)
    if changed:
        try:
            jf.post("Library/Refresh")
            ok("Library scan started (enables real-time monitoring)")
        except ApiError:
            warn("Could not start a library scan")
