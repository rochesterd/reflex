"""Tests for virtual_camera against real SyntheticCameras and an in-memory
backend. The driver isn't needed and usually isn't installed; the one test
that touches pyvirtualcam skips itself unless a virtual camera exists.

The slate tests are the important ones. In stream mode there is no Start
button to disable, so "a frozen camera is never shown as its last frame"
is the whole of loud-and-early, and it has to be proven on the pixels.
"""

from __future__ import annotations

import time
import unittest

import numpy as np

from compositor import LAYOUT_INSTRUMENT, LAYOUT_SIDE_BY_SIDE
from session_format import INSTRUMENT_STREAM, THIRD_PERSON_STREAM
from synthetic_camera import SyntheticCamera
from virtual_camera import (
    VirtualCameraSink,
    VirtualCameraUnavailable,
    PyVirtualCamBackend,
    slate,
)


class RecordingBackend:
    """Captures what the sink sends."""

    def __init__(self, fail_open: bool = False):
        self.frames: list[np.ndarray] = []
        self.opened: tuple[int, int, int] | None = None
        self.closed = False
        self.fail_open = fail_open

    def open(self, width: int, height: int, fps: int) -> str:
        if self.fail_open:
            raise VirtualCameraUnavailable("no driver")
        self.opened = (width, height, fps)
        return "Fake Virtual Camera"

    def send(self, bgr: np.ndarray) -> None:
        self.frames.append(bgr.copy())

    def close(self) -> None:
        self.closed = True


def _is_slate(image: np.ndarray) -> bool:
    """A slate is mostly its red background; video from SyntheticCamera is
    not. Looking at the pixels, not at a flag, is the point."""
    background = np.array([28, 28, 138], dtype=np.uint8)
    matches = np.all(image == background, axis=-1)
    return matches.mean() > 0.6


