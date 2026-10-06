"""postimport: checks and fixes every file Sonarr and Radarr import, so it
plays directly everywhere. Runs as a launchd agent (see flake.nix).

Every POSTIMPORT_INTERVAL seconds (60) it reads Sonarr's and Radarr's import
history and, for each newly imported file:

  1. Checks it (library.check_downloads). A file is rejected when it
     - won't play: no video or audio, or the video doesn't decode,
     - is far shorter than the episode or movie (a sample or a fake),
     - is anime made in Japanese with no Japanese audio (a dub), when
       quality.anime_block_dubs is on.
     Rejecting deletes the file in Sonarr/Radarr and marks the release as
     failed there, which blocklists it and searches for another one. After
     library.max_replacements tries the file is kept and you're notified;
     you're also notified if no other release turns up within 6 hours.
  2. Adds a stereo AAC audio track (library.stereo_audio) when the audio
     is only in formats browsers can't play (Dolby, DTS, TrueHD), so
     Jellyfin streams the file as it is instead of converting it. It comes
     first so players pick it; the original tracks stay.
  3. Turns picture subtitles (Blu-ray PGS) into a text .srt next to the
     video (library.ocr_subtitles), for each wanted language that has no
     text subtitles yet, so nothing has to be burned into the video.
  4. Sets each file's default tracks to your preferences, so every player
     picks them: audio in playback.anime_audio_language for anime (files
     in the anime library) or playback.audio_language for the rest (empty:
     the file's own default), and subtitles in the first of
     subtitles.languages that's there as text (e.g. English, else
     Spanish). Text subtitles win over picture ones in the same language:
     a picture track flagged "default" (common on Blu-rays) gets burned
     into the video by Jellyfin while players show the text one, so two
     subtitles appear at once. With library.drop_picture_subtitles, the
     picture track in a language that's there as text is removed: Moonfin
     prefers picture subtitles over text ones whatever the flags say.

Files already in the library get steps 2 to 4 too, a few per round. A file
whose torrent is still seeding is only rewritten when there's plenty of free
space, since the torrent keeps its own copy until seeding ends. Nothing runs
while an install or other operation holds setup's lock.

Rewrites are safe: the new file is written next to the old one, checked
(same duration, every stream present) and only then moved over it; if the
original changed in the meantime the new one is thrown away.

Settings come from $POSTIMPORT_STATE/settings.json, which setup writes from
config.toml; progress is kept in state.json, and status.json is what the
dashboard and `nix run .#doctor` show.

By hand: nix run .#postimport -- --check FILE (what's wrong with it,
changes nothing) or --fix FILE (steps 2 to 4 now).

Environment: POSTIMPORT_CONFIG (~/media/config), POSTIMPORT_STATE
(~/media/.state/postimport), POSTIMPORT_INTERVAL.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from mediaserver import common as c
from mediaserver import lock, playback, stuck
from mediaserver.config import local
from mediaserver.common import background, log, read_json

CONFIG = Path(os.environ.get("POSTIMPORT_CONFIG", Path.home() / "media/config"))
STATE = Path(os.environ.get("POSTIMPORT_STATE", Path.home() / "media/.state/postimport"))
LOCK = STATE.parent / "lock"

DEFAULTS = {
    "check_downloads": True,
    "stereo_audio": True,
    "ocr_subtitles": True,
    "max_replacements": 3,
    "block_dubs": True,
    "audio_language": "",
    "anime_audio_language": "jpn",
    "anime_dir": "",
    "drop_picture_subtitles": True,
    "default_tracks": True,
    "subtitle_languages": ["en"],
    "want": "first",
    "min_free_gb": 10,
    "warn_free_gb": 50,
    "library_dirs": [],
    "search_missing": True,
    "fallback_resolution": "",
    "repackage_mp4": True,
}
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v"}
# Audio every browser plays; anything else makes Jellyfin convert
BROWSER_AUDIO = {"aac", "mp3", "opus", "flac", "vorbis"}
TEXT_SUBTITLES = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}
PICTURE_SUBTITLES = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
# Bumped when fix() learns something new, so files checked before get
# looked at again (once)
FIX_VERSION = 5
SIDECAR_SUBTITLES = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
NOTHING_FOUND_AFTER = 6 * 3600
# A problem found on import is checked again this much later before the
# file is deleted (a tool or the disk can fail once)
CONFIRM_AFTER = 10 * 60
# An import that can't be checked (a service down) is retried, with growing
# pauses, this many times
IMPORT_TRIES = 10
# A fix that fails is retried this often, at most every RETRY_FIX_AFTER
FIX_TRIES = 3
RETRY_FIX_AFTER = 3600
SWEEP_PER_ROUND = 3

# ISO 639-1 → the ISO 639-2 codes files are tagged with (B and T forms)
LANGUAGES = {
    "ar": ["ara"], "bg": ["bul"], "ca": ["cat"], "cs": ["cze", "ces"], "da": ["dan"],
    "de": ["ger", "deu"], "el": ["gre", "ell"], "en": ["eng"], "es": ["spa"], "et": ["est"],
    "fa": ["per", "fas"], "fi": ["fin"], "fr": ["fre", "fra"], "he": ["heb"], "hi": ["hin"],
    "hr": ["hrv"], "hu": ["hun"], "id": ["ind"], "is": ["ice", "isl"], "it": ["ita"],
    "ja": ["jpn"], "ko": ["kor"], "lt": ["lit"], "lv": ["lav"], "ms": ["may", "msa"],
    "nl": ["dut", "nld"], "no": ["nor", "nob", "nno"], "pl": ["pol"], "pt": ["por"],
    "ro": ["rum", "ron"], "ru": ["rus"], "sk": ["slo", "slk"], "sl": ["slv"], "sr": ["srp"],
    "sv": ["swe"], "th": ["tha"], "tr": ["tur"], "uk": ["ukr"], "vi": ["vie"],
    "zh": ["chi", "zho"],
}
TAG_TO_LANGUAGE = {c: two for two, codes in LANGUAGES.items() for c in codes + [two]}


def language_of(code):
    """Any tag ("eng", "en", "en-US") → ISO 639-1, or "" when unknown/untagged"""
    code = (code or "").lower().split("-")[0]
    return TAG_TO_LANGUAGE.get(code, "")


def tags(stream):
    return {k.lower(): v for k, v in (stream.get("tags") or {}).items()}


def stream_language(stream):
    return language_of(tags(stream).get("language"))


def operation_running():
    return c.operation_running(LOCK)


def notify(title, message):
    log(f"notify: {title}: {message}")
    c.notify(title, message)


# ─── Inspecting files ────────────────────────────────────────────

class StateTrouble(Exception):
    """The worker's record can't be used: no round runs until it can"""


class ToolTrouble(Exception):
    """ffprobe/ffmpeg themselves don't work (missing, crashing): nothing can
    be concluded about the file, so nothing is rejected"""


def tools_work() -> bool:
    try:
        return all(subprocess.run([tool, "-version"], capture_output=True, check=False, timeout=30).returncode == 0
                   for tool in ("ffprobe", "ffmpeg"))
    except (OSError, subprocess.TimeoutExpired):
        return False


def probe(path, tries: int = 3):
    """ffprobe's streams and format, or None when it can't read the file
    (after <tries> attempts: a file still being moved or a busy disk can
    fail once). Raises ToolTrouble when ffprobe itself doesn't work."""
    for attempt in range(tries):
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                capture_output=True, text=True, check=False, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            try:
                info = json.loads(result.stdout)
                info.setdefault("streams", [])
                return info
            except ValueError:
                pass
        if attempt + 1 < tries:
            time.sleep(2)
    if not tools_work():
        raise ToolTrouble("ffprobe/ffmpeg don't run")
    return None


