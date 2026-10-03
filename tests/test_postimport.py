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

    def test_bad_file_is_replaced(self):
        app, w = FakeArr(), self.worker()
        with mock.patch.object(pi, "problem_with", return_value="the video is damaged"):
            w.handle_import(app, self.record())
        self.assertEqual(app.rejected, [1])
        r = w.state["rejections"]["radarr:7"]
        self.assertEqual((r["count"], r["status"]), (1, "looking"))

    def test_after_max_replacements_the_file_is_kept(self):
        app, w = FakeArr(), self.worker(max_replacements=2)
        with mock.patch.object(pi, "problem_with", return_value="it's dubbed (no Japanese audio)"):
            for n in (1, 2, 3):
                w.handle_import(app, self.record(n))
        self.assertEqual(app.rejected, [1, 2])
        self.assertEqual(w.state["rejections"]["radarr:7"]["status"], "kept")
        self.assertEqual(len(self.notes), 1)

    def test_hand_imported_file_is_left_alone(self):
        app, w = FakeArr(known_release=False), self.worker()
        with mock.patch.object(pi, "problem_with", return_value="the video is damaged"):
            w.handle_import(app, self.record())
        self.assertNotIn("radarr:7", w.state["rejections"])
        self.assertTrue(self.video.exists())

    def test_checks_off_means_no_rejection(self):
        app, w = FakeArr(), self.worker(check_downloads=False)
        with mock.patch.object(pi, "problem_with", return_value="the video is damaged"):
            w.handle_import(app, self.record())
        self.assertEqual(app.rejected, [])

    def test_good_replacement_settles_it(self):
        app, w = FakeArr(), self.worker()
        w.state["rejections"]["radarr:7"] = {"title": "Film", "count": 1, "reason": "x", "time": 0, "status": "looking"}
        with mock.patch.object(pi, "problem_with", return_value=None):
            w.handle_import(app, self.record())
        self.assertEqual(w.state["rejections"]["radarr:7"]["status"], "replaced")

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
