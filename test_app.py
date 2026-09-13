"""Tests for app.KioskWindow's camera-start handling.

A real camera's start() can fail (wrong serial, device already open
elsewhere, UVC device not present, ...) in ways SyntheticCamera's never
does, since SyntheticCamera's _open() cannot raise. These exercise that
failure path with fake BaseCamera subclasses rather than requiring real
IDS/UVC hardware -- no PySide6 event loop is entered (no .exec()/.show()),
so this stays headless.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

import app
import neco_reflex_theme as theme
from app import KioskWindow
from camera import ORIENTATION_NONE, ORIENTATION_ROTATE_180, BaseCamera
from config import ConfigError, InstrumentConfig
from device_presets import CUSTOM_PROFILE_ID
from kiosk import State
from synthetic_camera import SyntheticCamera

_qt_app = QApplication.instance() or QApplication([])


class FailingCamera(BaseCamera):
    """A camera whose _open() always raises, like IdsCamera/UvcCamera do
    when the device isn't found or is already open elsewhere.
    """

    def __init__(self, message: str):
        super().__init__()
        self._message = message

    @property
    def resolution(self) -> tuple[int, int]:
        return (0, 0)

    def _open(self) -> None:
        raise RuntimeError(self._message)

    def _close(self) -> None:
        pass

    def _grab(self):
        return None


class FlakyCamera(SyntheticCamera):
    """Fails _open() the first `fail_times` calls, then behaves like a
    normal SyntheticCamera -- for exercising retry-until-recovered.
    """

    def __init__(self, *args, fail_times: int = 1, **kwargs):
        super().__init__(*args, **kwargs)
        self._fail_times = fail_times
        self._attempts = 0

    def _open(self) -> None:
        self._attempts += 1
        if self._attempts <= self._fail_times:
            raise RuntimeError(f"attempt {self._attempts} failed")
        super()._open()


class TestCameraStartFailure(unittest.TestCase):
    def test_construction_does_not_raise_when_third_person_camera_fails_to_start(self):
        third_person = FailingCamera("could not open UVC device 0")
        instruments = {"slit_lamp": SyntheticCamera(160, 120, fps=30)}
        try:
            window = KioskWindow(third_person, instruments)
        except Exception as exc:
            self.fail(f"KioskWindow construction raised instead of degrading: {exc!r}")
        try:
            self.assertIn("third_person", window._camera_start_errors)
            self.assertIn("could not open UVC device", window._camera_start_errors["third_person"])

            # The third-person failure doesn't surface in the status line
            # until an instrument is picked -- before that, "pick one" is
            # the more useful message.
            status = window.controller.poll_preflight()
            self.assertEqual(window._idle_reason(status), "Select an instrument to begin.")

            window._on_instrument_clicked("slit_lamp")
            status = window.controller.poll_preflight()
            self.assertIn("could not open UVC device", window._idle_reason(status))
        finally:
            third_person.stop()
            instruments["slit_lamp"].stop()

    def test_instrument_start_failure_is_reported(self):
        third_person = SyntheticCamera(160, 120, fps=30)
        instruments = {"slit_lamp": FailingCamera("no IDS device with serial 'X' found")}
        try:
            window = KioskWindow(third_person, instruments)
            window._on_instrument_clicked("slit_lamp")

            self.assertIn("slit_lamp", window._camera_start_errors)
            self.assertIn("no IDS device", window._camera_start_errors["slit_lamp"])

            status = window.controller.poll_preflight()
            self.assertIn("no IDS device", window._idle_reason(status))
        finally:
            third_person.stop()

    def test_retry_recovers_once_the_instrument_starts_succeeding(self):
        third_person = SyntheticCamera(160, 120, fps=30)
        flaky = FlakyCamera(160, 120, fps=30, fail_times=1)
        instruments = {"slit_lamp": flaky}
        try:
            window = KioskWindow(third_person, instruments)
            window._on_instrument_clicked("slit_lamp")
            self.assertIn("slit_lamp", window._camera_start_errors)

            window._try_start_cameras()  # simulates the next retry-timer tick

            self.assertNotIn("slit_lamp", window._camera_start_errors)
        finally:
            third_person.stop()
            flaky.stop()


class TestCloseLockdown(unittest.TestCase):
    """A stray Alt+F4 or X-click shouldn't silently end a recording, but a
    deliberate force-quit must still be possible. See CLAUDE.md 'Who uses
    it' and DECISIONS.md's 2026-08-12 confirm-dialog entry.

    _confirm_stop_and_exit() is monkeypatched rather than driving the real
    QMessageBox, which would block waiting for input in a headless test.
    """

    def test_close_is_ignored_when_user_declines_the_confirm_dialog(self):
        third_person = SyntheticCamera(160, 120, fps=30)
        instruments = {"slit_lamp": SyntheticCamera(160, 120, fps=30)}
        window = KioskWindow(third_person, instruments)
        try:
            window._on_instrument_clicked("slit_lamp")
            time.sleep(0.2)  # let both cameras actually produce a frame
            window.controller.poll_preflight()
            window.controller.start_recording()
            window._confirm_stop_and_exit = lambda: False

            event = QCloseEvent()
            window.closeEvent(event)

            self.assertFalse(event.isAccepted())
            self.assertEqual(window.controller.state, State.RECORDING)
        finally:
            if window.controller.state == State.RECORDING:
                window.controller.stop_recording()
            third_person.stop()
            instruments["slit_lamp"].stop()

    def test_close_stops_recording_and_exits_when_user_confirms(self):
        third_person = SyntheticCamera(160, 120, fps=30)
        instruments = {"slit_lamp": SyntheticCamera(160, 120, fps=30)}
        window = KioskWindow(third_person, instruments)
        try:
            window._on_instrument_clicked("slit_lamp")
            time.sleep(0.2)  # let both cameras actually produce a frame
            window.controller.poll_preflight()
            window.controller.start_recording()
            window._confirm_stop_and_exit = lambda: True

            event = QCloseEvent()
            window.closeEvent(event)

            self.assertTrue(event.isAccepted())
            self.assertNotEqual(window.controller.state, State.RECORDING)
            self.assertIsNotNone(window.controller.last_session_info)
        finally:
            third_person.stop()
            instruments["slit_lamp"].stop()

    def test_close_is_accepted_when_not_recording(self):
        third_person = SyntheticCamera(160, 120, fps=30)
        instruments = {"slit_lamp": SyntheticCamera(160, 120, fps=30)}
        window = KioskWindow(third_person, instruments)
        try:
            event = QCloseEvent()
            window.closeEvent(event)

            self.assertTrue(event.isAccepted())
        finally:
            third_person.stop()
            instruments["slit_lamp"].stop()


class TestWatchButton(unittest.TestCase):
    """The Watch button opens the just-finished session in the viewer --
    see DECISIONS.md's Recorder/Viewer split entries. open_session is patched
    out: what matters here is the gating and that the live preview is
    paused around it, not the viewer itself (test_viewer.py covers that).
    """

    def _window(self, tmp_root: str) -> tuple[KioskWindow, SyntheticCamera, SyntheticCamera]:
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
        return window, third_person, instrument

    def test_disabled_until_a_session_has_been_recorded(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, third_person, instrument = self._window(tmp_root)
            try:
                window._sync_ui(window.controller.poll_preflight())
                self.assertFalse(window.watch_button.isEnabled())

                window.controller.last_session_dir = Path(tmp_root) / "2026-01-01_1200"
                window._sync_ui(window.controller.poll_preflight())
                self.assertTrue(window.watch_button.isEnabled())
            finally:
                third_person.stop()
                instrument.stop()

    def test_disabled_while_recording(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, third_person, instrument = self._window(tmp_root)
            try:
                window.controller.last_session_dir = Path(tmp_root) / "2026-01-01_1200"
                window.controller.state = State.RECORDING
                window._sync_ui()
                self.assertFalse(window.watch_button.isEnabled())
            finally:
                third_person.stop()
                instrument.stop()

    def test_clicking_opens_the_session_and_restarts_the_preview(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, third_person, instrument = self._window(tmp_root)
            try:
                session_dir = Path(tmp_root) / "2026-01-01_1200"
                window.controller.last_session_dir = session_dir

                with patch("app.open_session") as mock_open:
                    # The live preview must be paused while the modal
                    # viewer is up, and restarted afterwards.
                    mock_open.side_effect = lambda *a, **k: self.assertFalse(
                        window.preview_timer.isActive()
                    )
                    window._on_watch_clicked()

                mock_open.assert_called_once()
                self.assertEqual(mock_open.call_args.args[0], session_dir)
                self.assertTrue(window.preview_timer.isActive())
            finally:
                third_person.stop()
                instrument.stop()

    def test_preview_restarts_even_if_the_viewer_raises(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, third_person, instrument = self._window(tmp_root)
            try:
                window.controller.last_session_dir = Path(tmp_root) / "2026-01-01_1200"
                with patch("app.open_session", side_effect=RuntimeError("boom")):
                    with self.assertRaises(RuntimeError):
                        window._on_watch_clicked()
                self.assertTrue(window.preview_timer.isActive())
            finally:
                third_person.stop()
                instrument.stop()

    def test_does_nothing_when_no_session_has_been_recorded(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, third_person, instrument = self._window(tmp_root)
            try:
                with patch("app.open_session") as mock_open:
                    window._on_watch_clicked()
                mock_open.assert_not_called()
            finally:
                third_person.stop()
                instrument.stop()

    def test_the_kiosk_offers_no_way_to_browse_other_sessions(self):
        """Every session holds two students' faces and they share one
        folder, so a list of them is a list of other people's recordings.
        See DECISIONS.md 2026-09-13."""
        with tempfile.TemporaryDirectory() as tmp_root:
            window, third_person, instrument = self._window(tmp_root)
            try:
                self.assertFalse(hasattr(window, "past_button"))
                self.assertFalse(hasattr(window, "_on_past_recordings_clicked"))
                # The one a student just made is still reachable.
                self.assertTrue(hasattr(window, "watch_button"))
            finally:
                third_person.stop()
                instrument.stop()


class TestBranding(unittest.TestCase):
    """The mark follows KioskController.state, and the empty instrument
    pane shows the placeholder rather than black."""

    def _window(self, tmp_root: str) -> tuple[KioskWindow, SyntheticCamera, SyntheticCamera]:
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
        return window, third_person, instrument

    def test_mark_shows_recording_only_while_recording(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, third_person, instrument = self._window(tmp_root)
            try:
                window._sync_ui(window.controller.poll_preflight())
                self.assertFalse(window.mark.recording)

                window.controller.state = State.RECORDING
                window._sync_ui()
                self.assertTrue(window.mark.recording)

                # A recording that stopped on an error isn't recording.
                window.controller.state = State.ERROR
                window._sync_ui()
                self.assertFalse(window.mark.recording)
            finally:
                third_person.stop()
                instrument.stop()

    def test_instrument_pane_shows_the_placeholder_until_there_is_a_frame(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, third_person, instrument = self._window(tmp_root)
            try:
                deadline = time.monotonic() + 2.0
                while third_person.get_latest() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                window._update_preview()  # no instrument selected yet

                image = window.video_label.pixmap().toImage()
                left_pane_center = image.pixelColor(app.PREVIEW_CANVAS_SIZE[0] // 4, app.PREVIEW_CANVAS_SIZE[1] // 2)
                # Placeholder is black, blending with the video background
                self.assertEqual(left_pane_center.name().upper(), "#000000")
            finally:
                third_person.stop()
                instrument.stop()


class TestLabelsAndTimeLimit(unittest.TestCase):
    """Button labels say what they do, and the session time limit is
    visible before it's reached and reported as a normal stop when it is.
    See DECISIONS.md's "First round of student feedback" entry."""

    def test_buttons_say_what_they_do(self):
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        with tempfile.TemporaryDirectory() as tmp_root:
            window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
            try:
                self.assertEqual(window.start_button.text(), "Start Recording")
                self.assertEqual(window.stop_button.text(), "Stop Recording")
                self.assertEqual(window.watch_button.text(), "Watch Last Recording")
            finally:
                third_person.stop()
                instrument.stop()

    def test_status_counts_up_to_the_limit_which_stops_without_an_error(self):
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        with tempfile.TemporaryDirectory() as tmp_root:
            window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
            now = [0.0]
            window.controller._clock = lambda: now[0]
            try:
                window._on_instrument_clicked("slit_lamp")
                time.sleep(0.2)
                window._sync_ui(window.controller.poll_preflight())
                self.assertIn("Press Start Recording", window.status_label.text())
                self.assertIn("after 15 minutes", window.status_label.text())

                window._on_start_clicked()
                self.assertEqual(window.controller.state, State.RECORDING)
                self.assertEqual(window.status_label.text(), "Recording... 0:00 of 15:00")

                now[0] = 14 * 60 + 30.0
                window._sync_ui()
                self.assertEqual(
                    window.status_label.text(),
                    "Recording... 14:30 of 15:00 - stops automatically in 30s",
                )

                time.sleep(0.2)  # real frames, so nothing looks stalled
                now[0] = 15 * 60.0
                window._poll_tick()

                self.assertEqual(window.controller.state, State.IDLE)
                self.assertTrue(window.error_banner.isHidden())
                self.assertIn("15-minute limit", window.summary_label.text())
                self.assertTrue(window.watch_button.isEnabled())
                self.assertFalse(window.stop_button.isEnabled())
            finally:
                if window.controller.state == State.RECORDING:
                    window.controller.stop_recording()
                third_person.stop()
                instrument.stop()