def duration(info):
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def streams(info, kind):
    return [s for s in info["streams"] if s.get("codec_type") == kind
            and not (kind == "video" and (s.get("disposition") or {}).get("attached_pic"))]


def decodes(path, length):
    """Decode a few seconds at the start, middle and end; False if any part
    gives no picture at all (damaged or cut short). Glitches right after a
    seek are normal and don't count."""
    points = [0.0] if length < 90 else [10.0, length / 2, max(length - 60, 0)]

    def frames_at(start: float, hardware: bool) -> int:
        cmd = ["ffmpeg", "-nostdin", "-v", "error"] + (["-hwaccel", "videotoolbox"] if hardware else []) + [
            "-ss", f"{start:.0f}", "-progress", "pipe:1", "-i", str(path), "-map", "0:v:0", "-t", "8", "-f", "null", "-"]
        try:
            result = subprocess.run(background(cmd), capture_output=True, text=True, check=False, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            return 0
        frames = [int(m) for m in re.findall(r"^frame=(\d+)", result.stdout, re.M)]
        return frames[-1] if result.returncode == 0 and frames else 0
    for start in points:
        # The hardware decoder can fail where the file is fine (busy, an
        # unusual profile): only a failure in software too counts
        if frames_at(start, True) == 0 and frames_at(start, False) == 0:
            if not tools_work():
                raise ToolTrouble("ffmpeg doesn't run")
            return False
    return True


def problem_with(info, path, expected_minutes, needs_japanese, check_decoding=True):
    """Why a newly imported file should be replaced, or None if it's fine"""
    if info is None:
        return "the file can't be read"
    if not streams(info, "video"):
        return "it has no video"
    audio = streams(info, "audio")
    if not audio:
        return "it has no audio"
    length = duration(info)
    if expected_minutes and length and length < expected_minutes * 60 * 0.5:
        return f"it's only {length / 60:.0f} minutes of {expected_minutes} (a sample or a fake)"
    if needs_japanese:
        languages = [stream_language(s) for s in audio]
        # Untagged audio might well be Japanese; only reject what's known
        if all(languages) and "ja" not in languages:
            return "it's dubbed (no Japanese audio)"
    if check_decoding and not decodes(path, length):
        return "the video is damaged"
    return None


# ─── Stereo audio ────────────────────────────────────────────────

def source_audio(info, preferred):
    """The audio track players would pick: the preferred language, else
    the default track, else the first"""
    audio = streams(info, "audio")
    if not audio:
        return None
    want = language_of(preferred)
    for s in audio:
        if want and stream_language(s) == want:
            return s
    for s in audio:
        if (s.get("disposition") or {}).get("default"):
            return s
    return audio[0]


def needs_stereo(info, preferred):
    """The audio track that needs a stereo AAC copy, or None"""
    source = source_audio(info, preferred)
    if source is None or source.get("codec_name") in BROWSER_AUDIO:
        return None
    language = stream_language(source)
    for s in streams(info, "audio"):
        if s.get("codec_name") in BROWSER_AUDIO and stream_language(s) == language:
            return None
    return source


def aac_encoder():
    result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=False)
    return "aac_at" if re.search(r"\baac_at\b", result.stdout) else "aac"


def stereo_command(path, out, info, source, encoder):
    """ffmpeg arguments: video, then the new stereo track (default), then
    every original audio, subtitle and attachment track, all copied"""
    fmt = "matroska" if Path(path).suffix.lower() == ".mkv" else "mp4"
    language = tags(source).get("language", "und")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
           "-map", "0:V", "-map", f"0:{source['index']}", "-map", "0:a", "-map", "0:s?"]
    if fmt == "matroska":
        cmd += ["-map", "0:t?"]
    cmd += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy",
            "-c:a:0", encoder, "-b:a:0", "192k", "-ac:a:0", "2",
            "-metadata:s:a:0", f"language={language}", "-metadata:s:a:0", "title=Stereo",
            "-disposition:a", "0", "-disposition:a:0", "default"]
    if fmt == "mp4":
        cmd += ["-movflags", "+faststart"]
    else:
        # Exactly the flags given: by default the MKV writer flags the first
        # track of a type "default" when none is, which undid "no default
        # subtitles" (and the next check redid it)
        cmd += ["-default_mode", "passthrough"]
    return cmd + ["-f", fmt, str(out)]


def same_file(path, before):
    try:
        st = os.stat(path)
    except OSError:
        return False
    return (st.st_ino, st.st_size, st.st_mtime) == before


def track_counts(info):
    counts = {}
    for kind in ("video", "audio", "subtitle", "attachment"):
        counts[kind] = len(streams(info, kind))
    return counts


def room_for(path, settings):
    """Enough free space to rewrite the file? One still seeding (a second
    hard link) keeps its old copy until seeding ends, so it needs plenty"""
    st = os.stat(path)
    spare = shutil.disk_usage(Path(path).parent).free - st.st_size * 1.1
    gb = 1024 ** 3
    if st.st_nlink > 1:
        return spare > max(settings["warn_free_gb"] * 2, 100) * gb
    return spare > settings["min_free_gb"] * gb


def run_ffmpeg(cmd: list, total: float, progress=None) -> tuple[int, str]:
    """Run ffmpeg, calling progress(fraction done) as it goes (from its
    -progress output, at most every 2s); (exit status, end of its errors)"""
    if progress is None or not total:
        result = subprocess.run(background(cmd), capture_output=True, text=True, check=False)
        return result.returncode, result.stderr
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    with tempfile.TemporaryFile("w+") as errors:
        proc = subprocess.Popen(background(cmd), stdout=subprocess.PIPE, stderr=errors, text=True)
        try:
            last = 0.0
            assert proc.stdout is not None
            for line in proc.stdout:
                # out_time_us (and out_time_ms, despite its name) are microseconds
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.isdigit() and time.time() - last >= 2:
                    last = time.time()
                    progress(min(int(value) / 1e6 / total, 1.0))
            code = proc.wait()
        finally:
            # Reporting progress failed (a full disk, say): ffmpeg isn't
            # left running on its own
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
        errors.seek(0)
        return code, errors.read()


def rewrite(path, info, command, expected, what, progress=None):
    """Run ffmpeg <command> (a function of the output path) into a new file
    next to the original, check it (track counts as <expected>, same
    duration) and only then move it over the original; True when done.
    progress(fraction) is called while ffmpeg runs."""
    path = Path(path)
    st = path.stat()
    before = (st.st_ino, st.st_size, st.st_mtime)
    out = path.with_name(f".{path.name}.postimport")
    try:
        code, errors = run_ffmpeg(command(out), duration(info), progress)
        if code != 0:
            log(f"couldn't {what} in {path.name}: {errors.strip()[-300:]}")
            return False
        new = probe(out)
        if new is None or track_counts(new) != expected or abs(duration(new) - duration(info)) > 2:
            log(f"the rewritten {path.name} didn't check out; kept the original")
            return False
        if not same_file(path, before):
            log(f"{path.name} changed while being rewritten; kept the new original")
            return False
        os.chmod(out, st.st_mode & 0o777)
        os.replace(out, path)
        return True
    finally:
        out.unlink(missing_ok=True)


def add_stereo(path, info, source, progress=None):
    """Rewrite the file with a stereo AAC track; True when done"""
    expected = dict(track_counts(info), audio=track_counts(info)["audio"] + 1)
    encoder = aac_encoder()
    return rewrite(path, info, lambda out: stereo_command(path, out, info, source, encoder), expected, "add stereo audio",
                   progress)


def text_languages(info, path) -> set:
    """Languages there as (full, not forced) text subtitles, in the file or
    next to it"""
    found = sidecar_languages(path)
    for st in streams(info, "subtitle"):
        if st.get("codec_name") in TEXT_SUBTITLES and not is_forced(st):
            found.add(stream_language(st))
    found.discard("")
    return found


