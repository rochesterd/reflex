"""Records from two real SyntheticCamera instances and checks the two
resulting per-camera MP4s -- decoded, not just present: frame counts,
strictly increasing millisecond PTS on the shared session clock, both
streams spanning the same interval, the rate limit, drop accounting, and
the verify-then-delete-MKV rule (never delete both).
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import av
import cv2

import numpy as np

from audio_capture import AudioCapture, SyntheticAudio
from recorder import Recorder, _StreamWriter, _mp4_verifies
from session_format import AUDIO_STREAM, INSTRUMENT_STREAM, THIRD_PERSON_STREAM
from session_reader import Session
from synthetic_camera import SyntheticCamera

FPS = 30


def _pts_seconds(mp4_path: Path) -> list[float]:
    """Every decoded frame's presentation time, in seconds."""
    with av.open(str(mp4_path)) as container:
        stream = container.streams.video[0]
        tb = stream.time_base
        return [float(frame.pts * tb) for frame in container.decode(stream)]


def _record(tmp_root: str, seconds: float, *, cam_a: SyntheticCamera, cam_b: SyntheticCamera, fps: int = FPS, **kwargs):
    cam_a.start()
    cam_b.start()
    try:
        recorder = Recorder(
            cam_a,
            cam_b,
            instrument_key="slit_lamp",
            instrument_label="BI900",
            third_person_label="third-person camera",
            output_root=tmp_root,
            fps=fps,
            preset="ultrafast",
            **kwargs,
        )
        recorder.start()
        time.sleep(seconds)
        info = recorder.stop()
    finally:
        cam_a.stop()
        cam_b.stop()
    return recorder, info


