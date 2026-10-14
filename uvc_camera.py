"""UVC webcam implementation of BaseCamera, for the third-person view
(ELP-USB100W03M-L21) -- an ordinary UVC device via OpenCV's
cv2.VideoCapture, not a GenICam machine vision camera. See CLAUDE.md's
Hardware table.

Three deliberate deviations from ids_camera.py's contract, all accepted --
see DECISIONS.md's "Third-person UVC camera" entry for why:

- Frame.index is a locally-assigned counter, not a source-reported
  sequence number, since UVC/DirectShow exposes no such counter. This
  camera can't detect true source-side drops the way IdsCamera can; only
  consumer-side queue evictions show up in session.json's dropped_frames.
- Two identification modes, exactly one used per instance (constructor
  raises ValueError otherwise):
  - `device`: open this literal DirectShow index directly, no resolution.
    Used by settings.py's Preview, which wants to open exactly the
    highlighted dropdown entry.
  - `vid_pid`: resolved to an index via uvc_enumeration.resolve_device()
    inside _open() -- not at construction time -- so a camera that isn't
    attached yet when this object is constructed falls into BaseCamera's
    normal start()-fails/retry flow (see camera.py: start() only creates
    the capture thread after _open() succeeds, so a raise here leaves
    _thread as None and the next start() call retries from scratch) rather
    than raising once, synchronously, before any retry loop exists. Used
    by app.py's real runtime path. A resulting UvcDeviceResolutionError
    propagates unwrapped -- it's a distinct failure ("couldn't decide
    which device") from UvcCameraNotFoundError ("knew which device, cv2
    couldn't open it"), and callers already just stringify whichever one
    they get. See DECISIONS.md's "UVC third-person identity moves to
    VID/PID" entry.
- Autofocus/auto-exposure are let run for a short fixed warmup window at
  open, then turned off (best-effort), rather than polled until converged
  the way ids_camera.py's ExposureAuto/GainAuto "Once" mode is. UVC via
  cv2.VideoCapture exposes no "has it converged yet" signal to poll --
  see _configure_capture()'s docstring. Warmup frame/timeout constants
  below are a starting guess, unverified against real hardware as of
  2026-08-18 -- see DECISIONS.md's "Lock UVC autofocus/auto-exposure
  after a warmup window" entry.

Also caps acquisition rate (_apply_frame_rate_cap(), verified cv2.CAP_PROP_FPS
constant) and enables backlight compensation (verified cv2.CAP_PROP_BACKLIGHT
constant) -- see DECISIONS.md's 2026-08-26 entry. Both are best-effort, same
as the autofocus/exposure lock: a device that doesn't support a given
control just silently keeps its own default.

Also reopens the device by itself if it stops delivering frames mid-stream
(_try_reconnect()) -- cv2.VideoCapture doesn't recover on its own from a
USB power-management drop / another app grabbing the camera / a brief
unplug. See DECISIONS.md's "UVC camera reconnects itself" entry.
"""

from __future__ import annotations

import logging
import math
import threading
import time

import cv2
import numpy as np

import uvc_enumeration
from camera import BaseCamera, ControlAutoError, ControlSpec
from exposure_calibration import (
    DEFAULT_TARGET_MEDIAN,
    DEFAULT_TOLERANCE,
    MIN_COLOUR_PIXELS,
    center_crop,
    colour_balance,
    focus_measure,
    median_brightness,
)

logger = logging.getLogger(__name__)

# How long to let autofocus/auto-exposure run before locking them --
# mirrors ids_camera.py's exposure-convergence intent (adapt to the real
# scene once, then stop adjusting so the image doesn't visibly drift or
# hunt mid-recording) but can't poll for an actual "converged" signal the
# way GenICam's ExposureAuto/GainAuto nodes allow -- see module docstring.
_AUTOEXPOSURE_WARMUP_FRAMES = 10
_AUTOEXPOSURE_WARMUP_TIMEOUT_S = 2.0