class TestMissingConfigStartup(unittest.TestCase):
    """A missing/malformed config.json must surface visibly, not just to a
    log file. app.exe is built windowed (console=False, see
    packaging/app.spec) and launched from a Desktop shortcut with no
    console attached -- a log-and-exit with no QMessageBox is silent to
    whoever double-clicked the icon. Regression test for that gap, found
    by actually running the frozen installer on a machine with no
    config.json yet (see DECISIONS.md).
    """

    def test_config_error_shows_a_message_box_and_returns_nonzero(self):
        with (
            patch("sys.argv", ["app.py"]),
            patch("app.load_config", side_effect=ConfigError("config.json not found")),
            patch("app.QMessageBox.critical") as mock_critical,
        ):
            result = app.main()

        self.assertEqual(result, 1)
        mock_critical.assert_called_once()
        self.assertIn("config.json not found", mock_critical.call_args.args[-1])


class TestPresetPrecedence(unittest.TestCase):
    """app._resolve_presets: config.json beats the picked profile, which
    beats letting IdsCamera match the model string."""

    def _inst(self, **kwargs):
        base = dict(kind="ids", serial="111", label="Slit Lamp")
        base.update(kwargs)
        return InstrumentConfig(**base)

    def test_a_profile_supplies_its_presets(self):
        orientation, pixel_clock, black_level = app._resolve_presets(
            self._inst(profile="haag_streit_bi900_slit_lamp"), "slit_lamp"
        )
        self.assertEqual(orientation, ORIENTATION_ROTATE_180)
        self.assertEqual(pixel_clock, 80_000_000)
        # The factory value of 90 clips this sensor; see DECISIONS 2026-09-13.
        self.assertEqual(black_level, 110.0)

    def test_an_explicit_config_value_beats_the_profile(self):
        orientation, pixel_clock, black_level = app._resolve_presets(
            self._inst(
                profile="haag_streit_bi900_slit_lamp",
                orientation=ORIENTATION_NONE,
                pixel_clock_hz=60_000_000,
                black_level=95.0,
            ),
            "slit_lamp",
        )
        self.assertEqual(orientation, ORIENTATION_NONE)
        self.assertEqual(pixel_clock, 60_000_000)
        self.assertEqual(black_level, 95.0)

    def test_no_profile_leaves_both_to_the_camera(self):
        """Custom, or a config written before profiles existed. None means
        IdsCamera matches the model string itself."""
        self.assertEqual(app._resolve_presets(self._inst(), "slit_lamp"), (None, None, None))

    def test_custom_is_not_an_unknown_profile(self):
        with self.assertNoLogs("app", level="WARNING"):
            presets = app._resolve_presets(self._inst(profile=CUSTOM_PROFILE_ID), "slit_lamp")
        self.assertEqual(presets, (None, None, None))

    def test_a_self_managing_camera_is_left_alone(self):
        """The Keeler adjusts its black level continuously and does not clip;
        writing one would be taking over a job it does correctly."""
        _, _, black_level = app._resolve_presets(
            self._inst(profile="keeler_vantage_plus_digital"), "bio"
        )
        self.assertIsNone(black_level)

    def test_an_unknown_profile_warns_and_falls_back(self):
        with self.assertLogs("app", level="WARNING") as logs:
            result = app._resolve_presets(self._inst(profile="from_a_newer_build"), "slit_lamp")
        self.assertEqual(result, (None, None, None))
        self.assertIn("from_a_newer_build", "".join(logs.output))


if __name__ == "__main__":
    unittest.main()
