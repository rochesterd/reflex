"""Tests for session_reader against real recorded sessions -- record with
real SyntheticCameras, then read the result back. Integration-style rather
than mocked PyAV: the thing worth testing is whether timestamp alignment
actually holds across two independently-encoded files, which a mock can't
tell us.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from audio_capture import SyntheticAudio
from recorder import Recorder
from session_format import INSTRUMENT_STREAM, THIRD_PERSON_STREAM
from session_reader import Session, SessionError, SessionPlayer
from synthetic_camera import SyntheticCamera

FPS = 30


def record_session(root: str, seconds: float, *, instrument_fps: int = FPS, third_fps: int = FPS) -> Path:
    """A real recorded session directory."""
    instrument = SyntheticCamera(320, 240, name="instrument", fps=instrument_fps)
    third = SyntheticCamera(160, 120, name="third", fps=third_fps)
    instrument.start()
    third.start()
    try:
        recorder = Recorder(
            instrument, third,
            instrument_key="slit_lamp", instrument_label="BI900",
            third_person_label="third-person camera",
            output_root=root, fps=FPS, preset="ultrafast",
        )
        recorder.start()
        time.sleep(seconds)
        recorder.stop()
    finally:
        instrument.stop()
        third.stop()
    return recorder.session_dir


class SessionLoadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def test_loads_a_real_recorded_session(self):
        d = record_session(self.root, 2)
        session = Session.load(d)

        self.assertEqual(session.instrument_key, "slit_lamp")
        self.assertEqual(set(session.streams), {INSTRUMENT_STREAM, THIRD_PERSON_STREAM})
        self.assertEqual(session.instrument.label, "BI900")
        self.assertEqual((session.instrument.width, session.instrument.height), (320, 240))
        self.assertEqual((session.third_person.width, session.third_person.height), (160, 120))
        self.assertTrue(session.instrument.path.is_file())
        self.assertEqual(session.instrument.offset_s, 0.0)

    def test_missing_manifest_raises(self):
        empty = Path(self.root) / "not-a-session"
        empty.mkdir()
        with self.assertRaises(SessionError):
            Session.load(empty)

    def test_unsupported_format_version_is_refused_loudly(self):
        d = record_session(self.root, 1)
        manifest = d / "session.json"
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        raw["format_version"] = 1  # the pre-split composite.mp4 layout
        manifest.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaises(SessionError) as ctx:
            Session.load(d)
        self.assertIn("format_version", str(ctx.exception))

    def test_a_missing_stream_file_is_skipped_not_fatal(self):
        """One camera that captured nothing (the recorder then writes no
        file at all), or a session copied without all its files, must not
        make the *other* camera's recording unopenable -- that would throw
        away good, irreplaceable data over a partial failure. See
        DECISIONS.md's 2026-09-09 "a dead camera" entry.
        """
        d = record_session(self.root, 1)
        (d / "instrument.mp4").unlink()

        session = Session.load(d)

        self.assertEqual(set(session.streams), {THIRD_PERSON_STREAM})
        self.assertEqual(session.missing_streams, (INSTRUMENT_STREAM,))
        self.assertIsNone(session.instrument)
        # and it still plays
        with SessionPlayer(session) as player:
            self.assertGreater(player.duration, 0.0)
            self.assertIsNotNone(player.images()[THIRD_PERSON_STREAM])

    def test_a_null_stream_file_is_skipped(self):
        """What the recorder writes for a camera that captured nothing."""
        d = record_session(self.root, 1)
        manifest = d / "session.json"
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        raw["streams"][INSTRUMENT_STREAM]["file"] = None
        manifest.write_text(json.dumps(raw), encoding="utf-8")

        session = Session.load(d)

        self.assertEqual(set(session.streams), {THIRD_PERSON_STREAM})
        self.assertEqual(session.missing_streams, (INSTRUMENT_STREAM,))

    def test_all_stream_files_missing_raises(self):
        d = record_session(self.root, 1)
        (d / "instrument.mp4").unlink()
        (d / "third_person.mp4").unlink()
        with self.assertRaises(SessionError):
            Session.load(d)


class SessionPlayerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def test_duration_and_frames_at_time_zero(self):
        d = record_session(self.root, 3)
        with SessionPlayer(Session.load(d)) as player:
            self.assertAlmostEqual(player.duration, 3.0, delta=0.6)
            images = player.images()
            self.assertEqual(set(images), {INSTRUMENT_STREAM, THIRD_PERSON_STREAM})
            self.assertEqual(images[INSTRUMENT_STREAM].shape, (240, 320, 3))
            self.assertEqual(images[THIRD_PERSON_STREAM].shape, (120, 160, 3))

    def test_advancing_moves_both_streams_forward_together(self):
        d = record_session(self.root, 4)
        with SessionPlayer(Session.load(d)) as player:
            first = {r: img.copy() for r, img in player.images().items()}
            player.advance_to(2.0)
            self.assertAlmostEqual(player.position, 2.0, delta=0.01)
            later = player.images()
            for role in first:
                # SyntheticCamera burns a frame counter and timestamp into
                # each frame, so a real advance must change the pixels.
                self.assertFalse((first[role] == later[role]).all(), role)

    def test_mismatched_rates_stay_aligned(self):
        """The real case: an ~11fps instrument beside a 30fps third-person.
        The slow stream holds its frame between its own captures rather
        than anything duplicating or interpolating."""
        d = record_session(self.root, 4, instrument_fps=11, third_fps=30)
        session = Session.load(d)
        with SessionPlayer(session) as player:
            for t in (0.5, 1.5, 2.5, 3.0):
                player.seek(t)
                images = player.images()
                for role, img in images.items():
                    self.assertIsNotNone(img, f"{role} had no frame at t={t}")
        # Fewer instrument frames than third-person, spanning the same time.
        self.assertLess(session.instrument.frame_count, session.third_person.frame_count * 0.6)

    def test_seek_lands_at_or_before_the_target_and_is_repeatable(self):
        d = record_session(self.root, 4)
        with SessionPlayer(Session.load(d)) as player:
            player.seek(3.0)
            self.assertAlmostEqual(player.position, 3.0, delta=0.01)
            late = {r: img.copy() for r, img in player.images().items()}

            player.seek(0.5)  # backwards
            self.assertAlmostEqual(player.position, 0.5, delta=0.01)
            early = {r: img.copy() for r, img in player.images().items()}
            for role in late:
                self.assertFalse((late[role] == early[role]).all(), role)

            player.seek(3.0)  # same target again -> same frame
            again = player.images()
            for role in late:
                self.assertTrue((late[role] == again[role]).all(), role)

    def test_position_is_clamped_to_the_session(self):
        d = record_session(self.root, 2)
        with SessionPlayer(Session.load(d)) as player:
            player.advance_to(999.0)
            self.assertAlmostEqual(player.position, player.duration, delta=0.01)
            self.assertIsNotNone(player.images()[INSTRUMENT_STREAM])
            player.seek(-5.0)
            self.assertEqual(player.position, 0.0)

    def test_close_is_idempotent(self):
        d = record_session(self.root, 1)
        player = SessionPlayer(Session.load(d))
        player.close()
        player.close()



class AudioStreamTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _record_with_audio(self) -> Path:
        instrument = SyntheticCamera(160, 120, name="instrument", fps=30)
        third = SyntheticCamera(160, 120, name="third", fps=30)
        mic = SyntheticAudio()
        instrument.start()
        third.start()
        mic.start()
        try:
            recorder = Recorder(
                instrument, third, instrument_key="slit_lamp",
                output_root=self._tmp.name, fps=30, preset="ultrafast", audio=mic,
            )
            recorder.start()
            time.sleep(1.0)
            recorder.stop()
        finally:
            instrument.stop()
            third.stop()
            mic.stop()
        return recorder.session_dir

    def test_a_silent_session_has_no_audio(self):
        session = Session.load(record_session(self._tmp.name, 1))
        self.assertIsNone(session.audio)
        self.assertEqual(set(session.video_streams), {INSTRUMENT_STREAM, THIRD_PERSON_STREAM})

    def test_an_audio_stream_is_read_as_audio_and_kept_out_of_the_player(self):
        session = Session.load(self._record_with_audio())
        self.assertIsNotNone(session.audio)
        self.assertEqual(session.audio.kind, "audio")
        self.assertEqual(session.audio.samplerate, 48000)
        self.assertEqual(session.audio.channels, 1)
        self.assertEqual(set(session.video_streams), {INSTRUMENT_STREAM, THIRD_PERSON_STREAM})
        # The player is a video player: it must not try to decode the M4A
        # as video, and images() must still be exactly the two panes.
        with SessionPlayer(session) as player:
            self.assertEqual(set(player.images()), {INSTRUMENT_STREAM, THIRD_PERSON_STREAM})


if __name__ == "__main__":
    unittest.main()