# How far cap.get(CAP_PROP_FPS) is allowed to drift from the requested
# target_fps before _apply_frame_rate_cap() logs a mismatch -- some UVC
# devices only support fixed fps steps per resolution and silently pick the
# nearest one rather than the exact value requested.
_FPS_MISMATCH_LOG_TOLERANCE = 0.5

# Mid-stream reconnect. cv2.VideoCapture doesn't recover on its own when a
# UVC device drops (USB power management, another app grabbing it, a brief
# unplug) -- read() just returns (False, None) forever. Without this a
# transient glitch leaves a permanently dead pane, discovered late; with it
# the feed heals itself. See DECISIONS.md's "UVC camera reconnects itself"
# entry, and CLAUDE.md on why "a replugged camera shows black" is the
# outcome to avoid.
#
# Consecutive failed reads before treating it as a device drop rather than
# a single skipped frame (~0.5s at 30fps):
_RECONNECT_AFTER_FAILURES = 15
# Minimum gap between reopen attempts, so a device that's genuinely gone
# isn't hammered:
_RECONNECT_COOLDOWN_S = 1.0
# Small yield after a failed read so the capture thread doesn't spin a core
# while the device is down:
_FAILURE_BACKOFF_S = 0.03


class UvcCameraNotFoundError(RuntimeError):
    """The requested device index/path could not be opened."""


# The technician's levers on this webcam (camera.ControlSpec). DirectShow
# reports no ranges through OpenCV, so they are probed at open -- and the
# drivers disagree on how to say "out of range": one clamps every write and
# reports the clamped value, another refuses anything outside its range or
# off its step. _probe_range() handles both. Measured 2026-10-14 on a
# laptop's integrated webcam, not yet on the clinic's ELP.
_EXPOSURE = "exposure"
_GAIN = "gain"
_WHITE_BALANCE = "white_balance"
_FOCUS = "focus"
_GROUP_EXPOSURE = "Exposure"
_GROUP_COLOUR = "White balance"
_GROUP_FOCUS = "Focus"
_GROUPS = {_EXPOSURE: _GROUP_EXPOSURE, _GAIN: _GROUP_EXPOSURE, _WHITE_BALANCE: _GROUP_COLOUR, _FOCUS: _GROUP_FOCUS}
# Guesses wide enough to include any UVC device's range; the probe narrows
# them. Exposure is DirectShow's log2 seconds: -5 is 1/32 s.
_PROBE_LIMITS = {
    _EXPOSURE: (cv2.CAP_PROP_EXPOSURE, -14, 0),
    _GAIN: (cv2.CAP_PROP_GAIN, 0, 1024),
    _WHITE_BALANCE: (cv2.CAP_PROP_WB_TEMPERATURE, 1000, 12000),
    _FOCUS: (cv2.CAP_PROP_FOCUS, 0, 1023),
}
_STEPS_TO_TRY = (1, 2, 5, 10, 25, 50, 100)
# Frames to wait after a write before judging the picture: DirectShow keeps
# a frame or two in flight, and the sensor needs one more to expose.
_SETTLE_FRAMES = 4
_SETTLE_TIMEOUT_S = 2.0
# Default, for the levers the webcam can run itself: let it, then lock --
# the same two seconds the open-time warmup gives it.
_DEFAULT_RUN_S = 2.0


def _exposure_budget_log2(fps: float | None) -> int:
    """The longest DirectShow exposure step that fits one frame at `fps`."""
    return int(math.floor(math.log2(1.0 / (fps or 30.0))))


