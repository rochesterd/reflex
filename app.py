"""Kiosk recording app: an instrument picker (Slit Lamp / BIO), one Start
button, one Stop button. See CLAUDE.md 'Who uses it'. The picker and Start
are the only controls live before a session; the instant Start is pressed,
the picker disables along with everything else, same as before.

All actual decisions (which instrument may be picked, when Start may be
pressed, when a stall counts as a failure, what the post-session summary
says) live in kiosk.KioskController. This module is a thin PySide6 shell
that polls it on a timer and reflects what it reports.
"""

from __future__ import annotations

import argparse
import functools
import logging
import math
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app_icon import ICON_APP, icon_path
from camera import BaseCamera
from compositor import side_by_side
from config import (
    DEFAULT_RECORDING_FPS,
    ConfigError,
    InstrumentConfig,
    is_frozen,
    load_config,
    resolve_default_sessions_dir,
)
from device_presets import CUSTOM_PROFILE_ID, profile_for_id
from kiosk import KioskController, PreflightStatus, State
from neco_reflex_theme import BURGUNDY_BGR
from qt_image import bgr_to_pixmap
from reflex_mark import ReflexMark
import reflex_style
from synthetic_camera import SyntheticCamera
from uvc_camera import UvcCamera
from viewer import open_session

logger = logging.getLogger(__name__)

PREVIEW_FPS = 30
POLL_MS = 250
CAMERA_RETRY_MS = 2000
# Stand-in shapes for --instrument-synthetic, keyed by role -- not
# identity, so unlike serials/device index these stay out of config.json.
# Falls back to DEFAULT_SYNTHETIC_RESOLUTION for any role config.json
# defines that isn't one of today's two (see config.py).
INSTRUMENT_SYNTHETIC_RESOLUTIONS = {"slit_lamp": (1600, 1200), "bio": (2056, 1542)}
DEFAULT_SYNTHETIC_RESOLUTION = (1600, 1200)
# The real ELP-USB100W03M-L21's resolution is queried at runtime (see
# uvc_camera.py) rather than hardcoded here -- this is only the stand-in
# used when --third-person-synthetic substitutes a SyntheticCamera for it.
THIRD_PERSON_SYNTHETIC_RESOLUTION = (640, 480)
PREVIEW_CANVAS_SIZE = (1280, 540)  # downscaled preview of the 2560x1080 recording canvas
HEADER_MARK_SIZE = 36
# How close to the session time limit the status line starts counting down
# the seconds left. See KioskController.max_session_minutes.
LIMIT_COUNTDOWN_S = 60

