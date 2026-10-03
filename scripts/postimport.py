#!/usr/bin/env python3
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

Files already in the library get steps 2 and 3 too, a few per round. A file
whose torrent is still seeding is only rewritten when there's plenty of free
space, since the torrent keeps its own copy until seeding ends. Nothing runs
while an install or other operation holds setup's lock.

Rewrites are safe: the new file is written next to the old one, checked
(same duration, every stream present) and only then moved over it; if the
original changed in the meantime the new one is thrown away.

Settings come from $POSTIMPORT_STATE/settings.json, which setup writes from
config.toml; progress is kept in state.json, and status.json is what the
dashboard and `nix run .#doctor` show.

By hand: postimport.py --check FILE (what's wrong with it, changes nothing)
or postimport.py --fix FILE (steps 2 and 3 now).

Environment: POSTIMPORT_CONFIG (~/media/config), POSTIMPORT_STATE
(~/media/.state/postimport), POSTIMPORT_INTERVAL.
"""
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
    "subtitle_languages": ["en"],
    "want": "first",
    "min_free_gb": 10,
    "warn_free_gb": 50,
    "library_dirs": [],
}
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v"}
# Audio every browser plays; anything else makes Jellyfin convert
BROWSER_AUDIO = {"aac", "mp3", "opus", "flac", "vorbis"}
TEXT_SUBTITLES = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}
SIDECAR_SUBTITLES = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
NOTHING_FOUND_AFTER = 6 * 3600
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


def log(message):
    print(f"{time.strftime('%F %T')} {message}", flush=True)


def language_of(code):
    """Any tag ("eng", "en", "en-US") → ISO 639-1, or "" when unknown/untagged"""
    code = (code or "").lower().split("-")[0]
    return TAG_TO_LANGUAGE.get(code, "")


def tags(stream):
    return {k.lower(): v for k, v in (stream.get("tags") or {}).items()}


def stream_language(stream):
    return language_of(tags(stream).get("language"))


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def operation_running():
    """An install/update/restore/e2e holds setup's lock (and is alive)"""
    try:
        owner = int((LOCK / "pid").read_text().strip())
        os.kill(owner, 0)
        return True
    except (OSError, ValueError):
        return False


def notify(title, message):
    log(f"notify: {title}: {message}")
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, check=False)


def background(cmd):
    """Run with background priority (low CPU, efficiency cores) on macOS"""
    return (["/usr/sbin/taskpolicy", "-b"] + cmd) if os.path.exists("/usr/sbin/taskpolicy") else cmd


# ─── Inspecting files ────────────────────────────────────────────