class UvcCamera(BaseCamera):
    def __init__(
        self,
        device: int | str | None = None,
        vid_pid: str | None = None,
        name: str = "third-person",
        queue_size: int = 2,
        target_fps: float | None = None,
        controls: dict[str, float] | None = None,
        probe_controls: bool = False,
    ):
        if (device is None) == (vid_pid is None):
            raise ValueError("UvcCamera requires exactly one of device or vid_pid")
        super().__init__(queue_size=queue_size, label=name)
        self._device = device
        self._vid_pid = vid_pid
        self._cap: cv2.VideoCapture | None = None
        self._width = 0
        self._height = 0
        self._counter = 0
        self._consecutive_failures = 0
        self._last_reconnect_attempt = 0.0
        # Caps this camera's own acquisition rate -- see
        # _apply_frame_rate_cap()'s docstring. None means untouched
        # free-run, e.g. settings.py's Preview cameras, which deliberately
        # never pass this.
        self._target_fps = target_fps
        # The technician's values (config.json third_person.controls),
        # reapplied on every open and reconnect. An absent key leaves that
        # lever to the webcam's own automatic behaviour.
        self._controls: dict[str, float] = dict(controls or {})
        # Ranges, probed at open only when asked (settings.py's Preview):
        # the kiosk has no slider to size, and the driver clamps or refuses
        # a stored value itself.
        self._probe = probe_controls
        self._ranges: dict[str, tuple[float, float, float]] = {}
        # Writes from the UI thread, applied by the capture thread between
        # reads: cv2.VideoCapture is not shared across threads here.
        self._writes_lock = threading.Lock()
        self._pending_writes: list[tuple[int, float]] = []
        self._writes_applied_at = 0

    @property
    def resolution(self) -> tuple[int, int]:
        return (self._width, self._height)

    def _resolve_device_index(self) -> int | str:
        if self._device is not None:
            return self._device
        return uvc_enumeration.resolve_device(self._vid_pid).index

    def _open_capture(self, *, warmup: bool, want_size: tuple[int, int] | None = None) -> cv2.VideoCapture:
        """Open and configure a VideoCapture for the current device, or
        raise UvcCameraNotFoundError. Shared by _open() and the mid-stream
        reconnect path -- reconnect passes warmup=False, since the scene
        hasn't changed and a 2s autofocus/exposure re-warmup would just
        stall the capture thread.

        want_size: the reconnect path passes the resolution the first open
        settled on, so a device that would otherwise reopen at its
        DirectShow default keeps a frame size the recorder's fixed-size
        encoder can accept. Requested before _configure_capture(), since a
        resolution change can force a stream renegotiation. Best-effort --
        the caller checks what actually came back.
        """
        device = self._resolve_device_index()
        cap = cv2.VideoCapture(device, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            raise UvcCameraNotFoundError(f"could not open UVC device {device!r}")

        if want_size is not None:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, want_size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, want_size[1])
        if self._target_fps is not None:
            self._apply_frame_rate_cap(cap)
        self._configure_capture(cap, warmup=warmup)
        return cap

    def _open(self) -> None:
        cap = self._open_capture(warmup=True)

        # Queried rather than hardcoded -- CLAUDE.md's Conventions section:
        # measured values, not datasheet numbers, and this device's actual
        # resolution isn't known until it's opened.
        self._width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self._height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if not (self._width and self._height):
            # Some DirectShow drivers don't report a frame size until a
            # frame has actually been pulled. The frame itself is ground
            # truth, so ask it before giving up.
            ok, image = cap.read()
            if ok and image is not None:
                self._height, self._width = image.shape[:2]
        if self._width < 2 or self._height < 2:
            # Refuse rather than hand the recorder a nonsense resolution:
            # it would size its encoder from this and produce a
            # "successful", verified recording of a couple of pixels --
            # broken in exactly the way nobody notices until playback.
            # Raising here puts it in BaseCamera's normal start()-fails
            # path, so app.py retries and the status line says why.
            cap.release()
            raise UvcCameraNotFoundError(
                f"UVC device {self._device or self._vid_pid!r} reported an unusable "
                f"frame size ({self._width}x{self._height})"
            )
        self._counter = 0
        self._consecutive_failures = 0
        self._last_reconnect_attempt = 0.0
        if self._probe:
            self._ranges = self._probe_ranges(cap)
        self._cap = cap

    def _apply_frame_rate_cap(self, cap: cv2.VideoCapture) -> None:
        """Caps this camera's own acquisition rate to (not above) target_fps
        -- same bandwidth/no-benefit reasoning as ids_camera.py's method of
        the same name (recorder.py's _drain_latest() discards anything
        captured faster than recording.fps anyway). Called before
        _configure_capture() since changing fps can force a brief stream
        renegotiation on some UVC devices, and the autofocus/exposure
        warmup that follows depends on a stable stream.

        Best-effort: cv2.VideoCapture.set() returning False (or silently
        picking the nearest supported step) just means this device doesn't
        support the exact rate requested -- logged, not raised, the same
        as every other cap.set() call in this file.
        """
        cap.set(cv2.CAP_PROP_FPS, self._target_fps)
        actual = cap.get(cv2.CAP_PROP_FPS)
        if actual and abs(actual - self._target_fps) > _FPS_MISMATCH_LOG_TOLERANCE:
            logger.warning(
                "%s: requested %.1ffps, device reports %.1ffps", self.label, self._target_fps, actual
            )

    def _configure_capture(self, cap: cv2.VideoCapture, warmup: bool = True) -> None:
        """Backlight compensation, then let autofocus/auto-exposure settle
        against whatever this camera actually sees, then turn both off --
        otherwise a UVC webcam's driver defaults typically keep both
        continuously active, which can visibly hunt (refocus, re-expose)
        mid-recording, exactly the distracting-artifact problem
        ids_camera.py's `Once`-not-Continuous exposure convergence was
        built to avoid for the two instrument cameras (see DECISIONS.md).
        Never fails camera open: cv2.VideoCapture.set() returning False
        just means this device doesn't support that control, the same way
        ids_camera.py's IsAvailable() guard skips an axis the hardware
        doesn't expose.

        Backlight compensation is enabled first, before the warmup loop,
        so the auto-exposure convergence the warmup lets run happens
        *with* it already active -- it changes what auto-exposure is
        compensating for, so it needs to be in effect during, not after,
        that convergence.

        Fixed warmup window, not poll-until-converged: unlike GenICam's
        ExposureAuto/GainAuto nodes (which report back "Off" once `Once`
        mode finishes), UVC/DirectShow via cv2.VideoCapture exposes no
        readable "has this converged yet" signal to poll. See
        DECISIONS.md for why the specific numbers below are a starting
        guess, not a measured value.

        `warmup=False` (the mid-stream reconnect path) skips the read loop:
        the scene is unchanged from moments ago and the capture thread
        can't afford a 2s stall there.
        """
        cap.set(cv2.CAP_PROP_BACKLIGHT, 1)

        if warmup:
            deadline = time.monotonic() + _AUTOEXPOSURE_WARMUP_TIMEOUT_S
            for _ in range(_AUTOEXPOSURE_WARMUP_FRAMES):
                if time.monotonic() > deadline:
                    break
                cap.read()

        cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
        self._apply_stored_controls(cap)

    def _apply_stored_controls(self, cap: cv2.VideoCapture) -> None:
        """The technician's levers, after the warmup: a stored exposure
        replaces what auto-exposure reached, a stored white balance stops the
        webcam's continuous auto, a stored focus replaces the autofocus.
        On every open and every reconnect -- the app holds these values, not
        the webcam."""
        if _EXPOSURE in self._controls:
            cap.set(cv2.CAP_PROP_EXPOSURE, self._controls[_EXPOSURE])
        if _GAIN in self._controls:
            cap.set(cv2.CAP_PROP_GAIN, self._controls[_GAIN])
        if _WHITE_BALANCE in self._controls:
            cap.set(cv2.CAP_PROP_AUTO_WB, 0)
            if not cap.set(cv2.CAP_PROP_WB_TEMPERATURE, self._controls[_WHITE_BALANCE]):
                logger.warning("%s: white balance %.0f K refused by the webcam", self.label,
                               self._controls[_WHITE_BALANCE])
        if _FOCUS in self._controls:
            cap.set(cv2.CAP_PROP_FOCUS, self._controls[_FOCUS])

    # ------------------------------------------------------------ controls

    def _probe_ranges(self, cap: cv2.VideoCapture) -> dict[str, tuple[float, float, float]]:
        """(minimum, maximum, step) for each lever this webcam has, put back
        as it was afterwards. A lever whose range is a single value (a
        fixed-gain sensor) or that refuses every write is left out."""
        ranges = {}
        for key, (prop, low, high) in _PROBE_LIMITS.items():
            current = cap.get(prop)
            if current == -1:
                continue  # DirectShow's "no such control"
            try:
                found = _probe_range(cap, prop, low, high, current)
            finally:
                cap.set(prop, current)
            if found is not None and found[1] > found[0]:
                ranges[key] = found
        if _EXPOSURE in ranges:
            low, high, step = ranges[_EXPOSURE]
            # Exposure is a frame-rate budget (CLAUDE.md): never offer more
            # than a frame's worth.
            ranges[_EXPOSURE] = (low, min(high, max(low, _exposure_budget_log2(self._target_fps))), step)
        return ranges

    _LABELS = {_EXPOSURE: "Exposure", _GAIN: "Gain", _WHITE_BALANCE: "Colour temperature", _FOCUS: "Focus"}
    _UNITS = {_EXPOSURE: "log2 s", _GAIN: "", _WHITE_BALANCE: "K", _FOCUS: ""}
    _HINTS = {
        _GROUP_EXPOSURE: "Have the hands and the instrument in view, as during a session.",
        _GROUP_COLOUR: "Hold something white or grey in front of the camera first.",
        _GROUP_FOCUS: "Have the hands where they will be during a session.",
    }

    def controls(self) -> list[ControlSpec]:
        specs = []
        for key, (low, high, step) in self._ranges.items():
            value = self._controls.get(key)
            if value is None and self._cap is not None:
                value = self._cap.get(_PROBE_LIMITS[key][0])
            value = low if value is None else min(high, max(low, value))
            group = _GROUPS[key]
            specs.append(ControlSpec(
                key=key, label=self._LABELS[key], minimum=low, maximum=high, step=step, value=value,
                group=group, auto=True, auto_hint=self._HINTS[group], unit=self._UNITS[key],
            ))
        return specs

    def set_control(self, key: str, value: float) -> float:
        if key not in _PROBE_LIMITS:
            raise KeyError(key)
        if key in self._ranges:
            low, high, step = self._ranges[key]
            value = min(high, max(low, low + round((value - low) / step) * step))
        self._controls[key] = float(value)
        writes = [(_PROBE_LIMITS[key][0], value)]
        if key == _EXPOSURE:
            writes.insert(0, (cv2.CAP_PROP_AUTO_EXPOSURE, 0.25))
        elif key == _WHITE_BALANCE:
            writes.insert(0, (cv2.CAP_PROP_AUTO_WB, 0))
        elif key == _FOCUS:
            writes.insert(0, (cv2.CAP_PROP_AUTOFOCUS, 0))
        self._queue_writes(writes)
        return float(value)

    def _queue_writes(self, writes: list[tuple[int, float]]) -> None:
        with self._writes_lock:
            self._pending_writes.extend(writes)

    def _apply_pending_writes(self) -> None:
        """On the capture thread, between reads."""
        with self._writes_lock:
            writes, self._pending_writes = self._pending_writes, []
        if not writes or self._cap is None:
            return
        for prop, value in writes:
            self._cap.set(prop, value)
        self._writes_applied_at = self._counter

    def _settled_frame(self) -> np.ndarray:
        """The first frame exposed after the last queued writes landed."""
        deadline = time.monotonic() + _SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            with self._writes_lock:
                pending = bool(self._pending_writes)
            if not pending and self._counter >= self._writes_applied_at + _SETTLE_FRAMES:
                frame = self.get_latest()
                if frame is not None:
                    return frame.image
            time.sleep(0.01)
        raise ControlAutoError("The webcam stopped sending pictures. Check its cable and try again.")

    def _try(self, key: str, value: float) -> np.ndarray:
        self.set_control(key, value)
        return self._settled_frame()

    def auto_controls(self, group: str) -> dict[str, float]:
        if group == _GROUP_EXPOSURE and _EXPOSURE in self._ranges:
            return self._auto_exposure()
        if group == _GROUP_COLOUR and _WHITE_BALANCE in self._ranges:
            return self._auto_white_balance()
        if group == _GROUP_FOCUS and _FOCUS in self._ranges:
            return self._auto_focus()
        raise KeyError(group)

    def _auto_exposure(self) -> dict[str, float]:
        """Longest exposure that fits a frame and doesn't overshoot the
        target, then gain only if that is still too dark -- the same order
        as the instrument cameras' Auto-Calibrate, for the same reason:
        gain is noise."""
        low, high, step = self._ranges[_EXPOSURE]
        result = {}
        if _GAIN in self._ranges:
            result[_GAIN] = self.set_control(_GAIN, self._ranges[_GAIN][0])
        best, best_error = high, None
        exposure = high
        while exposure >= low:
            measured = median_brightness(center_crop(self._try(_EXPOSURE, exposure)))
            error = measured - DEFAULT_TARGET_MEDIAN
            if best_error is None or abs(error) < abs(best_error):
                best, best_error = exposure, error
            if error <= DEFAULT_TOLERANCE:
                break  # every shorter step is darker still
            exposure -= step
        result[_EXPOSURE] = self.set_control(_EXPOSURE, best)
        if best == high and best_error is not None and best_error < -DEFAULT_TOLERANCE and _GAIN in self._ranges:
            result[_GAIN] = self._bisect(
                _GAIN, lambda image: median_brightness(center_crop(image)) - DEFAULT_TARGET_MEDIAN
            )
        self._settled_frame()
        return result

    def _auto_white_balance(self) -> dict[str, float]:
        """The colour temperature at which the picture's usable pixels
        average grey: red and blue equal. Blue-minus-red moves one way as
        the setting rises, whichever way a driver has it."""
        def imbalance(image: np.ndarray) -> float:
            red, blue, count = colour_balance(image)
            if count < MIN_COLOUR_PIXELS:
                raise ControlAutoError(
                    "Too dark or too bright to judge colour. Set the exposure first, "
                    "then hold something white or grey in front of the camera."
                )
            return blue - red

        return {_WHITE_BALANCE: self._bisect(_WHITE_BALANCE, imbalance)}

    def _auto_focus(self) -> dict[str, float]:
        """A coarse sweep for the sharpest middle of the picture, then a
        fine one around it."""
        low, high, step = self._ranges[_FOCUS]
        coarse = max(step, (high - low) / 12.0)
        best = max(_frange(low, high, coarse), key=lambda v: focus_measure(self._try(_FOCUS, v)))
        fine = [v for v in _frange(best - coarse, best + coarse, max(step, coarse / 6.0)) if low <= v <= high]
        best = max(fine, key=lambda v: focus_measure(self._try(_FOCUS, v)))
        return {_FOCUS: self.set_control(_FOCUS, best)}

    def _bisect(self, key: str, error_of) -> float:
        """The lever value where `error_of(frame)` crosses zero, assuming it
        moves one way across the range. If it never crosses, the end that
        came closest."""
        low, high, step = self._ranges[key]
        e_low = error_of(self._try(key, low))
        e_high = error_of(self._try(key, high))
        if (e_low > 0) == (e_high > 0):
            return self.set_control(key, low if abs(e_low) < abs(e_high) else high)
        sign_low = e_low > 0
        while high - low > step:
            middle = low + round((high - low) / 2.0 / step) * step
            if not low < middle < high:
                break
            if (error_of(self._try(key, middle)) > 0) == sign_low:
                low = middle
            else:
                high = middle
        return self.set_control(key, low)

    def reset_controls(self, group: str) -> None:
        """Back to what the webcam does on its own: auto-exposure and
        autofocus run for two seconds then lock, as at open; white balance
        goes back to the webcam's continuous auto."""
        keys = [key for key, g in _GROUPS.items() if g == group]
        if not keys:
            raise KeyError(group)
        for key in keys:
            self._controls.pop(key, None)
        if group == _GROUP_EXPOSURE:
            self._queue_writes([(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)])
            time.sleep(_DEFAULT_RUN_S)
            self._queue_writes([(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)])
        elif group == _GROUP_COLOUR:
            self._queue_writes([(cv2.CAP_PROP_AUTO_WB, 1)])
        elif group == _GROUP_FOCUS:
            self._queue_writes([(cv2.CAP_PROP_AUTOFOCUS, 1)])
            time.sleep(_DEFAULT_RUN_S)
            self._queue_writes([(cv2.CAP_PROP_AUTOFOCUS, 0)])

    def _close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _grab(self) -> tuple[np.ndarray, float, int] | None:
        self._apply_pending_writes()
        ok, image = self._cap.read() if self._cap is not None else (False, None)
        if ok and image is not None:
            self._consecutive_failures = 0
            timestamp = time.monotonic()
            index = self._counter
            self._counter += 1
            return image, timestamp, index

        # Read failed. A handful in a row is a skipped frame; a sustained
        # run means the device dropped -- try to reopen it (see
        # _RECONNECT_AFTER_FAILURES).
        self._consecutive_failures += 1
        if self._consecutive_failures >= _RECONNECT_AFTER_FAILURES:
            self._try_reconnect()
        else:
            time.sleep(_FAILURE_BACKOFF_S)
        return None

    def _try_reconnect(self) -> None:
        """Best-effort reopen of a dropped UVC device, rate-limited to one
        attempt per _RECONNECT_COOLDOWN_S. Returns without raising on
        failure -- the capture thread keeps calling _grab(), so the next
        attempt happens on its own once the cooldown elapses.
        """
        if self._stop_event.is_set():
            return
        now = time.monotonic()
        if now - self._last_reconnect_attempt < _RECONNECT_COOLDOWN_S:
            time.sleep(_FAILURE_BACKOFF_S)
            return
        self._last_reconnect_attempt = now

        logger.warning(
            "%s: no frames from UVC device for %d reads; reopening",
            self.label,
            self._consecutive_failures,
        )
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        # self.resolution is fixed after the first _open() and the
        # recorder's encoder is sized from it, so ask a re-enumerated device
        # for the same frame size back rather than let it reopen at its
        # DirectShow default. The recorder resizes anything that still
        # doesn't match as a backstop.
        want_size = (self._width, self._height) if self._width and self._height else None
        try:
            cap = self._open_capture(warmup=False, want_size=want_size)
        except Exception as exc:
            logger.warning("%s: UVC reopen failed, will retry: %s", self.label, exc)
            return

        new_size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        if all(new_size) and new_size != (self._width, self._height):
            logger.warning(
                "%s: reopened at %dx%d, could not restore %dx%d; the recorder will resize to fit",
                self.label,
                *new_size,
                self._width,
                self._height,
            )
        self._cap = cap
        self._consecutive_failures = 0
        logger.info("%s: UVC device reopened", self.label)


