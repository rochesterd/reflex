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
            _quiesce(window)
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
            _quiesce(window)
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
            _quiesce(window)
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
        _quiesce(window)
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
        _quiesce(window)
        try:
            window._on_instrument_clicked("slit_lamp")
            time.sleep(0.2)  # let both cameras actually produce a frame
            window.controller.poll_preflight()
            window.controller.start_recording()
            window._confirm_stop_and_exit = lambda: True
            # The session that stop produces is unexported by definition;
            # TestUnexportedSessionOnClose covers that prompt on its own.
            window._confirm_discard_unexported = lambda: True

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
        _quiesce(window)
        try:
            event = QCloseEvent()
            window.closeEvent(event)

            self.assertTrue(event.isAccepted())
        finally:
            third_person.stop()
            instruments["slit_lamp"].stop()


def _quiesce(window) -> None:
    """Stop a KioskWindow's timers so it stops driving itself.

    A window that outlives its test keeps polling, and a test that forces
    controller.state (something only start_recording() does for real) makes
    every later tick raise. Tests that want a tick call _poll_tick()
    directly. The preview timer is restarted by _with_preview_paused(), so
    stopping it here doesn't hide anything those tests check.
    """
    window.preview_timer.stop()
    window.poll_timer.stop()
    window.camera_retry_timer.stop()
    window.brightness_timer.stop()