class TestRecorder(unittest.TestCase):
    def test_two_streams_with_shared_clock_pts(self):
        seconds = 6
        with tempfile.TemporaryDirectory() as tmp_root:
            recorder, info = _record(
                tmp_root,
                seconds,
                cam_a=SyntheticCamera(640, 480, name="cam-a", fps=FPS),
                cam_b=SyntheticCamera(320, 240, name="cam-b", fps=FPS),
            )
            d = recorder.session_dir

            # Manifest shape
            self.assertEqual(info["format_version"], 2)
            self.assertEqual(info["instrument"], "slit_lamp")
            self.assertEqual(info["fps"], FPS)
            self.assertEqual(set(info["streams"]), {INSTRUMENT_STREAM, THIRD_PERSON_STREAM})
            self.assertEqual(info["streams"][INSTRUMENT_STREAM]["label"], "BI900")
            self.assertEqual(info["streams"][INSTRUMENT_STREAM]["file"], "instrument.mp4")
            self.assertEqual(info["streams"][THIRD_PERSON_STREAM]["file"], "third_person.mp4")
            with open(d / "session.json", encoding="utf-8") as f:
                self.assertEqual(json.load(f), info)

            # Files: MP4s at native resolution, MKVs gone (verified)
            for role, (w, h) in ((INSTRUMENT_STREAM, (640, 480)), (THIRD_PERSON_STREAM, (320, 240))):
                s = info["streams"][role]
                self.assertTrue(s["verified"])
                self.assertNotIn("mkv", s)
                self.assertTrue((d / f"{role}.mp4").exists())
                self.assertFalse((d / f"{role}.mkv").exists())
                self.assertEqual((s["width"], s["height"]), (w, h))
                with av.open(str(d / f"{role}.mp4")) as c:
                    self.assertEqual((c.streams.video[0].width, c.streams.video[0].height), (w, h))

            # Frame counts near fps*seconds, and PTS strictly increasing
            pts = {role: _pts_seconds(d / f"{role}.mp4") for role in info["streams"]}
            for role, s in info["streams"].items():
                self.assertAlmostEqual(s["frame_count"], FPS * seconds, delta=FPS * seconds * 0.2)
                self.assertAlmostEqual(len(pts[role]), s["frame_count"], delta=2)
                self.assertTrue(all(b > a for a, b in zip(pts[role], pts[role][1:])), role)
                self.assertGreaterEqual(s["dropped_frames"], 0)
                self.assertEqual(s["offset_s"], 0.0)

            # Shared clock: both start near 0 and end near `seconds`, together.
            for role in pts:
                self.assertLess(pts[role][0], 0.5, role)
                self.assertAlmostEqual(pts[role][-1], seconds, delta=seconds * 0.2)
            self.assertLess(abs(pts[INSTRUMENT_STREAM][-1] - pts[THIRD_PERSON_STREAM][-1]), 0.5)

    def test_rate_limit_caps_a_fast_camera_at_recording_fps(self):
        seconds = 3
        with tempfile.TemporaryDirectory() as tmp_root:
            _, info = _record(
                tmp_root,
                seconds,
                cam_a=SyntheticCamera(160, 120, name="fast", fps=90),  # 3x the recording rate
                cam_b=SyntheticCamera(160, 120, name="normal", fps=FPS),
                fps=FPS,
            )
            fast = info["streams"][INSTRUMENT_STREAM]
            self.assertGreater(fast["rate_limited_frames"], 0)
            self.assertAlmostEqual(fast["frame_count"], FPS * seconds, delta=FPS * seconds * 0.25)
            self.assertLess(fast["frame_count"], 90 * seconds * 0.6)  # clearly not the full 90fps

    def test_dropped_frames_are_detected_from_the_camera_s_own_drops(self):
        """Frame.index must be the source's own sequence number, not a
        gapless counter (see camera.py / DECISIONS.md), or a camera's own
        drops are invisible. SyntheticCamera's drop_rate simulates exactly
        that."""
        with tempfile.TemporaryDirectory() as tmp_root:
            _, info = _record(
                tmp_root,
                3,
                cam_a=SyntheticCamera(160, 120, name="cam-a", fps=FPS, drop_rate=0.3),
                cam_b=SyntheticCamera(160, 120, name="cam-b", fps=FPS),
            )
            self.assertGreater(info["streams"][INSTRUMENT_STREAM]["dropped_frames"], 0)

    def test_mkv_is_kept_when_the_mp4_fails_verification(self):
        """Never delete both: a stream whose MP4 doesn't verify keeps its
        MKV as the recoverable copy and says so in the manifest."""
        with tempfile.TemporaryDirectory() as tmp_root:
            with patch("recorder._mp4_verifies", return_value=False):
                recorder, info = _record(
                    tmp_root,
                    2,
                    cam_a=SyntheticCamera(160, 120, name="cam-a", fps=FPS),
                    cam_b=SyntheticCamera(160, 120, name="cam-b", fps=FPS),
                )
            d = recorder.session_dir
            for role, s in info["streams"].items():
                self.assertFalse(s["verified"])
                self.assertEqual(s["mkv"], f"{role}.mkv")
                self.assertTrue((d / f"{role}.mkv").exists())
                self.assertTrue((d / f"{role}.mp4").exists())

    def test_a_writer_that_fails_mid_recording_keeps_its_mkv_and_the_other_stream_is_fine(self):
        """A full disk / encoder fault mid-recording must not silently kill
        the writer thread and leave a truncated file reported as complete.
        The failed stream keeps its MKV, is flagged unverified with an
        `error`, the session still loads, and the other camera's stream --
        a separate thread -- is unaffected. See DECISIONS.md's "Harden the
        recording path" entry.
        """
        real_encode = _StreamWriter._encode
        counts: dict[str, int] = {}

        def failing_encode(self, frame):
            counts[self.role] = counts.get(self.role, 0) + 1
            if self.role == INSTRUMENT_STREAM and counts[self.role] > 12:
                raise OSError("simulated disk full")
            return real_encode(self, frame)

        with tempfile.TemporaryDirectory() as tmp_root:
            with patch.object(_StreamWriter, "_encode", failing_encode):
                recorder, info = _record(
                    tmp_root,
                    4,
                    cam_a=SyntheticCamera(160, 120, name="cam-a", fps=FPS),
                    cam_b=SyntheticCamera(160, 120, name="cam-b", fps=FPS),
                )
            d = recorder.session_dir
            inst = info["streams"][INSTRUMENT_STREAM]
            third = info["streams"][THIRD_PERSON_STREAM]

            self.assertFalse(inst["verified"])
            self.assertIn("error", inst)
            self.assertTrue((d / "instrument.mkv").exists())
            self.assertTrue((d / inst["file"]).exists())
            self.assertLessEqual(inst["frame_count"], 13)

            self.assertTrue(third["verified"])
            self.assertNotIn("error", third)
            self.assertGreater(third["frame_count"], 30)

            # stop() still produced a usable manifest and the session opens.
            session = Session.load(d)
            self.assertEqual(set(session.streams), {INSTRUMENT_STREAM, THIRD_PERSON_STREAM})

    def test_a_frame_that_changes_size_mid_recording_is_resized_not_fatal(self):
        """UvcCamera._try_reconnect() can bring a device back at a different
        resolution after a USB drop. The recorder's encoder is fixed at the
        size the camera reported at start(), so a mismatched frame would
        make encode() raise -- it resizes to fit instead, keeping the rest
        of the recording. See DECISIONS.md's "Harden the recording path".
        """

        class _ResolutionChangingCamera(SyntheticCamera):
            def __init__(self, *args, change_after=15, shrunk=(320, 240), **kwargs):
                super().__init__(*args, **kwargs)
                self._change_after = change_after
                self._shrunk = shrunk

            def _render(self, elapsed, frame_index):
                image = super()._render(elapsed, frame_index)
                if frame_index >= self._change_after:
                    image = cv2.resize(image, self._shrunk)
                return image

        with tempfile.TemporaryDirectory() as tmp_root:
            recorder, info = _record(
                tmp_root,
                3,
                cam_a=_ResolutionChangingCamera(640, 480, name="reconnecting", fps=FPS),
                cam_b=SyntheticCamera(320, 240, name="cam-b", fps=FPS),
            )
            d = recorder.session_dir
            inst = info["streams"][INSTRUMENT_STREAM]

            self.assertTrue(inst["verified"])
            self.assertNotIn("error", inst)
            self.assertEqual((inst["width"], inst["height"]), (640, 480))
            self.assertGreater(inst["frame_count"], FPS)  # ran the whole time, not cut off at the size change

            with av.open(str(d / "instrument.mp4")) as c:
                stream = c.streams.video[0]
                self.assertEqual((stream.width, stream.height), (640, 480))
                sizes = {(f.width, f.height) for f in c.decode(stream)}
            self.assertEqual(sizes, {(640, 480)})

    def test_an_odd_camera_resolution_still_records(self):
        """libx264 with yuv420p refuses odd dimensions -- the encoder won't
        even open, so before _even() a camera reporting e.g. 641x481
        recorded nothing at all. session_export.py already guarded the
        export side; the record path didn't. See DECISIONS.md's 2026-09-09
        entry.
        """
        with tempfile.TemporaryDirectory() as tmp_root:
            recorder, info = _record(
                tmp_root,
                1.5,
                cam_a=SyntheticCamera(641, 481, name="odd", fps=FPS),
                cam_b=SyntheticCamera(320, 240, name="even", fps=FPS),
            )
            odd = info["streams"][INSTRUMENT_STREAM]

            self.assertTrue(odd["verified"])
            self.assertNotIn("error", odd)
            self.assertGreater(odd["frame_count"], 10)
            self.assertEqual((odd["width"], odd["height"]), (640, 480))

            with av.open(str(recorder.session_dir / "instrument.mp4")) as container:
                stream = container.streams.video[0]
                self.assertEqual((stream.width, stream.height), (640, 480))
                self.assertGreater(sum(1 for _ in container.decode(stream)), 10)

    def test_a_camera_that_captures_nothing_does_not_take_the_session_with_it(self):
        """PyAV never creates the container until a packet is written, so a
        camera delivering nothing for the whole session leaves no file at
        all. The manifest must say so (file: null) rather than name a
        phantom -- naming one made Session.load refuse the whole session,
        throwing away the *other* camera's good recording. See DECISIONS.md's
        2026-09-09 entry.
        """
        with tempfile.TemporaryDirectory() as tmp_root:
            recorder, info = _record(
                tmp_root,
                1.5,
                cam_a=SyntheticCamera(160, 120, name="dead", fps=FPS, drop_rate=1.0),
                cam_b=SyntheticCamera(160, 120, name="live", fps=FPS),
            )
            d = recorder.session_dir
            dead = info["streams"][INSTRUMENT_STREAM]
            live = info["streams"][THIRD_PERSON_STREAM]

            self.assertEqual(dead["frame_count"], 0)
            self.assertIsNone(dead["file"])
            self.assertFalse(dead["verified"])
            self.assertIn("no frames", dead["error"])
            self.assertNotIn("mkv", dead)  # there is no file to keep
            self.assertFalse((d / "instrument.mkv").exists())
            self.assertFalse((d / "instrument.mp4").exists())

            self.assertTrue(live["verified"])
            self.assertGreater(live["frame_count"], 10)

            # The surviving stream is still fully usable.
            session = Session.load(d)
            self.assertEqual(set(session.streams), {THIRD_PERSON_STREAM})
            self.assertEqual(session.missing_streams, (INSTRUMENT_STREAM,))

    def test_mp4_verifies_rejects_a_truncated_file_and_empty_recordings(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            recorder, info = _record(
                tmp_root,
                3,
                cam_a=SyntheticCamera(160, 120, name="cam-a", fps=FPS),
                cam_b=SyntheticCamera(160, 120, name="cam-b", fps=FPS),
            )
            good = recorder.session_dir / "instrument.mp4"
            expected = info["streams"][INSTRUMENT_STREAM]["frame_count"]
            self.assertTrue(_mp4_verifies(good, expected))

            truncated = recorder.session_dir / "truncated.mp4"
            data = good.read_bytes()
            truncated.write_bytes(data[: len(data) // 4])
            self.assertFalse(_mp4_verifies(truncated, expected))

            self.assertFalse(_mp4_verifies(good, 0))  # nothing recorded -> never treat as sole copy



class AudioRecordingTest(unittest.TestCase):
    """The microphone as a third stream on the shared clock. Recorded with
    SyntheticAudio through the real writer, remux and verify."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _record(self, seconds: float, mic=None) -> dict:
        instrument = SyntheticCamera(160, 120, name="instrument", fps=30)
        third = SyntheticCamera(160, 120, name="third", fps=30)
        instrument.start()
        third.start()
        if mic is not None:
            mic.start()
        try:
            recorder = Recorder(
                instrument, third, instrument_key="slit_lamp",
                output_root=self._tmp.name, fps=30, preset="ultrafast", audio=mic,
            )
            recorder.start()
            time.sleep(seconds)
            info = recorder.stop()
        finally:
            instrument.stop()
            third.stop()
            if mic is not None:
                mic.stop()
        self.session_dir = recorder.session_dir
        return info

    def test_no_microphone_means_no_audio_stream_and_nothing_else_changes(self):
        info = self._record(1.0)
        self.assertNotIn(AUDIO_STREAM, info["streams"])
        self.assertEqual(set(info["streams"]), {INSTRUMENT_STREAM, THIRD_PERSON_STREAM})

    def test_a_microphone_becomes_a_verified_m4a_on_the_session_clock(self):
        info = self._record(1.5, SyntheticAudio())
        audio = info["streams"][AUDIO_STREAM]
        self.assertEqual(audio["kind"], "audio")
        self.assertEqual(audio["file"], "audio.m4a")
        self.assertTrue(audio["verified"])
        self.assertEqual(audio["samplerate"], 48000)
        self.assertGreater(audio["samples"], 48000)
        self.assertFalse((self.session_dir / "audio.mka").exists(), "the interim MKA should be gone")

        with av.open(str(self.session_dir / "audio.m4a")) as container:
            stream = container.streams.audio[0]
            self.assertEqual(stream.rate, 48000)
            decoded = sum(frame.samples for frame in container.decode(stream))
        self.assertGreater(decoded, 48000)

    def test_audio_duration_matches_video_duration_closely(self):
        # Both come off one clock, so they must agree to within a block
        # plus the video's frame period -- no measured offset involved.
        info = self._record(1.5, SyntheticAudio())
        video_s = info["streams"][THIRD_PERSON_STREAM]["duration_s"]
        audio_s = info["streams"][AUDIO_STREAM]["duration_s"]
        self.assertLess(abs(video_s - audio_s), 0.15)

    def test_a_gap_in_the_blocks_is_filled_with_silence(self):
        # Push blocks by hand with a 200 ms hole: the writer must insert
        # silence so what follows lands where its timestamp says.
        mic = AudioCapture(device=None)
        instrument = SyntheticCamera(160, 120, name="instrument", fps=30)
        third = SyntheticCamera(160, 120, name="third", fps=30)
        instrument.start()
        third.start()
        try:
            recorder = Recorder(
                instrument, third, instrument_key="slit_lamp",
                output_root=self._tmp.name, fps=30, preset="ultrafast", audio=mic,
            )
            recorder.start()
            origin = recorder._origin_monotonic
            tone = (np.sin(np.arange(960) / 48000 * 2 * np.pi * 440) * 10000).astype(np.int16)[:, None]
            t = origin + 0.1
            for _ in range(10):
                t += 0.02
                mic.push(tone, timestamp=t)
            t += 0.2  # the hole
            for _ in range(10):
                t += 0.02
                mic.push(tone, timestamp=t)
            time.sleep(0.5)
            info = recorder.stop()
        finally:
            instrument.stop()
            third.stop()
        audio = info["streams"][AUDIO_STREAM]
        self.assertGreater(audio["silence_inserted_samples"], 0.15 * 48000)
        self.assertLess(audio["silence_inserted_samples"], 0.25 * 48000)
        # Total samples = 20 blocks + the gap; duration ends where the last block ended.
        self.assertAlmostEqual(audio["duration_s"], (t - origin), delta=0.03)

    def test_the_session_duration_is_the_videos_not_the_microphones(self):
        info = self._record(1.0, SyntheticAudio())
        video_s = max(info["streams"][r]["duration_s"] for r in (INSTRUMENT_STREAM, THIRD_PERSON_STREAM))
        self.assertEqual(info["duration_s"], video_s)


if __name__ == "__main__":
    unittest.main()
