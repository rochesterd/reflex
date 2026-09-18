"""Tests for session_export against real recorded sessions: every layout
exports to a real, decodable MP4 of the right size and duration, progress
and cancellation behave, and a cancelled or failed export leaves nothing
behind that could be mistaken for a finished file.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import av

from compositor import LAYOUT_MODES
from session_export import (
    ExportCancelled,
    default_export_name,
    export_session,
    natural_layout_size,
)
from session_reader import Session
from test_session_reader import record_session

FPS = 30


def decode(path: Path) -> tuple[int, int, int]:
    """(frame count, width, height) of a real MP4."""
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        size = (stream.width, stream.height)
        count = sum(1 for _ in container.decode(stream))
    return count, *size


class NaturalSizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        # 320x240 instrument, 160x120 third-person (see record_session).
        cls.session = Session.load(record_session(cls._tmp.name, 2))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_side_by_side_sums_widths(self):
        self.assertEqual(natural_layout_size(self.session, "side_by_side"), (320 + 160, 240))

    def test_single_camera_uses_that_camera(self):
        self.assertEqual(natural_layout_size(self.session, "instrument"), (320, 240))
        self.assertEqual(natural_layout_size(self.session, "third_person"), (160, 120))

    def test_pip_uses_the_main_camera(self):
        self.assertEqual(natural_layout_size(self.session, "picture_in_picture"), (320, 240))

    def test_dimensions_are_always_even_for_yuv420p(self):
        for layout in LAYOUT_MODES:
            width, height = natural_layout_size(self.session, layout)
            self.assertEqual(width % 2, 0, layout)
            self.assertEqual(height % 2, 0, layout)


class ExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.session_dir = record_session(cls._tmp.name, 3)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.session = Session.load(self.session_dir)
        self._out = tempfile.TemporaryDirectory()
        self.addCleanup(self._out.cleanup)
        self.out_dir = Path(self._out.name)

    def test_every_layout_exports_a_decodable_mp4(self):
        for layout in LAYOUT_MODES:
            with self.subTest(layout=layout):
                out = self.out_dir / default_export_name(layout)
                export_session(self.session, out, layout=layout, fps=FPS)

                self.assertTrue(out.exists())
                count, width, height = decode(out)
                self.assertEqual((width, height), natural_layout_size(self.session, layout))
                # Constant rate: one frame per 1/fps of session duration.
                expected = round(self.session_duration() * FPS)
                self.assertAlmostEqual(count, expected, delta=2)

    def session_duration(self) -> float:
        from session_reader import SessionPlayer

        with SessionPlayer(self.session) as player:
            return player.duration

    def test_export_does_not_touch_the_streams(self):
        before = {p.name: p.read_bytes() for p in self.session_dir.glob("*.mp4")}
        export_session(self.session, self.out_dir / "out.mp4", fps=FPS)
        after = {p.name: p.read_bytes() for p in self.session_dir.glob("*.mp4")}
        self.assertEqual(before, after)

    def test_refuses_to_overwrite_a_stream_file(self):
        with self.assertRaises(ValueError):
            export_session(self.session, self.session_dir / "instrument.mp4", fps=FPS)
        # ...and the stream is still intact.
        self.assertGreater((self.session_dir / "instrument.mp4").stat().st_size, 0)

    def test_progress_reaches_the_total(self):
        seen: list[tuple[int, int]] = []
        out = self.out_dir / "out.mp4"
        export_session(self.session, out, fps=FPS, progress_cb=lambda d, t: seen.append((d, t)))

        self.assertGreater(len(seen), 1)
        totals = {total for _, total in seen}
        self.assertEqual(len(totals), 1)  # total is stable throughout
        self.assertEqual(seen[0][0], 1)
        self.assertEqual(seen[-1][0], seen[-1][1])  # finished == total
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))

    def test_cancelling_raises_and_leaves_no_file_behind(self):
        out = self.out_dir / "out.mp4"
        calls = {"n": 0}

        def cancel() -> bool:
            calls["n"] += 1
            return calls["n"] > 5  # let a few frames through first

        with self.assertRaises(ExportCancelled):
            export_session(self.session, out, fps=FPS, cancel_cb=cancel)

        self.assertFalse(out.exists())
        self.assertEqual(list(self.out_dir.iterdir()), [])  # no .partial left either

    def test_a_failure_mid_encode_leaves_no_partial_file(self):
        out = self.out_dir / "out.mp4"
        with patch("session_export.compose_layout", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                export_session(self.session, out, fps=FPS)

        self.assertFalse(out.exists())
        self.assertEqual(list(self.out_dir.iterdir()), [])

    def test_explicit_out_size_is_honoured_and_made_even(self):
        out = self.out_dir / "out.mp4"
        export_session(self.session, out, fps=FPS, out_size=(641, 481))
        _, width, height = decode(out)
        self.assertEqual((width, height), (640, 480))

    def test_creates_the_output_directory(self):
        out = self.out_dir / "nested" / "deeper" / "out.mp4"
        export_session(self.session, out, fps=FPS)
        self.assertTrue(out.exists())



class AudioExportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_a_silent_session_exports_video_only(self):
        session = Session.load(record_session(self._tmp.name, 1))
        out = export_session(session, Path(self._tmp.name) / "silent.mp4", fps=FPS)
        with av.open(str(out)) as container:
            self.assertEqual([s.type for s in container.streams], ["video"])

    def test_an_audible_session_exports_with_an_aac_track(self):
        from test_audio_playback import record_with_audio

        session = record_with_audio(self._tmp.name, 1.5)
        out = export_session(session, Path(self._tmp.name) / "audible.mp4", fps=FPS)
        with av.open(str(out)) as container:
            kinds = sorted(s.type for s in container.streams)
            self.assertEqual(kinds, ["audio", "video"])
            audio = container.streams.audio[0]
            self.assertEqual(audio.codec_context.name, "aac")
            samples = sum(frame.samples for frame in container.decode(audio))
        self.assertGreater(samples, 48000)


if __name__ == "__main__":
    unittest.main()
