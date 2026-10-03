# Tests attach recorders to objects and use possibly-None results directly:
# pyright: reportOptionalSubscript=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportFunctionMemberAccess=false
"""More of the post-import worker: the rewrite's safety checks on real
files, OCR's outcomes, when a fix is retried or given up, the library
sweep, --check/--fix and the main loop.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mediaserver import postimport as pi
from tests.test_postimport import audio, media, sub

pi.log = lambda message: None

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class Scratch(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        patcher = mock.patch.object(pi, "STATE", self.dir / ".state")
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.dir / ".state").mkdir()


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
class Rewrite(Scratch):
    """The original is only replaced by a new file that checks out"""

    def setUp(self):
        super().setUp()
        self.video = self.dir / "Film.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=5",
                        "-f", "lavfi", "-i", "sine=duration=5", "-c:v", "mpeg4", "-c:a", "ac3", str(self.video)], check=True)
        self.video.chmod(0o640)
        self.info = pi.probe(self.video)
        self.original = self.video.read_bytes()

    def copy_command(self, extra=()):
        return lambda out: ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(self.video), "-map", "0", *extra,
                            "-c", "copy", "-f", "matroska", str(out)]

    def leftovers(self):
        return [p.name for p in self.dir.iterdir() if p.name.endswith(".postimport")]

    def test_success_keeps_permissions(self):
        self.assertTrue(pi.rewrite(self.video, self.info, self.copy_command(), pi.track_counts(self.info), "copy"))
        self.assertEqual(self.video.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.leftovers(), [])

    def test_ffmpeg_failing_keeps_the_original(self):
        bad = lambda out: ["ffmpeg", "-nostdin", "-v", "error", "-i", str(self.dir / "missing.mkv"), str(out)]  # noqa: E731
        self.assertFalse(pi.rewrite(self.video, self.info, bad, pi.track_counts(self.info), "copy"))
        self.assertEqual(self.video.read_bytes(), self.original)
        self.assertEqual(self.leftovers(), [])

    def test_a_track_lost_keeps_the_original(self):
        no_audio = lambda out: ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(self.video), "-map", "0:v",  # noqa: E731
                                "-c", "copy", "-f", "matroska", str(out)]
        self.assertFalse(pi.rewrite(self.video, self.info, no_audio, pi.track_counts(self.info), "copy"))
        self.assertEqual(self.video.read_bytes(), self.original)

    def test_shorter_result_keeps_the_original(self):
        cut = self.copy_command(["-t", "1"])
        self.assertFalse(pi.rewrite(self.video, self.info, cut, pi.track_counts(self.info), "copy"))
        self.assertEqual(self.video.read_bytes(), self.original)

    def test_file_changed_meanwhile_is_left_alone(self):
        def command(out):
            # Sonarr upgrades the file while ffmpeg runs
            replacement = self.dir / "new.mkv"
            shutil.copy(self.video, replacement)
            os.replace(replacement, self.video)
            return self.copy_command()(out)
        self.assertFalse(pi.rewrite(self.video, self.info, command, pi.track_counts(self.info), "copy"))
        self.assertEqual(self.leftovers(), [])

    def test_ocr_outcomes(self):
        srt = self.dir / "s.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
        withsub = self.dir / "Sub.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.video), "-i", str(srt), "-map", "0", "-map", "1",
                        "-c", "copy", "-c:s", "srt", str(withsub)], check=True)
        stream = {"index": 2}
        real_run = subprocess.run
        self.cues = 0

        def run(cmd, **kw):
            # A real PGS track can't be made here: extracting is faked
            # (failing for a track that isn't there), and so is the OCR
            if "pgsrip" in cmd:
                sup = Path(cmd[-1])
                cues = "".join(f"{i}\n00:00:0{i % 9},000 --> 00:00:0{i % 9},500\nline\n\n" for i in range(self.cues))
                sup.with_suffix(".srt").write_text(cues)
                return mock.Mock(returncode=0)
            if "ffmpeg" in cmd:
                if "0:9" in cmd:
                    return mock.Mock(returncode=1, stderr="no such stream")
                Path(cmd[-1]).write_bytes(b"PG")
                return mock.Mock(returncode=0, stderr="")
            return real_run(cmd, **kw)
        with mock.patch.object(pi.subprocess, "run", run):
            self.cues = 3
            self.assertEqual(pi.ocr(withsub, "en", stream), "failed")   # too little text
            self.cues = 20
            self.assertEqual(pi.ocr(withsub, "en", stream), "written")
            self.assertEqual(pi.ocr(withsub, "en", stream), "skipped")   # there now
            self.assertEqual(pi.ocr(withsub, "fr", {"index": 9}), "failed")   # no such track
        self.assertEqual((self.dir / "Sub.en.srt").read_text().count("-->"), 20)


class Space(unittest.TestCase):
    def test_a_file_still_seeding_needs_more_room(self):
        settings = {"min_free_gb": 10, "warn_free_gb": 50}
        gb = 1024 ** 3
        st = mock.Mock(st_size=gb, st_nlink=1)
        with mock.patch.object(pi.os, "stat", return_value=st), \
                mock.patch.object(pi.shutil, "disk_usage", return_value=mock.Mock(free=20 * gb)):
            self.assertTrue(pi.room_for("/x.mkv", settings))
            st.st_nlink = 2
            self.assertFalse(pi.room_for("/x.mkv", settings))


class Fixing(Scratch):
    def setUp(self):
        super().setUp()
        self.video = self.dir / "Film (2020).mkv"
        self.video.write_bytes(b"x")
        self.settings = {**pi.DEFAULTS, "ocr_subtitles": False, "default_tracks": False, "drop_picture_subtitles": False}
        self.worker = pi.Worker([], self.settings, {})
        self.info = media(audio(1, "ac3", "eng"))
        for name, value in (("jellyfin_updated", None), ("probe", self.info)):
            patcher = mock.patch.object(pi, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_no_room_retries_later_without_counting_a_failure(self):
        with mock.patch.object(pi, "room_for", return_value=False), mock.patch.object(pi, "add_stereo") as add:
            self.assertFalse(self.worker.fix(self.video, self.info, "Film"))
        add.assert_not_called()
        self.assertNotIn(str(self.video), self.worker.state["seen"])
        self.assertEqual(self.worker.state["failures"], {})

    def test_failing_fix_retried_then_given_up(self):
        with mock.patch.object(pi, "room_for", return_value=True), mock.patch.object(pi, "add_stereo", return_value=False):
            for attempt in range(1, pi.FIX_TRIES):
                self.worker.fix(self.video, self.info, "Film")
                self.assertEqual(self.worker.state["failures"][str(self.video)]["count"], attempt)
                self.assertNotIn(str(self.video), self.worker.state["seen"])
            self.worker.fix(self.video, self.info, "Film")
        self.assertEqual(self.worker.state["failures"], {})
        self.assertIn(str(self.video), self.worker.state["seen"])
        self.assertIn("gave up on stereo audio", self.worker.state["recent"][0]["what"])

    def test_success_is_remembered_and_jellyfin_told(self):
        with mock.patch.object(pi, "room_for", return_value=True), mock.patch.object(pi, "add_stereo", return_value=True):
            self.assertTrue(self.worker.fix(self.video, self.info, "Film"))
        pi.jellyfin_updated.assert_called_once_with(self.video)
        self.assertIn("added stereo audio (from ac3)", self.worker.state["recent"][0]["what"])
        self.assertIn(str(self.video), self.worker.state["seen"])

    def test_ocr_results(self):
        self.settings.update(stereo_audio=False, ocr_subtitles=True)
        targets = [("en", sub(2, "hdmv_pgs_subtitle", "eng")), ("fr", sub(3, "hdmv_pgs_subtitle", "fre"))]
        with mock.patch.object(pi, "ocr_targets", return_value=targets), \
                mock.patch.object(pi, "ocr", side_effect=["written", "failed"]):
            self.assertTrue(self.worker.fix(self.video, self.info, "Film"))
        self.assertEqual(self.worker.state["failures"][str(self.video)]["what"], ["fr subtitles from pictures"])

    def test_default_tracks_need_room_too(self):
        self.settings.update(stereo_audio=False, default_tracks=True)
        with mock.patch.object(pi, "default_tracks", return_value={1: "default"}), \
                mock.patch.object(pi, "room_for", return_value=False), mock.patch.object(pi, "set_defaults") as setting:
            self.worker.fix(self.video, self.info, "Film")
        setting.assert_not_called()
        self.assertNotIn(str(self.video), self.worker.state["seen"])


class Sweep(Scratch):
    def setUp(self):
        super().setUp()
        self.lib = self.dir / "movies"
        for name in ("A.mkv", "B.mkv", "C.mp4", "D.mkv", "notes.txt", ".hidden.mkv", ".trash/E.mkv"):
            (self.lib / name).parent.mkdir(parents=True, exist_ok=True)
            (self.lib / name).write_bytes(b"x")
        self.worker = pi.Worker([], {**pi.DEFAULTS, "library_dirs": [str(self.lib)]}, {})
        self.fixed = []
        for name, value in (("probe", media(audio(1, "aac", "eng"))), ("save", None), ("operation_running", False)):
            patcher = mock.patch.object(pi, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(pi.Worker, "fix", lambda w, path, info, title: self.fixed.append(path.name) or True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_few_per_round_skipping_hidden_and_other_files(self):
        self.worker.sweep()
        self.assertEqual(len(self.fixed), pi.SWEEP_PER_ROUND)
        self.assertTrue(set(self.fixed) <= {"A.mkv", "B.mkv", "C.mp4", "D.mkv"})

    def test_done_files_skipped_failures_wait(self):
        for name in ("A.mkv", "B.mkv"):
            path = self.lib / name
            self.worker.state["seen"][str(path)] = self.worker.mark(path)
        self.worker.state["failures"][str(self.lib / "C.mp4")] = {"count": 1, "last": int(time.time())}
        self.worker.sweep()
        self.assertEqual(self.fixed, ["D.mkv"])

    def test_forgets_files_that_are_gone(self):
        self.worker.state["seen"]["/gone.mkv"] = [1]
        self.worker.state["failures"]["/gone.mkv"] = {"count": 1, "last": 0}
        with mock.patch.object(pi.Worker, "fix", return_value=False):
            self.worker.sweep()
        self.assertNotIn("/gone.mkv", self.worker.state["seen"])
        self.assertEqual(self.worker.state["failures"], {})

    def test_stops_when_tools_fail_or_an_install_starts(self):
        with mock.patch.object(pi, "probe", side_effect=pi.ToolTrouble("ffprobe broken")):
            self.worker.sweep()
        self.assertEqual(self.fixed, [])
        with mock.patch.object(pi, "operation_running", return_value=True):
            self.worker.sweep()
        self.assertEqual(len(self.fixed), 1)

    def test_unreadable_files_marked_so_theyre_not_probed_every_round(self):
        with mock.patch.object(pi, "probe", return_value=None):
            self.worker.sweep()
        self.assertEqual(self.fixed, [])
        self.assertEqual(len(self.worker.state["seen"]), 4)


class ByHand(Scratch):
    def test_check(self):
        info = media(audio(1, "ac3", "eng"), sub(2, "hdmv_pgs_subtitle", "eng"))
        with mock.patch.object(pi, "probe", return_value=info), mock.patch.object(pi, "decodes", return_value=True), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(pi.by_hand("--check", str(self.dir / "Film.mkv")), 0)
        text = out.getvalue()
        self.assertIn("needs stereo audio (from ac3)", text)
        self.assertIn("en subtitles only as pictures (track 2)", text)

    def test_fix(self):
        with mock.patch.object(pi, "probe", return_value=None), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(pi.by_hand("--fix", str(self.dir / "Film.mkv")), 1)
        self.assertIn("can't read the file", out.getvalue())
        with mock.patch.object(pi, "probe", return_value=media(audio(1, "aac", "eng"))), \
                mock.patch.object(pi.Worker, "fix", return_value=False) as fix:
            self.assertEqual(pi.by_hand("--fix", str(self.dir / "Film.mkv")), 0)
        fix.assert_called_once()
        self.assertTrue((self.dir / ".state/state.json").exists())


class MainLoop(Scratch):
    def rounds(self, n):
        """time.sleep that ends the loop after n rounds"""
        count = iter(range(n))

        def sleep(seconds):
            if next(count, None) is None or n == 1:
                raise KeyboardInterrupt
        return sleep

    def test_a_round_saves_state_and_status_even_when_it_fails(self):
        with mock.patch.object(pi.sys, "argv", ["postimport"]), mock.patch.object(pi, "operation_running", return_value=False), \
                mock.patch.object(pi.Worker, "new_imports", side_effect=RuntimeError("boom")), \
                mock.patch.object(pi.time, "sleep", self.rounds(1)), self.assertRaises(KeyboardInterrupt):
            pi.main()
        self.assertTrue((self.dir / ".state/state.json").exists())
        self.assertTrue((self.dir / ".state/status.json").exists())

    def test_waits_while_an_install_runs(self):
        with mock.patch.object(pi.sys, "argv", ["postimport"]), mock.patch.object(pi, "operation_running", return_value=True), \
                mock.patch.object(pi.Worker, "new_imports") as work, mock.patch.object(pi.time, "sleep", self.rounds(1)), \
                self.assertRaises(KeyboardInterrupt):
            pi.main()
        work.assert_not_called()

    def test_by_hand_from_the_command_line(self):
        with mock.patch.object(pi.sys, "argv", ["postimport", "--check", "x.mkv"]), \
                mock.patch.object(pi, "by_hand", return_value=0) as by_hand:
            self.assertEqual(pi.main(), 0)
        by_hand.assert_called_once_with("--check", "x.mkv")


if __name__ == "__main__":
    unittest.main()


class Progress(Scratch):
    def test_worker_reports_percentage_and_time_left(self):
        worker = pi.Worker([], dict(pi.DEFAULTS), {})
        with mock.patch.object(pi.time, "time", return_value=1000.0):
            worker.working(self.dir / "Film.mkv", "Film", "adding stereo audio")
        with mock.patch.object(pi.time, "time", return_value=1060.0):
            worker.progress(0.25)   # a minute for a quarter: three more
        self.assertEqual((worker.current["progress"], worker.current["eta"]), (25, 180))
        status = pi.read_json(self.dir / ".state/status.json")
        self.assertEqual(status["current"]["progress"], 25)
        worker.progress(0.01)   # too early to tell
        self.assertIsNone(worker.current["eta"])
        worker.working(self.dir / "Film.mkv", "Film", None)
        worker.progress(0.5)    # nothing running: nothing to report
        self.assertIsNone(worker.current)

    @unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
    def test_real_rewrite_reports_progress(self):
        video = self.dir / "Film.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=20",
                        "-f", "lavfi", "-i", "sine=duration=20", "-ac", "6", "-c:v", "mpeg4", "-c:a", "ac3", str(video)], check=True)
        info = pi.probe(video)
        seen = []
        self.assertTrue(pi.add_stereo(video, info, pi.needs_stereo(info, ""), seen.append))
        self.assertTrue(seen, "no progress reported")
        self.assertTrue(all(0 <= f <= 1 for f in seen))
        # And a failure still reports ffmpeg's error
        code, errors = pi.run_ffmpeg(["ffmpeg", "-i", str(self.dir / "missing.mkv"), str(self.dir / "o.mkv")], 20, seen.append)
        self.assertNotEqual(code, 0)
        self.assertIn("missing.mkv", errors)


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
class RealCues(Scratch):
    def test_cues_read_from_the_file(self):
        from tests.test_postimport import srt
        subs = self.dir / "in.srt"
        subs.write_text(srt(cues=40, until=50))
        video = self.dir / "Film.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5:duration=60", "-i", str(subs),
                        "-map", "0", "-map", "1", "-c:v", "mpeg4", "-c:s", "srt", str(video)], check=True)
        cues = pi.stream_cues(video, 1)
        assert cues is not None
        count, last = cues
        self.assertEqual(count, 40)
        self.assertAlmostEqual(last, 48.75, delta=1)
        self.assertIsNone(pi.stream_cues(self.dir / "missing.mkv", 1))


class DamagedRecord(Scratch):
    def test_set_aside_and_reported_not_silently_replaced(self):
        f = self.dir / ".state/state.json"
        f.write_text("{damaged")
        with mock.patch.object(pi, "notify") as notify:
            self.assertEqual(pi.load_state(), {})
        notify.assert_called_once()
        aside = list((self.dir / ".state").glob("state.json.unreadable-*"))
        self.assertEqual(len(aside), 1)
        self.assertEqual(aside[0].read_text(), "{damaged")
        self.assertFalse(f.exists())
        f.write_text('{"seen": {}}')
        self.assertEqual(pi.load_state(), {"seen": {}})


class FullDisk(Scratch):
    """A write that runs out of space leaves the old file, nothing half-written"""

    def full(self, path_self, text, *args, **kw):
        with open(path_self, "w") as f:
            f.write(text[: len(text) // 2])
        raise OSError(28, "No space left on device")

    def test_records_keep_their_old_version(self):
        from mediaserver import common as c
        target = self.dir / "record.json"
        target.write_text('{"old": true}')
        with mock.patch.object(Path, "write_text", lambda path, text, *a, **kw: self.full(path, text)), \
                self.assertRaises(OSError):
            c.write_json(target, {"new": True})
        self.assertEqual(target.read_text(), '{"old": true}')
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".record")], [])

    def test_ocr_subtitles_not_left_half_written(self):
        video = self.dir / "Film.mkv"
        video.write_bytes(b"x")
        cues = "".join(f"{i}\n00:00:0{i % 9},000 --> 00:00:0{i % 9},500\nline\n\n" for i in range(20))

        def run(cmd, **kw):
            if "pgsrip" in cmd:
                Path(cmd[-1]).with_suffix(".srt").write_text(cues)
            else:
                Path(cmd[-1]).write_bytes(b"PG")
            return mock.Mock(returncode=0, stderr="")
        real_write = Path.write_text

        def write_text(path_self, text, *a, **kw):
            if path_self.name.startswith(".Film"):
                return self.full(path_self, text)
            return real_write(path_self, text, *a, **kw)
        with mock.patch.object(pi.subprocess, "run", run), mock.patch.object(Path, "write_text", write_text):
            self.assertEqual(pi.ocr(video, "en", {"index": 2}), "failed")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if "srt" in p.name), [])
