"""Technician tool: assigns which physical camera fills each role (slit
lamp, BIO, third-person) and writes config.json. See DECISIONS.md's
2026-08-18 settings.py entry and CLAUDE.md's "Who uses it" --
this is deliberately a separate program from app.py, never launched from
the kiosk window, so nothing reachable from here needs a "could a student
stumble into this" review.

Deliberately lean, not a wizard: one row per role, a dropdown of currently-
detected candidate devices, a Preview button, and global Rescan/Save. Save
writes config.json but does not hot-reload a running app.py -- restart the
kiosk app to pick up changes (an explicit accepted scope line, not a gap).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app_icon import ICON_SETTINGS, icon_path
from camera import BaseCamera
from config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_RECORDING_FPS,
    ConfigError,
    load_config,
    resolve_default_sessions_dir,
)
from qt_image import bgr_to_pixmap
from uvc_camera import UvcCamera
from device_presets import CUSTOM_PROFILE_ID, profile_for_id, profile_for_model, profiles_for_role
from exposure_calibration import exposure_budget_us
from synthetic_camera import SyntheticCamera
from uvc_enumeration import UvcDeviceInfo, list_uvc_devices

logger = logging.getLogger(__name__)

ROLE_TITLES = {"slit_lamp": "Slit Lamp", "bio": "BIO"}
THIRD_PERSON_ROLE = "third_person"
THIRD_PERSON_TITLE = "Third-Person"

_UNSET = object()  # sentinel: "no pending preselection", distinct from a real None key


@dataclass
class RowCandidate:
    # UI-level selection identity (dropdown matching, is_valid()'s "something
    # is selected" check) -- None if this device can't be saved (see uvc_pid
    # note). NOT always literally what's written to config.json's "serial":
    # for kind="net2860_winusb" this is a fixed sentinel, since
    # _instrument_data() writes that kind's config shape from `kind` alone,
    # not from this key.
    key: str | None
    kind: str  # "ids" / "uvc" / "net2860_winusb" -- which BaseCamera subclass this becomes
    display_name: str
    preview_target: object  # e.g. serial (ids) / device index (uvc) -- what the preview factory pulls out of this candidate
    source: object  # the original IdsDeviceInfo/UvcDeviceInfo, for pulling extra fields (e.g. friendly_name) at Save time


def _default_list_ids_devices() -> list:
    # Lazy: ids_camera.py imports ids_peak at module level, which isn't
    # installed on every machine that runs settings.py (e.g. this dev
    # machine) -- see app.py's _make_camera for the same pattern.
    from ids_camera import list_ids_devices

    return list_ids_devices()


def _default_make_ids_camera(candidate: RowCandidate) -> BaseCamera:
    from ids_camera import IdsCamera

    # No auto-convergence: Preview exists to calibrate, and converging a
    # dark, uncalibrated camera times out and fails the open -- leaving the
    # technician no way to reach Auto-Calibrate at all.
    return IdsCamera(serial=candidate.preview_target, converge_auto=False)


def _default_make_uvc_camera(candidate: RowCandidate) -> BaseCamera:
    return UvcCamera(device=candidate.preview_target, name="preview")


def _default_make_net2860_winusb_camera(_candidate: RowCandidate) -> BaseCamera:
    from net2860_winusb_camera import Net2860WinUsbCamera

    return Net2860WinUsbCamera(label="preview")


def _default_list_net2860_winusb() -> list:
    """Present-and-bound legacy BIO cameras, or [] if there are none.

    Unlike the vendor-driver route this really is a device scan: a WinUSB
    device has an enumerable interface, so we can tell "camera plugged in
    and driver installed" from "not there". Errors are swallowed into an
    empty list -- winusb.py binds Windows DLLs at import, and a settings.py
    that refuses to open because one optional camera's discovery failed
    would be worse than one that simply doesn't offer it.
    """
    try:
        import winusb
        from net2860_winusb_camera import PID, VID

        return winusb.find_by_vid_pid(VID, PID)
    except Exception:  # noqa: BLE001 -- see docstring
        logger.exception("legacy BIO (WinUSB) discovery failed; offering no candidate")
        return []


def _default_make_instrument_camera(candidate: RowCandidate) -> BaseCamera:
    """Shared preview factory for both instrument rows (slit lamp, BIO) --
    branches per-candidate rather than per-row, since the BIO row can offer
    both "ids" and "net2860_winusb" candidates."""
    if candidate.kind == "net2860_winusb":
        return _default_make_net2860_winusb_camera(candidate)
    return _default_make_ids_camera(candidate)


def _ids_candidates(devices: list) -> list[RowCandidate]:
    return [
        RowCandidate(
            key=d.serial,
            kind="ids",
            display_name=f"{d.model_name}  (serial {d.serial})",
            preview_target=d.serial,
            source=d,
        )
        for d in devices
    ]


def _net2860_winusb_candidates(devices: list) -> list[RowCandidate]:
    """The legacy BIO, reached through WinUSB.

    Device-scanned, like _ids_candidates()/_uvc_candidates(): a WinUSB-bound
    device has an enumerable interface, so an empty list here means
    something real -- either the camera isn't plugged in, or the driver
    package hasn't been installed on this machine. Offering nothing is the
    honest answer in both cases.

    key doubles as its own sentinel, the way the removed vendor-driver
    candidate's did: there's exactly one of this camera, so there is no real
    identity to key off.
    """
    return [
        RowCandidate(
            key="net2860_winusb",
            kind="net2860_winusb",
            display_name="Legacy BIO (NET GmbH KS722OUP, WinUSB)",
            preview_target=None,
            source=None,
        )
        for _ in devices[:1]
    ]


def _uvc_candidates(devices: list[UvcDeviceInfo]) -> list[RowCandidate]:
    return [
        RowCandidate(
            key=d.vid_pid,
            kind="uvc",
            display_name=f"{d.name}  ({d.vid_pid})" if d.vid_pid else f"{d.name}  (no VID/PID)",
            preview_target=d.index,
            source=d,
        )
        for d in devices
    ]


# Pre-filled defaults for the (opt-in, unchecked-by-default) recordings-
_DEFAULT_MAX_AGE_DAYS = 30
_DEFAULT_MIN_FREE_GB = 20
_DEFAULT_PROTECT_DAYS = 7

_GAIN_SLIDER_SCALE = 10  # QSlider is integer-only; gain is a small float (e.g. 1.0-24.0)


class PreviewDialog(QDialog):
    """Single-camera live preview for whichever device is highlighted in a
    DeviceRow's dropdown -- opened modally (.exec(), not .show()) so a
    concurrent Rescan can't mutate the row's candidate list out from under
    it, and so this camera's lifetime doesn't overlap another IDS open
    attempt while ids_peak.Library's Initialize/Close reentrancy across
    nested calls is unverified (not in vendor/ids_peak_api.txt's scope).

    For an instrument camera whose ExposureTime/Gain can be written at all
    (detected via `camera.supports_manual_calibration()`, duck-typed rather
    than an isinstance(IdsCamera) check so this module never needs to
    import ids_camera at all -- see CLAUDE.md's Environment section on why
    that import must stay lazy), also shows exposure/gain sliders and an
    Auto-Calibrate button. See DECISIONS.md's 2026-08-25 calibration
    entry for the full design, and DECISIONS.md's 2026-09-10
    entry for why this is no longer limited to cameras that lack
    ExposureAuto/GainAuto -- a camera that converges on its own does so at
    open, which is the moment a student taps the picker, with the
    instrument not yet in use.
    """

    def __init__(
        self,
        camera: BaseCamera,
        title: str,
        parent=None,
        initial_exposure_time_us: float | None = None,
        initial_gain: float | None = None,
        target_fps: float | None = None,
    ):
        super().__init__(parent)
        # The recording frame rate this calibration has to fit inside.
        # Exposure is a frame-rate budget -- see exposure_calibration's
        # exposure_budget_us() and CLAUDE.md's camera-configuration table.
        self._target_fps = target_fps
        self.setWindowTitle(f"Preview – {title}")
        self._camera = camera
        # Stop the camera on *any* dialog exit, not just closeEvent: the Esc
        # key routes through QDialog.reject() with no QCloseEvent, which
        # would otherwise leak the open IDS device. See DECISIONS.md's
        # 2026-09-01 entry.
        self.finished.connect(self._shutdown)
        self.calibration_supported = False
        self.final_exposure_time_us = initial_exposure_time_us
        self.final_gain = initial_gain

        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setMinimumSize(480, 360)
        self.video_label.setStyleSheet("background-color: black;")

        self.status_label = QLabel("Starting…")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.calibration_status_label = QLabel()
        self.calibration_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.calibration_status_label.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addWidget(self.video_label)
        layout.addWidget(self.status_label)

        try:
            self._camera.start()
            self.calibration_supported = bool(getattr(camera, "supports_manual_calibration", lambda: False)())
        except Exception as exc:
            self.status_label.setText(f"Failed to start: {exc}")

        # A raise partway through building the calibration controls would
        # propagate out of __init__ before the dialog is ever shown or
        # closed, leaking the just-started camera -- tear it down
        # explicitly, then let the error surface. See DECISIONS.md.
        try:
            if self.calibration_supported:
                self._build_exposure_gain_controls(layout, initial_exposure_time_us, initial_gain)
            layout.addWidget(self.calibration_status_label)
        except Exception:
            self._shutdown()
            raise

        self.setLayout(layout)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update)
        self.timer.start(33)

    def _add_slider_row(
        self, layout: QVBoxLayout, label_text: str, minimum: int, maximum: int, value: int
    ) -> tuple[QSlider, QLabel]:
        """Builds one labeled slider + value-readout row and appends it to
        layout. The caller wires valueChanged itself -- the handler differs
        per axis (exposure/gain/red/blue), so it isn't wired here.
        """
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        value_label = QLabel()

        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        row.addWidget(slider)
        row.addWidget(value_label)
        layout.addLayout(row)

        return slider, value_label

    def _build_exposure_gain_controls(
        self, layout: QVBoxLayout, initial_exposure_time_us: float | None, initial_gain: float | None
    ) -> None:
        exposure_min, exposure_max = self._camera.exposure_time_range_us()
        # Bounded by the frame-rate budget, not the sensor's own maximum:
        # anything past it costs frame rate and adds motion blur to exactly
        # the movement students are here to watch. A slider that cannot
        # reach a bad value beats a warning about one that was saved.
        if self._target_fps:
            exposure_max = min(exposure_max, exposure_budget_us(self._target_fps))
        gain_min, gain_max = self._camera.gain_range()

        # Seed from a previously-saved calibration if there is one, rather
        # than whatever the sensor happened to power on with.
        if initial_exposure_time_us is not None:
            self._camera.set_exposure_time_us(min(exposure_max, max(exposure_min, initial_exposure_time_us)))
        if initial_gain is not None:
            self._camera.set_gain(min(gain_max, max(gain_min, initial_gain)))

        self.exposure_slider, self.exposure_value_label = self._add_slider_row(
            layout, "Exposure", int(exposure_min), int(exposure_max), int(self._camera.get_exposure_time_us())
        )
        self.exposure_slider.valueChanged.connect(self._on_exposure_changed)

        self.gain_slider, self.gain_value_label = self._add_slider_row(
            layout,
            "Gain",
            int(gain_min * _GAIN_SLIDER_SCALE),
            int(gain_max * _GAIN_SLIDER_SCALE),
            int(self._camera.get_gain() * _GAIN_SLIDER_SCALE),
        )
        self.gain_slider.valueChanged.connect(self._on_gain_changed)

        self.calibrate_button = QPushButton("Auto-Calibrate")
        self.calibrate_button.clicked.connect(self._on_calibrate_clicked)
        layout.addWidget(self.calibrate_button)

        self.final_exposure_time_us = self._camera.get_exposure_time_us()
        self.final_gain = self._camera.get_gain()
        self._refresh_exposure_gain_labels()

    def _on_exposure_changed(self, value: int) -> None:
        self._camera.set_exposure_time_us(float(value))
        self.final_exposure_time_us = float(value)
        self._refresh_exposure_gain_labels()

    def _on_gain_changed(self, value: int) -> None:
        gain = value / _GAIN_SLIDER_SCALE
        self._camera.set_gain(gain)
        self.final_gain = gain
        self._refresh_exposure_gain_labels()

    def _on_calibrate_clicked(self) -> None:
        self.calibrate_button.setEnabled(False)
        self.calibration_status_label.setText("Calibrating…")
        QApplication.processEvents()
        try:
            converged = self._camera.auto_calibrate(target_fps=self._target_fps)
        except Exception as exc:
            QMessageBox.warning(self, "Calibration failed", str(exc))
            converged = None
        self.calibrate_button.setEnabled(True)

        self.exposure_slider.blockSignals(True)
        self.gain_slider.blockSignals(True)
        self.exposure_slider.setValue(int(self._camera.get_exposure_time_us()))
        self.gain_slider.setValue(int(self._camera.get_gain() * _GAIN_SLIDER_SCALE))
        self.exposure_slider.blockSignals(False)
        self.gain_slider.blockSignals(False)
        self.final_exposure_time_us = self._camera.get_exposure_time_us()
        self.final_gain = self._camera.get_gain()
        self._refresh_exposure_gain_labels()

        if converged is True:
            self.calibration_status_label.setText(f"Calibrated.   {self._calibration_cost()}")
        elif converged is False:
            self.calibration_status_label.setText(
                "Couldn't reach target brightness automatically -- adjust the sliders by eye.   "
                f"{self._calibration_cost()}"
            )

    def _calibration_cost(self) -> str:
        """What the calibration actually bought, in units a technician can
        judge without an imaging background.

        "87208.816" tells nobody anything; "11fps, gain 1.0 of 4.0" tells
        them it is wrong. Reporting the cost -- not adding another setting
        -- is what was missing when the slit lamp sat at an 11fps exposure.
        See CLAUDE.md's "Camera configuration: who decides what".
        """
        exposure_us = self._camera.get_exposure_time_us()
        gain = self._camera.get_gain()
        _gain_min, gain_max = self._camera.gain_range()
        possible_fps = 1_000_000.0 / max(exposure_us, 1e-6)
        parts = [
            f"exposure {exposure_us / 1000:.1f}ms",
            f"gain {gain:.1f}x of {gain_max:.1f} max",
            f"allows ~{possible_fps:.0f}fps",
        ]
        text = "   |   ".join(parts)
        if self._target_fps and possible_fps < self._target_fps:
            text += (
                f"   <-- BELOW the {self._target_fps:g}fps recording target, "
                "and blurs motion. Add light at the instrument."
            )
        return text

    def _refresh_exposure_gain_labels(self) -> None:
        self.exposure_value_label.setText(f"{int(self._camera.get_exposure_time_us())} µs")
        self.gain_value_label.setText(f"{self._camera.get_gain():.1f}x")

    def _update(self) -> None:
        frame = self._camera.get_latest()
        if frame is None:
            return
        h, w = frame.image.shape[:2]
        self.status_label.setText(f"{w}x{h}")
        self.video_label.setPixmap(bgr_to_pixmap(frame.image))

    def _shutdown(self, *_args) -> None:
        """Stop the preview timer and the camera. Idempotent -- reached from
        both the finished signal and closeEvent, and tolerant of being
        called before self.timer exists (an __init__ failure path). The
        camera's own stop() no-ops if it never started."""
        timer = getattr(self, "timer", None)
        if timer is not None:
            timer.stop()
        self._camera.stop()

    def closeEvent(self, event) -> None:
        self._shutdown()
        super().closeEvent(event)


class DeviceRow(QWidget):
    """One role's device dropdown, profile dropdown, name and Preview.

    Three fields, two of them chosen rather than typed:

      device   what the system reports is plugged in
      profile  what that camera *is* -- the supported list we ship, which
               carries the orientation and pixel clock a technician would
               otherwise have to know. Custom is always in it, so an
               unlisted camera never waits on a code change.
      name     the one thing a technician writes: what students read on the
               picker. Pre-filled from the profile, because the common case
               should need no typing, but theirs to change -- a room calls
               its instruments whatever its students already call them.

    Generic over instrument roles (editable label, one or more candidate
    kinds -- IDS devices, or IDS devices plus the legacy BIO candidate
    on the BIO row) and the third-person role (UVC devices, no label,
    key=vid_pid) -- the difference is entirely in the candidates/factory
    passed in, not in this class's behavior. A row's candidates can mix
    kinds (the BIO row does); each RowCandidate carries its own `kind` so
    the (shared) preview factory and Save's config-shape logic branch
    per-candidate. See DECISIONS.md's "Net2860Camera" entry.
    """

    changed = Signal()

    def __init__(
        self,
        role_key: str,
        title: str,
        has_label: bool,
        preview_camera_factory: Callable[[RowCandidate], BaseCamera],
        supports_calibration: bool = False,
        target_fps: float = DEFAULT_RECORDING_FPS,
        parent=None,
    ):
        super().__init__(parent)
        self.role_key = role_key
        self.has_label = has_label
        self.supports_calibration = supports_calibration
        self.target_fps = target_fps
        self._preview_camera_factory = preview_camera_factory
        self._candidates: list[RowCandidate] = []
        self._pending_selection = _UNSET
        # Populated from an existing config.json's exposure_time_us/gain
        # (instrument roles only) and updated after a Preview session that
        # used the calibration controls -- see PreviewDialog. None means
        # "no calibrated value yet," the same as a camera with working
        # auto-exposure never needing one.
        self._exposure_time_us: float | None = None
        self._gain: float | None = None
        self._pending_profile: str | None | object = _UNSET
        # Set by set_candidates() when enumeration failed. Outranks the
        # row's own notes: "the scan broke" is what a technician has to act
        # on, and a profile note underneath it would just hide it.
        self._scan_status = ""

        self.title_label = QLabel(title)
        self.title_label.setMinimumWidth(90)

        self.combo = QComboBox()
        self.combo.currentIndexChanged.connect(self._on_selection_changed)

        # Instrument rows only: the third-person row stays a raw device list,
        # since any UVC webcam works and there is nothing model-specific to
        # know about one. See device_presets.profiles_for_role("third_person").
        self.profile_combo = QComboBox() if has_label else None
        if self.profile_combo is not None:
            for profile in profiles_for_role(role_key):
                self.profile_combo.addItem(profile.name, profile.id)
            self.profile_combo.addItem("Custom (unlisted camera)...", CUSTOM_PROFILE_ID)
            self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)

        # The name students read on the picker. Pre-filled from the chosen
        # profile and editable: `_label_is_default` tracks whether it is
        # still ours to overwrite, so changing profile re-fills a name
        # nobody has touched but never clobbers one a technician typed.
        self.label_edit = QLineEdit() if has_label else None
        self._label_is_default = True
        if self.label_edit is not None:
            self.label_edit.setPlaceholderText("Name students see, e.g. Slit Lamp")
            self.label_edit.textChanged.connect(lambda _text: self.changed.emit())
            self.label_edit.textEdited.connect(self._on_label_edited)

        self.preview_button = QPushButton("Preview")
        self.preview_button.clicked.connect(self._on_preview_clicked)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)

        grid = QGridLayout()
        grid.addWidget(self.title_label, 0, 0)
        grid.addWidget(self.combo, 0, 1)
        col = 2
        for widget in (self.profile_combo, self.label_edit):
            if widget is not None:
                grid.addWidget(widget, 0, col)
                col += 1
        grid.addWidget(self.preview_button, 0, col)
        grid.addWidget(self.status_label, 1, 1, 1, col)
        self.setLayout(grid)

        self._sync_profile_fields()
        self._update_ui_state()

    def set_pending_selection(self, key: str | None) -> None:
        """Consumed once, by the next set_candidates() call -- used to
        preselect a device loaded from an existing config.json at startup.
        After that, set_candidates() preserves whatever's currently
        selected instead (so Rescan doesn't revert a technician's choice
        back to the originally-loaded config).
        """
        self._pending_selection = key

    def set_candidates(self, candidates: list[RowCandidate], status: str = "") -> None:
        if self._pending_selection is not _UNSET:
            target_key = self._pending_selection
            self._pending_selection = _UNSET
        else:
            current = self.selected_candidate()
            target_key = current.key if current is not None else None

        self._candidates = candidates
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem("‹not connected›", -1)
        for i, candidate in enumerate(candidates):
            self.combo.addItem(candidate.display_name, i)

        new_index = 0
        if target_key is not None:
            for i, candidate in enumerate(candidates):
                if candidate.key == target_key:
                    new_index = i + 1
                    break
        self.combo.setCurrentIndex(new_index)
        self.combo.blockSignals(False)

        if self._pending_profile is not _UNSET and self.profile_combo is not None:
            index = self.profile_combo.findData(self._pending_profile)
            # An unknown id (a newer build's) falls to Custom, matching how
            # app.py resolves it: the technician's own values, not a failure.
            self.profile_combo.setCurrentIndex(index if index >= 0 else self.profile_combo.count() - 1)
            self._pending_profile = _UNSET
        elif target_key is None:
            self._auto_select_profile()

        self._scan_status = status
        self._sync_profile_fields()
        self._update_ui_state()
        self.changed.emit()

    def selected_candidate(self) -> RowCandidate | None:
        idx = self.combo.currentData()
        if idx is None or idx < 0:
            return None
        return self._candidates[idx]

    def selected_key(self) -> str | None:
        candidate = self.selected_candidate()
        return candidate.key if candidate is not None else None

    def label_text(self) -> str:
        return self.label_edit.text().strip() if self.label_edit is not None else ""

    def set_label_text(self, text: str) -> None:
        """Used to restore a saved name, which is a technician's answer even
        though no one typed it here -- so it stops being ours to overwrite
        when the profile changes."""
        if self.label_edit is not None:
            self.label_edit.setText(text)
            self._label_is_default = not text

    def profile_id(self) -> str | None:
        """The selected profile id, CUSTOM_PROFILE_ID for custom, or None on
        a row that has no profiles (third-person)."""
        if self.profile_combo is None:
            return None
        return self.profile_combo.currentData()

    def set_pending_profile(self, profile_id: str | None) -> None:
        """Consumed by the next set_candidates(), like set_pending_selection:
        a config.json profile is restored *after* its device is, so choosing
        the device doesn't overwrite what was saved. An id this build doesn't
        know lands on Custom, which is what it behaves as."""
        self._pending_profile = profile_id

    def calibration(self) -> tuple[float | None, float | None]:
        return self._exposure_time_us, self._gain

    def set_calibration(self, exposure_time_us: float | None, gain: float | None) -> None:
        self._exposure_time_us = exposure_time_us
        self._gain = gain

    def is_valid(self) -> bool:
        if self.selected_key() is None:
            return False
        if self.has_label and not self.label_text():
            return False
        return True

    def _on_profile_changed(self, _index: int) -> None:
        self._sync_profile_fields()
        self._update_ui_state()
        self.changed.emit()

    def _on_label_edited(self, _text: str) -> None:
        """textEdited, not textChanged: this fires only for a person typing,
        which is exactly when the name stops being ours to overwrite."""
        self._label_is_default = False

    def _sync_profile_fields(self) -> None:
        """Offer the profile's name, without taking the field away. Custom
        leaves it empty on purpose: nobody but the technician can name a
        camera we don't know, and an empty required field says so."""
        if self.profile_combo is None or self.label_edit is None:
            return
        if not self._label_is_default:
            return
        profile = profile_for_id(self.profile_id())
        self.label_edit.setText(profile.picker_label if profile is not None else "")

    def _selected_model_name(self) -> str:
        candidate = self.selected_candidate()
        return str(getattr(candidate.source, "model_name", "") or "") if candidate else ""

    def _auto_select_profile(self) -> None:
        """Pick the profile matching the selected device, when one matches.
        A technician can override it afterwards -- this is a default, not a
        constraint."""
        if self.profile_combo is None:
            return
        candidate = self.selected_candidate()
        if candidate is None:
            return
        if candidate.kind == "net2860_winusb":
            match = next((p for p in profiles_for_role(self.role_key) if p.kind == candidate.kind), None)
        else:
            match = profile_for_model(self._selected_model_name(), self.role_key)
        index = self.profile_combo.findData(match.id if match else CUSTOM_PROFILE_ID)
        if index >= 0:
            self.profile_combo.setCurrentIndex(index)

    def _profile_mismatch_warning(self) -> str:
        """Non-blocking: a technician may know better than this table, and
        the result is visible in Preview either way."""
        profile = profile_for_id(self.profile_id())
        model = self._selected_model_name()
        if profile is None or not profile.model_tokens or not model:
            return ""
        if any(token.upper() in model.upper() for token in profile.model_tokens):
            return ""
        return f"Note: {profile.name} usually reports {profile.model_tokens[0]}; this camera reports {model}."

    def _on_selection_changed(self, _index: int) -> None:
        # Calibration belongs to the camera it was measured on, not to the
        # role. Kept across a change, it saves one camera's values under
        # another's serial -- found as the BIO's 25.4x gain reaching the
        # slit lamp (4.0x max), which then refused to open. Only a user's
        # change lands here: set_candidates() blocks signals, so values
        # loaded from config.json survive loading and Rescan.
        self._exposure_time_us = None
        self._gain = None
        self._auto_select_profile()
        self._sync_profile_fields()
        self._update_ui_state()
        self.changed.emit()

    def _row_note(self) -> str:
        """What this row has to say for itself: a failed scan first, then why
        a camera can't be saved, then anything odd about the pairing, then
        the profile's own note."""
        if self._scan_status:
            return self._scan_status
        candidate = self.selected_candidate()
        if candidate is None:
            # Nothing is plugged in yet as far as this row knows, so it has
            # no business claiming to be a particular instrument.
            return "Choose the camera that's plugged in."
        if candidate.key is None:
            return "This device has no discoverable VID/PID and can't be saved."
        mismatch = self._profile_mismatch_warning()
        if mismatch:
            return mismatch
        profile = profile_for_id(self.profile_id())
        if profile is not None:
            return profile.note
        if self.label_edit is not None and not self.label_text():
            # Custom: the one field nobody else can fill in for them.
            return "Type a name students will recognise."
        return ""

    def _update_ui_state(self) -> None:
        candidate = self.selected_candidate()
        self.preview_button.setEnabled(candidate is not None)
        # What a camera *is*, and what to call it, only mean something once
        # there is a camera: until then both fields are inert rather than
        # showing a confident answer about a row that holds nothing.
        for widget in (self.profile_combo, self.label_edit):
            if widget is not None:
                widget.setEnabled(candidate is not None)
        self.status_label.setText(self._row_note())

    def _on_preview_clicked(self) -> None:
        candidate = self.selected_candidate()
        if candidate is None:
            return
        try:
            camera = self._preview_camera_factory(candidate)
        except Exception as exc:
            QMessageBox.warning(self, "Preview failed", str(exc))
            return
        dialog = PreviewDialog(
            camera,
            self.title_label.text(),
            parent=self,
            initial_exposure_time_us=self._exposure_time_us if self.supports_calibration else None,
            initial_gain=self._gain if self.supports_calibration else None,
            target_fps=self.target_fps,
        )
        dialog.exec()
        if self.supports_calibration and dialog.calibration_supported:
            self._exposure_time_us = dialog.final_exposure_time_us
            self._gain = dialog.final_gain
        # Released only after its results are read. Qt parent-child
        # ownership would otherwise keep every Preview's window and its
        # pixmap alive for as long as Settings is open -- same reasoning as
        # viewer._release().
        dialog.setParent(None)
        dialog.deleteLater()


