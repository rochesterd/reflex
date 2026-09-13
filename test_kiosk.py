"""Headless tests for kiosk.KioskController, the Qt-free state machine
behind app.py. Drives real SyntheticCamera instances and a real Recorder,
the same integration-style approach as test_recorder.py, rather than
mocking internals.
"""

from __future__ import annotations

import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import kiosk
from kiosk import (
    REQUIRED_SPACE_MULTIPLIER,
    KioskController,
    State,
    _frame_signature,
    estimate_recording_bytes,
)
from recorder import _StreamWriter
from synthetic_camera import SyntheticCamera


class FakeClock:
    """Manually-advanced clock so stall-detection tests don't need to
    actually sleep past the timeout.
    """

    def __init__(self, start: float = 0.0):
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class _FreezablePictureCamera(SyntheticCamera):
    """A camera whose picture can be frozen on demand while its frame
    counter keeps advancing -- exactly what a blocked or switched-off
    webcam looks like from the capture layer: reads succeed, Frame.index
    climbs, the pixels never change.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._frozen_image = None

    def freeze(self) -> None:
        self._frozen_image = super()._render(0.0, 0)

    def _render(self, elapsed: float, frame_index: int):
        if self._frozen_image is not None:
            return self._frozen_image
        return super()._render(elapsed, frame_index)


class _FailOnceCamera(SyntheticCamera):
    """Fails _open() exactly once, then behaves like a normal
    SyntheticCamera -- for exercising a failed instrument switch.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._should_fail = True

    def _open(self) -> None:
        if self._should_fail:
            self._should_fail = False
            raise RuntimeError("simulated start failure")
        super()._open()


def dynamic_disk_usage(free_holder: dict):
    """Fake shutil.disk_usage backed by a mutable dict, so a test can
    change the reported free space between polls without reaching into
    KioskController's internals.
    """

    def _disk_usage(_path: str):
        return types.SimpleNamespace(total=10**12, used=0, free=free_holder["free"])

    return _disk_usage


class TestEstimateRecordingBytes(unittest.TestCase):
    def test_scales_with_resolution_and_minutes(self):
        base = estimate_recording_bytes(1280, 720, 30, minutes=10)
        double_res = estimate_recording_bytes(2560, 1440, 30, minutes=10)
        double_minutes = estimate_recording_bytes(1280, 720, 30, minutes=20)
        self.assertAlmostEqual(double_res, base * 4, delta=base * 0.01)
        self.assertAlmostEqual(double_minutes, base * 2, delta=base * 0.01)