# Stands in for the instrument pane until there's an instrument frame to
# show beside the third-person preview. Black, like the letterbox bars
# around real frames, so the placeholder blends in.
_INSTRUMENT_PLACEHOLDER = np.zeros(
    (PREVIEW_CANVAS_SIZE[1], PREVIEW_CANVAS_SIZE[0] // 2, 3), dtype=np.uint8
)

# See config.py's resolve_default_config_path()/resolve_default_sessions_dir()
# for why this needs the same frozen/dev split -- a frozen install has no
# repo checkout for "logs" to be relative to.
LOG_DIR = Path(os.environ["ProgramData"]) / "Reflex" / "logs" if is_frozen() else Path("logs")
LOG_FILE = LOG_DIR / "app.log"

THIRD_PERSON_LABEL = "third-person camera"


def _format_clock(seconds: float) -> str:
    """m:ss, for the recording time shown against the limit."""
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def _configure_logging() -> None:
    """Durable file logging plus an excepthook, so a failure on a student
    machine with no one around leaves a trace beyond the in-memory error
    banner and session.json -- see CLAUDE.md 'Who uses it'.

    RotatingFileHandler caps this at 4 * 2MB = 8MB total rather than
    growing without bound across a semester of unattended kiosk runs.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    def _log_uncaught(exc_type, exc_value, exc_tb) -> None:
        # Without this, an exception outside a caught path (e.g. during Qt
        # event handling) can leave the app frozen or gone with nothing in
        # the log explaining why.
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _log_uncaught


class KioskWindow(QMainWindow):
    def __init__(
        self,
        third_person_camera: BaseCamera,
        instruments: dict[str, BaseCamera],
        instrument_labels: dict[str, str] | None = None,
        fps: int = DEFAULT_RECORDING_FPS,
        output_root: str | Path = "sessions",
    ):
        super().__init__()
        self.setWindowTitle("Reflex")

        self.third_person_camera = third_person_camera
        self.instruments = instruments
        self.instrument_labels = instrument_labels or {key: key for key in instruments}
        self._display_names = {**self.instrument_labels, "third_person": THIRD_PERSON_LABEL}
        self.controller = KioskController(
            third_person_camera,
            instruments,
            instrument_labels=self.instrument_labels,
            third_person_label=THIRD_PERSON_LABEL,
            fps=fps,
            output_root=output_root,
        )
        self._camera_start_errors: dict[str, str] = {}
        # The instrument the student last picked -- distinct from
        # controller.selected_instrument, which only updates once that
        # camera actually confirms live. Kept so a failed start (device
        # not plugged in yet) can keep retrying the same choice on the
        # camera-retry timer instead of silently reverting to "nothing
        # selected."
        self._desired_instrument: str | None = None

        # The mark doubles as the recording indicator: its pupil opens
        # while recording (see _sync_ui). It takes no clicks or focus.
        self.mark = ReflexMark(size=HEADER_MARK_SIZE)
        brand_name = QLabel("Reflex")
        brand_name.setFont(reflex_style.heading_font())
        brand_rule = QFrame()
        brand_rule.setObjectName(reflex_style.BRAND_RULE)
        brand_rule.setFixedHeight(2)

        self.error_banner = QLabel()
        self.error_banner.setWordWrap(True)
        self.error_banner.setStyleSheet(
            "background-color: #b00020; color: white; font-weight: bold; padding: 12px; font-size: 14pt;"
        )
        self.error_banner.hide()

        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setMinimumSize(*PREVIEW_CANVAS_SIZE)
        self.video_label.setStyleSheet("background-color: black;")

        self._instrument_buttons: dict[str, QPushButton] = {}
        picker = QHBoxLayout()
        for key in self.instruments:
            button = QPushButton(self.instrument_labels.get(key, key))
            button.setObjectName(reflex_style.CHOICE)
            button.setCheckable(True)
            button.setMinimumHeight(48)
            button.clicked.connect(functools.partial(self._on_instrument_clicked, key))
            picker.addWidget(button)
            self._instrument_buttons[key] = button

        # Both "primary": only one is ever enabled, so whichever the student
        # can press is the one that stands out. Labels say what they do, not
        # just "Start"/"Stop" -- see DECISIONS.md's "First round of student
        # feedback" entry.
        self.start_button = QPushButton("Start Recording")
        self.start_button.setObjectName(reflex_style.PRIMARY)
        self.start_button.setEnabled(False)
        self.start_button.setMinimumHeight(64)
        self.start_button.clicked.connect(self._on_start_clicked)

        self.stop_button = QPushButton("Stop Recording")
        self.stop_button.setObjectName(reflex_style.PRIMARY)
        self.stop_button.setEnabled(False)
        self.stop_button.setMinimumHeight(64)
        self.stop_button.clicked.connect(self._on_stop_clicked)

        self.status_label = QLabel()
        self.status_label.setObjectName(reflex_style.STATUS)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)

        # Separate from status_label on purpose: status_label reflects live
        # state and gets overwritten every ~250ms by _sync_ui(), which would
        # blow away a "here's what just got recorded" message within one
        # poll tick. summary_label is only ever touched right after a
        # session ends and stays put until the next one.
        self.summary_label = QLabel()
        self.summary_label.setObjectName(reflex_style.SECONDARY)
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.summary_label.setWordWrap(True)

        # Watching is the point of recording -- the student reviews what
        # they just did while the muscle memory is fresh. Nothing is
        # rendered to make this possible; the viewer lays the session's two
        # streams out live. See DECISIONS.md's Recorder/Viewer split entries.
        self.watch_button = QPushButton("Watch Last Recording")
        self.watch_button.setMinimumHeight(40)
        self.watch_button.setEnabled(False)
        self.watch_button.clicked.connect(self._on_watch_clicked)

        # There is deliberately no "Watch Past Recordings" here. Every
        # session holds identifiable images of two students and they all
        # land in one folder, so browsing the list showed any student every
        # other student's recordings. Watch Last Recording covers the
        # session a student just made, which is the one they can consent to
        # seeing. See DECISIONS.md's 2026-09-13 entry and ROADMAP's
        # recordings-are-PII entry: this comes back when a session belongs
        # to somebody.

        buttons = QHBoxLayout()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)

        summary_row = QHBoxLayout()
        summary_row.addWidget(self.summary_label, stretch=1)
        summary_row.addWidget(self.watch_button)

        header = QHBoxLayout()
        header.addWidget(self.mark)
        header.addWidget(brand_name)
        header.addStretch(1)

        layout = QVBoxLayout()
        layout.addLayout(header)
        layout.addWidget(brand_rule)
        layout.addWidget(self.error_banner)
        layout.addWidget(self.video_label)
        layout.addLayout(picker)
        layout.addLayout(buttons)
        layout.addWidget(self.status_label)
        layout.addLayout(summary_row)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self._try_start_cameras()

        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self._update_preview)
        self.preview_timer.start(int(1000 / PREVIEW_FPS))

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_tick)
        self.poll_timer.start(POLL_MS)

        # A real camera's start() can fail for reasons that resolve on
        # their own (not plugged in yet) as well as ones that don't (wrong
        # serial, already open elsewhere) -- both look the same from here,
        # so this just keeps retrying rather than requiring an app restart.
        # Also drives retrying the student's last-picked instrument until
        # it actually comes up. SyntheticCamera's start() never fails, so
        # this is a no-op churn of already-running checks on the default
        # synthetic path.
        self.camera_retry_timer = QTimer(self)
        self.camera_retry_timer.timeout.connect(self._try_start_cameras)
        self.camera_retry_timer.start(CAMERA_RETRY_MS)

        self._sync_ui(self.controller.poll_preflight())

    def _try_start_cameras(self) -> None:
        try:
            self.third_person_camera.start()
        except Exception as exc:
            if "third_person" not in self._camera_start_errors:
                logger.warning("third-person camera failed to start: %s", exc)
            self._camera_start_errors["third_person"] = str(exc)
        else:
            if "third_person" in self._camera_start_errors:
                logger.info("third-person camera recovered and started")
            self._camera_start_errors.pop("third_person", None)

        self._try_select_instrument()

    def _try_select_instrument(self) -> None:
        if self._desired_instrument is None or self.controller.state == State.RECORDING:
            return
        if self.controller.selected_instrument == self._desired_instrument:
            return
        key = self._desired_instrument
        try:
            self.controller.select_instrument(key)
        except Exception as exc:
            if key not in self._camera_start_errors:
                logger.warning("%s failed to start: %s", key, exc, exc_info=True)
            self._camera_start_errors[key] = str(exc)
        else:
            if key in self._camera_start_errors:
                logger.info("%s recovered and started", key)
            self._camera_start_errors.pop(key, None)

    # --- Qt event handlers -------------------------------------------------

    def _on_instrument_clicked(self, key: str) -> None:
        if self.controller.state == State.RECORDING:
            return
        self._desired_instrument = key
        self._try_select_instrument()
        self._sync_ui()

    def _on_start_clicked(self) -> None:
        if self.controller.state != State.READY:
            return
        self.error_banner.hide()
        self.summary_label.clear()
        self.controller.start_recording()
        if self.controller.state == State.ERROR:
            # The recorder couldn't start (an uncreatable sessions_dir, a
            # full disk). Say so -- otherwise Start just appears inert.
            self._show_error()
        self._sync_ui()

    def _on_stop_clicked(self) -> None:
        if self.controller.state != State.RECORDING:
            return
        session_info = self.controller.stop_recording()
        self._sync_ui()
        if self.controller.state == State.ERROR:
            # stop_recording() couldn't finalize -- show the banner, same as
            # a mid-recording failure. last_session_dir still points at the
            # raw capture files.
            self._show_error()
        else:
            self.summary_label.setText(self._format_summary("Session complete", session_info))

    def _on_watch_clicked(self) -> None:
        session_dir = self.controller.last_session_dir
        if session_dir is None or self.controller.state == State.RECORDING:
            return
        self._with_preview_paused(lambda: open_session(session_dir, parent=self))

    def _with_preview_paused(self, action) -> None:
        """Run a modal viewer with the live preview paused.

        The cameras keep running: restarting an instrument camera costs
        seconds and re-enters the device-busy class of failures (see
        DECISIONS.md's settings.py Preview entry). Only the preview pauses
        -- there's no point compositing behind a modal window. The restart
        is in a finally so a viewer that raises can't leave a dead pane.
        """
        self.preview_timer.stop()
        try:
            action()
        finally:
            self.preview_timer.start(int(1000 / PREVIEW_FPS))

    def _update_preview(self) -> None:
        frame_tp = self.third_person_camera.get_latest()
        if frame_tp is None:
            return
        frame_instrument = None
        if self.controller.selected_instrument is not None:
            frame_instrument = self.instruments[self.controller.selected_instrument].get_latest()
        instrument_image = frame_instrument.image if frame_instrument is not None else _INSTRUMENT_PLACEHOLDER
        canvas = side_by_side(instrument_image, frame_tp.image, out_size=PREVIEW_CANVAS_SIZE)
        self.video_label.setPixmap(bgr_to_pixmap(canvas))

    def _poll_tick(self) -> None:
        preflight = None
        if self.controller.state == State.RECORDING:
            self.controller.poll_recording()
            if self.controller.state == State.ERROR:
                self._show_error()
            elif self.controller.stopped_at_time_limit:
                # Stopped by itself, but not broken: a normal summary, not
                # the red banner, naming why so it doesn't look like a fault.
                self.summary_label.setText(
                    self._format_summary(
                        f"Stopped at the {self._limit_minutes()}-minute limit",
                        self.controller.last_session_info,
                    )
                )
        else:
            preflight = self.controller.poll_preflight()
        self._sync_ui(preflight)

    # --- UI reflection --------------------------------------------------

    def _sync_ui(self, preflight: PreflightStatus | None = None) -> None:
        state = self.controller.state
        self.mark.set_recording(state == State.RECORDING)
        self.start_button.setEnabled(state == State.READY)
        self.stop_button.setEnabled(state == State.RECORDING)
        for key, button in self._instrument_buttons.items():
            button.setEnabled(state != State.RECORDING)
            button.setChecked(key == self._desired_instrument)
        self.watch_button.setEnabled(
            state != State.RECORDING and self.controller.last_session_dir is not None
        )

        if state == State.RECORDING:
            self.status_label.setText(self._recording_status())
        elif state == State.READY:
            # The limit is stated up front, so reaching it is never the
            # first the student hears of it.
            self.status_label.setText(
                "Ready. Press Start Recording. "
                f"Recordings stop automatically after {self._limit_minutes()} minutes."
            )
        elif state == State.IDLE:
            self.status_label.setText(self._idle_reason(preflight))
        # ERROR: leave whatever _show_error() just wrote in place.

    def _limit_minutes(self) -> str:
        return f"{self.controller.max_session_minutes:g}"

    def _recording_status(self) -> str:
        elapsed = self.controller.recording_elapsed_s() or 0.0
        limit_s = self.controller.max_session_minutes * 60.0
        text = f"Recording... {_format_clock(elapsed)} of {_format_clock(limit_s)}"
        remaining = limit_s - elapsed
        if remaining <= LIMIT_COUNTDOWN_S:
            text += f" - stops automatically in {max(0, math.ceil(remaining))}s"
        return text

    def _idle_reason(self, preflight) -> str:
        if self._desired_instrument is None:
            return "Select an instrument to begin."
        if preflight is None:
            return "Waiting for cameras..."
        if preflight.frozen_cameras:
            # Distinct from "waiting for cameras": the camera *is* running,
            # so telling a student to plug it in would send them the wrong
            # way. Name the thing they can actually check.
            names = ", ".join(
                self._display_names.get(key, key) for key in preflight.frozen_cameras
            )
            return (
                f"{names}: the picture is frozen - the camera is on but not seeing "
                "anything. Check that it isn't covered, switched off, or blocked."
            )

        missing = []
        if not preflight.cameras_ready:
            if self._camera_start_errors:
                errors = "; ".join(
                    f"{self._display_names.get(key, key)}: {msg}"
                    for key, msg in self._camera_start_errors.items()
                )
                missing.append(f"cameras live ({errors})")
            else:
                missing.append("cameras live")
        if not preflight.disk_ok:
            if preflight.disk_error is not None:
                missing.append(
                    f"the recordings folder ({self.controller.output_root}) - is that drive connected?"
                )
            else:
                free_mb = preflight.free_bytes / (1024 * 1024)
                required_mb = preflight.required_bytes / (1024 * 1024)
                missing.append(f"free disk space ({free_mb:.0f} MB free, {required_mb:.0f} MB required)")
        if not missing:
            return "Waiting for cameras..."
        return "Waiting for: " + "; ".join(missing)

    def _show_error(self) -> None:
        message = self.controller.error_message or "Recording stopped unexpectedly."
        self.error_banner.setText(f"RECORDING STOPPED - {message}")
        self.error_banner.show()
        if self.controller.last_session_info is not None:
            self.summary_label.setText(
                self._format_summary("Partial session saved", self.controller.last_session_info)
            )

    def _format_summary(self, headline: str, session_info: dict) -> str:
        if "error" in session_info and "streams" not in session_info:
            return f"{headline}, but the output could not be finalized: {session_info['error']}"

        parts = [headline]
        for role, stream in session_info["streams"].items():
            label = stream.get("label") or self._display_names.get(role, role)
            # Only claim the .mkv was kept when there actually is one -- a
            # camera that captured nothing leaves no file at all.
            kept = " - .mkv kept" if stream.get("mkv") else ""
            if stream.get("error"):
                note = f" (recording ended early: {stream['error']}{kept})"
            elif not stream.get("verified", True):
                note = f" (NOT verified{kept})"
            else:
                note = ""
            parts.append(f"{label}: {stream['frame_count']} frames, {stream['dropped_frames']} dropped{note}")
        if self.controller.last_session_dir is not None:
            parts.append(f"saved to {self.controller.last_session_dir}")
        return "  |  ".join(parts)

    def _confirm_stop_and_exit(self) -> bool:
        # Split out from closeEvent() so tests can monkeypatch this instead
        # of dealing with a real modal dialog headlessly.
        reply = QMessageBox.question(
            self,
            "Recording in progress",
            "A recording is in progress. Stop it and exit?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def closeEvent(self, event) -> None:
        # A stray Alt+F4 or X-click shouldn't be able to silently end a
        # session, but a deliberate force-quit should still be possible --
        # defaults to No so an accidental click doesn't exit either way.
        if self.controller.state == State.RECORDING:
            if not self._confirm_stop_and_exit():
                logger.info("close-during-recording declined; recording continues")
                event.ignore()
                return
            logger.info("close-during-recording confirmed; stopping and exiting")
            try:
                self.controller.stop_recording()
            except Exception:
                logger.exception("stop_recording() failed during confirmed close")

        self.preview_timer.stop()
        self.poll_timer.stop()
        self.camera_retry_timer.stop()
        self.third_person_camera.stop()
        if self.controller.selected_instrument is not None:
            self.instruments[self.controller.selected_instrument].stop()
        super().closeEvent(event)


def _resolve_presets(inst: InstrumentConfig, name: str) -> tuple[str | None, int | None, float | None]:
    """(orientation, pixel_clock_hz, black_level) for an IDS camera, most
    specific first:

    1. an explicit `config.json` value -- the escape hatch for a
       non-standard mounting, or a host that can't take the full clock;
    2. the device profile a technician picked in settings.py;
    3. None, which lets IdsCamera match the camera's own model string.

    An unknown profile id -- one written by a newer build -- warns and falls
    through to (3) rather than raising. A config this build doesn't fully
    understand must still start the kiosk; the cameras are what students
    came for, and a refused start helps nobody.
    """
    profile = profile_for_id(inst.profile)
    if inst.profile not in (None, CUSTOM_PROFILE_ID) and profile is None:
        logger.warning(
            "%s: config names unknown device profile %r; falling back to this camera's "
            "own model presets",
            name, inst.profile,
        )
    orientation = inst.orientation or (profile.orientation if profile is not None else None)
    pixel_clock_hz = inst.pixel_clock_hz or (profile.pixel_clock_hz if profile is not None else None)
    black_level = inst.black_level
    if black_level is None and profile is not None:
        black_level = profile.black_level
    return orientation, pixel_clock_hz, black_level


def _make_camera(
    inst: InstrumentConfig,
    synthetic: bool,
    resolution: tuple[int, int],
    name: str,
    target_fps: float | None = None,
) -> BaseCamera:
    if synthetic:
        return SyntheticCamera(*resolution, name=name, fps=30)

    if inst.kind == "net2860_winusb":
        # The legacy BIO, through Microsoft's inbox winusb.sys in-process.
        # Lazily imported for the same reason as ids_camera.py below --
        # winusb.py binds Windows DLLs at import time, which is pointless on
        # a machine running only --synthetic. No target_fps or calibration
        # args: this camera is fixed at PAL's 25fps and does its own
        # exposure on board, where the host can't reach it.
        from net2860_winusb_camera import Net2860WinUsbCamera

        return Net2860WinUsbCamera(label=name)

    # kind == "ids" -- config.py's _parse_instrument only allows "ids" or
    # "net2860_winusb", and "net2860_winusb" was handled above.
    # Imported lazily, not at module level: ids_camera.py pulls in
    # ids_peak/ids_peak_ipl, which aren't installed (or needed) on a dev
    # machine running only with --synthetic -- see CLAUDE.md's Environment
    # section. Importing it unconditionally at the top of this file would
    # break `python app.py --synthetic` on such a machine.
    from ids_camera import IdsCamera

    orientation, pixel_clock_hz, black_level = _resolve_presets(inst, name)
    return IdsCamera(
        serial=inst.serial,
        exposure_time_us=inst.exposure_time_us,
        gain=inst.gain,
        target_fps=target_fps,
        orientation=orientation,
        pixel_clock_hz=pixel_clock_hz,
        black_level=black_level,
    )


def _make_third_person_camera(
    vid_pid: str | None, synthetic: bool, name: str, target_fps: float | None = None
) -> BaseCamera:
    if synthetic:
        return SyntheticCamera(*THIRD_PERSON_SYNTHETIC_RESOLUTION, name=name, fps=30)
    return UvcCamera(vid_pid=vid_pid, name=name, target_fps=target_fps)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Use SyntheticCamera for all three cameras instead of real hardware "
        "(for development on a machine with no cameras attached)",
    )
    parser.add_argument(
        "--instrument-synthetic",
        action="store_true",
        help="Use SyntheticCamera for both instrument cameras (slit lamp, BIO), "
        "keeping the third-person camera real",
    )
    parser.add_argument(
        "--third-person-synthetic",
        action="store_true",
        help="Use SyntheticCamera for the third-person camera, keeping the "
        "instrument cameras real",
    )
    args = parser.parse_args()

    instrument_synthetic = args.synthetic or args.instrument_synthetic
    third_person_synthetic = args.synthetic or args.third_person_synthetic

    _configure_logging()

    # QApplication created before the config check (not after, as a plain
    # log-and-exit would allow) specifically so a missing/malformed
    # config.json can show a QMessageBox. app.exe is built with
    # console=False (see packaging/app.spec) and launched from a Desktop
    # shortcut with no console attached, so logger's StreamHandler->stderr
    # reaches nobody -- a technician on a freshly installed machine (no
    # config.json yet, the exact case this guards) would double-click the
    # icon and see literally nothing happen. The RotatingFileHandler still
    # captures it durably in LOG_FILE either way; this is about the
    # in-the-moment signal, not the record.
    # QApplication.instance() or ...: guards against a second instantiation
    # when main() runs inside a test process that already created one at
    # module import time (see test_app.py) -- in real usage there's never
    # an existing instance before main() runs, so this is a no-op there.
    app = QApplication.instance() or QApplication(sys.argv)
    # Application-wide, so every dialog this process opens (including the
    # viewer behind Watch, and the setup-required box below) inherits both.
    app.setWindowIcon(QIcon(str(icon_path(ICON_APP))))
    reflex_style.apply(app)
    try:
        cfg = load_config()
    except ConfigError as exc:
        logger.error(str(exc))
        QMessageBox.critical(None, "Reflex - Setup required", str(exc))
        return 1

    logger.info(
        "app starting: instruments=%s third_person_vid_pid=%s instrument_synthetic=%s third_person_synthetic=%s",
        {
            key: "synthetic" if instrument_synthetic else f"{inst.kind}:{inst.serial}"
            for key, inst in cfg.instruments.items()
        },
        cfg.third_person.vid_pid,
        instrument_synthetic,
        third_person_synthetic,
    )

    instruments = {
        key: _make_camera(
            inst,
            instrument_synthetic,
            INSTRUMENT_SYNTHETIC_RESOLUTIONS.get(key, DEFAULT_SYNTHETIC_RESOLUTION),
            key,
            target_fps=cfg.recording.fps,
        )
        for key, inst in cfg.instruments.items()
    }
    third_person = _make_third_person_camera(
        cfg.third_person.vid_pid, third_person_synthetic, "third-person", target_fps=cfg.recording.fps
    )
    instrument_labels = {key: inst.label for key, inst in cfg.instruments.items()}
    output_root = cfg.sessions_dir if cfg.sessions_dir is not None else resolve_default_sessions_dir()

    if cfg.retention is not None:
        # One cleanup pass at startup, before any recording -- never on the
        # recording timer. A failure here must not stop the kiosk starting.
        try:
            from retention import apply_retention

            result = apply_retention(Path(output_root), cfg.retention)
            if result.deleted:
                logger.info(
                    "retention: removed %d old session(s), freed %.1f GB (%.1f GB free now)",
                    len(result.deleted),
                    result.freed_gb,
                    result.free_bytes_after / 1024**3,
                )
            if not result.capacity_target_met:
                logger.warning(
                    "retention: free space still below %.0f GB after cleanup; recording stays "
                    "gated by the disk preflight until space is freed",
                    cfg.retention.min_free_gb,
                )
        except Exception:
            logger.exception("retention: cleanup pass failed; continuing without it")

    window = KioskWindow(
        third_person,
        instruments,
        instrument_labels=instrument_labels,
        fps=cfg.recording.fps,
        output_root=output_root,
    )
    window.resize(*PREVIEW_CANVAS_SIZE)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