def probe(path):
    """ffprobe's streams and format, or None when it can't read the file"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout)
    except ValueError:
        return None
    info.setdefault("streams", [])
    return info


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
    for start in points:
        result = subprocess.run(background(
            ["ffmpeg", "-nostdin", "-v", "error", "-hwaccel", "videotoolbox", "-ss", f"{start:.0f}",
             "-progress", "pipe:1", "-i", str(path), "-map", "0:v:0", "-t", "8", "-f", "null", "-"]),
            capture_output=True, text=True, check=False)
        frames = [int(m) for m in re.findall(r"^frame=(\d+)", result.stdout, re.M)]
        if result.returncode != 0 or not frames or frames[-1] == 0:
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


def add_stereo(path, info, source):
    """Rewrite the file with a stereo AAC track; True when done"""
    path = Path(path)
    st = path.stat()
    before = (st.st_ino, st.st_size, st.st_mtime)
    out = path.with_name(f".{path.name}.postimport")
    try:
        result = subprocess.run(background(stereo_command(path, out, info, source, aac_encoder())),
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            log(f"couldn't add stereo audio to {path.name}: {result.stderr.strip()[-300:]}")
            return False
        new = probe(out)
        expected = dict(track_counts(info), audio=track_counts(info)["audio"] + 1)
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


# ─── Subtitles ───────────────────────────────────────────────────

def sidecar_languages(path):
    """Languages of the subtitle files next to the video (Movie.en.srt,
    Movie.en.hi.srt, Movie.eng.forced.srt, ...)"""
    path = Path(path)
    found = set()
    try:
        siblings = list(path.parent.iterdir())
    except OSError:
        return found
    for f in siblings:
        if f.suffix.lower() in SIDECAR_SUBTITLES and f.name.startswith(path.stem + "."):
            for part in f.name[len(path.stem) + 1:].split(".")[:-1]:
                if language_of(part):
                    found.add(language_of(part))
                    break
    return found


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
    """Read a PGS track into Movie.<lang>.srt next to the video; True if written"""
    path = Path(path)
    dest = path.with_name(f"{path.stem}.{lang}.srt")
    if dest.exists():
        return False
    with tempfile.TemporaryDirectory(dir=STATE) as tmp:
        sup = Path(tmp) / f"subtitles.{lang}.sup"
        extract = subprocess.run(background(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", f"0:{stream['index']}",
             "-c", "copy", str(sup)]), capture_output=True, text=True, check=False)
        if extract.returncode != 0:
            log(f"couldn't extract the {lang} picture subtitles of {path.name}")
            return False
        subprocess.run(background(["pgsrip", "-l", lang, "-w", "2", str(sup)]),
                       capture_output=True, text=True, check=False)
        srt = sup.with_suffix(".srt")
        text = srt.read_text(errors="replace") if srt.exists() else ""
        if text.count("-->") < 10:
            log(f"reading the {lang} picture subtitles of {path.name} gave nothing usable")
            return False
        tmp_dest = dest.with_name(f".{dest.name}.tmp")
        tmp_dest.write_text(text)
        os.replace(tmp_dest, dest)
    return True


# ─── Sonarr and Radarr ───────────────────────────────────────────

class Arr:
    """A Sonarr/Radarr API client; key from its config.xml"""

    def __init__(self, name, url, kind):
        self.name, self.url, self.kind = name, url, kind  # kind: "series" | "movie"

    def key(self):
        try:
            m = re.search(r"<ApiKey>(.*?)</ApiKey>", (CONFIG / self.name.lower() / "config.xml").read_text())
            return m.group(1) if m else ""
        except OSError:
            return ""

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}/api/v3/{path}", data=data, method=method,
                                     headers={"X-Api-Key": self.key(), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def imports_after(self, last_id):
        """Import events newer than last_id, oldest first"""
        page = self.call("GET", "history?page=1&pageSize=50&sortKey=date&sortDirection=descending&eventType=3")
        return sorted((r for r in page.get("records", []) if r["id"] > last_id), key=lambda r: r["id"])

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
        file_id = (record.get("data") or {}).get("fileId")
        if file_id:
            try:
                self.call("DELETE", f"{'episodefile' if self.kind == 'series' else 'moviefile'}/{file_id}")
            except urllib.error.HTTPError as e:
                if e.code != 404:  # already gone (a season pack's other episode, or by hand)
                    raise
        self.call("POST", f"history/failed/{grabs[0]['id']}")
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


def jellyfin_updated(path):
    """Tell Jellyfin a file changed, so it re-reads its tracks"""
    try:
        key = (STATE.parent / "dashstatus/jellyfin-key").read_text().strip()
        req = urllib.request.Request(
            "http://127.0.0.1:8096/Library/Media/Updated", method="POST",
            data=json.dumps({"Updates": [{"Path": str(path), "UpdateType": "Modified"}]}).encode(),
            headers={"Authorization": f'MediaBrowser Token="{key}"', "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).close()
    except (OSError, urllib.error.URLError):
        pass


# ─── Rounds ──────────────────────────────────────────────────────

class Worker:
    def __init__(self, apps, settings, state):
        self.apps, self.settings, self.state = apps, settings, state
        self.state.setdefault("last_import", {})
        self.state.setdefault("rejections", {})
        self.state.setdefault("seen", {})
        self.state.setdefault("recent", [])

    def remember(self, title, what):
        self.state["recent"] = ([{"time": int(time.time()), "title": title, "what": what}]
                                + self.state["recent"])[:30]
        log(f"{title}: {what}")

    def fix(self, path, info, title):
        """Steps 2 and 3; True if anything changed. A file that can't be
        rewritten for lack of space isn't marked done, so it's retried."""
        s = self.settings
        changed, done = False, True
        if s["stereo_audio"] and path.suffix.lower() in VIDEO_EXTENSIONS:
            source = needs_stereo(info, s["audio_language"])
            if source is not None and not room_for(path, s):
                done = False
            elif source is not None and add_stereo(path, info, source):
                changed = True
                self.remember(title, f"added stereo audio (from {source.get('codec_name')}) so browsers play it directly")
                info = probe(path) or info
        if s["ocr_subtitles"]:
            for lang, stream in ocr_targets(info, path, s["subtitle_languages"], s["want"]):
                if ocr(path, lang, stream):
                    self.remember(title, f"turned the {lang} picture subtitles into text")
                    changed = True
        if changed:
            jellyfin_updated(path)
        if done:
            st = path.stat()
            self.state["seen"][str(path)] = [st.st_size, int(st.st_mtime)]
        return changed

    def handle_import(self, app, record):
        path = Path((record.get("data") or {}).get("importedPath", ""))
        if not path.is_file():
            return  # replaced or deleted since
        title, minutes, japanese, key = app.item(record)
        info = probe(path)
        rejection = self.state["rejections"].get(key)
        if self.settings["check_downloads"]:
            problem = problem_with(info, path, minutes, japanese and self.settings["block_dubs"])
            if problem:
                tries = (rejection or {}).get("count", 0)
                if tries >= self.settings["max_replacements"]:
                    self.state["rejections"][key] = dict(rejection, status="kept", reason=problem, time=int(time.time()))
                    self.remember(title, f"kept although {problem}: {tries} other releases weren't better")
                    notify("Media server: no good release", f"{title}: kept although {problem}; tried {tries} other releases.")
                elif app.reject(record):
                    self.state["rejections"][key] = {"title": title, "count": tries + 1, "reason": problem,
                                                     "time": int(time.time()), "status": "looking"}
                    self.remember(title, f"replaced because {problem}; {app.name} is looking for another release")
                    return
                else:
                    self.remember(title, f"{problem} (imported by hand, so left alone)")
        # A release that passed (or was kept) after an earlier rejection
        rejection = self.state["rejections"].get(key)
        if rejection and rejection.get("status") == "looking":
            self.state["rejections"][key] = dict(rejection, status="replaced")
        if info is not None and self.fix(path, info, title):
            try:
                app.rescan(record)
            except (OSError, urllib.error.URLError):
                pass

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
                try:
                    self.handle_import(app, record)
                except (OSError, urllib.error.URLError, ValueError, KeyError) as e:
                    log(f"couldn't check import {record.get('id')} from {app.name}: {e}")
                self.state["last_import"][app.name] = record["id"]
                save(self.state)

    def sweep(self):
        """Steps 2 and 3 for files already in the library, a few per round"""
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
                    if seen.get(str(path)) == [st.st_size, int(st.st_mtime)]:
                        continue
                    info = probe(path)
                    if info is None:
                        seen[str(path)] = [st.st_size, int(st.st_mtime)]
                        continue
                    if self.fix(path, info, display_name(path)):
                        done += 1
                        save(self.state)
                    if done >= SWEEP_PER_ROUND or operation_running():
                        return
        # Forget files that are gone
        self.state["seen"] = {p: v for p, v in seen.items() if os.path.exists(p)}

    def status(self):
        rejections = self.state["rejections"].values()
        return {
            "updated": int(time.time()),
            "recent": self.state["recent"][:10],
            "looking": [{"title": r["title"], "reason": r["reason"], "since": r["time"]}
                        for r in rejections if r.get("status") == "looking"],
            "kept": [{"title": r["title"], "reason": r["reason"]} for r in rejections if r.get("status") == "kept"],
        }


def display_name(path):
    """"Movie (2020) Bluray-1080p Proper" → "Movie (2020)"; episodes keep
    "Show - S01E02 - Title" (Sonarr/Radarr's naming, quality last)"""
    return re.sub(r" (Remux|Bluray|WEBDL|WEBRip|HDTV|DVD|SDTV|Raw-HD|BR-DISK)-\S+( Proper| Repack)*$", "", Path(path).stem)


def save(state):
    write_json(STATE / "state.json", state)


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
            source = needs_stereo(info, settings["audio_language"])
            print(f"needs stereo audio (from {source['codec_name']})" if source else "audio plays in browsers")
            for lang, s in ocr_targets(info, path, settings["subtitle_languages"], settings["want"]):
                print(f"{lang} subtitles only as pictures (track {s['index']})")
        return 0
    if info is None:
        print("can't read the file")
        return 1
    state = read_json(STATE / "state.json", {})
    Worker([], settings, state).fix(path, info, display_name(path))
    save(state)
    return 0


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 3 and sys.argv[1] in ("--check", "--fix"):
        return by_hand(sys.argv[1], sys.argv[2])
    apps = [Arr("Sonarr", "http://127.0.0.1:8989", "series"), Arr("Radarr", "http://127.0.0.1:7878", "movie")]
    waiting = False
    while True:
        if operation_running():
            if not waiting:
                log("an install or other operation is running; waiting")
            waiting = True
        else:
            waiting = False
            worker = Worker(apps, load_settings(), read_json(STATE / "state.json", {}))
            worker.new_imports()
            worker.check_rejections()
            worker.sweep()
            save(worker.state)
            write_json(STATE / "status.json", worker.status())
        time.sleep(int(os.environ.get("POSTIMPORT_INTERVAL", "60")))


if __name__ == "__main__":
    sys.exit(main())