PARTIAL = re.compile(r"sign|song|forced|karaoke|commentary", re.I)


def partial_subtitles(stream) -> bool:
    """A track titled as signs/songs only (or commentary), whatever its flags"""
    return bool(PARTIAL.search(tags(stream).get("title", "")))


def default_tracks(info, path, audio_language: str, subtitle_languages: list) -> dict:
    """{stream index: "default" or "0"}: the flags to set so players pick
    the preferred tracks; empty when they're right already.

    Audio: the first track in <audio_language> (a browser-friendly one, like
    the added stereo track, first); unchanged when it's empty or not there.
    Subtitles: the first of <subtitle_languages> there as text. In the file,
    its full track (not one titled Signs/Songs) becomes the default; some
    releases flag the full track "forced", which makes Jellyfin skip it for
    the signs track, so that flag goes. Only next to the file: no embedded
    track stays default, so Jellyfin picks the file next to it. Real forced
    tracks keep their flags. Unchanged when none of the languages is there
    as text: then a picture track is all there is."""
    plan: dict[int, str] = {}

    def flags(stream) -> dict:
        return stream.get("disposition") or {}

    def want(stream, on: bool) -> None:
        if on and (not flags(stream).get("default") or flags(stream).get("forced")):
            plan[stream["index"]] = "default"
        elif not on and flags(stream).get("default"):
            plan[stream["index"]] = "0"

    lang = language_of(audio_language)
    audio = [st for st in streams(info, "audio") if lang and stream_language(st) == lang]
    if audio:
        target = next((st for st in audio if st.get("codec_name") in BROWSER_AUDIO), audio[0])
        for st in streams(info, "audio"):
            if bool(flags(st).get("default")) != (st is target):
                plan[st["index"]] = "default" if st is target else "0"

    available = text_languages(info, path) | full_but_forced_languages(info)
    chosen = next((language_of(x) for x in subtitle_languages if language_of(x) in available), None)
    if chosen:
        text = [st for st in streams(info, "subtitle") if st.get("codec_name") in TEXT_SUBTITLES
                and stream_language(st) == chosen and not partial_subtitles(st)]
        target = next((st for st in text if not flags(st).get("forced")), text[0] if text else None)
        for st in streams(info, "subtitle"):
            if st is target:
                want(st, True)
            elif not flags(st).get("forced"):
                want(st, False)
    return plan


def full_but_forced_languages(info) -> set:
    """Languages whose only full text track is flagged forced (mislabelled
    releases), shown by a separate Signs/Songs track in the same language"""
    out = set()
    subs = [st for st in streams(info, "subtitle") if st.get("codec_name") in TEXT_SUBTITLES]
    for st in subs:
        lang = stream_language(st)
        if (st.get("disposition") or {}).get("forced") and not partial_subtitles(st) \
                and any(o is not st and stream_language(o) == lang and partial_subtitles(o) for o in subs):
            out.add(lang)
    return out


CUE_TIME = {
    "srt": re.compile(r"(\d+):(\d\d):(\d\d)[,.]\d+\s*-->"),
    "vtt": re.compile(r"(?:(\d+):)?(\d\d):(\d\d)\.\d+\s*-->"),
    "ass": re.compile(r"^Dialogue:\s*\d+,(\d+):(\d\d):(\d\d)", re.M),
}


def enough_cues(cues: int, last: float, seconds: float) -> bool:
    """A whole film's or episode's dialogue: at least 2 cues a minute (films
    have 5 to 12) and lasting to 70% of it; without a known length, 100 cues"""
    if not seconds:
        return cues >= 100
    return cues >= max(20, seconds / 60 * 2) and last >= 0.7 * seconds


def sidecar_cues(f: Path) -> tuple[int, float] | None:
    """(cues, last cue's start in seconds) in a subtitle file; None for a
    format that can't be read this way"""
    kind = {".srt": "srt", ".vtt": "vtt", ".ass": "ass", ".ssa": "ass"}.get(f.suffix.lower())
    if not kind:
        return None
    try:
        text = f.read_text(errors="replace")[:20_000_000]
    except OSError:
        return None
    times = [(int(h or 0) * 3600 + int(m) * 60 + int(sec)) for h, m, sec in CUE_TIME[kind].findall(text)]
    return len(times), float(max(times, default=0))


