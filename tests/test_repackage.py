"""MKV → MP4 overnight (mediaserver/repackage.py, postimport's Worker):
which files fit MP4 whole, the subtitles saved next to the video, only
files nobody started, only at night, and a real file repackaged end to end.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mediaserver import postimport as pi
from mediaserver import repackage as rp

pi.log = lambda message: None


def stream(index, kind, codec, **extra):
    return dict({"index": index, "codec_type": kind, "codec_name": codec, "disposition": {}}, **extra)


def info(*streams, length=600.0):
    return {"streams": list(streams), "format": {"duration": str(length)}}


class Fits(unittest.TestCase):
    def test_what_fits_mp4_whole(self):
        ok = info(stream(0, "video", "hevc"), stream(1, "audio", "eac3"), stream(2, "audio", "aac"),
                  stream(3, "subtitle", "subrip"))
        subs, why = rp.fits(ok)
        self.assertEqual((len(subs), why), (1, ""))
        # A cover picture and an image attachment don't count
        cover = info(stream(0, "video", "h264"), stream(1, "video", "mjpeg", disposition={"attached_pic": 1}),
                     stream(2, "audio", "ac3"), stream(3, "attachment", "png", tags={"mimetype": "image/png"}))
        self.assertEqual(rp.fits(cover)[1], "")

    def test_what_stays_mkv(self):
        cases = {
            "video in vp9": info(stream(0, "video", "vp9"), stream(1, "audio", "aac")),
            "audio in dts": info(stream(0, "video", "hevc"), stream(1, "audio", "dts"), stream(2, "audio", "aac")),
            "audio in truehd": info(stream(0, "video", "h264"), stream(1, "audio", "truehd")),
            "subtitles in ass": info(stream(0, "video", "h264"), stream(1, "audio", "aac"), stream(2, "subtitle", "ass")),
            "subtitles in hdmv_pgs_subtitle": info(stream(0, "video", "h264"), stream(1, "audio", "aac"),
                                                   stream(2, "subtitle", "hdmv_pgs_subtitle")),
            "embedded fonts": info(stream(0, "video", "h264"), stream(1, "audio", "aac"),
                                   stream(2, "attachment", "ttf", tags={"mimetype": "application/x-truetype-font"})),
            "no video track": info(stream(0, "audio", "aac")),
        }
        for expected, case in cases.items():
            subs, why = rp.fits(case)
            self.assertIn(expected, why)
            self.assertEqual(subs, [])

    def test_hevc_tagged_for_apple(self):
        cmd = rp.remux_command(Path("a.mkv"), Path("b.mp4"), info(stream(0, "video", "hevc")))
        self.assertIn("hvc1", cmd)
        self.assertNotIn("hvc1", rp.remux_command(Path("a.mkv"), Path("b.mp4"), info(stream(0, "video", "h264"))))

    def test_sidecar_names(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        video = d / "Film (2010).mkv"
        self.assertEqual(rp.sidecar_name(video, stream(3, "subtitle", "subrip"), "en", set()).name, "Film (2010).en.srt")
        forced = stream(4, "subtitle", "subrip", disposition={"forced": 1})
        self.assertEqual(rp.sidecar_name(video, forced, "en", set()).name, "Film (2010).en.forced.srt")
        sdh = stream(5, "subtitle", "subrip", disposition={"hearing_impaired": 1})
        self.assertEqual(rp.sidecar_name(video, sdh, "es", set()).name, "Film (2010).es.sdh.srt")
        (d / "Film (2010).en.srt").write_text("Bazarr's")   # one already there stays
        self.assertEqual(rp.sidecar_name(video, stream(3, "subtitle", "subrip"), "en", set()).name, "Film (2010).en.3.srt")
        self.assertEqual(rp.sidecar_name(video, stream(6, "subtitle", "subrip"), "", set()).name, "Film (2010).srt")

    def test_night_only(self):
        def at(hour):
            return time.mktime((2026, 10, 6, hour, 30, 0, 0, 0, -1))
        self.assertTrue(rp.night(at(3)))
        self.assertFalse(rp.night(at(0)))
        self.assertFalse(rp.night(at(7)))
        self.assertFalse(rp.night(at(15)))

    def test_only_what_nobody_started(self):
        data = {"a": [{"Played": False, "PlaybackPositionTicks": 0}], "b": [{"PlaybackPositionTicks": 5}], "c": [{"Played": True}]}
        check = rp.unwatched({"/m/a.mkv": "a", "/m/b.mkv": "b", "/m/c.mkv": "c", "/m/d.mkv": "d"}, data.get)
        self.assertTrue(check("/m/a.mkv"))
        self.assertFalse(check("/m/b.mkv"))
        self.assertFalse(check("/m/c.mkv"))
        self.assertIsNone(check("/m/d.mkv"))   # Jellyfin didn't answer for it
        self.assertIsNone(check("/m/new.mkv"))   # not listed yet


class FakeApp:
    def __init__(self, name, kind, items):
        self.name, self.kind, self.items, self.rescans = name, kind, items, []

    def call(self, method, path, body=None):
        return self.items

    def rescan(self, record):
        self.rescans.append(record)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class RealFile(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.movies = self.dir / "movies"
        folder = self.movies / "Film (2010)"
        folder.mkdir(parents=True)
        self.video = folder / "Film (2010).mkv"
        sub = self.dir / "in.srt"
        sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        # H.264 + AAC + an English SubRip track: fits MP4 whole
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=4",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-i", str(sub), "-map", "0", "-map", "1", "-map", "2",
                        "-c:v", "libx264", "-c:a", "aac", "-c:s", "srt", "-metadata:s:s:0", "language=eng", str(self.video)], check=True)
        self.radarr = FakeApp("Radarr", "movie", [{"id": 7, "path": str(folder)}])
        self.state: dict = {}
        settings: dict = dict(pi.DEFAULTS, library_dirs=[str(self.movies)])
        self.worker = pi.Worker([self.radarr], settings, self.state)
        self.updates: list = []
        for patcher in (mock.patch.object(pi, "STATE", self.dir), mock.patch.object(pi, "room_for", return_value=True),
                        mock.patch.object(pi, "jellyfin_updated", lambda p, changes=None: self.updates.append(changes)),
                        mock.patch.object(pi, "operation_running", return_value=False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_repackaged_with_its_subtitles_kept_next_to_it(self):
        mark = pi.seen_mark(self.video)
        self.assertTrue(self.worker.repackage_one(self.video, mark, lambda p: True, {}))
        new = self.video.with_suffix(".mp4")
        self.assertFalse(self.video.exists())
        after = pi.probe(new)
        assert after is not None
        self.assertEqual(sorted(s["codec_type"] for s in after["streams"]), ["audio", "video"])
        self.assertIn("Hello", (new.parent / "Film (2010).en.srt").read_text())
        self.assertEqual(self.radarr.rescans, [{"movieId": 7}])
        self.assertEqual(self.updates, [[(self.video, "Deleted"), (new, "Created")]])
        self.assertIn(str(new), self.state["playback_pending"])
        self.assertIn("repackaged as MP4", self.state["recent"][0]["what"])

    def test_started_files_are_left_alone_and_remembered(self):
        skip: dict = {}
        self.assertFalse(self.worker.repackage_one(self.video, pi.seen_mark(self.video), lambda p: False, skip))
        self.assertTrue(self.video.exists())
        self.assertIn("started or watched", skip[str(self.video)]["reason"])

    def test_failed_remux_keeps_the_mkv_and_no_stray_subtitles(self):
        with mock.patch.object(rp, "checks_out", return_value=False):
            self.assertFalse(self.worker.repackage_one(self.video, pi.seen_mark(self.video), lambda p: True, {}))
        self.assertTrue(self.video.exists())
        self.assertEqual(sorted(f.name for f in self.video.parent.iterdir()), ["Film (2010).mkv"])

    def test_the_overnight_pass(self):
        jf = mock.Mock()
        jf.items.return_value = {str(self.video): "item"}
        jf.user_data.return_value = [{"Played": False}]
        night = time.mktime((2026, 10, 6, 3, 0, 0, 0, 0, -1))
        with mock.patch.object(pi.playback, "Jellyfin", return_value=jf):
            self.worker.repackage(now=night + 12 * 3600)   # afternoon: nothing
            self.assertTrue(self.video.exists())
            self.worker.settings["repackage_mp4"] = False
            self.worker.repackage(now=night)   # switched off: nothing
            self.assertTrue(self.video.exists())
            self.worker.settings["repackage_mp4"] = True
            self.worker.repackage(now=night)
        self.assertTrue(self.video.with_suffix(".mp4").exists())


if __name__ == "__main__":
    unittest.main()
