"""Tests for uvc_camera.UvcCamera's construction contract and the
vid_pid identification mode. The vid_pid path's actual device-opening
behavior is exercised end to end (against real hardware) by
test_uvc_enumeration.py; these tests are headless and hardware-free,
covering the two properties this module's docstring calls out as load-
bearing: construction rejects an ambiguous device/vid_pid combination, and
resolution failure surfaces from start() (so BaseCamera's retry loop can
catch it) rather than from __init__.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call, patch

import cv2
import numpy as np

from uvc_camera import _AUTOEXPOSURE_WARMUP_FRAMES, _RECONNECT_AFTER_FAILURES, UvcCamera
from uvc_enumeration import UvcDeviceResolutionError


def _cap(*, opened=True, read=(True, None)):
    cap = MagicMock()
    cap.isOpened.return_value = opened
    cap.get.return_value = 640  # width/height queries
    cap.read.return_value = read
    return cap


class UvcCameraConstructionTest(unittest.TestCase):
    def test_neither_device_nor_vid_pid_raises(self):
        with self.assertRaises(ValueError):
            UvcCamera()

    def test_both_device_and_vid_pid_raises(self):
        with self.assertRaises(ValueError):
            UvcCamera(device=0, vid_pid="1111:2222")

    def test_device_only_is_accepted(self):
        UvcCamera(device=0)

    def test_vid_pid_only_is_accepted(self):
        UvcCamera(vid_pid="1111:2222")


class UvcCameraVidPidResolutionTest(unittest.TestCase):
    def test_start_raises_resolution_error_without_opening_cv2_when_no_device_attached(self):
        camera = UvcCamera(vid_pid="1111:2222")

        with patch("uvc_camera.uvc_enumeration.resolve_device", side_effect=UvcDeviceResolutionError("none attached")):
            with patch("uvc_camera.cv2.VideoCapture") as mock_video_capture:
                with self.assertRaises(UvcDeviceResolutionError):
                    camera.start()
                mock_video_capture.assert_not_called()

        # BaseCamera.start() only creates the capture thread after _open()
        # succeeds -- confirm the failed resolution left it retryable.
        self.assertIsNone(camera._thread)


class UvcCameraAutofocusExposureLockTest(unittest.TestCase):
    def test_reads_warmup_frames_then_locks_both_controls(self):
        camera = UvcCamera(device=0)
        cap = MagicMock()

        camera._configure_capture(cap)

        self.assertEqual(cap.read.call_count, _AUTOEXPOSURE_WARMUP_FRAMES)
        cap.set.assert_has_calls(
            [call(cv2.CAP_PROP_AUTOFOCUS, 0), call(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)]
        )

    def test_backlight_compensation_is_set_before_the_warmup_reads(self):
        camera = UvcCamera(device=0)
        cap = MagicMock()
        calls = []
        cap.set.side_effect = lambda *args: calls.append(("set", *args))
        cap.read.side_effect = lambda: calls.append(("read",))

        camera._configure_capture(cap)

        self.assertEqual(calls[0], ("set", cv2.CAP_PROP_BACKLIGHT, 1))
        first_read_index = calls.index(("read",))
        self.assertGreater(first_read_index, 0)

    def test_does_not_raise_when_device_does_not_support_the_controls(self):
        camera = UvcCamera(device=0)
        cap = MagicMock()
        cap.set.return_value = False  # cv2's own signal for "unsupported control"

        camera._configure_capture(cap)  # must not raise

    def test_warmup_false_skips_the_read_loop_but_still_locks_controls(self):
        camera = UvcCamera(device=0)
        cap = MagicMock()

        camera._configure_capture(cap, warmup=False)

        cap.read.assert_not_called()
        cap.set.assert_has_calls(
            [call(cv2.CAP_PROP_AUTOFOCUS, 0), call(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)]
        )


class UvcCameraReconnectTest(unittest.TestCase):
    """A UVC device that stops delivering frames mid-stream must be reopened
    automatically -- see uvc_camera.py's _try_reconnect and DECISIONS.md's
    "UVC camera reconnects itself" entry.
    """

    def test_a_few_failed_reads_do_not_trigger_a_reopen(self):
        camera = UvcCamera(device=0)
        dead = _cap(read=(False, None))
        with patch("uvc_camera.time.sleep"), patch("uvc_camera.cv2.VideoCapture", return_value=dead) as vc:
            camera._open()
            vc.reset_mock()
            for _ in range(_RECONNECT_AFTER_FAILURES - 1):
                self.assertIsNone(camera._grab())
            vc.assert_not_called()

    def test_sustained_failure_triggers_exactly_one_reopen_within_the_cooldown(self):
        camera = UvcCamera(device=0)
        dead = _cap(read=(False, None))
        with patch("uvc_camera.time.sleep"), patch("uvc_camera.time.monotonic", return_value=1000.0), patch(
            "uvc_camera.cv2.VideoCapture", return_value=dead
        ) as vc:
            camera._open()
            vc.reset_mock()
            for _ in range(_RECONNECT_AFTER_FAILURES + 40):
                camera._grab()
            vc.assert_called_once()  # rate-limited by _RECONNECT_COOLDOWN_S

    def test_reopen_restores_the_stream_and_resets_the_failure_count(self):
        camera = UvcCamera(device=0)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        caps = [_cap(read=(False, None)), _cap(read=(True, frame))]
        with patch("uvc_camera.time.sleep"), patch("uvc_camera.time.monotonic", return_value=1000.0), patch(
            "uvc_camera.cv2.VideoCapture", side_effect=lambda *a, **k: caps.pop(0)
        ):
            camera._open()  # takes the dead cap
            for _ in range(_RECONNECT_AFTER_FAILURES):
                camera._grab()  # crosses the threshold, reopens onto the live cap
            result = camera._grab()

        self.assertIsNotNone(result)
        self.assertEqual(camera._consecutive_failures, 0)

    def test_reopen_failure_is_swallowed_and_retried_not_raised(self):
        camera = UvcCamera(device=0)
        caps = [_cap(read=(False, None)), _cap(opened=False), _cap(opened=False)]
        with patch("uvc_camera.time.sleep"), patch("uvc_camera.cv2.VideoCapture", side_effect=lambda *a, **k: caps.pop(0) if caps else _cap(opened=False)):
            camera._open()
            for _ in range(_RECONNECT_AFTER_FAILURES + 5):
                self.assertIsNone(camera._grab())  # device still gone, never raises
        self.assertIsNone(camera._cap)

    def test_reconnect_bails_out_when_stopping(self):
        camera = UvcCamera(device=0)
        dead = _cap(read=(False, None))
        with patch("uvc_camera.time.sleep"), patch("uvc_camera.cv2.VideoCapture", return_value=dead) as vc:
            camera._open()
            camera._stop_event.set()
            vc.reset_mock()
            for _ in range(_RECONNECT_AFTER_FAILURES + 5):
                camera._grab()
            vc.assert_not_called()


class UvcCameraFrameRateCapTest(unittest.TestCase):
    def test_sets_cap_prop_fps_to_the_target(self):
        camera = UvcCamera(device=0, target_fps=30.0)
        cap = MagicMock()
        cap.get.return_value = 30.0

        camera._apply_frame_rate_cap(cap)

        cap.set.assert_called_once_with(cv2.CAP_PROP_FPS, 30.0)

    def test_mismatch_beyond_tolerance_logs_a_warning(self):
        camera = UvcCamera(device=0, target_fps=30.0)
        cap = MagicMock()
        cap.get.return_value = 15.0  # device only supports half the request

        with self.assertLogs("uvc_camera", level="WARNING"):
            camera._apply_frame_rate_cap(cap)

    def test_no_target_fps_means_open_never_calls_the_cap(self):
        camera = UvcCamera(device=0)  # target_fps defaults to None

        with patch("uvc_camera.cv2.VideoCapture") as mock_video_capture:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = True
            mock_cap.get.return_value = 640  # so int(cap.get(...)) for width/height doesn't choke on a MagicMock
            mock_video_capture.return_value = mock_cap

            camera._open()

            mock_cap.set.assert_any_call(cv2.CAP_PROP_BACKLIGHT, 1)  # _configure_capture still ran
            fps_calls = [c for c in mock_cap.set.call_args_list if c.args[0] == cv2.CAP_PROP_FPS]
            self.assertEqual(fps_calls, [])



class _ClampingCap:
    """A driver that takes any write and clamps it, like the laptop
    webcam's exposure; or refuses anything off its range and grid, like
    its white balance. Measured 2026-10-14."""

    def __init__(self, low, high, step, value, refuse):
        self.low, self.high, self.step, self.value, self.refuse = low, high, step, value, refuse

    def set(self, prop, value):
        on_grid = abs((value - self.low) / self.step - round((value - self.low) / self.step)) < 1e-9
        if self.refuse and (not self.low <= value <= self.high or not on_grid):
            return False
        self.value = min(self.high, max(self.low, value))
        return True

    def get(self, prop):
        return self.value


class UvcControlsTest(unittest.TestCase):
    def test_probe_finds_a_clamping_drivers_limits(self):
        from uvc_camera import _probe_range

        cap = _ClampingCap(-10, -4, 1, -6, refuse=False)
        self.assertEqual(_probe_range(cap, 0, -14, 0, -6), (-10.0, -4.0, 1.0))

    def test_probe_finds_a_refusing_drivers_limits_on_its_grid(self):
        from uvc_camera import _probe_range

        cap = _ClampingCap(2800, 6500, 10, 4600, refuse=True)
        self.assertEqual(_probe_range(cap, 0, 1000, 12000, 4600), (2800.0, 6500.0, 10.0))

    def test_a_lever_with_one_value_is_not_offered(self):
        from uvc_camera import _probe_range

        cap = _ClampingCap(1, 1, 1, 1, refuse=True)
        self.assertIsNone(_probe_range(cap, 0, 0, 1024, 1))

    def test_exposure_is_held_to_a_frame(self):
        from uvc_camera import _exposure_budget_log2

        self.assertEqual(_exposure_budget_log2(30), -5)   # 31 ms
        self.assertEqual(_exposure_budget_log2(15), -4)   # 62 ms
        self.assertEqual(_exposure_budget_log2(None), -5)

    def test_stored_levers_are_applied_after_the_warmup(self):
        camera = UvcCamera(device=0, controls={"exposure": -6, "white_balance": 4500})
        cap = MagicMock()
        cap.set.return_value = True
        camera._configure_capture(cap)
        cap.set.assert_has_calls([
            call(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25),
            call(cv2.CAP_PROP_EXPOSURE, -6),
            call(cv2.CAP_PROP_AUTO_WB, 0),
            call(cv2.CAP_PROP_WB_TEMPERATURE, 4500),
        ])

    def test_no_stored_levers_leaves_the_webcam_its_auto(self):
        camera = UvcCamera(device=0)
        cap = MagicMock()
        camera._configure_capture(cap)
        props = [c.args[0] for c in cap.set.call_args_list]
        self.assertNotIn(cv2.CAP_PROP_AUTO_WB, props)
        self.assertNotIn(cv2.CAP_PROP_EXPOSURE, props)

    def test_a_live_write_waits_for_the_capture_thread(self):
        camera = UvcCamera(device=0)
        camera._ranges = {"white_balance": (2800.0, 6500.0, 10.0)}
        camera._cap = MagicMock()
        camera._cap.read.return_value = (True, np.zeros((4, 4, 3), dtype=np.uint8))
        self.assertEqual(camera.set_control("white_balance", 3017), 3020.0)
        camera._cap.set.assert_not_called()  # not from the calling thread
        camera._grab()
        camera._cap.set.assert_has_calls([call(cv2.CAP_PROP_AUTO_WB, 0), call(cv2.CAP_PROP_WB_TEMPERATURE, 3020.0)])
        self.assertEqual(camera._controls, {"white_balance": 3020.0})

    def test_default_hands_white_balance_back_to_the_webcam(self):
        camera = UvcCamera(device=0, controls={"white_balance": 4500})
        camera._cap = MagicMock()
        camera._cap.read.return_value = (True, np.zeros((4, 4, 3), dtype=np.uint8))
        camera.reset_controls("White balance")
        camera._grab()
        camera._cap.set.assert_called_with(cv2.CAP_PROP_AUTO_WB, 1)
        self.assertEqual(camera._controls, {})

    def test_config_knows_the_same_names(self):
        from config import CONTROL_KEYS
        from uvc_camera import _PROBE_LIMITS

        self.assertEqual(set(CONTROL_KEYS["uvc"]), set(_PROBE_LIMITS))

if __name__ == "__main__":
    unittest.main()