def stream_cues(path, index: int) -> tuple[int, float] | None:
    """(packets, last packet's time) of one subtitle track, read from the
    file itself (metadata can be missing or wrong); None if it can't be read"""
    try:
        r = subprocess.run(background(["ffprobe", "-v", "error", "-select_streams", str(index), "-show_entries",
                                       "packet=pts_time", "-of", "csv=p=0", str(path)]),
                           capture_output=True, text=True, timeout=3600, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    times = []
    for line in r.stdout.split():
        try:
            times.append(float(line.strip(",")))
        except ValueError:
            continue
    return len(times), max(times, default=0.0)


def covers(text: tuple[int, float], picture: tuple[int, float], codec: str, seconds: float) -> bool:
    """The text version has the picture track's dialogue: about as many cues
    (a PGS track has two packets per subtitle, one showing and one clearing
    it), lasting as long, and a whole film's worth"""
    events = picture[0] // 2 if codec == "hdmv_pgs_subtitle" else picture[0]
    return (picture[0] > 0 and text[0] >= 0.8 * events and text[1] >= picture[1] - 120
            and enough_cues(text[0], text[1], seconds))


def redundant_pictures(info, path) -> list:
    """Picture subtitle tracks whose dialogue is also there as text (in the
    file, or a file next to it), forced ones (signs) aside. Measured, not
    assumed: the text's cues against the picture track's own, read from the
    files; anything that can't be read or doesn't match keeps the track."""
    seconds = duration(info)
    texts: dict[str, list] = {}
    for f, lang in sidecars(path):
        if (cues := sidecar_cues(f)) is not None:
            texts.setdefault(lang, []).append(cues)
    for st in streams(info, "subtitle"):
        if st.get("codec_name") in TEXT_SUBTITLES and not partial_subtitles(st) and not is_forced(st):
            lang = stream_language(st)
            if lang and (cues := stream_cues(path, st["index"])) is not None:
                texts.setdefault(lang, []).append(cues)
    dropped = []
    for st in streams(info, "subtitle"):
        if st.get("codec_name") not in PICTURE_SUBTITLES or is_forced(st) or not texts.get(stream_language(st)):
            continue
        picture = stream_cues(path, st["index"])
        if picture and any(covers(t, picture, st["codec_name"], seconds) for t in texts[stream_language(st)]):
            dropped.append(st)
    return dropped


def without(info, dropped: list) -> dict:
    gone = {st["index"] for st in dropped}
    return dict(info, streams=[st for st in info["streams"] if st["index"] not in gone])


def defaults_command(path, out, info, plan: dict, dropped: list | None = None):
    """Copy everything except <dropped> tracks, setting the <plan> flags
    (track numbers per type count only the tracks that are kept)"""
    fmt = "matroska" if Path(path).suffix.lower() == ".mkv" else "mp4"
    dropped = dropped or []
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", "0", "-map", "-0:d?"]
    for st in dropped:
        cmd += ["-map", f"-0:{st['index']}"]
    cmd += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
    kept = without(info, dropped)
    for kind, letter in (("audio", "a"), ("subtitle", "s")):
        indexes = [st["index"] for st in streams(kept, kind)]
        for index, flag in sorted(plan.items()):
            if index in indexes:
                cmd += [f"-disposition:{letter}:{indexes.index(index)}", flag]
    if fmt == "mp4":
        cmd += ["-movflags", "+faststart"]
    else:
        # Exactly the flags given: by default the MKV writer flags the first
        # track of a type "default" when none is, which undid "no default
        # subtitles" (and the next check redid it)
        cmd += ["-default_mode", "passthrough"]
    return cmd + ["-f", fmt, str(out)]


def set_defaults(path, info, plan: dict, dropped: list | None = None, progress=None) -> bool:
    dropped = dropped or []
    expected = dict(track_counts(info), subtitle=track_counts(info)["subtitle"] - len(dropped))
    return rewrite(path, info, lambda out: defaults_command(path, out, info, plan, dropped), expected,
                   "set the default tracks", progress)


# ─── Subtitles ───────────────────────────────────────────────────

def sidecars(path) -> list:
    """(file, language) for the full subtitle files next to the video
    (Movie.en.srt, Movie.en.hi.srt; not Movie.eng.forced.srt), by name"""
    path = Path(path)
    out = []
    try:
        siblings = list(path.parent.iterdir())
    except OSError:
        return out
    for f in siblings:
        if f.suffix.lower() in SIDECAR_SUBTITLES and f.name.startswith(path.stem + "."):
            parts = [x.lower() for x in f.name[len(path.stem) + 1:].split(".")[:-1]]
            # Forced/signs files cover only part of the dialogue
            if any(x in ("forced", "signs", "songs") for x in parts):
                continue
            lang = next((language_of(x) for x in parts if language_of(x)), "")
            if lang:
                out.append((f, lang))
    return out


def sidecar_languages(path):
    """Languages of the subtitle files next to the video, by their names"""
    return {lang for _, lang in sidecars(path)}


def sidecar_files(path) -> list:
    """Names of the subtitle files next to the video (they change what the
    fixes decide, e.g. when Bazarr adds one)"""
    path = Path(path)
    try:
        return sorted(f.name for f in path.parent.iterdir()
                      if f.suffix.lower() in SIDECAR_SUBTITLES and f.name.startswith(path.stem + "."))
    except OSError:
        return []


def is_forced(stream):
    return bool((stream.get("disposition") or {}).get("forced")) or "forced" in tags(stream).get("title", "").lower()


def ocr_targets(info, path, languages, want):
    """[(language, PGS stream)] to turn into text: wanted languages with no
    text subtitles (embedded or next to the file) but a full picture track.
    With want = "first", only until one language has text."""
    have = sidecar_languages(path)
    for s in streams(info, "subtitle"):
        if s.get("codec_name") in TEXT_SUBTITLES and not is_forced(s):
            have.add(stream_language(s))
    targets = []
    for lang in [language_of(x) for x in languages]:
        if not lang:
            continue
        if lang in have:
            if want == "first":
                break
            continue
        pictures = [s for s in streams(info, "subtitle")
                    if s.get("codec_name") == "hdmv_pgs_subtitle" and stream_language(s) == lang and not is_forced(s)]
        if pictures:
            # The fullest track (mkvmerge records the count), not commentary
            def size(s):
                t = tags(s)
                n = next((v for k, v in t.items() if k.startswith("number_of_frames")), "0")
                return (("commentary" not in t.get("title", "").lower()), int(n) if str(n).isdigit() else 0)
            targets.append((lang, max(pictures, key=size)))
            if want == "first":
                break
    return targets


def ocr(path, lang, stream):
    """Read a PGS track into Movie.<lang>.srt next to the video. Returns
    "written", "skipped" (already there) or "failed"."""
    path = Path(path)
    dest = path.with_name(f"{path.stem}.{lang}.srt")
    if dest.exists():
        return "skipped"
    with tempfile.TemporaryDirectory(dir=STATE) as tmp:
        sup = Path(tmp) / f"subtitles.{lang}.sup"
        extract = subprocess.run(background(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", f"0:{stream['index']}",
             "-c", "copy", str(sup)]), capture_output=True, text=True, check=False)
        if extract.returncode != 0:
            log(f"couldn't extract the {lang} picture subtitles of {path.name}")
            return "failed"
        subprocess.run(background(["pgsrip", "-l", lang, "-w", "2", str(sup)]),
                       capture_output=True, text=True, check=False)
        srt = sup.with_suffix(".srt")
        text = srt.read_text(errors="replace") if srt.exists() else ""
        if text.count("-->") < 10:
            log(f"reading the {lang} picture subtitles of {path.name} gave nothing usable")
            return "failed"
        tmp_dest = dest.with_name(f".{dest.name}.tmp")
        try:
            tmp_dest.write_text(text)
            os.replace(tmp_dest, dest)
        except OSError as e:   # e.g. the disk is full: no half-written file left
            tmp_dest.unlink(missing_ok=True)
            log(f"couldn't save the {lang} subtitles of {path.name}: {e.strerror}")
            return "failed"
    return "written"


# ─── Sonarr and Radarr ───────────────────────────────────────────