class SinkTest(unittest.TestCase):
    def setUp(self):
        self.instrument = SyntheticCamera(320, 240, name="instrument", fps=30)
        self.third = SyntheticCamera(160, 120, name="third", fps=30)
        self.instrument.start()
        self.third.start()
        self.addCleanup(self.instrument.stop)
        self.addCleanup(self.third.stop)
        # Let both deliver a frame before anything asks for one.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and (
            self.instrument.get_latest() is None or self.third.get_latest() is None
        ):
            time.sleep(0.01)
        self.backend = RecordingBackend()
        self.selected: SyntheticCamera | None = self.instrument
        self.sink = VirtualCameraSink(
            lambda: self.selected, self.third, self.backend, size=(640, 240), fps=30
        )

    def _pane(self, canvas: np.ndarray, role: str) -> np.ndarray:
        # Side-by-side at 640x240 with a 320x240 instrument and a 160x120
        # third-person: instrument fills the left half; third-person is
        # letterboxed into the right half. Sample the centre of each.
        if role == INSTRUMENT_STREAM:
            return canvas[:, :320]
        return canvas[60:180, 400:560]

    # -- composition -----------------------------------------------------

    def test_a_healthy_frame_carries_both_cameras(self):
        canvas = self.sink.compose_frame()
        self.assertEqual(canvas.shape, (240, 640, 3))
        self.assertFalse(_is_slate(self._pane(canvas, INSTRUMENT_STREAM)))
        self.assertFalse(_is_slate(self._pane(canvas, THIRD_PERSON_STREAM)))

    def test_a_faulted_role_is_a_slate_not_its_last_frame(self):
        self.sink.set_faults({INSTRUMENT_STREAM: "Slit lamp: picture frozen"})
        canvas = self.sink.compose_frame()
        self.assertTrue(_is_slate(self._pane(canvas, INSTRUMENT_STREAM)))
        self.assertFalse(_is_slate(self._pane(canvas, THIRD_PERSON_STREAM)))

    def test_clearing_the_fault_restores_the_picture(self):
        self.sink.set_faults({THIRD_PERSON_STREAM: "covered"})
        self.assertTrue(_is_slate(self._pane(self.sink.compose_frame(), THIRD_PERSON_STREAM)))
        self.sink.set_faults({})
        self.assertFalse(_is_slate(self._pane(self.sink.compose_frame(), THIRD_PERSON_STREAM)))

    def test_no_instrument_selected_is_a_slate(self):
        self.selected = None
        canvas = self.sink.compose_frame()
        self.assertTrue(_is_slate(self._pane(canvas, INSTRUMENT_STREAM)))

    def test_switching_instruments_needs_no_restart(self):
        other = SyntheticCamera(320, 240, name="bio", fps=30)
        other.start()
        self.addCleanup(other.stop)
        while other.get_latest() is None:
            time.sleep(0.01)
        before = self.sink.compose_frame()
        self.selected = other
        after = self.sink.compose_frame()
        # Different cameras burn in different counters, so the panes differ.
        self.assertFalse(np.array_equal(self._pane(before, INSTRUMENT_STREAM), self._pane(after, INSTRUMENT_STREAM)))

    def test_a_camera_not_yet_delivering_is_a_slate_not_black(self):
        cold = SyntheticCamera(320, 240, name="cold", fps=30)  # never started
        self.selected = cold
        canvas = self.sink.compose_frame()
        self.assertTrue(_is_slate(self._pane(canvas, INSTRUMENT_STREAM)))

    def test_the_slate_keeps_the_cameras_size_so_the_layout_holds(self):
        healthy = self.sink.compose_frame()
        self.sink.set_faults({INSTRUMENT_STREAM: "frozen"})
        faulted = self.sink.compose_frame()
        # The third-person pane must land in the same place either way.
        self.assertTrue(
            np.array_equal(
                self._pane(healthy, THIRD_PERSON_STREAM).shape, self._pane(faulted, THIRD_PERSON_STREAM).shape
            )
        )
        self.assertFalse(_is_slate(self._pane(faulted, THIRD_PERSON_STREAM)))

    def test_layout_is_honoured(self):
        self.sink.layout = LAYOUT_INSTRUMENT
        canvas = self.sink.compose_frame()
        self.assertEqual(canvas.shape, (240, 640, 3))
        # Instrument only: the right half is the instrument too, not a
        # letterboxed third-person.
        self.assertFalse(_is_slate(canvas[:, 320:]))

    # -- lifecycle -------------------------------------------------------

    def test_start_opens_the_backend_at_the_configured_size_and_streams(self):
        self.sink.start()
        self.addCleanup(self.sink.stop)
        self.assertEqual(self.backend.opened, (640, 240, 30))
        self.assertEqual(self.sink.device_name, "Fake Virtual Camera")

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(self.backend.frames) < 10:
            time.sleep(0.01)
        self.assertGreaterEqual(len(self.backend.frames), 10)
        self.assertEqual(self.backend.frames[0].shape, (240, 640, 3))

    def test_the_rate_is_roughly_the_configured_fps(self):
        self.sink.start()
        time.sleep(1.0)
        self.sink.stop()
        # 30 fps for one second, with generous slack for a loaded machine.
        self.assertGreater(self.backend.frames.__len__(), 15)
        self.assertLess(len(self.backend.frames), 45)

    def test_stop_closes_the_backend_and_is_idempotent(self):
        self.sink.start()
        self.sink.stop()
        self.sink.stop()
        self.assertTrue(self.backend.closed)
        self.assertFalse(self.sink.running)

    def test_a_missing_driver_fails_at_start_not_silently(self):
        sink = VirtualCameraSink(lambda: self.instrument, self.third, RecordingBackend(fail_open=True))
        with self.assertRaises(VirtualCameraUnavailable):
            sink.start()
        self.assertFalse(sink.running)

    def test_a_bad_frame_does_not_stop_the_stream(self):
        calls = {"n": 0}
        original = self.backend.send

        def flaky(bgr):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("driver hiccup")
            original(bgr)

        self.backend.send = flaky
        self.sink.start()
        self.addCleanup(self.sink.stop)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(self.backend.frames) < 5:
            time.sleep(0.01)
        self.assertGreaterEqual(len(self.backend.frames), 5)


class SlateTest(unittest.TestCase):
    def test_slate_is_the_requested_size_and_visibly_red(self):
        image = slate("Slit lamp: picture frozen", (1600, 1200))
        self.assertEqual(image.shape, (1200, 1600, 3))
        self.assertTrue(_is_slate(image))

    def test_slate_carries_text(self):
        # Text is white; a blank panel would have no white at all.
        image = slate("frozen", (640, 480))
        white = np.all(image == 255, axis=-1)
        self.assertGreater(white.sum(), 200)

    def test_tiny_slates_do_not_crash(self):
        self.assertEqual(slate("x", (16, 16)).shape, (16, 16, 3))


class RealDriverTest(unittest.TestCase):
    def test_opens_a_real_virtual_camera_if_one_is_installed(self):
        backend = PyVirtualCamBackend()
        try:
            name = backend.open(640, 480, 30)
        except VirtualCameraUnavailable as exc:
            self.skipTest(f"no virtual camera driver here: {exc}")
        try:
            backend.send(np.zeros((480, 640, 3), dtype=np.uint8))
            self.assertTrue(name)
        finally:
            backend.close()


if __name__ == "__main__":
    unittest.main()
