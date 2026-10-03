# Tests use possibly-None results directly (a None fails the test anyway)
# and attach recorders to objects:
# pyright: reportOptionalSubscript=false, reportArgumentType=false, reportAttributeAccessIssue=false
"""Tests for mediaserver/postimport.py: what counts as a bad download, which
files get a stereo track or OCR'd subtitles, and what happens on
rejection. Sonarr/Radarr are fakes; ffmpeg is used for real when it's on
PATH (nix run .#unit provides it).

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mediaserver import postimport as pi

pi.log = lambda message: None  # quiet; the tests check results, not logs


def audio(index, codec, language=None, default=False, channels=6):
    s = {"index": index, "codec_type": "audio", "codec_name": codec, "channels": channels,
         "disposition": {"default": int(default)}}
    if language:
        s["tags"] = {"language": language}
    return s


def sub(index, codec, language, forced=False, title=""):
    return {"index": index, "codec_type": "subtitle", "codec_name": codec,
            "disposition": {"forced": int(forced)}, "tags": {"language": language, "title": title}}


def media(*streams, length=3600.0):
    return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc", "disposition": {}}] + list(streams),
            "format": {"duration": str(length)}}


def srt(cues: int, until: float) -> str:
    """A SubRip file with <cues> lines spread up to <until> seconds"""
    def ts(t):
        return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d},000"
    step = until / cues
    return "".join(f"{i + 1}\n{ts(i * step)} --> {ts(i * step + 1)}\nLine {i}\n\n" for i in range(cues))


def ass(cues: int, until: float) -> str:
    step = until / cues
    lines = [f"Dialogue: 0,{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}.00,0:00:00.00,Default,,0,0,0,,Line"
             for t in (i * step for i in range(cues))]
    return "[Script Info]\n\n[Events]\n" + "\n".join(lines) + "\n"


class Languages(unittest.TestCase):
    def test_codes(self):
        for tag, want in [("eng", "en"), ("en", "en"), ("en-US", "en"), ("fre", "fr"), ("fra", "fr"),
                          ("JPN", "ja"), ("und", ""), (None, ""), ("", "")]:
            self.assertEqual(pi.language_of(tag), want, tag)


class Checks(unittest.TestCase):
    def check(self, info, minutes=0, japanese=False):
        return pi.problem_with(info, "x.mkv", minutes, japanese, check_decoding=False)

    def test_unreadable_or_missing_tracks(self):
        self.assertIn("can't be read", self.check(None))
        self.assertIn("no audio", self.check(media()))
        self.assertIn("no video", self.check({"streams": [audio(0, "aac")], "format": {}}))

    def test_too_short_is_a_sample_or_fake(self):
        self.assertIn("sample", self.check(media(audio(1, "aac"), length=600), minutes=45))
        self.assertIsNone(self.check(media(audio(1, "aac"), length=40 * 60), minutes=45))
        self.assertIsNone(self.check(media(audio(1, "aac"), length=600), minutes=0))  # runtime unknown

    def test_dubbed_anime(self):
        self.assertIn("dubbed", self.check(media(audio(1, "aac", "eng")), japanese=True))
        self.assertIsNone(self.check(media(audio(1, "aac", "eng"), audio(2, "flac", "jpn")), japanese=True))
        # Untagged audio may well be Japanese: not rejected
        self.assertIsNone(self.check(media(audio(1, "aac")), japanese=True))
        self.assertIsNone(self.check(media(audio(1, "aac", "eng"))))  # not anime made in Japanese


class Stereo(unittest.TestCase):
    def test_only_dolby_needs_stereo(self):
        self.assertEqual(pi.needs_stereo(media(audio(1, "eac3", "eng")), "jpn")["index"], 1)

    def test_browser_audio_in_the_same_language_is_enough(self):
        self.assertIsNone(pi.needs_stereo(media(audio(1, "eac3", "eng"), audio(2, "aac", "eng")), ""))
        self.assertIsNone(pi.needs_stereo(media(audio(1, "opus", "eng")), ""))

    def test_preferred_language_is_the_source(self):
        info = media(audio(1, "aac", "eng", default=True), audio(2, "truehd", "jpn"))
        self.assertEqual(pi.needs_stereo(info, "jpn")["index"], 2)
        # No preferred-language track: the default one
        info = media(audio(1, "dts", "spa"), audio(2, "ac3", "eng", default=True))
        self.assertEqual(pi.needs_stereo(info, "jpn")["index"], 2)

    def test_command_puts_the_new_track_first_and_keeps_everything(self):
        info = media(audio(3, "eac3", "jpn"))
        cmd = pi.stereo_command("a.mkv", "out", info, info["streams"][1], "aac_at")
        maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
        self.assertEqual(maps, ["0:V", "0:3", "0:a", "0:s?", "0:t?"])
        self.assertIn("language=jpn", cmd)
        self.assertEqual(cmd[cmd.index("-disposition:a:0") + 1], "default")
        mp4 = pi.stereo_command("a.mp4", "out", info, info["streams"][1], "aac")
        self.assertNotIn("0:t?", mp4)
        self.assertEqual(mp4[mp4.index("-f") + 1], "mp4")


class Subtitles(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.video = self.dir / "Movie (2020).mkv"
        self.video.touch()

    def targets(self, info, languages=("en", "es"), want="first"):
        return [(lang, s["index"]) for lang, s in pi.ocr_targets(info, self.video, list(languages), want)]

    def test_pictures_only_get_read(self):
        info = media(sub(2, "hdmv_pgs_subtitle", "eng"), sub(3, "subrip", "chi"))
        self.assertEqual(self.targets(info), [("en", 2)])

    def test_text_already_there(self):
        self.assertEqual(self.targets(media(sub(2, "hdmv_pgs_subtitle", "eng"), sub(3, "subrip", "eng"))), [])
        (self.dir / "Movie (2020).en.hi.srt").touch()
        self.assertEqual(self.targets(media(sub(2, "hdmv_pgs_subtitle", "eng"))), [])

    def test_first_stops_at_the_first_language_with_text(self):
        info = media(sub(2, "hdmv_pgs_subtitle", "eng"), sub(3, "hdmv_pgs_subtitle", "spa"))
        self.assertEqual(self.targets(info), [("en", 2)])
        self.assertEqual(self.targets(info, want="all"), [("en", 2), ("es", 3)])
        (self.dir / "Movie (2020).en.srt").touch()
        self.assertEqual(self.targets(info), [])
        self.assertEqual(self.targets(info, want="all"), [("es", 3)])

    def test_forced_and_commentary_tracks(self):
        info = media(sub(2, "hdmv_pgs_subtitle", "eng", forced=True))
        self.assertEqual(self.targets(info), [])
        info = media(sub(2, "hdmv_pgs_subtitle", "eng", title="Commentary"), sub(3, "hdmv_pgs_subtitle", "eng"))
        self.assertEqual(self.targets(info), [("en", 3)])
        # Forced text subtitles don't count as having subtitles
        info = media(sub(2, "subrip", "eng", forced=True), sub(3, "hdmv_pgs_subtitle", "eng"))
        self.assertEqual(self.targets(info), [("en", 3)])


class FakeArr:
    name, kind = "Radarr", "movie"

    def __init__(self, known_release=True, has_file=False):
        self.known_release, self.file = known_release, has_file
        self.rejected, self.rescanned = [], []
        self.history_down = False

    def failed_releases(self, record):
        """Like Radarr's history: each rejection marked a release failed"""
        if self.history_down:
            raise OSError("Radarr isn't answering")
        return len(self.rejected) if self.known_release else 0

    def item(self, record):
        return "Film (2020)", 100, False, f"radarr:{record['movieId']}"

    def reject(self, record):
        self.rejected.append(record["id"])
        return self.known_release

    def rescan(self, record):
        self.rescanned.append(record["id"])

    def has_file(self, key):
        return self.file