class SettingsWindow(QMainWindow):
    def __init__(
        self,
        config_path: Path | str = DEFAULT_CONFIG_PATH,
        list_ids_devices_fn: Callable[[], list] = _default_list_ids_devices,
        list_uvc_devices_fn: Callable[[], list[UvcDeviceInfo]] = list_uvc_devices,
        list_net2860_winusb_fn: Callable[[], list] = _default_list_net2860_winusb,
        instrument_preview_camera_factory: Callable[[RowCandidate], BaseCamera] = _default_make_instrument_camera,
        uvc_preview_camera_factory: Callable[[RowCandidate], BaseCamera] = _default_make_uvc_camera,
    ):
        super().__init__()
        self.setWindowTitle("Camera Settings")
        self.config_path = Path(config_path)
        # Filled in by _load_existing_config(); the fallback matches what
        # config.py applies when config.json has no `recording` section.
        self._recording_fps: float = DEFAULT_RECORDING_FPS
        self._list_ids_devices_fn = list_ids_devices_fn
        self._list_uvc_devices_fn = list_uvc_devices_fn
        self._list_net2860_winusb_fn = list_net2860_winusb_fn

        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(
            "background-color: #b00020; color: white; font-weight: bold; padding: 8px;"
        )
        self.warning_label.hide()

        # Separate from warning_label: that one is about an existing
        # config.json this window couldn't read; this one is live
        # validation against the *current* on-screen selections (today,
        # just "same physical camera picked for two instrument roles" --
        # app.py would otherwise have both roles fighting to open one
        # device). Recomputed on every row change, not just at startup.
        self.conflict_label = QLabel()
        self.conflict_label.setWordWrap(True)
        self.conflict_label.setStyleSheet(
            "background-color: #b00020; color: white; font-weight: bold; padding: 8px;"
        )
        self.conflict_label.hide()

        # Amber, not red: leaving an instrument out is allowed (a room may
        # genuinely have only one), but it must never happen by accident --
        # a camera that was merely unplugged at configuration time would
        # otherwise silently disappear from the students' picker.
        self.omission_label = QLabel()
        self.omission_label.setWordWrap(True)
        self.omission_label.setStyleSheet(
            "background-color: #8a6d00; color: white; font-weight: bold; padding: 8px;"
        )
        self.omission_label.hide()

        self._instrument_rows: dict[str, DeviceRow] = {
            key: DeviceRow(
                key,
                title,
                has_label=True,
                preview_camera_factory=instrument_preview_camera_factory,
                supports_calibration=True,
            )
            for key, title in ROLE_TITLES.items()
        }
        self._third_person_row = DeviceRow(
            THIRD_PERSON_ROLE, THIRD_PERSON_TITLE, has_label=False, preview_camera_factory=uvc_preview_camera_factory
        )

        self.rescan_button = QPushButton("Rescan")
        self.rescan_button.clicked.connect(self.rescan)
        self.save_button = QPushButton("Save")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._on_save_clicked)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)

        # Not a per-role DeviceRow: this is where recordings land, not a
        # camera. Pre-filled with the resolved default so it's always a
        # valid value even if the technician never touches it -- see
        # config.py's resolve_default_sessions_dir().
        self.sessions_dir_title = QLabel("Recordings folder")
        self.sessions_dir_title.setMinimumWidth(90)
        self.sessions_dir_edit = QLineEdit(str(resolve_default_sessions_dir()))
        self.sessions_dir_edit.setReadOnly(True)
        self.sessions_dir_browse_button = QPushButton("Browse...")
        self.sessions_dir_browse_button.clicked.connect(self._on_browse_sessions_dir)
        sessions_dir_row = QHBoxLayout()
        sessions_dir_row.addWidget(self.sessions_dir_title)
        sessions_dir_row.addWidget(self.sessions_dir_edit)
        sessions_dir_row.addWidget(self.sessions_dir_browse_button)


        layout = QVBoxLayout()
        layout.addWidget(self.warning_label)
        layout.addWidget(self.conflict_label)
        layout.addWidget(self.omission_label)
        for row in self._all_rows():
            layout.addWidget(row)
            row.changed.connect(self._update_save_enabled)
        layout.addLayout(sessions_dir_row)

        buttons = QHBoxLayout()
        buttons.addWidget(self.rescan_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        layout.addWidget(self.status_label)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self._load_existing_config()
        self.rescan()

    def _all_rows(self) -> list[DeviceRow]:
        return [*self._instrument_rows.values(), self._third_person_row]

    def _load_existing_config(self) -> None:
        if not self.config_path.exists():
            return  # normal first-run state -- no config yet, nothing to warn about
        try:
            cfg = load_config(self.config_path)
        except ConfigError as exc:
            self.warning_label.setText(f"Existing {self.config_path} could not be read: {exc}")
            self.warning_label.show()
            return

        for key, row in self._instrument_rows.items():
            inst = cfg.instruments.get(key)
            if inst is not None:
                row.set_label_text(inst.label)
                # None means a config written before profiles existed, which
                # is a custom entry -- see device_presets.CUSTOM_PROFILE_ID.
                row.set_pending_profile(inst.profile or CUSTOM_PROFILE_ID)
                # inst.serial is None for kind="net2860_winusb" -- that
                # candidate's key is a sentinel (== inst.kind), not a serial.
                pending_key = inst.serial if inst.kind == "ids" else inst.kind
                row.set_pending_selection(pending_key)
                row.set_calibration(inst.exposure_time_us, inst.gain)
        self._third_person_row.set_pending_selection(cfg.third_person.vid_pid)
        if cfg.sessions_dir is not None:
            self.sessions_dir_edit.setText(str(cfg.sessions_dir))
        self._recording_fps = cfg.recording.fps
        for row in self._instrument_rows.values():
            row.target_fps = cfg.recording.fps
    def rescan(self) -> None:
        ids_devices, ids_status = self._safe_list_ids_devices()
        ids_candidates = _ids_candidates(ids_devices)
        winusb_devices = self._list_net2860_winusb_fn()
        for key, row in self._instrument_rows.items():
            # "bio" only: this camera is a BIO-specific alternative, not a
            # slit-lamp one -- see DECISIONS.md's "Net2860Camera" entry. The
            # WinUSB candidate appears only when one is actually present and
            # bound; the vendor-driver one is always offered because it
            # can't be scanned for.
            if key == "bio":
                candidates = ids_candidates + _net2860_winusb_candidates(winusb_devices)
            else:
                candidates = ids_candidates
            row.set_candidates(candidates, status=ids_status)

        uvc_devices, uvc_status = self._safe_list_uvc_devices()
        self._third_person_row.set_candidates(_uvc_candidates(uvc_devices), status=uvc_status)

        self._update_save_enabled()

    def _safe_list_ids_devices(self) -> tuple[list, str]:
        try:
            return self._list_ids_devices_fn(), ""
        except Exception as exc:
            logger.warning("could not enumerate IDS devices: %s", exc)
            return [], f"Could not enumerate IDS devices: {exc}"

    def _safe_list_uvc_devices(self) -> tuple[list[UvcDeviceInfo], str]:
        """Wrapped exactly like the IDS scan above, and for the same reason:
        __init__ calls rescan(), so an enumeration that raises stops Settings
        opening at all. This one reaches DirectShow through pygrabber, where
        a single misbehaving USB device is enough -- and a technician then
        has no way in to fix the configuration. Report it in the row's status
        line and keep the window usable. See DECISIONS.md's 2026-09-09 entry.
        """
        try:
            return self._list_uvc_devices_fn(), ""
        except Exception as exc:
            logger.warning("could not enumerate webcams: %s", exc)
            return [], f"Could not enumerate webcams: {exc}"

    def _on_browse_sessions_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose recordings folder", self.sessions_dir_edit.text())
        if chosen:
            self.sessions_dir_edit.setText(chosen)

    def _duplicate_serial_roles(self) -> dict[str, list[str]]:
        """Serials currently selected by more than one instrument row --
        app.py has no way to open the same physical IDS camera for two
        roles at once, so this must block Save, not just look odd.
        """
        by_serial: dict[str, list[str]] = {}
        for key, row in self._instrument_rows.items():
            serial = row.selected_key()
            if serial is not None:
                by_serial.setdefault(serial, []).append(key)
        return {serial: roles for serial, roles in by_serial.items() if len(roles) > 1}

    def _update_save_enabled(self) -> None:
        duplicates = self._duplicate_serial_roles()
        if duplicates:
            conflicts = []
            for serial, roles in duplicates.items():
                role_titles = " and ".join(ROLE_TITLES.get(r, r) for r in roles)
                conflicts.append(f"{role_titles} are both set to serial {serial}")
            self.conflict_label.setText(f"Can't save: {'; '.join(conflicts)}. Pick a different camera for each role.")
            self.conflict_label.show()
            self.save_button.setEnabled(False)
            return

        self.conflict_label.hide()

        omitted = self._omitted_instrument_roles()
        if omitted:
            titles = " and ".join(ROLE_TITLES.get(r, r) for r in omitted)
            was = "were" if len(omitted) > 1 else "was"
            self.omission_label.setText(
                f"{titles} has no camera selected and will be left out of the saved "
                f"configuration - students won't be offered it. If that camera is simply "
                f"not plugged in right now, connect it and press Rescan before saving; if "
                f"it {was} already configured, saving now removes it."
            )
            self.omission_label.show()
        else:
            self.omission_label.hide()

        self.save_button.setEnabled(self._can_save())

    def _omitted_instrument_roles(self) -> list[str]:
        return [key for key, row in self._instrument_rows.items() if row.selected_key() is None]

    def _can_save(self) -> bool:
        """Every instrument role used to be mandatory. It isn't any more: a
        room may have only one instrument, and a technician configuring a
        BIO-only machine shouldn't be blocked by a slit lamp that doesn't
        exist. What remains mandatory is that *something* records -- at
        least one instrument plus the third-person camera -- and that any
        role actually selected is completely specified.

        The gate that protects the student is unchanged and lives in
        kiosk.py: Start stays disabled until the selected instrument and the
        third-person camera are both confirmed live. This one only decides
        what a technician is allowed to write down.
        """
        if self._duplicate_serial_roles():
            return False
        if not self._third_person_row.is_valid():
            return False
        selected = [r for r in self._instrument_rows.values() if r.selected_key() is not None]
        return bool(selected) and all(r.is_valid() for r in selected)

    def _instrument_data(self, row: DeviceRow) -> dict:
        candidate = row.selected_candidate()
        # `label` is what students read on the picker -- the one field a
        # technician types. `profile` is stored beside it so reopening
        # Settings shows what was chosen, not just the name it produced.
        shared = {"label": row.label_text()}
        if row.profile_id() is not None:
            shared["profile"] = row.profile_id()

        if candidate is not None and candidate.kind == "net2860_winusb":
            # No serial and nothing to calibrate -- see config.py's
            # _parse_instrument.
            return {"kind": candidate.kind, **shared}

        data = {"kind": "ids", "serial": row.selected_key(), **shared}
        exposure_time_us, gain = row.calibration()
        if exposure_time_us is not None:
            data["exposure_time_us"] = exposure_time_us
        if gain is not None:
            data["gain"] = gain
        return data

    def _existing_config_dict(self) -> dict:
        """The current config.json as a raw dict, or {} if there's none or
        it doesn't parse. Save merges its edits onto this so config the UI
        has no field for -- `recording`, a per-instrument `orientation` /
        `pixel_clock_hz` override, an extra instrument role -- survives a
        Save instead of being silently dropped. See DECISIONS.md's
        2026-09-09 "Config that would only fail at Start" entry.
        """
        if not self.config_path.exists():
            return {}
        try:
            existing = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return existing if isinstance(existing, dict) else {}

    def _on_save_clicked(self) -> None:
        if not self._can_save():
            return

        base = self._existing_config_dict()
        prior_instruments = base.get("instruments")
        if not isinstance(prior_instruments, dict):
            prior_instruments = {}

        # Start from every instrument already in the file (keeps a third
        # role this two-row UI can't show), then overwrite the ones this UI
        # owns -- carrying each one's orientation / pixel_clock_hz override
        # forward, since there's no field for them here.
        instruments = dict(prior_instruments)
        for key, row in self._instrument_rows.items():
            if row.selected_key() is None:
                # Dropped rather than carried forward from the file: a role
                # left unselected means "this room doesn't have one", and
                # keeping a stale entry would leave students an instrument
                # the technician couldn't verify. _update_save_enabled()
                # warns before this point that it is about to happen.
                instruments.pop(key, None)
                continue
            entry = self._instrument_data(row)
            prior = prior_instruments.get(key)
            if entry.get("kind") == "ids" and isinstance(prior, dict):
                for carry in ("orientation", "pixel_clock_hz"):
                    if carry in prior and carry not in entry:
                        entry[carry] = prior[carry]
            instruments[key] = entry

        third_person_candidate = self._third_person_row.selected_candidate()
        data = dict(base)  # preserve unknown top-level keys, e.g. `recording`
        data["instruments"] = instruments
        data["third_person"] = {
            "kind": "uvc",
            "vid_pid": third_person_candidate.key,
            "friendly_name": third_person_candidate.source.name,
        }
        data["sessions_dir"] = self.sessions_dir_edit.text()

        self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.status_label.setText(f"Saved to {self.config_path}. Restart app.py to apply.")
        self.warning_label.hide()


@dataclass
class _SyntheticIdsDevice:
    """Shaped like ids_camera.IdsDeviceInfo, which is all _ids_candidates()
    reads."""

    serial: str
    model_name: str


class _SyntheticCalibratableCamera(SyntheticCamera):
    """A synthetic camera that also answers the questions PreviewDialog asks
    a real IDS one, so --synthetic exercises the sliders and Auto-Calibrate
    rather than the plain-preview branch."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._exposure_time_us = 8000.0
        self._gain = 1.0

    def supports_manual_calibration(self) -> bool:
        return True

    def get_exposure_time_us(self) -> float:
        return self._exposure_time_us

    def set_exposure_time_us(self, value: float) -> None:
        self._exposure_time_us = value

    def exposure_time_range_us(self) -> tuple[float, float]:
        return 20.0, 33_321.0

    def get_gain(self) -> float:
        return self._gain

    def set_gain(self, value: float) -> None:
        self._gain = value

    def gain_range(self) -> tuple[float, float]:
        return 1.0, 4.0

    def auto_calibrate(self, target_fps: float | None = None) -> bool:
        self._exposure_time_us, self._gain = 30_000.0, 1.4
        return True


def _synthetic_ids_devices() -> list[_SyntheticIdsDevice]:
    """Two cameras whose model strings match real profiles, plus one that
    matches none -- so the Custom path is reachable without unplugging
    anything."""
    return [
        _SyntheticIdsDevice(serial="4103484089", model_name="UI325xCP-C"),
        _SyntheticIdsDevice(serial="4110050487", model_name="U3-327xCP-C"),
        _SyntheticIdsDevice(serial="0000000001", model_name="Unlisted-Cam-1"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="fake cameras, for working on this UI with no hardware attached",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv[:1])
    app.setWindowIcon(QIcon(str(icon_path(ICON_SETTINGS))))
    if args.synthetic:
        # Same seams the tests use -- so what you click here is the real
        # window, not a stand-in for it.
        logger.info("synthetic mode: no camera is real, and Save writes a real config.json")
        window = SettingsWindow(
            list_ids_devices_fn=_synthetic_ids_devices,
            list_uvc_devices_fn=lambda: [
                UvcDeviceInfo(index=0, name="Synthetic webcam", vid_pid="32E4:9310")
            ],
            list_net2860_winusb_fn=lambda: [("synthetic-legacy-bio", "synthetic-path")],
            instrument_preview_camera_factory=lambda c: _SyntheticCalibratableCamera(
                640, 480, fps=30, name=str(c.display_name)
            ),
            uvc_preview_camera_factory=lambda c: SyntheticCamera(640, 480, fps=30, name="third-person"),
        )
    else:
        window = SettingsWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