class TestKioskControllerPreflight(unittest.TestCase):
    def test_start_stays_blocked_with_no_instrument_selected(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            third_person = SyntheticCamera(160, 120, fps=30)
            instrument = SyntheticCamera(160, 120, fps=30)
            third_person.start()
            instrument.start()
            try:
                controller = KioskController(third_person, {"instrument": instrument}, output_root=tmp_root)
                time.sleep(0.2)
                status = controller.poll_preflight()

                self.assertIsNone(controller.selected_instrument)
                self.assertFalse(status.cameras_ready)
                self.assertEqual(controller.state, State.IDLE)
            finally:
                third_person.stop()
                instrument.stop()

    def test_start_stays_blocked_until_both_cameras_have_a_frame(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            instrument = SyntheticCamera(160, 120, fps=30)
            third_person = SyntheticCamera(160, 120, fps=30)
            instrument.start()
            # third_person is deliberately never started: get_latest() stays None.
            try:
                controller = KioskController(third_person, {"instrument": instrument}, output_root=tmp_root)
                controller.select_instrument("instrument")
                time.sleep(0.2)  # let instrument actually produce a frame
                status = controller.poll_preflight()

                self.assertFalse(status.cameras_ready)
                self.assertEqual(controller.state, State.IDLE)
            finally:
                instrument.stop()

    def test_disk_space_gate_blocks_and_then_releases_start(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            instrument = SyntheticCamera(160, 120, fps=30)
            third_person = SyntheticCamera(160, 120, fps=30)
            instrument.start()
            third_person.start()
            try:
                free_holder = {"free": 0}
                controller = KioskController(
                    third_person,
                    {"instrument": instrument},
                    output_root=tmp_root,
                    disk_usage_fn=dynamic_disk_usage(free_holder),
                )
                controller.select_instrument("instrument")
                time.sleep(0.2)

                status = controller.poll_preflight()
                self.assertTrue(status.cameras_ready)
                self.assertFalse(status.disk_ok)
                self.assertEqual(controller.state, State.IDLE)

                free_holder["free"] = 10**15
                status = controller.poll_preflight()
                self.assertTrue(status.disk_ok)
                self.assertEqual(controller.state, State.READY)
            finally:
                instrument.stop()
                third_person.stop()

    def test_a_missing_sessions_dir_drive_is_reported_not_raised(self):
        """A configured sessions_dir on a drive that isn't there (a removed
        USB disk, an offline share) makes shutil.disk_usage raise.
        poll_preflight() must surface that as not-ready with a reason, not
        let it propagate every poll and wedge the kiosk -- at startup it
        would otherwise be a bare crash with only a log line. See
        DECISIONS.md's "Config that would only fail at Start" entry.
        """
        def exploding_disk_usage(_path):
            raise OSError(2, "The system cannot find the path specified")

        instrument = SyntheticCamera(160, 120, fps=30)
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument.start()
        third_person.start()
        try:
            controller = KioskController(
                third_person,
                {"instrument": instrument},
                output_root="E:\\gone\\sessions",
                disk_usage_fn=exploding_disk_usage,
            )
            controller.select_instrument("instrument")
            time.sleep(0.2)

            status = controller.poll_preflight()  # must not raise

            self.assertTrue(status.cameras_ready)
            self.assertFalse(status.disk_ok)
            self.assertFalse(status.ok)
            self.assertIsNotNone(status.disk_error)
            self.assertEqual(controller.state, State.IDLE)
        finally:
            instrument.stop()
            third_person.stop()


class TestInstrumentSelection(unittest.TestCase):
    def test_select_instrument_starts_and_stops_cameras(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            third_person = SyntheticCamera(160, 120, fps=30)
            cam_a = SyntheticCamera(160, 120, fps=30)
            cam_b = SyntheticCamera(160, 120, fps=30)
            third_person.start()
            try:
                controller = KioskController(third_person, {"a": cam_a, "b": cam_b}, output_root=tmp_root)
                self.assertIsNone(controller.selected_instrument)

                controller.select_instrument("a")
                self.assertEqual(controller.selected_instrument, "a")
                time.sleep(0.1)
                self.assertIsNotNone(cam_a.get_latest())

                controller.select_instrument("b")
                self.assertEqual(controller.selected_instrument, "b")
                time.sleep(0.1)
                self.assertIsNotNone(cam_b.get_latest())
                # Switching must stop the camera that's no longer selected --
                # only one instrument camera runs at a time (see CLAUDE.md's
                # Architecture section and DECISIONS.md's "Third-person UVC
                # camera" entry).
                self.assertIsNone(cam_a._thread)
            finally:
                third_person.stop()
                cam_a.stop()
                cam_b.stop()

    def test_select_instrument_is_a_noop_when_already_selected(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            third_person = SyntheticCamera(160, 120, fps=30)
            cam_a = SyntheticCamera(160, 120, fps=30)
            third_person.start()
            try:
                controller = KioskController(third_person, {"a": cam_a}, output_root=tmp_root)
                controller.select_instrument("a")
                thread_before = cam_a._thread
                controller.select_instrument("a")
                self.assertIs(cam_a._thread, thread_before)
            finally:
                third_person.stop()
                cam_a.stop()

    def test_select_instrument_raises_while_recording(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            third_person = SyntheticCamera(160, 120, fps=30)
            cam_a = SyntheticCamera(160, 120, fps=30)
            cam_b = SyntheticCamera(160, 120, fps=30)
            third_person.start()
            try:
                controller = KioskController(
                    third_person,
                    {"a": cam_a, "b": cam_b},
                    output_root=tmp_root,
                    width=160,
                    height=120,
                    fps=30,
                )
                controller.select_instrument("a")
                time.sleep(0.2)
                controller.poll_preflight()
                controller.start_recording()
                try:
                    with self.assertRaises(RuntimeError):
                        controller.select_instrument("b")
                finally:
                    controller.stop_recording()
            finally:
                third_person.stop()
                cam_a.stop()
                cam_b.stop()

    def test_failed_switch_leaves_no_instrument_selected(self):
        """Regression test: if the newly-selected camera's start() raises,
        the previous camera has already been stopped -- selected_instrument
        must not keep pointing at it, or preflight would trust a frozen
        stale frame from a camera that isn't actually running anymore.
        """
        with tempfile.TemporaryDirectory() as tmp_root:
            third_person = SyntheticCamera(160, 120, fps=30)
            cam_a = SyntheticCamera(160, 120, fps=30)
            cam_b = _FailOnceCamera(160, 120, fps=30)
            third_person.start()
            try:
                controller = KioskController(third_person, {"a": cam_a, "b": cam_b}, output_root=tmp_root)
                controller.select_instrument("a")
                self.assertEqual(controller.selected_instrument, "a")

                with self.assertRaises(RuntimeError):
                    controller.select_instrument("b")

                self.assertIsNone(controller.selected_instrument)
            finally:
                third_person.stop()
                cam_a.stop()
                cam_b.stop()


class TestFrameSignature(unittest.TestCase):
    def test_identical_images_have_equal_signatures(self):
        image = np.random.default_rng(0).integers(0, 255, (120, 160, 3), dtype=np.uint8)
        self.assertTrue(np.array_equal(_frame_signature(image), _frame_signature(image.copy())))

    def test_a_changed_sampled_pixel_shows_up(self):
        image = np.zeros((120, 160, 3), dtype=np.uint8)
        changed = image.copy()
        changed[0, 0] = (1, 0, 0)  # [0,0] is always sampled by the stride
        self.assertFalse(np.array_equal(_frame_signature(image), _frame_signature(changed)))

    def test_signature_is_a_copy_not_a_view(self):
        """The controller holds signatures across polls, so a camera reusing
        its frame buffer must not silently rewrite history."""
        image = np.zeros((120, 160, 3), dtype=np.uint8)
        signature = _frame_signature(image)
        image[:] = 255
        self.assertFalse(np.array_equal(signature, _frame_signature(image)))


class TestFrozenCameraDetection(unittest.TestCase):
    """A camera that delivers frames but stops *seeing* is the failure
    CLAUDE.md calls the worst outcome: nothing else notices it, because
    reads succeed and Frame.index keeps advancing."""

    def _controller(self, tmp_root, third, instrument, clock):
        return KioskController(
            third,
            {"instrument": instrument},
            output_root=tmp_root,
            width=160,
            height=120,
            fps=30,
            freeze_timeout_s=1.0,
            clock=clock,
        )

    def test_a_live_camera_is_never_flagged_as_frozen(self):
        """The false-positive direction, which matters more than the true
        positive: wrongly refusing to record is its own failure."""
        with tempfile.TemporaryDirectory() as tmp_root:
            third = SyntheticCamera(160, 120, fps=30, name="third")
            instrument = SyntheticCamera(160, 120, fps=30, name="instrument")
            third.start()
            instrument.start()
            try:
                clock = FakeClock()
                controller = self._controller(tmp_root, third, instrument, clock)
                controller.select_instrument("instrument")
                time.sleep(0.2)
                controller.poll_preflight()

                for _ in range(5):
                    clock.advance(2.0)  # well past freeze_timeout_s each time
                    time.sleep(0.15)  # ...but real frames really do arrive
                    status = controller.poll_preflight()
                    self.assertEqual(status.frozen_cameras, ())

                self.assertEqual(controller.state, State.READY)
            finally:
                third.stop()
                instrument.stop()

    def test_frozen_camera_blocks_start(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            third = _FreezablePictureCamera(160, 120, fps=30, name="third")
            instrument = SyntheticCamera(160, 120, fps=30, name="instrument")
            third.freeze()  # blocked from the outset
            third.start()
            instrument.start()
            try:
                clock = FakeClock()
                controller = self._controller(tmp_root, third, instrument, clock)
                controller.select_instrument("instrument")
                time.sleep(0.2)
                controller.poll_preflight()  # baseline
                # Real frames must keep arriving while fake time passes --
                # otherwise nothing is delivering and this would be testing
                # the stall path instead.
                status = None
                for _ in range(6):
                    time.sleep(0.1)
                    clock.advance(0.5)
                    status = controller.poll_preflight()

                self.assertEqual(status.frozen_cameras, ("third_person",))
                self.assertFalse(status.ok)
                # Frames *are* arriving -- this is not the "no camera" case.
                self.assertTrue(status.cameras_ready)
                self.assertEqual(controller.state, State.IDLE)
            finally:
                third.stop()
                instrument.stop()

    def test_a_camera_that_freezes_mid_recording_stops_it_loudly(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            third = _FreezablePictureCamera(160, 120, fps=30, name="third")
            instrument = SyntheticCamera(160, 120, fps=30, name="instrument")
            third.start()
            instrument.start()
            try:
                clock = FakeClock()
                controller = self._controller(tmp_root, third, instrument, clock)
                controller.select_instrument("instrument")
                time.sleep(0.2)
                self.assertTrue(controller.poll_preflight().ok)

                controller.start_recording()
                time.sleep(0.3)
                controller.poll_recording()
                self.assertEqual(controller.state, State.RECORDING)

                third.freeze()  # the camera gets blocked mid-session
                # Frames keep arriving and Frame.index keeps climbing, so
                # the stall check stays quiet -- only the pixels are dead.
                for _ in range(6):
                    if controller.state != State.RECORDING:
                        break
                    time.sleep(0.1)
                    clock.advance(0.5)
                    controller.poll_recording()

                self.assertEqual(controller.state, State.ERROR)
                self.assertIn("stopped changing", controller.error_message)
                self.assertIn("third", controller.error_message.lower())
                # Stopped cleanly, not abandoned: the partial session is real.
                self.assertIsNotNone(controller.last_session_info)
                self.assertTrue((controller.last_session_dir / "instrument.mp4").exists())
            finally:
                third.stop()
                instrument.stop()

    def test_deselecting_an_instrument_drops_its_freshness_history(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            third = SyntheticCamera(160, 120, fps=30, name="third")
            cam_a = SyntheticCamera(160, 120, fps=30, name="a")
            cam_b = SyntheticCamera(160, 120, fps=30, name="b")
            third.start()
            try:
                clock = FakeClock()
                controller = KioskController(
                    third, {"a": cam_a, "b": cam_b}, output_root=tmp_root,
                    width=160, height=120, fps=30, freeze_timeout_s=1.0, clock=clock,
                )
                controller.select_instrument("a")
                time.sleep(0.2)
                controller.poll_preflight()
                self.assertIn("instrument", controller._last_signature)

                controller.select_instrument("b")
                self.assertEqual(controller._last_signature, {})
            finally:
                third.stop()
                for cam in (cam_a, cam_b):
                    cam.stop()


class TestKioskControllerSession(unittest.TestCase):
    def test_happy_path_full_session(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            instrument = SyntheticCamera(160, 120, fps=30, name="instrument")
            third_person = SyntheticCamera(160, 120, fps=30, name="third-person")
            instrument.start()
            third_person.start()
            try:
                controller = KioskController(
                    third_person,
                    {"instrument": instrument},
                    output_root=tmp_root,
                    width=160,
                    height=120,
                    fps=30,
                )
                controller.select_instrument("instrument")
                time.sleep(0.2)
                status = controller.poll_preflight()
                self.assertTrue(status.ok)
                self.assertEqual(controller.state, State.READY)

                controller.start_recording()
                self.assertEqual(controller.state, State.RECORDING)
                time.sleep(2.0)
                controller.poll_recording()
                self.assertEqual(controller.state, State.RECORDING)

                session_info = controller.stop_recording()
                self.assertEqual(controller.state, State.IDLE)
                self.assertIsNone(controller.error_message)

                self.assertEqual(session_info["format_version"], 2)
                self.assertEqual(session_info["instrument"], "instrument")
                self.assertEqual(set(session_info["streams"]), {"instrument", "third_person"})
                for stream in session_info["streams"].values():
                    self.assertGreaterEqual(stream["frame_count"], 1)
                    self.assertGreaterEqual(stream["dropped_frames"], 0)
                    self.assertTrue(stream["verified"])

                for role in ("instrument", "third_person"):
                    mp4_path = controller.last_session_dir / f"{role}.mp4"
                    self.assertTrue(mp4_path.exists(), role)
                    self.assertGreater(mp4_path.stat().st_size, 0, role)
                    self.assertFalse((controller.last_session_dir / f"{role}.mkv").exists(), role)

                # Re-running preflight after the session ends should let
                # Start become available again.
                status = controller.poll_preflight()
                self.assertTrue(status.ok)
                self.assertEqual(controller.state, State.READY)
            finally:
                instrument.stop()
                third_person.stop()

    def test_mid_recording_stall_triggers_loud_error_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            instrument = SyntheticCamera(160, 120, fps=30, name="instrument")
            third_person = SyntheticCamera(160, 120, fps=30, name="third-person")
            instrument.start()
            third_person.start()
            try:
                fake_clock = FakeClock()
                controller = KioskController(
                    third_person,
                    {"instrument": instrument},
                    output_root=tmp_root,
                    width=160,
                    height=120,
                    fps=30,
                    stall_timeout_s=1.0,
                    clock=fake_clock,
                )
                controller.select_instrument("instrument")
                time.sleep(0.2)
                controller.poll_preflight()
                self.assertEqual(controller.state, State.READY)

                controller.start_recording()
                time.sleep(0.3)  # let a few real frames get encoded first
                controller.poll_recording()  # baseline reflecting those real frames
                self.assertEqual(controller.state, State.RECORDING)

                third_person.drop_rate = 1.0  # simulate the third-person camera going silent
                fake_clock.advance(1.5)  # past stall_timeout_s, no real wait needed
                controller.poll_recording()

                self.assertEqual(controller.state, State.ERROR)
                self.assertIsNotNone(controller.error_message)
                self.assertIn("third_person", controller.error_message)

                # The recording must have been stopped cleanly, not abandoned.
                self.assertIsNotNone(controller.last_session_info)
                self.assertIn("streams", controller.last_session_info)
                # The instrument kept delivering; its partial file is playable.
                mp4_path = controller.last_session_dir / "instrument.mp4"
                self.assertTrue(mp4_path.exists())
                self.assertGreater(mp4_path.stat().st_size, 0)

                # The state machine itself isn't stuck: the next preflight
                # poll re-evaluates and moves on, even though the banner
                # (error_message / last_session_info) stays put for the UI.
                controller.poll_preflight()
                self.assertNotEqual(controller.state, State.ERROR)
            finally:
                instrument.stop()
                third_person.stop()

    def test_start_recording_reports_a_recorder_that_cannot_start(self):
        """The disk preflight measures free space on the nearest *existing*
        ancestor, so it can't see a sessions_dir that can't be created (a
        technician pointed it at a file, or somewhere unwritable). It says
        READY and then Recorder.start() raises. That must land in ERROR
        with a reason, not propagate into the Qt event loop and leave Start
        silently doing nothing forever. See DECISIONS.md's 2026-09-09 entry.
        """
        with tempfile.TemporaryDirectory() as tmp_root:
            blocker = Path(tmp_root) / "notadir.txt"
            blocker.write_text("this is a file, not a folder", encoding="utf-8")

            instrument = SyntheticCamera(160, 120, fps=30, name="instrument")
            third_person = SyntheticCamera(160, 120, fps=30, name="third-person")
            instrument.start()
            third_person.start()
            try:
                controller = KioskController(
                    third_person,
                    {"instrument": instrument},
                    output_root=blocker / "sessions",
                    width=160,
                    height=120,
                    fps=30,
                )
                controller.select_instrument("instrument")
                time.sleep(0.2)
                controller.poll_preflight()
                self.assertEqual(controller.state, State.READY)

                controller.start_recording()  # must not raise

                self.assertEqual(controller.state, State.ERROR)
                self.assertIn("Could not start recording", controller.error_message)
                self.assertIsNone(controller._recorder)
                self.assertIsNone(controller.last_session_dir)

                controller.poll_preflight()
                self.assertNotEqual(controller.state, State.ERROR)
            finally:
                instrument.stop()
                third_person.stop()

    def test_a_camera_reporting_an_unusable_resolution_refuses_to_record(self):
        """Refusing to start beats recording something broken (CLAUDE.md).
        _even() floors at 2, so without an explicit guard a camera whose
        driver hasn't filled in its frame size (0x0) would give a 2x2
        encoder and a "verified" recording of nothing.
        """
        class ZeroResolutionCamera(SyntheticCamera):
            @property
            def resolution(self):
                return (0, 0)

        with tempfile.TemporaryDirectory() as tmp_root:
            instrument = ZeroResolutionCamera(160, 120, fps=30, name="instrument")
            third_person = SyntheticCamera(160, 120, fps=30, name="third-person")
            instrument.start()
            third_person.start()
            try:
                controller = KioskController(
                    third_person,
                    {"instrument": instrument},
                    output_root=tmp_root,
                    width=160,
                    height=120,
                    fps=30,
                )
                controller.select_instrument("instrument")
                time.sleep(0.2)
                controller.poll_preflight()

                controller.start_recording()

                self.assertEqual(controller.state, State.ERROR)
                self.assertIn("unusable resolution", controller.error_message)
                # No litter: the half-made session directory is cleaned up.
                self.assertEqual(list(Path(tmp_root).iterdir()), [])
            finally:
                instrument.stop()
                third_person.stop()

    def test_a_writer_failing_mid_recording_stops_the_session_loudly(self):
        """A writer thread that hits a full disk / encoder fault leaves the
        camera itself fine, so the stall and freeze checks won't catch it.
        poll_recording() checks recorder.failed_stream() and stops the
        session immediately rather than letting one stream run on frozen
        until Stop. See DECISIONS.md's "Harden the recording path" entry.
        """
        real_encode = _StreamWriter._encode
        counts: dict[str, int] = {}

        def failing_encode(self, frame):
            counts[self.role] = counts.get(self.role, 0) + 1
            if self.role == "instrument" and counts[self.role] > 10:
                raise OSError("simulated disk full")
            return real_encode(self, frame)

        with tempfile.TemporaryDirectory() as tmp_root:
            instrument = SyntheticCamera(160, 120, fps=30, name="instrument")
            third_person = SyntheticCamera(160, 120, fps=30, name="third-person")
            instrument.start()
            third_person.start()
            try:
                controller = KioskController(
                    third_person,
                    {"instrument": instrument},
                    output_root=tmp_root,
                    width=160,
                    height=120,
                    fps=30,
                    instrument_labels={"instrument": "BI900"},
                )
                controller.select_instrument("instrument")
                time.sleep(0.2)
                controller.poll_preflight()
                self.assertEqual(controller.state, State.READY)

                with patch.object(_StreamWriter, "_encode", failing_encode):
                    controller.start_recording()
                    time.sleep(0.8)  # let the instrument writer pass its failure threshold
                    controller.poll_recording()

                self.assertEqual(controller.state, State.ERROR)
                self.assertIn("BI900", controller.error_message)
                self.assertIn("streams", controller.last_session_info)

                inst = controller.last_session_info["streams"]["instrument"]
                self.assertFalse(inst["verified"])
                self.assertIn("error", inst)
                self.assertTrue((controller.last_session_dir / "instrument.mkv").exists())
                # The other stream still finalized normally.
                self.assertTrue(controller.last_session_info["streams"]["third_person"]["verified"])

                controller.poll_preflight()
                self.assertNotEqual(controller.state, State.ERROR)
            finally:
                instrument.stop()
                third_person.stop()

    def test_stop_recording_lands_in_error_when_the_recorder_cannot_finalize(self):
        """A full disk during the MKV->MP4 remux / manifest write, or a
        stuck writer thread, makes Recorder.stop() raise. The normal Stop
        path must catch that, land in ERROR with the raw-files location
        named, and not propagate into the Qt event loop leaving the
        controller stuck in RECORDING -- same contract as _fail().
        """
        with tempfile.TemporaryDirectory() as tmp_root:
            instrument = SyntheticCamera(160, 120, fps=30, name="instrument")
            third_person = SyntheticCamera(160, 120, fps=30, name="third-person")
            instrument.start()
            third_person.start()
            try:
                session_dir = Path(tmp_root) / "2026-09-09_1200"
                session_dir.mkdir()

                class ExplodingRecorder:
                    def __init__(self):
                        self.session_dir = session_dir

                    def start(self):
                        pass

                    def stop(self):
                        raise OSError("no space left on device while writing session.json")

                controller = KioskController(
                    third_person,
                    {"instrument": instrument},
                    output_root=tmp_root,
                    width=160,
                    height=120,
                    fps=30,
                    recorder_factory=lambda _cam, _name: ExplodingRecorder(),
                )
                controller.select_instrument("instrument")
                time.sleep(0.2)
                controller.poll_preflight()
                self.assertEqual(controller.state, State.READY)

                controller.start_recording()
                self.assertEqual(controller.state, State.RECORDING)

                info = controller.stop_recording()  # must not raise

                self.assertEqual(controller.state, State.ERROR)
                self.assertIn("error", info)
                self.assertNotIn("streams", info)
                self.assertIsNotNone(controller.error_message)
                self.assertIn(str(session_dir), controller.error_message)
                self.assertEqual(controller.last_session_dir, session_dir)

                controller.poll_preflight()
                self.assertNotEqual(controller.state, State.ERROR)
            finally:
                instrument.stop()
                third_person.stop()


class TestSessionTimeLimit(unittest.TestCase):
    """A session stops by itself at max_session_minutes -- as a normal,
    verified stop, not an error. See DECISIONS.md's "First round of
    student feedback" entry."""

    def test_a_session_stops_cleanly_at_the_time_limit(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            instrument = SyntheticCamera(160, 120, fps=30, name="instrument")
            third_person = SyntheticCamera(160, 120, fps=30, name="third-person")
            instrument.start()
            third_person.start()
            try:
                clock = FakeClock()
                controller = KioskController(
                    third_person,
                    {"instrument": instrument},
                    output_root=tmp_root,
                    width=160,
                    height=120,
                    fps=30,
                    max_session_minutes=1.0,
                    clock=clock,
                )
                controller.select_instrument("instrument")
                time.sleep(0.2)
                controller.poll_preflight()
                self.assertIsNone(controller.recording_elapsed_s())

                controller.start_recording()
                self.assertEqual(controller.recording_elapsed_s(), 0.0)

                # Just short of the limit: still recording. Real frames
                # arrive during the sleep, so the stall check stays quiet.
                time.sleep(0.3)
                clock.advance(59.0)
                controller.poll_recording()
                self.assertEqual(controller.state, State.RECORDING)
                self.assertEqual(controller.recording_elapsed_s(), 59.0)
                self.assertFalse(controller.stopped_at_time_limit)

                time.sleep(0.2)
                clock.advance(1.0)
                controller.poll_recording()

                self.assertEqual(controller.state, State.IDLE)
                self.assertTrue(controller.stopped_at_time_limit)
                self.assertIsNone(controller.error_message)
                self.assertIsNone(controller.recording_elapsed_s())
                for role, stream in controller.last_session_info["streams"].items():
                    self.assertTrue(stream["verified"], role)
                    self.assertTrue((controller.last_session_dir / f"{role}.mp4").exists(), role)

                # The next session starts with a clean slate, and a manual
                # Stop is never reported as the limit.
                self.assertEqual(controller.poll_preflight().ok, True)
                controller.start_recording()
                self.assertFalse(controller.stopped_at_time_limit)
                time.sleep(0.2)
                controller.stop_recording()
                self.assertFalse(controller.stopped_at_time_limit)
            finally:
                instrument.stop()
                third_person.stop()

    def test_disk_preflight_budgets_for_a_full_length_session(self):
        """The cap and the disk estimate are one number: a preflight that
        passes has room for the longest session that can exist."""
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        with tempfile.TemporaryDirectory() as tmp_root:
            controller = KioskController(
                third_person,
                {"instrument": instrument},
                output_root=tmp_root,
                width=160,
                height=120,
                fps=30,
                max_session_minutes=7.0,
                disk_usage_fn=dynamic_disk_usage({"free": 10**15}),
            )
            status = controller.poll_preflight()
        self.assertAlmostEqual(
            status.required_bytes,
            REQUIRED_SPACE_MULTIPLIER * estimate_recording_bytes(160, 120, 30, minutes=7.0),
        )


class KeepAwakeTests(unittest.TestCase):
    """A recording holds the machine awake for exactly its own duration.
    Students reported the screen sleeping two minutes into a session."""

    def _run(self, body):
        calls = []
        with tempfile.TemporaryDirectory() as tmp_root:
            third = SyntheticCamera(160, 120, fps=30, name="third")
            instrument = SyntheticCamera(160, 120, fps=30, name="instrument")
            third.start()
            instrument.start()
            try:
                controller = KioskController(
                    third_person_camera=third,
                    instruments={"instrument": instrument},
                    output_root=tmp_root,
                    fps=30,
                )
                controller.select_instrument("instrument")
                # Recording is only reachable from READY, which preflight
                # grants once frames are actually arriving.
                for _ in range(20):
                    time.sleep(0.1)
                    controller.poll_preflight()
                    if controller.state is State.READY:
                        break
                with patch.object(kiosk, "set_keep_awake", side_effect=calls.append):
                    body(controller)
            finally:
                third.stop()
                instrument.stop()
        return calls

    def test_recording_holds_it_awake_and_stopping_releases_it(self):
        def body(controller):
            controller.start_recording()
            time.sleep(0.3)
            controller.stop_recording()

        self.assertEqual(self._run(body), [True, False])

    def test_nothing_is_held_when_no_recording_starts(self):
        self.assertEqual(self._run(lambda controller: controller.poll_preflight()), [])

    def test_a_failure_releases_it_too(self):
        """A session that ends badly must not leave the machine pinned
        awake -- the flag outlives the process that set it."""
        def body(controller):
            controller.start_recording()
            time.sleep(0.2)
            controller._fail("something went wrong")

        self.assertEqual(self._run(body), [True, False])


class _BrightnessCamera(SyntheticCamera):
    """Records what it was told, the way a real camera would apply it."""

    BRIGHTNESS_ADJUSTABLE = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.amounts = []

    def set_brightness(self, amount: float) -> None:
        self.amounts.append(amount)


class BrightnessTests(unittest.TestCase):
    def _controller(self, tmp_root, instruments, third):
        return KioskController(third, instruments, output_root=tmp_root, fps=30)

    def test_the_amount_reaches_the_selected_instrument(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            third = SyntheticCamera(160, 120, fps=30, name="third")
            instrument = _BrightnessCamera(160, 120, fps=30, name="instrument")
            controller = self._controller(tmp_root, {"instrument": instrument}, third)
            try:
                controller.select_instrument("instrument")
                controller.set_brightness(0.75)
                self.assertEqual(instrument.amounts[-1], 0.75)
                self.assertEqual(controller.brightness, 0.75)
            finally:
                instrument.stop()

    def test_switching_instruments_keeps_the_students_choice(self):
        """A camera that was restarted must come back the way they left it
        -- otherwise changing instrument silently resets the picture."""
        with tempfile.TemporaryDirectory() as tmp_root:
            third = SyntheticCamera(160, 120, fps=30, name="third")
            first = _BrightnessCamera(160, 120, fps=30, name="first")
            second = _BrightnessCamera(160, 120, fps=30, name="second")
            controller = self._controller(tmp_root, {"a": first, "b": second}, third)
            try:
                controller.select_instrument("a")
                controller.set_brightness(0.4)
                controller.select_instrument("b")
                self.assertEqual(second.amounts[-1], 0.4)
            finally:
                first.stop()
                second.stop()

    def test_a_camera_with_nothing_to_adjust_is_simply_told_anyway(self):
        """It takes the call and ignores it, so callers never branch on
        camera type."""
        class _PlainCamera(SyntheticCamera):
            BRIGHTNESS_ADJUSTABLE = False

        with tempfile.TemporaryDirectory() as tmp_root:
            third = SyntheticCamera(160, 120, fps=30, name="third")
            plain = _PlainCamera(160, 120, fps=30, name="plain")
            controller = self._controller(tmp_root, {"plain": plain}, third)
            try:
                controller.select_instrument("plain")
                controller.set_brightness(1.0)  # must not raise
                self.assertFalse(controller.brightness_adjustable())
            finally:
                plain.stop()

    def test_the_amount_is_clamped_to_its_range(self):
        """The range is what was measured on the model, so out-of-range is
        a caller bug, not a request to push the hardware further."""
        with tempfile.TemporaryDirectory() as tmp_root:
            third = SyntheticCamera(160, 120, fps=30, name="third")
            instrument = _BrightnessCamera(160, 120, fps=30, name="instrument")
            controller = self._controller(tmp_root, {"instrument": instrument}, third)
            try:
                controller.select_instrument("instrument")
                controller.set_brightness(4.0)
                self.assertEqual(controller.brightness, 1.0)
                controller.set_brightness(-1.0)
                self.assertEqual(controller.brightness, 0.0)
            finally:
                instrument.stop()


if __name__ == "__main__":
    unittest.main()