def _frange(start: float, stop: float, step: float) -> list[float]:
    values, value = [], start
    while value <= stop + 1e-9:
        values.append(value)
        value += step
    return values


def _probe_range(cap: cv2.VideoCapture, prop: int, low: float, high: float, current: float):
    """(minimum, maximum, step) a driver accepts for `prop`, or None.

    The step first, from the current value: the smallest of _STEPS_TO_TRY
    either side of it that the driver takes and reads back exactly -- a
    refusing driver rejects anything off its grid (2799 K and 4555 K, where
    2800 K and 6500 K were taken). Then each limit: a clamping driver
    accepts the guess and reads back its own limit; a refusing one rejects
    it, so the limit is bisected on that grid toward the current value."""
    if not cap.set(prop, current):
        return None
    current = cap.get(prop)

    def takes(value: float) -> bool:
        return bool(cap.set(prop, value)) and abs(cap.get(prop) - value) < 1e-6

    step = next((s for s in _STEPS_TO_TRY if takes(current - s) or takes(current + s)), None)
    if step is None:
        return None

    def limit(guess: float) -> float:
        if cap.set(prop, guess):
            return cap.get(prop)
        direction = 1.0 if guess > current else -1.0
        good, bad = 0, int(math.ceil(abs(guess - current) / step))
        while bad - good > 1:
            middle = (good + bad) // 2
            if cap.set(prop, current + direction * middle * step):
                good = middle
            else:
                bad = middle
        return current + direction * good * step

    return float(limit(low)), float(limit(high)), float(step)
