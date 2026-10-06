"""MKV files repackaged as MP4, overnight, so Apple's players (Safari, the
Media Server app, iPhone, iPad, Apple TV) open them directly instead of
Jellyfin repacking them on the fly while you watch. The picture and sound
are copied as they are: nothing is re-encoded.

Only a file that fits MP4 whole, so nothing is lost:
- video H.264, HEVC (tagged hvc1, which Apple's players want) or AV1;
- every audio track in a format MP4 holds well (AAC, AC-3, E-AC-3, MP3,
  ALAC, FLAC): one with DTS or TrueHD stays MKV;
- subtitles only as plain text, saved first as .srt files next to the
  video (Jellyfin shows those the same way). Styled subtitles (ASS, with
  their fonts) and picture subtitles stay MKV;
- and nobody has started watching it: a new file name is a new Jellyfin
  item, which would lose its progress and watched mark.

The new file is checked (the same video and audio tracks, the same length)
before the MKV is removed; postimport then has Sonarr/Radarr rescan the
title, tells Jellyfin, and checks the new file streams. Run from the
postimport service between NIGHT hours, a few files a round.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

NIGHT = range(1, 7)            # local hours it runs in (1:00 to 6:59)
PER_ROUND = 2
# AV1 and FLAC too: a device that can't decode them has Jellyfin convert
# that track whatever the container, so MP4 costs nothing there. DTS and
# TrueHD stay MKV: MP4 can carry them, but players that pass them through
# to a receiver handle them less well in MP4
VIDEO = {"h264", "hevc", "av1"}
AUDIO = {"aac", "ac3", "eac3", "mp3", "alac", "flac"}
PLAIN_TEXT = {"subrip", "webvtt", "mov_text", "text"}


def night(now: float | None = None) -> bool:
    return time.localtime(time.time() if now is None else now).tm_hour in NIGHT


def video_streams(info: dict) -> list:
    """The real video tracks (not cover pictures)"""
    return [s for s in info.get("streams", []) if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")]


def fits(info: dict) -> tuple[list, str]:
    """(the subtitle tracks to save as .srt, "") when the file fits MP4
    whole; ([], why not) otherwise"""
    streams = info.get("streams", [])
    video = video_streams(info)
    if not video:
        return [], "no video track"
    if any(s.get("codec_name") not in VIDEO for s in video):
        return [], f"video in {video[0].get('codec_name')}, not H.264, HEVC or AV1"
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    other = sorted({s.get("codec_name") or "?" for s in audio if s.get("codec_name") not in AUDIO})
    if other:
        return [], f"audio in {', '.join(other)}, which players handle less well in MP4"
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    kept = sorted({s.get("codec_name") or "?" for s in subtitles if s.get("codec_name") not in PLAIN_TEXT})
    if kept:
        return [], f"subtitles in {', '.join(kept)} (styled or pictures), which would be lost"
    fonts = [s for s in streams if s.get("codec_type") == "attachment"
             and not str((s.get("tags") or {}).get("mimetype", "")).startswith("image/")]
    if fonts:
        return [], "embedded fonts, which would be lost"
    return subtitles, ""


def sidecar_name(path: Path, stream: dict, language: str, taken: set) -> Path:
    """Movie.en.srt, Movie.en.forced.srt, Movie.en.sdh.srt; when that name
    is taken (a subtitle Bazarr already put there stays), Movie.en.3.srt,
    then Movie.en.3.2.srt and so on: a name nothing has yet"""
    d = stream.get("disposition") or {}
    parts = [language] if language else []
    if d.get("forced"):
        parts.append("forced")
    elif d.get("hearing_impaired"):
        parts.append("sdh")
    candidates = [[*parts], [*parts, str(stream["index"])]] + [[*parts, str(stream["index"]), str(n)] for n in range(2, 1000)]
    for extra in candidates:
        name = path.with_name(".".join([path.stem, *extra, "srt"]))
        if name not in taken and not name.exists() and not os.path.lexists(name):
            return name
    raise FileExistsError(f"no free subtitle name next to {path.name}")


def complete(saved: tuple | None, source: tuple | None) -> bool:
    """The saved .srt has every cue the track has, to its last one"""
    if not saved or not source:
        return False
    (cues, last), (packets, source_last) = saved, source
    return packets > 0 and cues >= packets and abs(last - source_last) <= 2


def publish(tmp: Path, final: Path) -> None:
    """tmp → final without ever replacing a file already there (a hard link
    fails if final exists), then tmp goes"""
    os.link(tmp, final)
    tmp.unlink()


def extract_command(path: Path, stream: dict, out: Path) -> list:
    return ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", f"0:{stream['index']}",
            "-c:s", "srt", "-f", "srt", str(out)]


def remux_command(path: Path, out: Path, info: dict) -> list:
    """Video (no cover pictures) and audio copied into MP4, with chapters
    and the track flags; subtitles left out (saved as .srt first)"""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", "0:V", "-map", "0:a?",
           "-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
    if any(s.get("codec_name") == "hevc" for s in video_streams(info)):
        cmd += ["-tag:v", "hvc1"]
    return cmd + ["-movflags", "+faststart", "-f", "mp4", str(out)]


def checks_out(old: dict, new: dict | None) -> bool:
    """The same video and audio tracks, and the same length"""
    if not new:
        return False

    def count(info, kind):
        return len([s for s in info.get("streams", []) if s.get("codec_type") == kind])

    def length(info):
        try:
            return float((info.get("format") or {}).get("duration") or 0)
        except ValueError:
            return 0.0
    return (len(video_streams(new)) == len(video_streams(old)) and count(new, "audio") == count(old, "audio")
            and count(new, "subtitle") == 0 and abs(length(new) - length(old)) <= 2)


def unwatched(items: dict, user_data) -> Callable[[object], "bool | None"]:
    """A function path → True (nobody started it), False, or None (Jellyfin
    doesn't list it or didn't answer: not now)"""
    def check(path) -> bool | None:
        item = items.get(str(path))
        if not item:
            return None
        data = user_data(item)
        if data is None:
            return None
        return not any(d.get("Played") or (d.get("PlaybackPositionTicks") or 0) > 0 or (d.get("PlayCount") or 0) > 0
                       for d in data)
    return check


def same(path: Path, before: tuple) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return (st.st_ino, st.st_size, st.st_mtime) == before