class TestUnexportedSessionOnClose(unittest.TestCase):
    """Closing deletes the buffer, so a session nobody exported is about
    to be lost. That must never be silent: the student is told, and can go
    back and export. See session_buffer.py and ROADMAP's ephemeral-buffer
    entry.

    _confirm_discard_unexported() is monkeypatched for the same reason
    _confirm_stop_and_exit() is -- a real QMessageBox blocks headlessly.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # A real folder with a real manifest: an unopenable session is
        # deliberately not warned about, so a fake path would test nothing.
        self.session_dir = Path(self._tmp.name) / "2026-01-01_1200"
        self.session_dir.mkdir()
        (self.session_dir / "session.json").write_text("{}", encoding="utf-8")

    def _window(self):
        third_person = SyntheticCamera(160, 120, fps=30)
        instruments = {"slit_lamp": SyntheticCamera(160, 120, fps=30)}
        window = KioskWindow(third_person, instruments)
        _quiesce(window)
        self.addCleanup(instruments["slit_lamp"].stop)
        self.addCleanup(third_person.stop)
        return window

    def test_close_is_ignored_when_the_student_wants_to_export_first(self):
        window = self._window()
        window.controller.last_session_dir = self.session_dir
        window._confirm_discard_unexported = lambda: False

        event = QCloseEvent()
        window.closeEvent(event)

        self.assertFalse(event.isAccepted())

    def test_close_proceeds_once_the_student_accepts_losing_it(self):
        window = self._window()
        window.controller.last_session_dir = self.session_dir
        window._confirm_discard_unexported = lambda: True

        event = QCloseEvent()
        window.closeEvent(event)

        self.assertTrue(event.isAccepted())

    def test_an_exported_session_closes_without_a_prompt(self):
        window = self._window()
        window.controller.last_session_dir = self.session_dir
        window._on_exported(self.session_dir / "side_by_side.mp4")

        def refuse():
            raise AssertionError("an exported session must not prompt")

        window._confirm_discard_unexported = refuse

        event = QCloseEvent()
        window.closeEvent(event)

        self.assertTrue(event.isAccepted())

    def test_the_summary_says_the_recording_is_not_saved_yet(self):
        window = self._window()
        window.controller.last_session_dir = self.session_dir
        info = {"streams": {"instrument": {"frame_count": 10, "dropped_frames": 0, "verified": True}}}

        summary = window._format_summary("Session complete", info)
        self.assertIn("NOT saved", summary)
        # The buffer path is never shown: it is about to be deleted.
        self.assertNotIn("2026-01-01_1200", summary)

        window._on_exported(self.session_dir / "out.mp4")
        self.assertIn("saved to your drive", window._format_summary("Session complete", info))


class TestAutoReview(unittest.TestCase):
    """Stopping opens the viewer by itself -- there is no Watch button.
    With an ephemeral buffer, a student who never reaches Export loses the
    take, so landing them in the viewer is part of recording, not an extra.

    open_session is patched out: what matters here is the gating and that
    the live preview is paused around it, not the viewer (test_viewer.py).
    """

    def _window(self, tmp_root: str) -> tuple[KioskWindow, SyntheticCamera, SyntheticCamera]:
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
        _quiesce(window)
        self.addCleanup(instrument.stop)
        self.addCleanup(third_person.stop)
        return window, third_person, instrument

    @staticmethod
    def _confirm_close_passed_to(mock_open):
        """The gate the kiosk hands the viewer. With open_session patched
        the viewer never runs, so a test that wants the close question has
        to call this itself."""
        return mock_open.call_args.kwargs["confirm_close"]

    @staticmethod
    def _recorded(tmp_root: str, name: str = "2026-01-01_1200") -> Path:
        """A session folder complete enough for the viewer to open."""
        session_dir = Path(tmp_root) / name
        session_dir.mkdir(parents=True)
        (session_dir / "session.json").write_text("{}", encoding="utf-8")
        return session_dir

    def test_there_is_no_watch_button(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, _third, _inst = self._window(tmp_root)
            self.assertFalse(hasattr(window, "watch_button"))

    def test_stopping_opens_the_session_and_restarts_the_preview(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, _third, _inst = self._window(tmp_root)
            session_dir = self._recorded(tmp_root)
            window.controller.last_session_dir = session_dir

            with patch("app.open_session") as mock_open:
                # The live preview must be paused while the modal viewer is
                # up, and restarted afterwards.
                mock_open.side_effect = lambda *a, **k: self.assertFalse(
                    window.preview_timer.isActive()
                )
                window._review_last_session()

            mock_open.assert_called_once()
            self.assertEqual(mock_open.call_args.args[0], session_dir)
            self.assertTrue(window.preview_timer.isActive())

    def test_the_same_session_is_not_reopened_on_every_poll_tick(self):
        """stopped_at_time_limit stays set until the next recording starts,
        so without a guard the poll tick would reopen the viewer forever."""
        with tempfile.TemporaryDirectory() as tmp_root:
            window, _third, _inst = self._window(tmp_root)
            window.controller.last_session_dir = self._recorded(tmp_root)

            with patch("app.open_session") as mock_open:
                window._review_last_session()
                window._review_last_session()
                window._poll_tick()

            mock_open.assert_called_once()

    def test_a_later_session_opens_on_its_own(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, _third, _inst = self._window(tmp_root)

            with patch("app.open_session") as mock_open:
                window.controller.last_session_dir = self._recorded(tmp_root, "2026-01-01_1200")
                window._review_last_session()
                window.controller.last_session_dir = self._recorded(tmp_root, "2026-01-01_1300")
                window._review_last_session()

            self.assertEqual(mock_open.call_count, 2)

    def test_a_session_with_no_manifest_is_not_opened(self):
        """Finalizing failed badly enough that there is nothing to read --
        the error banner is the whole story, not a second failed dialog."""
        with tempfile.TemporaryDirectory() as tmp_root:
            window, _third, _inst = self._window(tmp_root)
            broken = Path(tmp_root) / "2026-01-01_1200"
            broken.mkdir()

            window.controller.last_session_dir = broken
            with patch("app.open_session") as mock_open:
                window._review_last_session()

            mock_open.assert_not_called()
            # And nothing warns the student about losing something they
            # could never have exported.
            self.assertIsNone(window._unexported_session())

    def test_nothing_opens_while_recording(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, _third, _inst = self._window(tmp_root)
            window.controller.last_session_dir = self._recorded(tmp_root)
            window.controller.state = State.RECORDING
            self.addCleanup(setattr, window.controller, "state", State.IDLE)

            with patch("app.open_session") as mock_open:
                window._review_last_session()

            mock_open.assert_not_called()

    def test_save_it_now_keeps_the_viewer_open(self):
        """The window must not close and reopen -- changing your mind
        should cost nothing. False means "do not close"."""
        with tempfile.TemporaryDirectory() as tmp_root:
            window, _third, _inst = self._window(tmp_root)
            window.controller.last_session_dir = self._recorded(tmp_root)
            asked = []
            window._confirm_discard_after_review = lambda parent=None: (
                asked.append(1), False
            )[1]

            with patch("app.open_session") as mock_open:
                window._review_last_session()
                gate = self._confirm_close_passed_to(mock_open)
                self.assertFalse(gate(None))

            self.assertEqual(len(asked), 1)
            # And the viewer was opened exactly once: no close-and-reopen.
            self.assertEqual(mock_open.call_count, 1)

    def test_discarding_lets_it_close_and_is_not_asked_again(self):
        """Being asked twice about the same recording teaches students to
        click through the question, which is how the real one gets missed."""
        with tempfile.TemporaryDirectory() as tmp_root:
            window, _third, _inst = self._window(tmp_root)
            session_dir = self._recorded(tmp_root)
            window.controller.last_session_dir = session_dir
            window._confirm_discard_after_review = lambda parent=None: True

            with patch("app.open_session") as mock_open:
                window._review_last_session()
                gate = self._confirm_close_passed_to(mock_open)
                self.assertTrue(gate(None))

            self.assertIn(session_dir, window._discarded)
            # Nothing left for app close to raise.
            self.assertIsNone(window._unexported_session())

            def refuse():
                raise AssertionError("already answered once")

            window._confirm_discard_unexported = refuse
            event = QCloseEvent()
            window.closeEvent(event)
            self.assertTrue(event.isAccepted())

    def test_an_exported_session_closes_without_being_asked(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, _third, _inst = self._window(tmp_root)
            session_dir = self._recorded(tmp_root)
            window.controller.last_session_dir = session_dir

            def refuse(parent=None):
                raise AssertionError("an exported session must not be asked about")

            window._confirm_discard_after_review = refuse

            with patch("app.open_session") as mock_open:
                mock_open.side_effect = lambda *a, **k: window._on_exported(session_dir)
                window._review_last_session()
                gate = self._confirm_close_passed_to(mock_open)
                self.assertTrue(gate(None))

            self.assertIn(session_dir, window._exported)

            def refuse():
                raise AssertionError("already answered once")

            window._confirm_discard_unexported = refuse
            event = QCloseEvent()
            window.closeEvent(event)
            self.assertTrue(event.isAccepted())

    def test_the_summary_does_not_name_a_button_that_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, _third, _inst = self._window(tmp_root)
            window.controller.last_session_dir = self._recorded(tmp_root)
            info = {"streams": {"instrument": {"frame_count": 5, "dropped_frames": 0, "verified": True}}}

            summary = window._format_summary("Session complete", info)
            self.assertNotIn("Watch Last Recording", summary)
            self.assertIn("NOT saved", summary)

    def test_preview_restarts_even_if_the_viewer_raises(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            window, _third, _inst = self._window(tmp_root)
            window.controller.last_session_dir = self._recorded(tmp_root)
            with patch("app.open_session", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    window._review_last_session()
            self.assertTrue(window.preview_timer.isActive())


class TestBranding(unittest.TestCase):
    """The mark follows KioskController.state, and the empty instrument
    pane shows the placeholder rather than black."""

    def _window(self, tmp_root: str) -> tuple[KioskWindow, SyntheticCamera, SyntheticCamera]:
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
        _quiesce(window)
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

    def test_brightness_stays_usable_while_recording(self):
        """The one control a student may touch mid-session: the views that
        need it differ in brightness within a single recording, and a
        control they must stop to use costs them the take."""
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        with tempfile.TemporaryDirectory() as tmp_root:
            window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
            _quiesce(window)
            try:
                window.controller.state = State.RECORDING
                window._sync_ui()

                self.assertFalse(window.start_button.isEnabled())
                self.assertTrue(window.brightness_slider.isEnabled())
            finally:
                third_person.stop()
                instrument.stop()

    def test_the_ends_are_named_and_the_middle_is_a_percentage(self):
        """The two ends mean something a student can act on -- the
        technician's calibration, and the most this model was measured to
        give. In between, a percentage is the only honest label."""
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        with tempfile.TemporaryDirectory() as tmp_root:
            window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
            _quiesce(window)
            try:
                self.assertEqual(window.brightness_value_label.text(), "Normal")

                window.brightness_slider.setValue(100)
                self.assertEqual(window.brightness_value_label.text(), "Brightest")
                self.assertEqual(window.controller.brightness, 1.0)

                window.brightness_slider.setValue(40)
                self.assertEqual(window.brightness_value_label.text(), "40%")
            finally:
                third_person.stop()
                instrument.stop()

    def test_the_slider_is_continuous_over_the_measured_range(self):
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        with tempfile.TemporaryDirectory() as tmp_root:
            window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
            _quiesce(window)
            try:
                window._on_instrument_clicked("slit_lamp")
                window._sync_ui()

                self.assertTrue(window.controller.brightness_adjustable())
                self.assertEqual(window.brightness_slider.minimum(), 0)
                self.assertEqual(
                    window.brightness_slider.maximum(), app.BRIGHTNESS_SLIDER_RANGE
                )
            finally:
                third_person.stop()
                instrument.stop()

    def test_a_drag_reaches_the_camera_at_most_once_per_interval(self):
        """Every write is a GenICam node write or a USB control transfer on
        a stream that may be recording, and a drag fires far faster than
        that. See app.py's BRIGHTNESS_WRITE_MS."""
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        with tempfile.TemporaryDirectory() as tmp_root:
            window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
            _quiesce(window)
            try:
                with patch.object(window.controller, "set_brightness") as setter:
                    # The first move of a drag is never delayed: the picture
                    # has to respond the instant they touch it.
                    window.brightness_slider.setValue(10)
                    self.assertEqual(setter.call_count, 1)
                    self.assertEqual(setter.call_args.args[0], 0.1)

                    # The rest of the drag is coalesced, but the label
                    # follows every move -- only the camera write is thinned.
                    window.brightness_slider.setValue(20)
                    window.brightness_slider.setValue(30)
                    self.assertEqual(setter.call_count, 1)
                    self.assertEqual(window.brightness_value_label.text(), "30%")

                    # When the interval elapses, the newest value goes --
                    # not the ones it skipped past.
                    window._flush_brightness()
                    self.assertEqual(setter.call_count, 2)
                    self.assertEqual(setter.call_args.args[0], 0.3)
            finally:
                third_person.stop()
                instrument.stop()

    def test_the_resting_position_is_always_what_the_camera_gets(self):
        """The one property the throttle must never break: a student lets
        go, and the picture matches where they let go."""
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        with tempfile.TemporaryDirectory() as tmp_root:
            window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
            _quiesce(window)
            try:
                with patch.object(window.controller, "set_brightness") as setter:
                    for position in range(0, 71, 5):  # a drag, many moves
                        window.brightness_slider.setValue(position)
                    window._flush_brightness()  # the interval after they stop

                    self.assertEqual(setter.call_args.args[0], 0.7)
                    # Far fewer writes than moves.
                    self.assertLess(setter.call_count, 5)

                    # And once nothing is pending the timer stops rather
                    # than ticking forever behind an idle slider.
                    window._flush_brightness()
                    self.assertFalse(window.brightness_timer.isActive())
            finally:
                third_person.stop()
                instrument.stop()

    def test_syncing_reflects_the_level_without_rewriting_it(self):
        """_sync_ui runs four times a second; it must show what the
        controller holds without sending it back to the camera."""
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        with tempfile.TemporaryDirectory() as tmp_root:
            window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
            _quiesce(window)
            try:
                window._on_instrument_clicked("slit_lamp")
                window.controller.brightness = 0.6  # e.g. reapplied on restart

                with patch.object(window.controller, "set_brightness") as setter:
                    window._sync_ui()

                setter.assert_not_called()
                self.assertEqual(window.brightness_slider.value(), 60)
                self.assertEqual(window.brightness_value_label.text(), "60%")
            finally:
                third_person.stop()
                instrument.stop()

    def test_buttons_say_what_they_do(self):
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        with tempfile.TemporaryDirectory() as tmp_root:
            window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
            _quiesce(window)
            try:
                self.assertEqual(window.start_button.text(), "Start Recording")
                self.assertEqual(window.stop_button.text(), "Stop Recording")
            finally:
                third_person.stop()
                instrument.stop()

    def test_status_counts_up_to_the_limit_which_stops_without_an_error(self):
        third_person = SyntheticCamera(160, 120, fps=30)
        instrument = SyntheticCamera(160, 120, fps=30)
        with tempfile.TemporaryDirectory() as tmp_root:
            window = KioskWindow(third_person, {"slit_lamp": instrument}, output_root=tmp_root)
            _quiesce(window)
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
                with patch("app.open_session") as mock_open:
                    window._poll_tick()

                self.assertEqual(window.controller.state, State.IDLE)
                self.assertTrue(window.error_banner.isHidden())
                self.assertIn("15-minute limit", window.summary_label.text())
                self.assertFalse(window.stop_button.isEnabled())
                # Stopping at the limit is still a stop: the student is put
                # in front of the recording, not left to find it.
                mock_open.assert_called_once()
                self.assertEqual(
                    mock_open.call_args.args[0], window.controller.last_session_dir
                )
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