class Arr:
    """A Sonarr/Radarr API client; key from its config.xml"""

    def __init__(self, name, url, kind, version="v3"):
        self.name, self.url, self.kind, self.version = name, url, kind, version  # kind: "series" | "movie"

    def key(self):
        try:
            m = re.search(r"<ApiKey>(.*?)</ApiKey>", (CONFIG / self.name.lower() / "config.xml").read_text())
            return m.group(1) if m else ""
        except OSError:
            return ""

    def call(self, method, path, body=None, timeout: float = 30) -> Any:
        """The decoded JSON answer ({} when there's no body)"""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}/api/{self.version}/{path}", data=data, method=method,
                                     headers={"X-Api-Key": self.key(), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def imports_after(self, last_id, page_size=50, max_pages=100):
        """Every import event newer than last_id (paging back as far as
        needed, not just the latest page), oldest first"""
        found = []
        for page in range(1, max_pages + 1):
            records = self.call("GET", f"history?page={page}&pageSize={page_size}&sortKey=date"
                                       f"&sortDirection=descending&eventType=3").get("records", [])
            found += [r for r in records if r["id"] > last_id]
            if len(records) < page_size or any(r["id"] <= last_id for r in records):
                break
        return sorted(found, key=lambda r: r["id"])

    def latest_import_id(self):
        page = self.call("GET", "history?page=1&pageSize=1&sortKey=date&sortDirection=descending&eventType=3")
        records = page.get("records", [])
        return records[0]["id"] if records else 0

    def item(self, record):
        """(title, expected minutes, made-in-Japanese anime?, item key)"""
        if self.kind == "series":
            series = self.call("GET", f"series/{record['seriesId']}")
            episode = self.call("GET", f"episode/{record['episodeId']}")
            title = f"{series['title']} S{episode.get('seasonNumber', 0):02d}E{episode.get('episodeNumber', 0):02d}"
            japanese = series.get("seriesType") == "anime" and \
                (series.get("originalLanguage") or {}).get("name") == "Japanese"
            minutes = episode.get("runtime") or series.get("runtime") or 0
            return title, minutes, japanese, f"sonarr:{record['episodeId']}"
        movie = self.call("GET", f"movie/{record['movieId']}")
        genres = [g.lower() for g in movie.get("genres", [])]
        japanese = (movie.get("originalLanguage") or {}).get("name") == "Japanese" and \
            ("animation" in genres or "anime" in genres)
        return f"{movie['title']} ({movie.get('year', '')})", movie.get("runtime") or 0, japanese, \
            f"radarr:{record['movieId']}"

    def failed_releases(self, record) -> int:
        """How many releases of this episode/film Sonarr/Radarr record as
        failed: each replacement marks one, and their history doesn't reset
        with ours. Failures for other reasons count too, which only stops
        replacing sooner. Raises when the history can't be read."""
        if self.kind == "series":
            rows = self.call("GET", f"history/series?seriesId={record['seriesId']}&eventType=4") or []
            return sum(1 for r in rows if r.get("episodeId") == record.get("episodeId"))
        return len(self.call("GET", f"history/movie?movieId={record['movieId']}&eventType=4") or [])

    def reject(self, record):
        """Delete the imported file and mark its release as failed: Sonarr/
        Radarr blocklist it and search for another. False if the release
        isn't known (e.g. imported by hand), so nothing was deleted."""
        download = record.get("downloadId")
        if not download:
            return False
        grabs = self.call("GET", f"history?page=1&pageSize=10&eventType=1&downloadId={urllib.parse.quote(download)}")
        grabs = [r for r in grabs.get("records", []) if r.get("downloadId") == download]
        if not grabs:
            return False
        # The failure is recorded in Sonarr/Radarr first (their history is
        # what the replacement limit counts), the file deleted after: an
        # interruption between the two leaves a file, never a deletion that
        # wasn't counted
        self.call("POST", f"history/failed/{grabs[0]['id']}")
        file_id = (record.get("data") or {}).get("fileId")
        if file_id:
            try:
                self.call("DELETE", f"{'episodefile' if self.kind == 'series' else 'moviefile'}/{file_id}")
            except urllib.error.HTTPError as e:
                if e.code != 404:  # already gone (a season pack's other episode, or by hand)
                    raise
        return True

    def rescan(self, record):
        if self.kind == "series":
            self.call("POST", "command", {"name": "RescanSeries", "seriesId": record["seriesId"]})
        else:
            self.call("POST", "command", {"name": "RescanMovie", "movieId": record["movieId"]})

    def has_file(self, item_key):
        item_id = item_key.split(":")[1]
        if self.kind == "series":
            return bool(self.call("GET", f"episode/{item_id}").get("hasFile"))
        return bool(self.call("GET", f"movie/{item_id}").get("hasFile"))


def jellyfin_updated(path, changes: list | None = None) -> bool:
    """Tell Jellyfin a file changed, so it re-reads its tracks (or <changes>:
    [(path, "Created"/"Deleted"/"Modified")]); False when it didn't take it"""
    try:
        key = (STATE.parent / "dashstatus/jellyfin-key").read_text().strip()
        updates = [{"Path": str(p), "UpdateType": t} for p, t in (changes or [(path, "Modified")])]
        req = urllib.request.Request(
            local("jellyfin") + "/Library/Media/Updated", method="POST",
            data=json.dumps({"Updates": updates}).encode(),
            headers={"Authorization": f'MediaBrowser Token="{key}"', "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).close()
        return True
    except (OSError, urllib.error.URLError):
        return False


# ─── Rounds ──────────────────────────────────────────────────────

class Worker:
    def __init__(self, apps, settings, state):
        self.apps, self.settings, self.state = apps, settings, state
        for key, empty in (("last_import", {}), ("rejections", {}), ("seen", {}), ("recent", []),
                           ("pending", {}), ("failures", {}), ("playback", []), ("playback_pending", {})):
            self.state.setdefault(key, empty)
        self.current: dict | None = None

    def working(self, path, title, what):
        """What's being done right now (shown on the dashboard while a long
        rewrite or OCR runs); None when idle"""
        self.current = {"path": str(path), "title": title, "what": what, "since": int(time.time())} if what else None
        try:
            c.write_json(STATE / "status.json", self.status())
        except OSError:
            pass

    def progress(self, fraction: float) -> None:
        """How far the current rewrite is, and the time left at this pace"""
        if not self.current:
            return
        elapsed = time.time() - self.current["since"]
        self.current["progress"] = int(fraction * 100)
        # Too early to tell for the first few percent
        self.current["eta"] = int(elapsed / fraction * (1 - fraction)) if fraction >= 0.03 else None
        try:
            c.write_json(STATE / "status.json", self.status())
        except OSError:
            pass

    def remember(self, title, what):
        self.state["recent"] = ([{"time": int(time.time()), "title": title, "what": what}]
                                + self.state["recent"])[:30]
        log(f"{title}: {what}")

    def mark(self, path, st=None):
        """What marks a file as done: its size and time, the subtitle files
        next to it and the settings that decide the fixes (any change looks
        at it again), and the fixes known then (FIX_VERSION)"""
        s = self.settings
        decides = [self.audio_language_for(path), s.get("subtitle_languages"), s.get("want"), s.get("stereo_audio"),
                   s.get("ocr_subtitles"), s.get("default_tracks", True), s.get("drop_picture_subtitles", True),
                   sidecar_files(path)]
        fingerprint = hashlib.sha1(json.dumps(decides, sort_keys=True).encode()).hexdigest()[:12]
        return seen_mark(path, st) + [fingerprint]

    def fix(self, path, info, title):
        """Steps 2 to 4; True if anything changed. A file isn't marked done
        while a fix couldn't run (no space) or failed: it's retried later
        (a failing fix FIX_TRIES times, at most every RETRY_FIX_AFTER)."""
        s = self.settings
        changed, done, failed = False, True, []
        audio_language = self.audio_language_for(path)
        if s["stereo_audio"] and path.suffix.lower() in VIDEO_EXTENSIONS:
            source = needs_stereo(info, audio_language)
            if source is not None and not room_for(path, s):
                done = False
            elif source is not None:
                self.working(path, title, "adding stereo audio")
                if add_stereo(path, info, source, self.progress):
                    changed = True
                    self.remember(title, f"added stereo audio (from {source.get('codec_name')}) so browsers play it directly")
                    info = probe(path) or info
                else:
                    failed.append("stereo audio")
        if s["ocr_subtitles"]:
            for lang, stream in ocr_targets(info, path, s["subtitle_languages"], s["want"]):
                self.working(path, title, f"reading the {lang} picture subtitles into text")
                result = ocr(path, lang, stream)
                if result == "written":
                    self.remember(title, f"turned the {lang} picture subtitles into text")
                    changed = True
                elif result == "failed":
                    failed.append(f"{lang} subtitles from pictures")
        # The preferred audio and subtitles as the file's defaults
        if path.suffix.lower() in VIDEO_EXTENSIONS and (s.get("default_tracks", True) or s.get("drop_picture_subtitles", True)):
            info = probe(path) or info
            dropped = redundant_pictures(info, path) if s.get("drop_picture_subtitles", True) else []
            plan = default_tracks(without(info, dropped), path, audio_language, s["subtitle_languages"]) \
                if s.get("default_tracks", True) else {}
            if (plan or dropped) and not room_for(path, s):
                done = False
            elif plan or dropped:
                self.working(path, title, "setting the default audio and subtitles")
                if set_defaults(path, info, plan, dropped, self.progress):
                    changed = True
                    if dropped:
                        langs = ", ".join(sorted({stream_language(st) or "?" for st in dropped}))
                        self.remember(title, f"removed the {langs} picture subtitles (there as text)")
                    if plan:
                        self.remember(title, "set the default " + describe_defaults(probe(path) or info, path))
                else:
                    failed.append("default tracks")
        self.working(path, title, None)
        if changed:
            jellyfin_updated(path)
        key = str(path)
        if failed:
            f = self.state["failures"].get(key, {"count": 0})
            f = {"count": f["count"] + 1, "last": int(time.time()), "what": failed}
            if f["count"] >= FIX_TRIES:
                self.remember(title, f"gave up on {', '.join(failed)} after {f['count']} tries (see the log)")
                self.state["failures"].pop(key, None)
            else:
                self.state["failures"][key] = f
                done = False
        else:
            self.state["failures"].pop(key, None)
        if done:
            self.state["seen"][key] = self.mark(path)
        return changed

    def audio_language_for(self, path) -> str:
        """Anime (files in the anime library) and the rest can prefer
        different audio"""
        anime = self.settings.get("anime_dir")
        if anime and str(path).startswith(str(anime).rstrip("/") + "/"):
            return self.settings.get("anime_audio_language", "")
        return self.settings.get("audio_language", "")

    def handle_import(self, app, record, entry) -> bool:
        """Check and fix one imported file; True when finished with it, False
        to look again later. A problem is confirmed CONFIRM_AFTER later before
        the file is deleted: one failed read or decode isn't proof."""
        path = Path((record.get("data") or {}).get("importedPath", ""))
        if not path.is_file():
            return True  # replaced or deleted since
        title, minutes, japanese, key = app.item(record)
        info = probe(path)
        rejection = self.state["rejections"].get(key)
        if self.settings["check_downloads"]:
            problem = problem_with(info, path, minutes, japanese and self.settings["block_dubs"])
            suspect = entry.get("suspect")
            if problem and not suspect:
                entry["suspect"] = {"problem": problem, "since": int(time.time())}
                entry["next"] = int(time.time()) + CONFIRM_AFTER
                log(f"{title}: {problem}? checking again in {CONFIRM_AFTER // 60} minutes before replacing it")
                return False
            if problem and time.time() - suspect["since"] < CONFIRM_AFTER:
                entry["next"] = suspect["since"] + CONFIRM_AFTER
                return False
            if problem:
                # Ours or Sonarr/Radarr's count, whichever is higher: a lost or
                # damaged state file must never reset the replacement limit
                tries = max((rejection or {}).get("count", 0), app.failed_releases(record))
                if tries >= self.settings["max_replacements"]:
                    self.state["rejections"][key] = dict(rejection or {}, title=title, status="kept", reason=problem, time=int(time.time()))
                    self.remember(title, f"kept although {problem}: {tries} other releases weren't better")
                    notify("Media server: no good release", f"{title}: kept although {problem}; tried {tries} other releases.")
                else:
                    # Counted (and saved) before anything is deleted: an
                    # interruption from here on only counts one too many
                    before = self.state["rejections"].get(key)
                    self.state["rejections"][key] = {"title": title, "count": tries + 1, "reason": problem,
                                                     "time": int(time.time()), "status": "rejecting"}
                    save(self.state)
                    if app.reject(record):
                        self.state["rejections"][key]["status"] = "looking"
                        self.remember(title, f"replaced because {problem}; {app.name} is looking for another release")
                        return True
                    # Not a known release (imported by hand): nothing was done
                    if before is None:
                        self.state["rejections"].pop(key, None)
                    else:
                        self.state["rejections"][key] = before
                    self.remember(title, f"{problem} (imported by hand, so left alone)")
            elif suspect:
                log(f"{title}: fine on a second look (first: {suspect['problem']})")
        # A release that passed (or was kept) after an earlier rejection
        rejection = self.state["rejections"].get(key)
        if rejection and rejection.get("status") == "looking":
            self.state["rejections"][key] = dict(rejection, status="replaced")
        if info is not None and self.fix(path, info, title):
            try:
                app.rescan(record)
            except (OSError, urllib.error.URLError):
                pass
        # Imported and checked; whether Jellyfin plays it is verified next
        playback.queue(self.state, path, title)
        return True

    def check_rejections(self):
        """Notify once about rejected releases nothing replaced"""
        now = time.time()
        for key, r in self.state["rejections"].items():
            if r.get("status") != "looking" or r.get("notified") or now - r["time"] < NOTHING_FOUND_AFTER:
                continue
            app = next(a for a in self.apps if key.startswith(a.name.lower()))
            try:
                if app.has_file(key):
                    r["status"] = "replaced"
                    continue
            except (OSError, urllib.error.URLError, ValueError):
                continue
            r["notified"] = True
            notify("Media server: no better release yet",
                   f"{r['title']}: the download was rejected ({r['reason']}) and no other release has turned up. {app.name} keeps looking.")
        # Forget settled ones after a month
        self.state["rejections"] = {k: r for k, r in self.state["rejections"].items()
                                    if r.get("status") == "looking" or now - r["time"] < 30 * 86400}

    def new_imports(self):
        """New imports go into a queue (kept in state.json) before the
        history position moves on, so none is missed; each is checked until
        it's done, with growing pauses while it can't be"""
        pending = self.state["pending"]
        for app in self.apps:
            last = self.state["last_import"].get(app.name)
            try:
                if last is None:  # first run: only what's imported from now on
                    self.state["last_import"][app.name] = app.latest_import_id()
                    continue
                records = app.imports_after(last)
            except (OSError, urllib.error.URLError, ValueError) as e:
                log(f"couldn't read {app.name}'s history: {e}")
                continue
            for record in records:
                pending.setdefault(f"{app.name}:{record['id']}", {"app": app.name, "record": record, "attempts": 0, "next": 0})
            if records:
                self.state["last_import"][app.name] = records[-1]["id"]
                save(self.state)
        by_name = {a.name: a for a in self.apps}
        now = time.time()
        for key in sorted(pending, key=lambda k: pending[k]["record"]["id"]):
            entry = pending[key]
            app = by_name.get(entry["app"])
            if app is None or entry.get("next", 0) > now:
                continue
            try:
                finished = self.handle_import(app, entry["record"], entry)
            except ToolTrouble as e:
                log(f"can't check imports right now ({e}); retrying")
                break
            except (OSError, urllib.error.URLError, ValueError, KeyError) as e:
                entry["attempts"] += 1
                entry["next"] = int(now) + 300 * entry["attempts"]
                finished = entry["attempts"] >= IMPORT_TRIES
                log(f"couldn't check import {entry['record'].get('id')} from {app.name}: {e}"
                    + (" (giving up)" if finished else f" (retrying, {entry['attempts']}/{IMPORT_TRIES})"))
            if finished:
                pending.pop(key, None)
            save(self.state)
            if operation_running():
                return

    def sweep(self):
        """Steps 2 to 4 for files already in the library, a few per round"""
        done = 0
        seen = self.state["seen"]
        for root in self.settings["library_dirs"]:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    path = Path(dirpath) / name
                    if name.startswith(".") or path.suffix.lower() not in VIDEO_EXTENSIONS:
                        continue
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    if seen.get(str(path)) == self.mark(path, st):
                        continue
                    failure = self.state["failures"].get(str(path))
                    if failure and time.time() - failure["last"] < RETRY_FIX_AFTER:
                        continue
                    try:
                        info = probe(path)
                    except ToolTrouble as e:
                        log(f"can't look at the library right now ({e})")
                        return
                    if info is None:
                        seen[str(path)] = self.mark(path, st)
                        continue
                    if self.fix(path, info, display_name(path)):
                        done += 1
                        save(self.state)
                    if done >= SWEEP_PER_ROUND or operation_running():
                        return
        # Forget files that are gone
        self.state["seen"] = {p: v for p, v in seen.items() if os.path.exists(p)}
        self.state["failures"] = {p: v for p, v in self.state["failures"].items() if os.path.exists(p)}

    # ─── MKV → MP4, overnight (repackage.py) ─────────────────────

    def repackage(self, now: float | None = None) -> None:
        """A few MKV files a round, between repackage.NIGHT hours; files that
        don't fit (or were started) are remembered by their mark, so they're
        not looked at again until they change. Conversions a crash or a
        failed rescan left unfinished are finished first, at any hour."""
        from mediaserver import repackage as rp
        for key in list(self.state.get("repackage_pending", {})):
            self.finish_repackage(key)
        if not self.settings.get("repackage_mp4", True) or not rp.night(now):
            return
        skip = self.state.setdefault("repackage_skip", {})
        jf = playback.Jellyfin(STATE.parent)
        try:
            items = jf.items()
        except c.HTTP_ERRORS:
            return   # Jellyfin isn't answering: not tonight
        unwatched = rp.unwatched(items, jf.user_data)

        def busy(path) -> bool:
            playing = jf.now_playing()
            return playing is None or str(path) in playing   # unknown counts as busy
        done = 0
        for root in self.settings["library_dirs"]:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in sorted(filenames):
                    path = Path(dirpath) / name
                    if name.startswith(".") or path.suffix.lower() != ".mkv":
                        continue
                    try:
                        mark = seen_mark(path)
                    except OSError:
                        continue
                    if (skip.get(str(path)) or {}).get("mark") == mark:
                        continue
                    if done >= rp.PER_ROUND or operation_running():
                        return
                    if self.repackage_one(path, mark, unwatched, skip, busy):
                        done += 1
                        save(self.state)
        self.state["repackage_skip"] = {p: v for p, v in skip.items() if os.path.exists(p)}

    def repackage_one(self, path: Path, mark: list, unwatched, skip: dict, busy=lambda p: False) -> bool:
        """True when it was repackaged. Nothing next to the file is ever
        overwritten: subtitles and the MP4 are written to temporary files,
        checked, and published under names nothing has yet. Watch state is
        checked again just before the switch."""
        from mediaserver import repackage as rp
        title = display_name(path)

        def not_now(reason: str) -> bool:
            skip[str(path)] = {"mark": mark, "reason": reason}
            return False
        info = probe(path)
        if info is None:
            return not_now("unreadable")
        subtitles, why = rp.fits(info)
        if why:
            return not_now(why)
        watched = unwatched(path)
        if watched is None:
            return False   # Jellyfin doesn't list it yet: another night
        if not watched:
            return not_now("started or watched (its progress would be lost)")
        new = path.with_suffix(".mp4")
        if os.path.lexists(new):
            return not_now(f"{new.name} is already there")
        if not room_for(path, self.settings):
            return False
        st = path.stat()
        before = (st.st_ino, st.st_size, st.st_mtime)
        self.working(path, title, "repackaging as MP4 for Apple devices")
        temps: list[Path] = []
        planned: list[tuple[Path, Path]] = []   # (temporary, final) subtitle files
        out = path.with_name(f".{path.stem}.mp4.postimport")
        temps.append(out)
        committed = False
        try:
            taken: set = set()
            for stream in subtitles:
                final = rp.sidecar_name(path, stream, stream_language(stream), taken)
                taken.add(final)
                tmp = final.with_name(f".{final.stem}.postimport.srt")   # hidden; .srt so its cues can be counted
                temps.append(tmp)
                code, errors = run_ffmpeg(rp.extract_command(path, stream, tmp), 0)
                if code != 0 or not rp.complete(sidecar_cues(tmp), stream_cues(path, stream["index"])):
                    log(f"couldn't save all the subtitles of {path.name} as {final.name} {errors.strip()[-200:]}; kept it as it is")
                    return not_now("its subtitles couldn't be saved whole")
                planned.append((tmp, final))
            code, errors = run_ffmpeg(rp.remux_command(path, out, info), duration(info), self.progress)
            if code != 0 or not rp.checks_out(info, probe(out)):
                why = errors.strip()[-200:] or "the new file didn't check out"
                log(f"couldn't repackage {path.name} as MP4 ({why}); kept it")
                return not_now("repackaging failed")
            # The last look before anything changes: someone may have started
            # it while it was being copied
            if busy(path) or unwatched(path) is not True:
                log(f"{path.name} was started while being repackaged; kept the MKV")
                return not_now("started or watched (its progress would be lost)")
            if not rp.same(path, before):
                log(f"{path.name} changed while being repackaged; kept it")
                return False
            # Recorded before any name changes, with the identity (inode)
            # each published file will have (a hard link keeps it): a crash
            # from here on is finished or undone next round, removing only
            # files that are provably ours
            os.chmod(out, st.st_mode & 0o777)
            self.state.setdefault("repackage_pending", {})[str(new)] = {
                "old": str(path), "old_id": list(before), "new_ino": out.stat().st_ino, "title": title,
                "subtitles": [{"path": str(final), "ino": tmp.stat().st_ino} for tmp, final in planned],
                "temps": [str(f) for f in temps], "steps": ["switch", "rescan", "jellyfin", "playback"]}
            save(self.state)
            for tmp, final in planned:
                rp.publish(tmp, final)
            rp.publish(out, new)
            committed = True
        except OSError as e:
            log(f"couldn't repackage {path.name}: {e}; kept it")
            return False
        finally:
            if not committed and str(new) in self.state.get("repackage_pending", {}):
                self.abandon_repackage(str(new), "it didn't finish")
            for f in temps:
                f.unlink(missing_ok=True)
            self.working(path, title, None)
        self.finish_repackage(str(new))
        return True

    @staticmethod
    def ours(path, ino) -> bool:
        """<path> is the very file this published (same inode)"""
        try:
            return os.stat(path).st_ino == ino
        except OSError:
            return False

    def abandon_repackage(self, key: str, reason: str) -> None:
        """Undone: the subtitles and the MP4 this published go (only those:
        checked by inode), its temporary files too; the MKV stays"""
        entry = self.state.get("repackage_pending", {}).pop(key, None)
        if not entry:
            return
        for sub in entry.get("subtitles", []):
            if self.ours(sub["path"], sub["ino"]):
                Path(sub["path"]).unlink(missing_ok=True)
        if Path(entry["old"]).exists() and self.ours(key, entry.get("new_ino")):
            Path(key).unlink(missing_ok=True)
        for f in entry.get("temps", []):
            Path(f).unlink(missing_ok=True)
        save(self.state)
        log(f"{entry.get('title', key)}: repackaging undone ({reason}); the MKV stays")

    def watch_checks(self):
        """(unwatched, busy) from Jellyfin now; None when it isn't answering"""
        from mediaserver import repackage as rp
        jf = playback.Jellyfin(STATE.parent)
        try:
            items = jf.items()
        except c.HTTP_ERRORS:
            return None
        playing = jf.now_playing()
        if playing is None:
            return None
        return rp.unwatched(items, jf.user_data), (lambda p: str(p) in playing)

    def finish_repackage(self, key: str) -> None:
        """The steps of a conversion left, each saved when done. Before the
        MKV goes, everything is checked again (it may be a resumed one): the
        MP4 is the one published here, the MKV hasn't changed, and nobody is
        watching or has started it; otherwise it's undone."""
        from mediaserver import repackage as rp
        entry = self.state.get("repackage_pending", {}).get(key)
        if not entry:
            return
        new, old = Path(key), Path(entry["old"])
        while entry["steps"]:
            step = entry["steps"][0]
            if step == "switch":
                if not self.ours(new, entry.get("new_ino")):
                    return self.abandon_repackage(key, "the MP4 wasn't published, or isn't the one made here")
                if old.exists():
                    if not rp.same(old, tuple(entry.get("old_id") or ())):
                        return self.abandon_repackage(key, "the MKV changed")
                    checks = self.watch_checks()
                    if checks is None:
                        return   # Jellyfin isn't answering: checked next round
                    unwatched, busy = checks
                    if busy(old) or unwatched(old) is not True:
                        return self.abandon_repackage(key, "someone started it, or Jellyfin doesn't list it")
                    old.unlink()
                self.remember(entry["title"], "repackaged as MP4 so Apple devices and the app play it directly (nothing re-encoded)")
            elif step == "rescan" and not self.rescan_owner(new):
                return   # Sonarr/Radarr didn't answer: next round
            elif step == "jellyfin" and not jellyfin_updated(new, [(old, "Deleted"), (new, "Created")]):
                return   # Jellyfin didn't take it: next round
            elif step == "playback":
                playback.queue(self.state, new, entry["title"])
            entry["steps"].pop(0)
            save(self.state)
        self.state["repackage_pending"].pop(key, None)
        save(self.state)

    def rescan_owner(self, path: Path) -> bool:
        """Sonarr/Radarr rescan the title whose folder holds <path>, so they
        track the new file; False when they couldn't be asked"""
        for app in self.apps:
            try:
                for item in app.call("GET", "series" if app.kind == "series" else "movie") or []:
                    folder = item.get("path")
                    if folder and str(path).startswith(folder.rstrip("/") + "/"):
                        app.rescan({"seriesId": item["id"]} if app.kind == "series" else {"movieId": item["id"]})
                        return True
            except (OSError, urllib.error.URLError, ValueError) as e:
                log(f"couldn't ask {app.name} to rescan after repackaging {path.name}: {e}; retried next round")
                return False
        return True   # neither has it (added by hand): nothing to rescan

    def status(self):
        rejections = self.state["rejections"].values()
        return {
            "updated": int(time.time()),
            "recent": self.state["recent"][:10],
            "looking": [{"title": r["title"], "reason": r["reason"], "since": r["time"]}
                        for r in rejections if r.get("status") == "looking"],
            "kept": [{"title": r["title"], "reason": r["reason"]} for r in rejections if r.get("status") == "kept"],
            # For the dashboard's pipeline: what's being worked on, imports
            # waiting to be checked (by Sonarr/Radarr ids), files whose fixes
            # are being retried
            "current": self.current,
            "queued": [{"app": e["app"], "movieId": e["record"].get("movieId"), "seriesId": e["record"].get("seriesId"),
                        "episodeId": e["record"].get("episodeId"), "path": (e["record"].get("data") or {}).get("importedPath"),
                        "suspect": (e.get("suspect") or {}).get("problem")} for e in self.state["pending"].values()],
            "retrying": sorted(self.state["failures"]),
            # Playback verified (Jellyfin opens it, tracks, a short stream),
            # separate from imported
            "playback": self.state["playback"][:50],
            "playback_waiting": len(self.state["playback_pending"]),
        }


def describe_defaults(info, path) -> str:
    """"audio jpn, subtitles en (text)" for the log and dashboard"""
    parts = []
    audio = next((st for st in streams(info, "audio") if (st.get("disposition") or {}).get("default")), None)
    if audio:
        parts.append(f"audio {stream_language(audio) or 'untagged'}")
    sub = next((st for st in streams(info, "subtitle") if (st.get("disposition") or {}).get("default") and not is_forced(st)), None)
    parts.append(f"subtitles {stream_language(sub) or 'untagged'}" if sub else "subtitles from the file next to it")
    return ", ".join(parts)


def seen_mark(path, st=None):
    """What marks a file as done: its size and time, and the fixes known
    then (a newer FIX_VERSION looks at it again)"""
    st = st or os.stat(path)
    return [st.st_size, int(st.st_mtime), FIX_VERSION]


def display_name(path):
    """"Movie (2020) Bluray-1080p Proper" → "Movie (2020)"; episodes keep
    "Show - S01E02 - Title" (Sonarr/Radarr's naming, quality last)"""
    return c.strip_quality(Path(path).stem)


def save(state):
    c.write_json(STATE / "state.json", state)


def load_state() -> dict:
    """The worker's record; empty when there's none. An unreadable one is
    set aside (not silently replaced) and reported; starting fresh is safe
    because the replacement limit also counts Sonarr/Radarr's history."""
    f = STATE / "state.json"
    if not f.exists():
        return {}
    data = read_json(f)
    if isinstance(data, dict):
        return data
    aside = f.with_name(f"state.json.unreadable-{int(time.time())}")
    try:
        os.replace(f, aside)
    except OSError as e:
        # Not preserved: don't carry on (and later overwrite it) as if it were
        raise StateTrouble(f"{f} is unreadable and couldn't be set aside ({e.strerror}); nothing is checked or "
                           "replaced until it's fixed or removed") from None
    log(f"{f} was unreadable; set aside as {aside.name}, starting with a fresh record")
    notify("Media server: post-import record was damaged",
           f"It was set aside ({aside.name}); checks continue. Replacement limits still hold (they also use Sonarr/Radarr's history).")
    return {}


def load_settings():
    return {**DEFAULTS, **read_json(STATE / "settings.json", {})}


def by_hand(mode, path):
    """--check FILE / --fix FILE"""
    path = Path(path).resolve()
    info = probe(path)
    settings = load_settings()
    if mode == "--check":
        print(problem_with(info, path, 0, False) or "plays fine")
        if info:
            worker = Worker([], settings, {})
            source = needs_stereo(info, worker.audio_language_for(path))
            print(f"needs stereo audio (from {source['codec_name']})" if source else "audio plays in browsers")
            for lang, s in ocr_targets(info, path, settings["subtitle_languages"], settings["want"]):
                print(f"{lang} subtitles only as pictures (track {s['index']})")
            dropped = redundant_pictures(info, path) if settings.get("drop_picture_subtitles", True) else []
            if dropped:
                print(f"picture subtitles to remove (there as text): tracks {[st['index'] for st in dropped]}")
            plan = default_tracks(without(info, dropped), path, worker.audio_language_for(path), settings["subtitle_languages"])
            print(f"default tracks to change: {plan}" if plan else "default tracks: as preferred")
        return 0
    if info is None:
        print("can't read the file")
        return 1
    with lock.worker_round(STATE.parent) as ok, lock.exclusive(STATE / "run.lock") as mine:
        if not ok:
            print("an install or other operation is running; try again when it's done")
            return 1
        if not mine:
            print("the post-import service is working on a file right now; try again in a minute")
            return 1
        try:
            state = load_state()
        except StateTrouble as e:
            print(e)
            return 1
        Worker([], settings, state).fix(path, info, display_name(path))
        save(state)
    return 0


class Rounds:
    """The service's loop state: what's been said already (once, not every round)"""

    def __init__(self):
        self.waiting = self.stopped = False
        self.apps = [Arr("Sonarr", local("sonarr"), "series"), Arr("Radarr", local("radarr"), "movie")]
        self.prowlarr = Arr("Prowlarr", local("prowlarr"), "", "v1")

    def round(self) -> None:
        # The worker lock for the whole round: an install, restore or
        # deletion holds it while it runs, and waits for a round underway
        # Two locks: the workers' (shared: keeps installs out) and
        # postimport's own (exclusive: the service and --fix never rewrite
        # files or save the record at the same time)
        with lock.worker_round(STATE.parent) as ok, lock.exclusive(STATE / "run.lock") as mine:
            if not ok:
                if not self.waiting:
                    log("an install or other operation is running; waiting")
                self.waiting = True
                return
            if not mine:
                log("a --fix by hand is running; this round waits for it")
                return
            self.waiting = False
            try:
                state = load_state()
            except StateTrouble as e:
                if not self.stopped:
                    log(str(e))
                    notify("Media server: post-import checks stopped", str(e))
                self.stopped = True
                return
            self.stopped = False
            worker = Worker(self.apps, load_settings(), state)
            try:
                worker.new_imports()
                worker.check_rejections()
                worker.sweep()
                worker.repackage()
                playback.run(worker.state, STATE.parent)
                # What isn't arriving: searched again, and why (hourly)
                offline = c.read_text(STATE.parent / "netwatch/connection") == "offline"
                stuck.run(self.apps + [self.prowlarr], worker.settings, STATE / "stuck.json", offline)
            except ToolTrouble as e:
                log(f"skipping this round: {e}")
            except Exception as e:  # keep running; what's saved so far stays
                log(f"round failed: {e!r}")
            save(worker.state)
            c.write_json(STATE / "status.json", worker.status())


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 3 and sys.argv[1] in ("--check", "--fix"):
        return by_hand(sys.argv[1], sys.argv[2])
    rounds = Rounds()
    while True:
        rounds.round()
        time.sleep(int(os.environ.get("POSTIMPORT_INTERVAL", "60")))


if __name__ == "__main__":
    sys.exit(main())