class Imports(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.video = self.dir / "Film.mkv"
        self.video.write_bytes(b"x")
        self.notes = []
        for target, value in [("probe", lambda p: media(audio(1, "aac", "eng"))),
                              ("notify", lambda t, m: self.notes.append(m)),
                              ("STATE", self.dir)]:
            patcher = mock.patch.object(pi, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def record(self, n=1):
        return {"id": n, "movieId": 7, "downloadId": "ABC", "data": {"importedPath": str(self.video), "fileId": "3"}}

    def worker(self, **settings):
        return pi.Worker([], {**pi.DEFAULTS, **settings}, {})

    def check(self, w, app, record):
        """handle_import as the queue runs it: a problem is looked at again
        CONFIRM_AFTER later (the clock is moved on here)"""
        entry: dict = {"record": record, "attempts": 0, "next": 0}
        if w.handle_import(app, record, entry):
            return True
        entry["suspect"]["since"] -= pi.CONFIRM_AFTER
        return w.handle_import(app, record, entry)

    def test_bad_file_is_replaced_once_confirmed(self):
        app, w = FakeArr(), self.worker()
        entry: dict = {"record": self.record(), "attempts": 0, "next": 0}
        with mock.patch.object(pi, "problem_with", return_value="the video is damaged"):
            self.assertFalse(w.handle_import(app, self.record(), entry))  # first sighting: look again later
            self.assertEqual(app.rejected, [])
            self.assertFalse(w.handle_import(app, self.record(), entry))  # too soon
            entry["suspect"]["since"] -= pi.CONFIRM_AFTER
            self.assertTrue(w.handle_import(app, self.record(), entry))
        self.assertEqual(app.rejected, [1])
        r = w.state["rejections"]["radarr:7"]
        self.assertEqual((r["count"], r["status"]), (1, "looking"))

    def test_after_max_replacements_the_file_is_kept(self):
        app, w = FakeArr(), self.worker(max_replacements=2)
        with mock.patch.object(pi, "problem_with", return_value="it's dubbed (no Japanese audio)"):
            for n in (1, 2, 3):
                self.check(w, app, self.record(n))
        self.assertEqual(app.rejected, [1, 2])
        self.assertEqual(w.state["rejections"]["radarr:7"]["status"], "kept")
        self.assertEqual(len(self.notes), 1)

    def test_lost_record_doesnt_reset_the_limit(self):
        """Our record is gone (damaged, set aside): Radarr's history still
        shows two failed releases, so the third bad one is kept"""
        app = FakeArr()
        app.rejected = [1, 2]
        w = self.worker(max_replacements=2)   # a fresh, empty record
        with mock.patch.object(pi, "problem_with", return_value="the video is damaged"):
            self.check(w, app, self.record(3))
        self.assertEqual(app.rejected, [1, 2])   # nothing more deleted
        self.assertEqual(w.state["rejections"]["radarr:7"]["status"], "kept")

    def test_unreadable_history_deletes_nothing(self):
        app, w = FakeArr(), self.worker()
        app.history_down = True
        with mock.patch.object(pi, "problem_with", return_value="the video is damaged"):
            with self.assertRaises(OSError):   # the import queue retries it later
                self.check(w, app, self.record())
        self.assertEqual(app.rejected, [])
        self.assertTrue(self.video.exists())

    def test_hand_imported_file_is_left_alone(self):
        app, w = FakeArr(known_release=False), self.worker()
        with mock.patch.object(pi, "problem_with", return_value="the video is damaged"):
            self.check(w, app, self.record())
        self.assertNotIn("radarr:7", w.state["rejections"])
        self.assertTrue(self.video.exists())

    def test_checks_off_means_no_rejection(self):
        app, w = FakeArr(), self.worker(check_downloads=False)
        with mock.patch.object(pi, "problem_with", return_value="the video is damaged"):
            self.check(w, app, self.record())
        self.assertEqual(app.rejected, [])

    def test_good_replacement_settles_it(self):
        app, w = FakeArr(), self.worker()
        w.state["rejections"]["radarr:7"] = {"title": "Film", "count": 1, "reason": "x", "time": 0, "status": "looking"}
        with mock.patch.object(pi, "problem_with", return_value=None):
            self.check(w, app, self.record())
        self.assertEqual(w.state["rejections"]["radarr:7"]["status"], "replaced")

    def test_fine_on_the_second_look_isnt_replaced(self):
        app, w = FakeArr(), self.worker()
        answers = iter(["the file can't be read", None])
        with mock.patch.object(pi, "problem_with", side_effect=lambda *a, **k: next(answers)):
            self.check(w, app, self.record())
        self.assertEqual(app.rejected, [])

    def test_failed_imports_stay_queued(self):
        """An import that can't be checked (a service down) stays queued
        while the history position moves on; nothing is missed"""
        app = FakeArr()
        app.name = "Radarr"
        app.imports_after = lambda last: [self.record(5)]
        w = pi.Worker([app], dict(pi.DEFAULTS), {"last_import": {"Radarr": 4}, "pending": {}})
        with mock.patch.object(FakeArr, "item", side_effect=OSError("Radarr down")), mock.patch.object(pi, "save"):
            w.new_imports()
        self.assertEqual(w.state["last_import"]["Radarr"], 5)
        entry: dict = w.state["pending"]["Radarr:5"]
        self.assertEqual(entry["attempts"], 1)
        entry["next"] = 0
        app.imports_after = lambda last: []
        with mock.patch.object(pi, "problem_with", return_value=None), mock.patch.object(pi, "save"):
            w.new_imports()
        self.assertNotIn("Radarr:5", w.state["pending"])

    def test_tool_trouble_rejects_nothing(self):
        app = FakeArr()
        app.name = "Radarr"
        app.imports_after = lambda last: [self.record(5)]
        w = pi.Worker([app], dict(pi.DEFAULTS), {"last_import": {"Radarr": 4}, "pending": {}})
        with mock.patch.object(pi, "problem_with", side_effect=pi.ToolTrouble("ffprobe broken")), mock.patch.object(pi, "save"):
            w.new_imports()
        self.assertEqual(app.rejected, [])
        self.assertIn("Radarr:5", w.state["pending"])

    def test_nothing_found_is_notified_once(self):
        app = FakeArr(has_file=False)
        w = pi.Worker([app], dict(pi.DEFAULTS), {})
        w.state["rejections"]["radarr:7"] = {"title": "Film", "count": 1, "reason": "x",
                                             "time": time.time() - 7 * 3600, "status": "looking"}
        w.check_rejections()
        w.check_rejections()
        self.assertEqual(len(self.notes), 1)
        # Too recent: no notification yet
        w.state["rejections"]["radarr:8"] = {"title": "Other", "count": 1, "reason": "x",
                                             "time": time.time(), "status": "looking"}
        w.check_rejections()
        self.assertEqual(len(self.notes), 1)


class Rejecting(unittest.TestCase):
    """Arr.reject talks to the API in the right order"""

    def arr(self, grabs):
        arr = pi.Arr("Sonarr", "http://x", "series")
        arr.calls = []

        def call(method, path, body=None):
            arr.calls.append((method, path.split("?")[0]))
            return {"records": grabs} if method == "GET" else None
        arr.call = call
        return arr

    def test_deletes_the_file_then_marks_the_release_failed(self):
        arr = self.arr([{"id": 9, "downloadId": "ABC"}])
        self.assertTrue(arr.reject({"downloadId": "ABC", "data": {"fileId": "4"}}))
        self.assertEqual(arr.calls, [("GET", "history"), ("DELETE", "episodefile/4"), ("POST", "history/failed/9")])

    def test_unknown_release_changes_nothing(self):
        arr = self.arr([{"id": 9, "downloadId": "OTHER"}])
        self.assertFalse(arr.reject({"downloadId": "ABC", "data": {"fileId": "4"}}))
        self.assertEqual(arr.calls, [("GET", "history")])
        self.assertFalse(arr.reject({"data": {"fileId": "4"}}))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class RealFiles(unittest.TestCase):
    """With a real (tiny) file: the rewrite keeps every track and adds one"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.video = self.dir / "Film.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=20",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=20", "-filter_complex",
                        "[1:a]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0[a]", "-map", "0:v", "-map", "[a]",
                        "-c:v", "mpeg4", "-c:a", "ac3", "-metadata:s:a:0", "language=jpn", str(self.video)],
                       check=True)
        patcher = mock.patch.object(pi, "STATE", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_adds_stereo_and_plays(self):
        info = pi.probe(self.video)
        self.assertIsNone(pi.problem_with(info, self.video, 0, True))
        source = pi.needs_stereo(info, "jpn")
        self.assertIsNotNone(source)
        self.assertTrue(pi.add_stereo(self.video, info, source))
        after = pi.probe(self.video)
        kinds = [(s["codec_type"], s["codec_name"]) for s in after["streams"]]
        self.assertEqual(kinds, [("video", "mpeg4"), ("audio", "aac"), ("audio", "ac3")])
        self.assertEqual(after["streams"][1]["channels"], 2)
        self.assertEqual(pi.stream_language(after["streams"][1]), "ja")
        self.assertIsNone(pi.needs_stereo(after, "jpn"))
        self.assertEqual(list(self.dir.glob(".*postimport*")), [])

    def test_original_changing_meanwhile_wins(self):
        info = pi.probe(self.video)
        real_run = subprocess.run

        def run(cmd, **kw):
            result = real_run(cmd, **kw)
            if "-f" in cmd and cmd[-1].endswith(".postimport"):
                os.utime(self.video, (1, 1))  # Sonarr replaced it while we worked
            return result
        with mock.patch.object(pi.subprocess, "run", side_effect=run):
            self.assertFalse(pi.add_stereo(self.video, info, pi.needs_stereo(info, "")))
        self.assertEqual(len(pi.probe(self.video)["streams"]), 2)

    def test_damaged_file_is_caught(self):
        data = self.video.read_bytes()
        self.video.write_bytes(data[:2000])
        info = pi.probe(self.video)
        self.assertIsNotNone(pi.problem_with(info, self.video, 0, False))


if __name__ == "__main__":
    unittest.main()


class DefaultTracks(unittest.TestCase):
    """The preferred audio and subtitles become each file's defaults"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.video = self.dir / "Film.mkv"
        self.video.touch()

    def flagged(self, stream, on=True):
        stream["disposition"]["default"] = int(on)
        return stream

    def plan(self, info, audio="", subs=("en", "es")):
        return pi.default_tracks(info, self.video, audio, list(subs))

    def test_default_picture_track_loses_to_text(self):
        """Skyfall: a default English picture track next to English text
        got burned in while the player showed the text: two subtitles"""
        info = media(self.flagged(sub(5, "hdmv_pgs_subtitle", "eng")), sub(6, "hdmv_pgs_subtitle", "spa"))
        self.assertEqual(self.plan(info), {})  # no text anywhere: a picture track is all there is
        (self.dir / "Film.en.srt").touch()
        self.assertEqual(self.plan(info), {5: "0"})  # the file next to it wins

    def test_embedded_text_in_the_preferred_language(self):
        info = media(self.flagged(sub(4, "subrip", "chi")), sub(5, "subrip", "eng"), sub(6, "subrip", "spa"))
        self.assertEqual(self.plan(info), {4: "0", 5: "default"})

    def test_spanish_when_no_english(self):
        info = media(self.flagged(sub(4, "subrip", "chi")), sub(6, "subrip", "spa"))
        self.assertEqual(self.plan(info), {4: "0", 6: "default"})
        self.assertEqual(self.plan(info, subs=("en",)), {})  # Spanish not wanted: left alone

    def test_forced_tracks_keep_their_flag(self):
        info = media(self.flagged(sub(4, "subrip", "eng", forced=True)), sub(5, "subrip", "eng"))
        self.assertEqual(self.plan(info), {5: "default"})

    def test_mislabelled_forced_full_track(self):
        """Lain: the full English track is flagged forced and a "Songs +
        Signs" track isn't; Jellyfin then shows only the signs"""
        full = self.flagged(sub(3, "ass", "eng", forced=True))
        signs = sub(4, "ass", "eng", title="Songs + Signs")
        self.assertEqual(self.plan(media(full, signs)), {3: "default"})  # forced flag goes, default stays
        signs = self.flagged(sub(4, "ass", "eng", title="Songs + Signs"))
        self.assertEqual(self.plan(media(sub(3, "ass", "eng", forced=True), signs)), {3: "default", 4: "0"})
        # A real forced track next to a full one is left alone
        self.assertEqual(self.plan(media(self.flagged(sub(3, "ass", "eng", forced=True)), sub(4, "ass", "eng"))), {4: "default"})

    def test_anime_japanese_audio(self):
        info = media(self.flagged(audio(1, "aac", "eng")), audio(2, "ac3", "jpn"), audio(3, "aac", "jpn"))
        self.assertEqual(self.plan(info, audio="jpn"), {1: "0", 3: "default"})  # the browser-friendly Japanese track
        self.assertEqual(self.plan(info, audio=""), {})                 # no preference: the file's own default
        self.assertEqual(self.plan(info, audio="fre"), {})              # not there: unchanged

    def test_anime_library_decides_the_audio(self):
        w = pi.Worker([], {**pi.DEFAULTS, "anime_dir": "/media/anime", "audio_language": "", "anime_audio_language": "jpn"}, {})
        self.assertEqual(w.audio_language_for(Path("/media/anime/Show/S01E01.mkv")), "jpn")
        self.assertEqual(w.audio_language_for(Path("/media/movies/Film (2020)/Film.mkv")), "")
        self.assertEqual(w.audio_language_for(Path("/media/animeXYZ/a.mkv")), "")

    def test_command_sets_flags_by_track_type(self):
        info = media(audio(1, "eac3", "eng"), audio(2, "aac", "jpn"), sub(3, "subrip", "chi"), sub(4, "subrip", "eng"))
        cmd = pi.defaults_command(self.video, "out", info, {1: "0", 2: "default", 3: "0", 4: "default"})
        flags = [(cmd[i], cmd[i + 1]) for i, a in enumerate(cmd) if a.startswith("-disposition")]
        self.assertEqual(flags, [("-disposition:a:0", "0"), ("-disposition:a:1", "default"),
                                 ("-disposition:s:0", "0"), ("-disposition:s:1", "default")])

    def cues(self, by_index):
        """stream_cues answering from {track index: (packets, last time)}"""
        return mock.patch.object(pi, "stream_cues", lambda path, index: by_index.get(index))

    def test_picture_track_removed_when_there_as_text(self):
        """Moonfin prefers picture subtitles over text ones whatever the
        flags; with the text version there, the picture one goes"""
        info = media(self.flagged(sub(5, "hdmv_pgs_subtitle", "eng")), sub(6, "hdmv_pgs_subtitle", "spa"),
                     sub(7, "hdmv_pgs_subtitle", "eng", forced=True))
        pictures = {5: (800, 3500.0), 6: (800, 3500.0), 7: (20, 3000.0)}   # PGS: 400 subtitles shown and cleared
        with self.cues(pictures):
            self.assertEqual(pi.redundant_pictures(info, self.video), [])
            # A file named like English subtitles isn't proof: an empty one
            # doesn't let the picture track go
            (self.dir / "Film.en.srt").touch()
            self.assertEqual(pi.redundant_pictures(info, self.video), [])
            (self.dir / "Film.en.srt").write_text(srt(cues=400, until=3500))
            dropped = pi.redundant_pictures(info, self.video)
        self.assertEqual([st["index"] for st in dropped], [5])  # Spanish has no text; forced signs stay
        kept = pi.without(info, dropped)
        cmd = pi.defaults_command(self.video, "out", info, pi.default_tracks(kept, self.video, "", ["en"]), dropped)
        self.assertIn("-0:5", cmd)
        self.assertEqual([st["index"] for st in pi.streams(kept, "subtitle")], [6, 7])

    def test_text_must_match_the_picture_track(self):
        info = media(sub(5, "hdmv_pgs_subtitle", "eng"))   # an hour long
        cases = [
            (srt(cues=400, until=1200), "srt", (800, 3500.0), False),   # stops a third of the way in
            (srt(cues=150, until=3500), "srt", (800, 3500.0), False),   # far fewer lines than the pictures
            (srt(cues=400, until=3500), "srt", (800, 3500.0), True),
            (ass(cues=400, until=3500), "ass", (800, 3500.0), True),
            (srt(cues=400, until=3500), "srt", None, False),            # the picture track can't be read
            ("not subtitles at all", "srt", (800, 3500.0), False),
            (srt(cues=400, until=3500), "sub", (800, 3500.0), False),   # a format that can't be checked
        ]
        for text, ext, picture, drops in cases:
            for f in self.dir.glob("Film.en.*"):
                f.unlink()
            (self.dir / f"Film.en.{ext}").write_text(text)
            with self.cues({5: picture}):
                self.assertEqual(bool(pi.redundant_pictures(info, self.video)), drops, (ext, picture, text[:40]))

    def test_embedded_text_is_measured_not_trusted(self):
        """An unlabelled text track with part of the dialogue (no metadata
        saying so) doesn't let the full picture track go"""
        info = media(sub(5, "hdmv_pgs_subtitle", "eng"), sub(6, "subrip", "eng"))
        signs = media(sub(5, "hdmv_pgs_subtitle", "eng"), sub(6, "subrip", "eng", title="Signs & Songs"))
        for case, cues, drops in ((info, {5: (800, 3500.0), 6: (400, 3490.0)}, True),
                                  (info, {5: (800, 3500.0), 6: (60, 3400.0)}, False),    # partial, unlabelled
                                  (info, {5: (800, 3500.0)}, False),                    # text unreadable
                                  (signs, {5: (800, 3500.0), 6: (400, 3490.0)}, False)):  # labelled partial
            with self.cues(cues):
                self.assertEqual(bool(pi.redundant_pictures(case, self.video)), drops, cues)

    def test_files_checked_before_are_looked_at_again(self):
        st = self.video.stat()
        self.assertNotEqual([st.st_size, int(st.st_mtime)], pi.seen_mark(self.video))  # an older version's mark


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class PictureDefaultsRealFile(unittest.TestCase):
    def test_default_flag_cleared(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        video, srt = d / "Film.mkv", d / "in.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        # A default subtitle track (text here; the rewrite is the same for pictures)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=5", "-i", str(srt),
                        "-map", "0", "-map", "1", "-c:v", "mpeg4", "-c:s", "srt", "-metadata:s:s:0", "language=eng",
                        "-disposition:s:0", "default", str(video)], check=True)
        with mock.patch.object(pi, "STATE", d):
            info = pi.probe(video)
            index = pi.streams(info, "subtitle")[0]["index"]
            self.assertTrue(pi.set_defaults(video, info, {index: "0"}))
            after = pi.probe(video)
        self.assertEqual(pi.streams(after, "subtitle")[0]["disposition"]["default"], 0)
        self.assertEqual(len(after["streams"]), 2)


class Robustness(unittest.TestCase):
    """What a failed tool, a failed fix or changed settings do"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.video = self.dir / "Film.mkv"
        self.video.write_bytes(b"x")
        patcher = mock.patch.object(pi, "STATE", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_probe_retries_and_blames_broken_tools(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd[0])
            return mock.Mock(returncode=1, stdout="")
        with mock.patch.object(pi.subprocess, "run", run), mock.patch.object(pi.time, "sleep"):
            with self.assertRaises(pi.ToolTrouble):
                pi.probe(self.video)
        self.assertEqual(calls.count("ffprobe"), 4)  # 3 tries + the "-version" check

    def test_hardware_decode_failure_retried_in_software(self):
        def run(cmd, **kw):
            hardware = "videotoolbox" in cmd
            return mock.Mock(returncode=1 if hardware else 0, stdout="" if hardware else "frame=50\n")
        with mock.patch.object(pi.subprocess, "run", run):
            self.assertTrue(pi.decodes(self.video, 30))

    def test_failed_fix_is_retried_then_given_up(self):
        w = pi.Worker([], {**pi.DEFAULTS, "ocr_subtitles": False, "default_tracks": False, "drop_picture_subtitles": False}, {})
        info = media(audio(1, "eac3", "eng"))
        with mock.patch.object(pi, "add_stereo", return_value=False), mock.patch.object(pi, "room_for", return_value=True):
            for n in range(1, pi.FIX_TRIES):
                w.fix(self.video, info, "Film")
                self.assertNotIn(str(self.video), w.state["seen"])  # not done: retried later
                self.assertEqual(w.state["failures"][str(self.video)]["count"], n)
            w.fix(self.video, info, "Film")
        self.assertIn(str(self.video), w.state["seen"])  # gave up
        self.assertIn("gave up", w.state["recent"][0]["what"])

    def test_forced_sidecars_dont_count_as_full_subtitles(self):
        (self.dir / "Film.en.forced.srt").touch()
        (self.dir / "Film.es.srt").touch()
        self.assertEqual(pi.sidecar_languages(self.video), {"es"})

    def test_new_settings_or_subtitles_look_again(self):
        w = pi.Worker([], dict(pi.DEFAULTS), {})
        before = w.mark(self.video)
        (self.dir / "Film.en.srt").touch()       # Bazarr added a subtitle
        after = w.mark(self.video)
        self.assertNotEqual(before, after)
        w.settings["subtitle_languages"] = ["es"]  # preferences changed
        self.assertNotEqual(after, w.mark(self.video))

    def test_default_tracks_off_means_no_rewrite(self):
        w = pi.Worker([], {**pi.DEFAULTS, "stereo_audio": False, "ocr_subtitles": False, "default_tracks": False,
                           "drop_picture_subtitles": False}, {})
        info = media(audio(1, "aac", "eng", default=False), audio(2, "aac", "jpn"))
        with mock.patch.object(pi, "set_defaults") as rewrite:
            w.fix(self.video, info, "Film")
        rewrite.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class NoDefaultStaysNoDefault(unittest.TestCase):
    def test_all_subtitle_defaults_off_survives_the_rewrite(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        video, srt = d / "Film.mkv", d / "in.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=5", "-i", str(srt), "-i", str(srt),
                        "-map", "0", "-map", "1", "-map", "2", "-c:v", "mpeg4", "-c:s", "srt",
                        "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=spa",
                        "-disposition:s:0", "default", str(video)], check=True)
        with mock.patch.object(pi, "STATE", d):
            info = pi.probe(video)
            first = pi.streams(info, "subtitle")[0]["index"]
            self.assertTrue(pi.set_defaults(video, info, {first: "0"}, [pi.streams(info, "subtitle")[0]]))
            after = pi.probe(video)
        subs = pi.streams(after, "subtitle")
        self.assertEqual([pi.stream_language(st) for st in subs], ["es"])
        self.assertEqual(subs[0]["disposition"]["default"], 0)  # not made default by the muxer
