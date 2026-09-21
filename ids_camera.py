"""IDS peak GenICam camera implementation of BaseCamera.

One class covers both real cameras (Haag-Streit slit lamp UI-3250CP-C-HQ,
via the uEye Transport Layer, and Keeler U3-327xCP-C, native USB3 Vision).

The stack, simply: a GenICam camera publishes its own feature list (a "node
map"), IDS peak finds cameras and moves buffers, and a *transport layer*
per camera family puts each camera on that one DeviceManager list -- and
decides which features reach us at all. Both are therefore ordinary GenICam
devices here, differing only by the serial passed in (SETUP.md Section 3
installs the uEye layer) -- but not by feature set: the uEye layer
publishes no ExposureAuto/GainAuto/BalanceWhiteAuto, which is why every
optional node below goes through TryFindNode() and why the slit lamp
depends on auto_calibrate() rather than anything on the camera.

What this module uses, in order: Library.Initialize(); DeviceManager to
find the serial; OpenDevice(Control) to claim it; the RemoteDevice node map
for every setting; one DataStream fed with buffers we allocate and queue;
then WaitForFinishedBuffer() -> Buffer (FrameID plus pixels), which
ids_peak_ipl converts from Bayer to BGR8. _open()'s comments say why its
order is what it is; the order is load-bearing.

See CLAUDE.md's Architecture section: nothing outside this module may
import ids_peak/ids_peak_ipl. What each camera's stack publishes, and what
each pixel format would cost, is IMAGING.md; tools/probe_camera_features.py
re-answers it against attached hardware.

Cameras are native Bayer sensors; frames are converted to BGR8 here so
every consumer downstream of BaseCamera (compositor, recorder, preview)
keeps working against Frame.image unmodified. Source pixel format is read
from each captured buffer rather than assumed, since the two camera
models may use different Bayer patterns.

Frame.index is each buffer's own Buffer.FrameID(), not a locally-assigned
counter -- required by BaseCamera._grab's contract so a consumer draining
frames via read() can detect real gaps (including ones the device itself
introduced) rather than a renumbering that's gapless by construction. See
DECISIONS.md's "Frame.index was never actually gap-detectable" entry.

Hardware-verified against both real cameras via tools/smoke_test_camera.py
as of 2026-08-12 -- Keeler (U3-327xCP-C, serial 4110050487) and slit lamp
(UI325xCP-C, uEye Transport Layer, serial 4103484089). See DECISIONS.md's
two "Hardware smoke test" entries for what each surfaced and fixed. Known
platform difference between them, both handled: the uEye Transport Layer
doesn't implement DataStream.PayloadSize() (_payload_size() falls back to
the NodeMap) and has no ExposureAuto/GainAuto (_converge_auto_nodes()
skips both gracefully) -- the slit lamp camera needs a one-time
exposure/gain calibration once mounted on the instrument. That calibration
is done in-app now (supports_manual_calibration()/auto_calibrate()/the
manual get_/set_exposure_time_us()/get_/set_gain() pair below, driven by
settings.py's PreviewDialog), not via an external tool like IDS peak
Cockpit -- see DECISIONS.md's 2026-08-25 calibration entry.
Since 2026-09-10 that calibration is offered for *any* camera whose
ExposureTime/Gain a technician can write, not only one with no
ExposureAuto/GainAuto -- see supports_manual_calibration() for why the
Keeler's converge-at-open turned out to be the wrong thing to rely on.

The acquisition frame-rate cap (_apply_frame_rate_cap()) follows the same
in-app-not-external-tool philosophy, added per DECISIONS.md's 2026-08-26
entry. White balance is deliberately not ours: the Keeler converges its own
once at open and the slit lamp exposes no white-balance node at all, so
there is nothing here to set -- see DECISIONS.md's 2026-09-11 entry. `_converge_auto_nodes()` generalizes what used to be a single
exposure/gain-specific convergence loop (`_converge_auto_exposure()`) to
also cover `BalanceWhiteAuto`, since all three follow the identical
Once-then-poll-until-Off-then-lock shape.

Hardware-verified against both real cameras again on 2026-09-08, which
closed most of the "unverified" notes this module used to carry:
`_converge_auto_nodes()`, `exposure_time_range_us()`/`gain_range()`'s
Minimum()/Maximum() accessors, `_apply_frame_rate_cap()`,
`auto_calibrate()`, and the two additions below -- `_apply_pixel_clock()`
and `_apply_auto_exposure_limit()`. Both cameras now deliver the
configured 30fps with no dropped frames; see DECISIONS.md's 2026-09-08
entries for what that took.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np
from ids_peak import ids_peak
from ids_peak_ipl import ids_peak_ipl

from camera import BaseCamera
from device_presets import (
    black_level_for_model,
    floor_model_for_model,
    gamma_for_model,
    metering_for_model,
    pixel_format_for_model,
    orientation_for_model,
    pixel_clock_hz_for_model,
)
from tone_curve import (
    ToneCurve,
    build_luts,
    floor_slope_for_noise,
    max_gain_for_noise,
    raw_to_bgr8,
)
from exposure_calibration import (
    DEFAULT_MAX_ITERATIONS,
    center_crop,
    exposure_budget_us,
    is_converged,
    metering_brightness,
    metering_target,
    next_exposure_gain,
)

logger = logging.getLogger(__name__)

# Comfortably under BaseCamera.stop()'s 2.0s thread-join timeout, so a
# stop() call isn't left waiting on a blocked _grab().
_ACQUISITION_TIMEOUT_MS = 1000

# Buffers queued with the driver at any time. NumBuffersAnnouncedMinRequired
# is a per-device minimum; padding it gives the driver room to keep filling
# buffers while one is being converted here.
_MIN_BUFFER_COUNT = 4

# Wall-clock and frame-count bounds on the one-time auto-convergence pass
# (exposure/gain/white-balance, whichever axes lack a manually-calibrated
# config value) in _open(). Measured on real hardware (Keeler, serial
# 4110050487) at ~20fps: exposure/gain convergence took ~10 frames (~0.5s).
# Both bounds are generous multiples of that so a genuinely stuck
# convergence still fails loudly within a few seconds rather than hanging
# start(). Not re-measured with BalanceWhiteAuto added to the same pass --
# revisit if it turns out to need materially longer.
_AUTO_CONVERGE_TIMEOUT_S = 5.0
_AUTO_CONVERGE_MAX_FRAMES = 150

# auto_calibrate()'s per-iteration settle/read bounds. A frame already
# queued when ExposureTime/Gain just changed was captured under the
# *previous* setting -- this is how long to wait before trusting the next
# one read() returns to reflect the new value. Not yet measured against
# real hardware (no camera attached to this dev machine -- see CLAUDE.md's
# Environment section); revisit against tools/smoke_test_camera.py once
# hardware is available, the same way _AUTO_CONVERGE_TIMEOUT_S above was.
_CALIBRATION_SETTLE_S = 0.2
_CALIBRATION_FRAME_TIMEOUT_S = 1.0


class IdsCameraNotFoundError(RuntimeError):
    """No device with the requested serial number is present."""


class IdsCameraConvergenceTimeoutError(RuntimeError):
    """A one-time Once-mode auto-convergence pass (exposure/gain/white
    balance) didn't finish in time."""


class IdsCameraCalibrationError(RuntimeError):
    """auto_calibrate() couldn't get a live frame to measure."""


@dataclass
class IdsDeviceInfo:
    serial: str
    model_name: str


def list_ids_devices() -> list[IdsDeviceInfo]:
    """Currently-attached IDS peak GenICam devices, for settings.py's
    instrument-role dropdowns. Empty list is the correct, expected result
    with none attached -- same "0 found is fine" precedent as SETUP.md's
    verification script, which this mirrors exactly (including bracketing
    Library.Initialize()/Close() itself: unlike _open_device() below,
    which assumes _open() already did that around the whole camera
    lifecycle, this is a standalone one-shot scan with no camera object
    involved).
    """
    ids_peak.Library.Initialize()
    try:
        device_manager = ids_peak.DeviceManager.Instance()
        device_manager.Update()
        return [
            IdsDeviceInfo(serial=d.SerialNumber(), model_name=d.ModelName()) for d in device_manager.Devices()
        ]
    finally:
        ids_peak.Library.Close()


class IdsCamera(BaseCamera):
    """A single IDS peak GenICam device, opened by serial number.

    CLAUDE.md: cameras are identified by serial number, never device
    index — index order changes across reboots and USB port changes.
    """

    def __init__(
        self,
        serial: str,
        queue_size: int = 2,
        exposure_time_us: float | None = None,
        gain: float | None = None,
        target_fps: float | None = None,
        orientation: str | None = None,
        pixel_clock_hz: int | None = None,
        black_level: float | None = None,
        binning: int | None = None,
        pixel_format: str | None = None,
        gamma: float | None = None,
        converge_auto: bool = True,
    ):
        super().__init__(queue_size=queue_size, label=serial, orientation=orientation)
        self._serial = serial
        # None (the default) means "resolve from the device-model preset in
        # _open()" -- e.g. the Keeler BIO camera delivers a vertically-
        # flipped image. An explicit value from config.json's `orientation`
        # is passed through instead and wins over the preset. See
        # device_presets.py.
        self._model_name: str | None = None
        # None means "use device_presets' per-model value" -- the same
        # preset-with-escape-hatch shape as `orientation`. Overridable
        # because the *safe* clock depends on the host USB controller,
        # which is per-install. See _apply_pixel_clock().
        self._pixel_clock_hz = pixel_clock_hz
        # Sums an NxN block of photosites into one pixel: N^2 the light for
        # 1/N^2 the pixels. Like the pixel clock, it does NOT persist across
        # opens on the uEye transport layer, so it is set here every time --
        # and before Width/Height are read, since it changes them.
        self._black_level = black_level
        # Set by set_brightness() and reapplied at every open, since a
        # camera that was restarted (switching instruments) must come back
        # the way the student left it.
        self._brightness = 0.0
        self._binning = binning
        # None keeps whatever the camera powers up in (BayerRG8 on both of
        # ours). Set before buffers are announced, since payload size
        # depends on it. _grab() reads each buffer's own format, so a
        # higher-depth capture still reaches consumers as BGR8.
        self._pixel_format = pixel_format
        # The resting tone curve; None means "ask the device-model preset",
        # like orientation and black level. Applied on the camera when it
        # has a Gamma node, otherwise in _grab() by this corrector.
        self._gamma = gamma
        # The host-side curve as three per-channel lookup tables (R, G, B),
        # 12-bit raw in, 8-bit out -- see tone_curve.py. None means no host
        # curve: the frame goes through IDS's own conversion. Swapped whole,
        # never mutated: _grab() reads this reference from its thread.
        self._host_luts = None
        # Per-instrument calibrated values from config.json (InstrumentConfig's
        # optional exposure_time_us/gain fields) -- see DECISIONS.md's
        # 2026-08-25 calibration entry. None means "let _converge_auto_nodes()
        # handle this axis," not "leave whatever the device's NVRAM happens to
        # have," so a camera with no ExposureAuto/GainAuto at all (the slit
        # lamp) and no config values yet just keeps today's pre-calibration
        # behavior.
        self._exposure_time_us = exposure_time_us
        self._gain = gain
        # Caps this camera's own acquisition rate (distinct from
        # recording.fps, which paces the encoder) -- see
        # _apply_frame_rate_cap()'s docstring. None means untouched free-run,
        # e.g. settings.py's Preview cameras, which deliberately never pass
        # this.
        self._target_fps = target_fps
        # False skips _converge_auto_nodes() for the axes config left unset
        # -- settings.py's Preview, which calibrates them itself and must
        # open even when convergence would time out against a dark scene.
        self._converge_auto = converge_auto
        # Every one of these must be kept as an instance attribute, not a
        # local in _open(). They wrap child GenTL handles (NodeMap,
        # DataStream) whose validity is tied to their parent's Python
        # wrapper staying alive -- a local `device` variable gets garbage
        # collected the moment _open() returns, which invalidates the
        # DataStream/NodeMap handles derived from it and makes the very
        # next WaitForFinishedBuffer() call raise InvalidInstanceException
        # from the capture thread. Found via hardware smoke test.
        self._device = None
        self._remote_device = None
        self._node_map = None
        self._data_stream = None
        self._acquisition_started = False
        self._width = 0
        self._height = 0

    @property
    def resolution(self) -> tuple[int, int]:
        return (self._width, self._height)

    def _open(self) -> None:
        ids_peak.Library.Initialize()
        try:
            self._device = self._open_device()
            self._remote_device = self._device.RemoteDevice()
            self._node_map = self._remote_device.NodeMaps()[0]

            # Resolve a not-yet-decided orientation from the device model
            # now that we know it (self._model_name is set by
            # _open_device()). An explicit config value was already
            # validated in __init__.
            if self._orientation is None:
                self._orientation = orientation_for_model(self._model_name)

            # Before anything reads or writes ExposureTime: the pixel clock
            # sets the frame period, and ExposureTime's own maximum is
            # derived from it.
            self._apply_pixel_clock()
            self._select_analog_gain()
            self._apply_black_level()
            self._apply_binning()
            self._apply_pixel_format()
            # Owned here, every open: a GenICam camera keeps whatever tone
            # curve the last process left, so without this a student's
            # brightness setting would leak into the next calibration.
            self._apply_gamma()

            self._width = int(self._node_map.FindNode("Width").Value())
            self._height = int(self._node_map.FindNode("Height").Value())

            data_stream = self._device.DataStreams()[0].OpenDataStream()
            payload_size = self._payload_size(data_stream)
            buffer_count = max(data_stream.NumBuffersAnnouncedMinRequired(), _MIN_BUFFER_COUNT)
            for _ in range(buffer_count):
                buffer = data_stream.AllocAndAnnounceBuffer(payload_size)
                data_stream.QueueBuffer(buffer)
            self._data_stream = data_stream

            # Applied before the stream is locked, not after.
            # AcquisitionFrameRate's Maximum() is derived from ExposureTime
            # and freezes at TLParamsLocked -- so setting exposure after
            # StartAcquisition leaves the frame rate capped at a limit
            # belonging to whatever exposure the device happened to be
            # holding. Since GenICam cameras persist exposure across power
            # cycles, that is usually the *previous* calibration. Found on
            # real hardware: config.json's 30ms was applied correctly and
            # the camera still delivered 11.5fps, because the rate had
            # already been clamped to a stale 87ms exposure's 11.46 limit.
            auto_converge_nodes = []
            if self._exposure_time_us is not None:
                self._ensure_manual_exposure()
                # Clamped: the pixel clock above may have moved this
                # node's range, and a config value written under a
                # different clock would otherwise be out of bounds.
                exposure_min, exposure_max = self.exposure_time_range_us()
                # Exposure is a frame-rate budget, and a saved value can
                # exceed it -- a 124ms calibration was found in the field,
                # capping this camera at 8fps and blurring every frame of
                # exactly the motion the recording exists to show. Clamping
                # here rather than trusting config is the same reasoning
                # CLAUDE.md applies to the budget itself: it belongs in code.
                if self._target_fps:
                    exposure_max = min(exposure_max, exposure_budget_us(self._target_fps))
                wanted = self._exposure_time_us
                # A hair of tolerance: the camera quantises exposure, and a
                # calibration *at* the budget reads back as 30002.7us -- which
                # then warned "recalibrate with more light" at every start
                # about a value this app chose itself.
                if wanted > exposure_max * 1.001:
                    logger.warning(
                        "%s: config exposure %.1fms exceeds the %.1ffps budget; using %.1fms. "
                        "Recalibrate with more light at the instrument.",
                        self.label, wanted / 1000, self._target_fps, exposure_max / 1000,
                    )
                self.set_exposure_time_us(min(exposure_max, max(exposure_min, wanted)))
            else:
                auto_converge_nodes.append("ExposureAuto")
            if self._gain is not None:
                self._ensure_manual_gain()
                # Clamped too, for a different reason: a gain outside this
                # camera's range was calibrated on another camera (the BIO's
                # 25.4x once reached the slit lamp, whose max is 4.0x).
                # Refusing to open strands a student behind a disabled
                # Start; a visibly wrong picture does not.
                gain_min, gain_max = self.gain_range()
                if not gain_min <= self._gain <= gain_max:
                    logger.warning(
                        "%s: config gain %.2fx is outside this camera's %.2f-%.2fx range; clamped. "
                        "Recalibrate in Settings.",
                        self._serial, self._gain, gain_min, gain_max,
                    )
                self.set_gain(min(gain_max, max(gain_min, self._gain)))
            else:
                auto_converge_nodes.append("GainAuto")
            # Always the camera's own: nothing here sets white balance.
            auto_converge_nodes.append("BalanceWhiteAuto")
            # Whichever axes config didn't supply a calibrated value for
            # still get today's one-time auto-converge, all in one pass (a
            # no-op for any axis with no *Auto node at all -- see
            # _converge_auto_nodes()'s docstring).
            self._node_map.FindNode("TLParamsLocked").SetValue(1)
            data_stream.StartAcquisition()
            self._acquisition_started = True
            self._node_map.FindNode("AcquisitionStart").Execute()
            self._node_map.FindNode("AcquisitionStart").WaitUntilDone()

            # Bound the *vendor's* auto-exposure by the same frame-rate
            # budget our own calibration obeys, before letting it converge.
            self._apply_auto_exposure_limit()
            if self._converge_auto:
                try:
                    self._converge_auto_nodes(auto_converge_nodes)
                except IdsCameraConvergenceTimeoutError as exc:
                    # Whatever it reached is locked and usable. Refusing to
                    # open here would strand a student behind a disabled
                    # Start over white balance -- which is visible in the
                    # preview, and so is not a readiness gate. See
                    # CLAUDE.md's "Who uses it".
                    logger.warning("%s: %s; continuing with what it reached", self.label, exc)

            # After exposure/gain are settled, never before. This node's
            # own Maximum() is derived from the current ExposureTime, so
            # capping first reads a limit belonging to whatever the device
            # happened to be holding -- and GenICam cameras persist
            # exposure across power cycles. Found on real hardware: with
            # the cap applied first, a camera whose stale exposure was
            # 87ms stayed pinned at 11.5fps even after config.json's 30ms
            # was applied, because AcquisitionFrameRate had already been
            # clamped to 11.46 and nothing raised it again.
            if self._target_fps is not None:
                self._apply_frame_rate_cap(self._target_fps)
            else:
                self._release_frame_rate_cap()
            # Gamma is already applied above; only a camera without one
            # spends light, and only when there is a setting to restore.
            if self._brightness and not self._has_gamma():
                self._apply_light(self._brightness)
        except Exception:
            # A failure partway through leaves whatever got opened so far
            # (device, data stream, a running acquisition) dangling with
            # nothing to release it -- the next start() attempt would then
            # fail Control access as "busy" against our own leaked handle,
            # forever. _close() already tolerates being called from any
            # partial-init state (every step it touches is None-guarded).
            #
            # A cleanup failure is logged, never raised: raising it would
            # replace the exception that explains why the open failed.
            try:
                self._close()
            except Exception:
                logger.exception("%s: cleanup after a failed open also failed", self._serial)
            raise

    def _payload_size(self, data_stream: ids_peak.DataStream) -> int:
        """DataStream.PayloadSize() raises NotImplementedException on the
        uEye Transport Layer (slit lamp camera) -- STREAM_INFO_PAYLOAD_SIZE
        isn't implemented there, confirmed via hardware smoke test. The
        standard GenICam PayloadSize node on the remote device's node map
        works on both cameras, so use that as the fallback rather than the
        primary path, to avoid changing already-verified behavior on the
        Keeler.
        """
        try:
            return data_stream.PayloadSize()
        except (ids_peak.NotImplementedException, ids_peak.InternalErrorException):
            # The GenTL error code underneath is GC_ERR_NOT_IMPLEMENTED either
            # way, but which Python exception class it surfaces as depends on
            # the transport layer -- confirmed InternalErrorException on the
            # uEye Transport Layer via hardware smoke test; catching both
            # rather than trusting a single mapping across producers.
            return int(self._node_map.FindNode("PayloadSize").Value())

    def _converge_auto_nodes(self, node_names: list[str]) -> None:
        """One-time auto-convergence pass against whatever this camera
        actually sees, for any of `node_names` (each a GenICam *Auto enum
        node -- "ExposureAuto"/"GainAuto"/"BalanceWhiteAuto") that's
        actually available, then left locked for the session.

        Found via hardware smoke test (exposure/gain specifically): the
        sensor's power-on defaults (ExposureTime ~15ms, Gain 1.0) produced a
        near-black frame even pointed directly at a lamp. The right values
        depend on the room/instrument this camera is installed on -- so
        converge once against reality instead of hardcoding a number
        that's already been shown wrong for at least one room. `Once`
        rather than `Continuous` so nothing visibly hunts mid-recording.

        The uEye Transport Layer (slit lamp camera) only exposes a basic
        feature set per SETUP.md Section 3 and may lack any of these nodes
        entirely -- skip whichever isn't available rather than fail camera
        open over it. Returns immediately if none of `node_names` exist.
        `_open()` omits an axis from `node_names` entirely (rather than
        passing a skip flag) when config.json already supplied a manually-
        calibrated value for it -- distinct from "not available," since a
        camera that *has* e.g. ExposureAuto but was given a manual
        exposure_time_us shouldn't have this method fight the value
        _open() just set.

        Generalized from a single exposure/gain-specific convergence loop
        so BalanceWhiteAuto (added 2026-08-26) follows the identical
        Once-then-poll-until-Off shape in the same pass, rather than a
        second, separately-polled loop.
        """
        active_nodes = []
        for name in node_names:
            node = self._node_map.TryFindNode(name)
            if node is not None and node.IsAvailable() and node.IsWriteable():
                node.SetCurrentEntry("Once")
                active_nodes.append(node)

        if not active_nodes:
            return

        deadline = time.monotonic() + _AUTO_CONVERGE_TIMEOUT_S
        for _ in range(_AUTO_CONVERGE_MAX_FRAMES):
            buffer = self._data_stream.WaitForFinishedBuffer(_ACQUISITION_TIMEOUT_MS)
            self._data_stream.QueueBuffer(buffer)

            if all(node.CurrentEntry().SymbolicValue() == "Off" for node in active_nodes):
                return
            if time.monotonic() > deadline:
                break

        raise IdsCameraConvergenceTimeoutError(
            f"auto-convergence for {node_names} didn't finish within "
            f"{_AUTO_CONVERGE_TIMEOUT_S}s for serial {self._serial!r}"
        )

    def supports_manual_calibration(self) -> bool:
        """True when a technician can set ExposureTime/Gain on this camera
        at all -- the case settings.py's PreviewDialog shows the sliders
        and the Auto-Calibrate button for, instead of just a static
        preview. Must be called after start() -- self._node_map doesn't
        exist before _open() has run.

        This deliberately asks a *different* question than it used to.
        The old one (needs_manual_calibration(): "does this camera lack
        ExposureAuto/GainAuto?") answered False for the Keeler, on the
        reasoning that a camera which converges on its own has nothing for
        a technician to calibrate. That reasoning was wrong about *when*
        it converges. _converge_auto_nodes() runs inside _open(), which
        kiosk.select_instrument() calls the instant a student taps the
        instrument on the picker -- with the BIO still on the desk, its
        illumination off, pointed at nothing. `Once` then leaves that
        result locked for the whole session, and the camera is never
        reopened, so switching the lamp on afterwards changes nothing.
        There is no pre-recording moment when the scene is representative
        (students press Start *before* raising the instrument to the eye),
        which is what makes a technician-calibrated config value the only
        answer that is right at record time. See DECISIONS.md's
        2026-09-10 entry.

        Device-side auto-convergence remains the fallback: _open() drops
        an axis from _converge_auto_nodes() only when config.json actually
        supplied a value for it, so an uncalibrated install behaves
        exactly as before.
        """
        return self._is_writeable("ExposureTime") and self._is_writeable("Gain")

    def _is_writeable(self, node_name: str) -> bool:
        node = self._node_map.TryFindNode(node_name)
        return node is not None and node.IsAvailable() and node.IsWriteable()

    def _ensure_manual_exposure(self) -> None:
        node = self._node_map.TryFindNode("ExposureAuto")
        if node is not None and node.IsAvailable() and node.IsWriteable():
            node.SetCurrentEntry("Off")

    def _ensure_manual_gain(self) -> None:
        node = self._node_map.TryFindNode("GainAuto")
        if node is not None and node.IsAvailable() and node.IsWriteable():
            node.SetCurrentEntry("Off")

    # ExposureTime/Gain's Minimum()/Maximum() accessors are confirmed
    # against both real cameras (2026-09-08). Worth knowing what they
    # report, because neither range is a fixed property of the sensor:
    # ExposureTime's maximum is the frame period, so it moves with
    # _apply_pixel_clock() (87.21ms at 24MHz, 26.31ms at 80MHz on the slit
    # lamp). Gain's range is a real hardware difference between the two --
    # 1.00-4.00x on the slit lamp against 1.00-25.41x on the Keeler --
    # which is why the calibration cost line reports gain against its own
    # maximum rather than as a bare number.

    def get_exposure_time_us(self) -> float:
        return float(self._node_map.FindNode("ExposureTime").Value())

    def set_exposure_time_us(self, value: float) -> None:
        self._node_map.FindNode("ExposureTime").SetValue(value)

    def exposure_time_range_us(self) -> tuple[float, float]:
        node = self._node_map.FindNode("ExposureTime")
        return float(node.Minimum()), float(node.Maximum())

    def get_gain(self) -> float:
        return float(self._node_map.FindNode("Gain").Value())

    def set_gain(self, value: float) -> None:
        self._node_map.FindNode("Gain").SetValue(value)
        # The floor and its noise scale with gain, and so must what the
        # host curve subtracts and how far it lifts -- see _set_host_gamma().
        if self._host_luts is not None:
            self._set_host_gamma(self._resting_gamma())

    def gain_range(self) -> tuple[float, float]:
        node = self._node_map.FindNode("Gain")
        return float(node.Minimum()), float(node.Maximum())

    def gain_ceiling(self) -> float:
        """The most gain worth using on this camera: where its floor's
        noise, through a straight line, reaches the model's tolerance --
        the same ceiling auto_calibrate() stops at. The node's own maximum
        when the model has no measured floor. Reported by settings.py so
        "3.3x of 3.3 usable" reads as the limit it is, not as headroom."""
        _gain_min, gain_max = self.gain_range()
        floor = floor_model_for_model(self._model_name)
        if floor is None:
            return gain_max
        return min(
            max_gain_for_noise(ks, cs, kb, cb, floor.max_output_sigma, gain_max)
            for ks, cs, kb, cb in zip(
                floor.sigma_slope, floor.sigma_intercept, floor.black_slope, floor.black_intercept
            )
        )

    BRIGHTNESS_ADJUSTABLE = True
    # What a camera *does* with the amount differs, deliberately: the
    # Keeler applies a tone curve on board, which lifts shadows without
    # touching the highlight, while the slit lamp publishes no gamma node
    # at all and has to spend light instead.
    #
    # Measured 2026-09-13 on the Keeler: gamma 2.4 raises the frame median
    # from 2 to roughly 28 while the highlight stays under 240. Linear in
    # amount, gamma being a perceptual curve already.
    _GAMMA_AT_FULL = 2.4
    # For a camera with no gamma: total light at amount 1.0, relative to
    # the technician's calibration. Spent on exposure up to the frame-rate
    # budget first and only then on gain -- the same preference
    # auto_calibrate uses. Geometric rather than linear, so equal travel is
    # equal stops: halfway is 2x, not 2.5x.
    _LIGHT_AT_FULL = 4.0

    def set_brightness(self, amount: float) -> None:
        """Brighten without leaving what the hardware can sustain: never
        past the frame-rate budget, never past the gain ceiling, never
        below the technician's calibration.
        """
        amount = max(0.0, min(1.0, float(amount)))
        self._brightness = amount
        if self._node_map is None:
            return  # applied at open instead
        if not self._apply_gamma():
            self._apply_light(amount)

    def _has_gamma(self) -> bool:
        node = self._node_map.TryFindNode("Gamma")
        return node is not None and node.IsWriteable()

    def _resting_gamma(self) -> float:
        resting = self._gamma if self._gamma is not None else gamma_for_model(self._model_name)
        return float(resting) if resting else 1.0

    def _apply_gamma(self) -> bool:
        """Write the tone curve for the current brightness, from this
        model's resting value up to _GAMMA_AT_FULL. False when this camera
        has no gamma node (the slit lamp), so the caller can spend light
        instead -- that camera's *resting* curve is applied on the host, in
        _grab(). Clamped to the node's own range, which is not the
        documented one: 0.3 was rejected against a minimum of 0.30000001."""
        resting = self._resting_gamma()
        if not self._has_gamma():
            self._set_host_gamma(resting)
            return False
        gamma_node = self._node_map.FindNode("Gamma")
        wanted = resting + self._brightness * max(0.0, self._GAMMA_AT_FULL - resting)
        gamma_node.SetValue(
            min(float(gamma_node.Maximum()), max(float(gamma_node.Minimum()), wanted))
        )
        logger.info(
            "%s: brightness %.0f%% (gamma %.2f)",
            self.label, self._brightness * 100, gamma_node.Value(),
        )
        return True

    def _set_host_gamma(self, gamma: float) -> None:
        """Arm (or disarm, at 1.0) the host-side tone curve _grab() applies
        to the raw 12-bit frame, in place of IDS's conversion.

        Built from the model's measured floor (device_presets.FloorModel)
        at the *current* gain: each channel's black is subtracted so the
        floor is neutral, and the curve's toe slope is set so the floor's
        noise comes out at no more than the model's max_output_sigma --
        tone_curve.py has the construction and DECISIONS.md 2026-09-21 the
        measurements. Without a floor model the curve is a bare gamma
        with no black, which is what the corrector used to be.
        """
        if abs(gamma - 1.0) < 1e-6:
            self._host_luts = None
            return
        full_scale = 4095
        floor = floor_model_for_model(self._model_name)
        if floor is None:
            curve = ToneCurve(gamma=gamma, black=(0.0, 0.0, 0.0), floor_slope=64.0)
        else:
            gain = self.get_gain()
            black = floor.black(gain, full_scale)
            sigma = floor.sigma(gain, full_scale)
            # One slope for all three channels -- the noisiest decides --
            # so a flat grey stays grey through the toe.
            slope = min(
                floor_slope_for_noise(sg, bk, full_scale, floor.max_output_sigma)
                for sg, bk in zip(sigma, black)
            )
            curve = ToneCurve(gamma=gamma, black=black, floor_slope=slope)
        self._host_luts = build_luts(curve)
        logger.info(
            "%s: host tone curve, gamma %.2f, black R%.0f G%.0f B%.0f, floor slope %.2f",
            self.label, curve.gamma, *curve.black, curve.floor_slope,
        )

    def _apply_light(self, amount: float) -> None:
        """No tone curve on this camera: spend light instead, exposure first."""
        base_exposure = self._exposure_time_us or self.get_exposure_time_us()
        base_gain = self._gain or 1.0
        exposure, gain = next_exposure_gain(
            1.0,
            base_exposure,
            self.exposure_time_range_us(),
            base_gain,
            self.gain_range(),
            target=self._LIGHT_AT_FULL**amount,
            max_exposure_us=exposure_budget_us(self._target_fps) if self._target_fps else None,
        )
        self.set_exposure_time_us(exposure)
        self.set_gain(gain)
        logger.info(
            "%s: brightness %.0f%% (exposure %.1fms, gain %.2fx)",
            self.label, amount * 100, exposure / 1000, gain,
        )

    def _select_analog_gain(self) -> None:
        """Point `Gain` at the analog stage before anything reads or writes
        it. The Keeler's selector also offers DigitalAll and per-channel
        digital gains, and a persisted selector left on one of those would
        make every gain write here land on the wrong stage -- with a
        colour cast, not an error. Read 2026-09-14: AnalogAll, as assumed;
        this makes the assumption a write. The slit lamp's uEye transport
        offers "All" instead. Best-effort like every optional node."""
        node = self._node_map.TryFindNode("GainSelector")
        if node is None or not node.IsAvailable() or not node.IsWriteable():
            return
        available = {e.SymbolicValue() for e in node.AvailableEntries()}
        for wanted in ("AnalogAll", "All"):
            if wanted in available:
                node.SetCurrentEntry(wanted)
                return

    def _release_frame_rate_cap(self) -> None:
        """No target: let the camera free-run, which means undoing the cap
        a previous process left. A GenICam camera persists
        AcquisitionFrameRate, and the Keeler was found holding 20fps from
        nobody knows what (2026-09-13, again 2026-09-14) -- so "untouched"
        is not free-running. Settings' Preview is the caller."""
        rate_node = self._node_map.TryFindNode("AcquisitionFrameRate")
        if rate_node is None or not rate_node.IsAvailable() or not rate_node.IsWriteable():
            return
        rate_node.SetValue(float(rate_node.Maximum()))

    def _apply_black_level(self) -> None:
        """Set the sensor's black floor, when a profile or config names one.

        Best-effort like the other optional nodes. Clamped to the node's own
        range, which is not a common scale: the slit lamp counts 0-255 while
        the Keeler counts 0-31.94, so a value is only meaningful against the
        camera it was measured on.
        """
        # None means "ask the device-model preset", the same escape route
        # orientation takes -- so a config written before profiles existed
        # still gets the fix, without a technician re-saving anything.
        wanted = self._black_level
        if wanted is None:
            wanted = black_level_for_model(self._model_name)
        if wanted is None:
            return
        node = self._node_map.TryFindNode("BlackLevel")
        if node is None or not node.IsWriteable():
            logger.info("%s: black level not writable; leaving it alone", self.label)
            return
        previous = float(node.Value())
        value = min(float(node.Maximum()), max(float(node.Minimum()), wanted))
        node.SetValue(value)
        logger.info("%s: black level %.0f (camera default %.0f)", self.label, value, previous)

    def _apply_pixel_format(self) -> None:
        """Best-effort, like the other optional nodes: a camera that doesn't
        offer the requested format keeps its current one rather than failing
        to open."""
        if not self._pixel_format:
            self._pixel_format = pixel_format_for_model(self._model_name)
        if not self._pixel_format:
            return
        node = self._node_map.TryFindNode("PixelFormat")
        if node is None or not node.IsWriteable():
            logger.info("%s: pixel format not writable; keeping the current one", self.label)
            return
        available = {e.SymbolicValue() for e in node.AvailableEntries()}
        if self._pixel_format not in available:
            logger.warning(
                "%s: pixel format %r not offered by this camera; keeping %s",
                self.label, self._pixel_format, node.CurrentEntry().SymbolicValue(),
            )
            return
        node.SetCurrentEntry(self._pixel_format)
        logger.info("%s: pixel format %s", self.label, self._pixel_format)

    def _apply_binning(self) -> None:
        """Best-effort, like every optional node here: a camera without
        binning, or one whose maximum is below the requested factor, keeps
        full resolution rather than failing to open."""
        if not self._binning or self._binning <= 1:
            return
        horizontal = self._node_map.TryFindNode("BinningHorizontal")
        vertical = self._node_map.TryFindNode("BinningVertical")
        if horizontal is None or not horizontal.IsWriteable():
            logger.info("%s: no writable binning; staying at full resolution", self.label)
            return
        factor = min(int(horizontal.Maximum()), self._binning)
        horizontal.SetValue(factor)
        if vertical is not None and vertical.IsWriteable():
            vertical.SetValue(min(int(vertical.Maximum()), self._binning))
        logger.info("%s: binning %dx%d", self.label, factor, factor)

    def _apply_pixel_clock(self) -> None:
        """Set the sensor pixel clock, which is what actually determines
        this camera's frame period -- and therefore both its maximum frame
        rate and its maximum exposure.

        The legacy uEye slit lamp camera powers up at 24MHz of a 10-128MHz
        range on *every* open (unlike ExposureTime/Gain, this does not
        persist), and 24MHz on a 1600x1200 sensor is an ~87ms frame period.
        That single unset value is the whole of this project's
        long-standing "the slit lamp only does ~11fps" and "87.2ms is its
        sensor maximum" -- neither was ever a sensor limit. See
        device_presets.pixel_clock_hz_for_model() and DECISIONS.md.

        Best-effort, like every other optional node in this file: the BIO's
        USB3 Vision camera reports a fixed, unwritable 197MHz and simply
        has nothing to set.
        """
        clock_hz = self._pixel_clock_hz
        if clock_hz is None:
            clock_hz = pixel_clock_hz_for_model(self._model_name)
        if clock_hz is None:
            return

        node = self._node_map.TryFindNode("DeviceClockFrequency")
        if node is None or not node.IsAvailable() or not node.IsWriteable():
            return
        # Clamped rather than refused: a technician override that this
        # particular camera cannot reach should still get as close as it can.
        wanted = min(float(node.Maximum()), max(float(node.Minimum()), float(clock_hz)))
        node.SetValue(wanted)
        logger.info("%s: pixel clock set to %.1f MHz", self.label, node.Value() / 1e6)

    def _apply_auto_exposure_limit(self) -> None:
        """Cap how long the camera's *own* ExposureAuto may expose for.

        `auto_calibrate()` obeys a frame-rate budget (see
        exposure_budget_us), but that only covers cameras with no
        auto-exposure. A camera that has one -- the BIO -- was free to
        ignore the budget entirely, and did: its ExposureAuto settled on
        49.92ms, which caps AcquisitionFrameRate at 20 against a 30fps
        target. Exposure is a frame-rate budget whoever is choosing it.

        Measured on the BIO: with the limit on at 30ms, ExposureAuto
        converges to 30.00ms, the frame-rate ceiling rises 20.00 -> 33.24
        and delivery goes 20 -> 29.9fps. The extra light it can no longer
        get from time it takes from gain, which is the intended trade.

        Best-effort, like every optional node here -- the slit lamp's uEye
        transport exposes neither node, and has no auto-exposure to bound.
        """
        if not self._target_fps:
            return
        limit_us = exposure_budget_us(self._target_fps)

        max_node = self._node_map.TryFindNode("BrightnessAutoExposureTimeMax")
        mode_node = self._node_map.TryFindNode("BrightnessAutoExposureTimeLimitMode")
        if max_node is None or not max_node.IsAvailable() or not max_node.IsWriteable():
            return

        # The ceiling has to be set before the mode is switched on, or the
        # camera would briefly enforce whatever stale maximum it held.
        max_node.SetValue(min(float(max_node.Maximum()), max(float(max_node.Minimum()), limit_us)))
        if mode_node is not None and mode_node.IsAvailable() and mode_node.IsWriteable():
            mode_node.SetCurrentEntry("On")
        logger.info(
            "%s: auto-exposure limited to %.1fms for a %.0ffps target",
            self.label, max_node.Value() / 1000, self._target_fps,
        )

    def _apply_frame_rate_cap(self, target_fps: float) -> None:
        """Caps this camera's own acquisition rate to (not above) target_fps
        -- distinct from recording.fps, which only paces Recorder's encoder
        and does nothing to stop a camera from free-running faster than
        that and burning USB bandwidth for frames recorder.py's
        _drain_latest() then just discards unused. See DECISIONS.md's
        2026-08-26 entry for the bandwidth reasoning.

        Best-effort like every other node access in this file: silently
        does nothing if either node is absent, and clamps to whatever the
        camera can actually do (via Maximum()) rather than failing if
        target_fps exceeds that. That clamp used to be load-bearing on the
        slit lamp, whose 24MHz default pixel clock put its ceiling at
        11.46fps; with _apply_pixel_clock() raising the clock, both cameras
        now have headroom above the 30fps target and this genuinely caps
        rather than throttles.

        Hardware-verified 2026-09-08 on both cameras: AcquisitionFrameRate
        can be set after AcquisitionStart, and AcquisitionFrameRateEnable
        is absent on both (the tolerate-either-way handling below is what
        makes that a non-event). Its Maximum() is derived from the current
        frame period, so this must run only once exposure, gain and the
        pixel clock are settled -- which is why _open() calls it last.
        """
        enable_node = self._node_map.TryFindNode("AcquisitionFrameRateEnable")
        if enable_node is not None and enable_node.IsAvailable() and enable_node.IsWriteable():
            enable_node.SetValue(True)

        rate_node = self._node_map.TryFindNode("AcquisitionFrameRate")
        if rate_node is None or not rate_node.IsAvailable() or not rate_node.IsWriteable():
            return
        rate_node.SetValue(min(target_fps, float(rate_node.Maximum())))

    def auto_calibrate(
        self,
        target: float | None = None,
        tolerance: float | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        target_fps: float | None = None,
        metering: str | None = None,
    ) -> bool:
        """One-shot software auto-exposure, run once when a technician
        clicks settings.py's Auto-Calibrate button (see
        supports_manual_calibration()) -- not a continuous loop during
        real recording. See exposure_calibration.py
        for the actual median-brightness/correction-step math and
        DECISIONS.md's 2026-08-25 calibration entry for the design rationale.

        `metering` defaults to this model's profile rule
        (device_presets.metering_for_model): what counts as "the picture"
        differs between a slit beam and the BIO's lit field.

        Returns True once within `tolerance` of `target`; False if
        `max_iterations` ran out first (e.g. a scene brighter/darker than
        the achievable ExposureTime/Gain range can reach) -- not raised,
        since settings.py's sliders remain a valid manual fallback either
        way. Only raises IdsCameraCalibrationError if a live frame never
        arrives at all, which points at the camera/scene, not the
        algorithm.
        """
        if metering is None:
            metering = metering_for_model(self._model_name)
        default_target, default_tolerance = metering_target(metering)
        target = default_target if target is None else target
        tolerance = default_tolerance if tolerance is None else tolerance

        self._ensure_manual_exposure()
        self._ensure_manual_gain()
        exposure_range = self.exposure_time_range_us()
        gain_range = self.gain_range()
        # Above the ceiling even a straight line shows the floor's noise;
        # the answer past it is light, not gain, and the technician's
        # calibration report says where it stopped.
        gain_range = (gain_range[0], max(gain_range[0], self.gain_ceiling()))
        # Exposure is a frame-rate budget. Without this the search spends
        # the whole frame interval to avoid gain -- see
        # next_exposure_gain()'s docstring and DECISIONS.md. Falls back to
        # this camera's own acquisition target if the caller didn't name
        # one; None on both means the old unbounded behaviour.
        fps = target_fps if target_fps is not None else self._target_fps
        max_exposure_us = exposure_budget_us(fps) if fps else None

        # Meter linear light: every target was chosen on an uncurved frame,
        # and a calibration that does not depend on the curve stays valid
        # when the curve is retuned. The picture flickers for the second
        # this takes; the curve comes back whatever happens.
        self._set_tone_curve_active(False)
        try:
            for _ in range(max_iterations):
                measured = metering_brightness(self._wait_for_fresh_frame(), metering)
                if is_converged(measured, target, tolerance):
                    return True

                new_exposure, new_gain = next_exposure_gain(
                    measured,
                    self.get_exposure_time_us(),
                    exposure_range,
                    self.get_gain(),
                    gain_range,
                    target=target,
                    max_exposure_us=max_exposure_us,
                )
                self.set_exposure_time_us(new_exposure)
                self.set_gain(new_gain)

            return False
        finally:
            self._set_tone_curve_active(True)

    def _set_tone_curve_active(self, active: bool) -> None:
        """Switch this camera's tone curve off (a straight line) or back
        to where brightness and the model's resting value put it."""
        if active:
            self._apply_gamma()
        elif self._has_gamma():
            node = self._node_map.FindNode("Gamma")
            node.SetValue(min(float(node.Maximum()), max(float(node.Minimum()), 1.0)))
        else:
            self._host_luts = None

    def _wait_for_fresh_frame(self) -> np.ndarray:
        """A frame already queued when ExposureTime/Gain just changed was
        captured under the *previous* setting -- sleep briefly for the
        sensor to apply the new value, drain whatever's now stale in the
        queue, then block for one truly new frame. Uses read() (the
        draining API), not get_latest() -- see camera.py's BaseCamera
        docstring on why a consumer that must know a frame is fresh drains
        the queue instead of peeking the latest-frame slot.
        """
        time.sleep(_CALIBRATION_SETTLE_S)
        while self.read(timeout=0) is not None:
            pass
        frame = self.read(timeout=_CALIBRATION_FRAME_TIMEOUT_S)
        if frame is None:
            raise IdsCameraCalibrationError(f"no frame received while calibrating serial {self._serial!r}")
        return frame.image

    def _close(self) -> None:
        try:
            if self._data_stream is not None:
                # An open that failed before StartAcquisition() gets here
                # with a stream that never started. StopAcquisition() on it
                # raises GC_ERR_RESOURCE_IN_USE ("Stream is not started!"),
                # and on the uEye Transport Layer so does Flush() (GC_ERR_IO,
                # is_LockSeqBuf). Dropping the handles below releases it:
                # the kiosk's 2s retry reopened cleanly after every such
                # failure on the slit lamp (2026-09-11).
                if self._acquisition_started:
                    self._node_map.FindNode("AcquisitionStop").Execute()
                    self._node_map.FindNode("AcquisitionStop").WaitUntilDone()
                    self._data_stream.StopAcquisition()
                    self._data_stream.Flush(ids_peak.DataStreamFlushMode_DiscardAll)
                    for buffer in list(self._data_stream.AnnouncedBuffers()):
                        self._data_stream.RevokeBuffer(buffer)
        finally:
            self._device = None
            self._remote_device = None
            self._node_map = None
            self._data_stream = None
            self._acquisition_started = False
            ids_peak.Library.Close()

    def _grab(self) -> tuple[np.ndarray, float, int] | None:
        try:
            buffer = self._data_stream.WaitForFinishedBuffer(_ACQUISITION_TIMEOUT_MS)
        except ids_peak.TimeoutException:
            return None

        timestamp = time.monotonic()
        # The device's own frame sequence number, read before the buffer is
        # requeued -- see BaseCamera._grab's docstring for why this must be
        # the source's own counter rather than one we assign ourselves.
        # Confirmed via hardware smoke test to start at 0 and increment per
        # frame on both real cameras.
        frame_id = buffer.FrameID()
        image = ids_peak_ipl.Image.from_image_view(buffer.ToImageView())
        array = _to_bgr8(image, self._host_luts)
        self._data_stream.QueueBuffer(buffer)

        return array, timestamp, frame_id

    def _open_device(self) -> ids_peak.Device:
        device_manager = ids_peak.DeviceManager.Instance()
        device_manager.Update()
        descriptors = device_manager.Devices()
        for descriptor in descriptors:
            if descriptor.SerialNumber() == self._serial:
                # Stashed for orientation_for_model() in _open(); ModelName()
                # is the same accessor list_ids_devices() already uses.
                self._model_name = descriptor.ModelName()
                return descriptor.OpenDevice(ids_peak.DeviceAccessType_Control)
        raise IdsCameraNotFoundError(
            f"no IDS device with serial {self._serial!r} found "
            f"({len(descriptors)} device(s) present)"
        )


def _to_bgr8(image, luts=None) -> np.ndarray:
    """One captured Image as a BGR8 array.

    With `luts` (a 12-bit BayerRG capture and an armed host curve) the raw
    samples go through tone_curve's per-channel tables and its demosaic --
    black subtracted per channel and the curve applied while the frame
    still has 12 bits, which is the only moment the shadow levels exist.
    Without, IDS's own conversion, as for the Keeler and any 8-bit format.

    Every intermediate stays in a local until the pixels are copied out:
    the numpy views do not keep their Image alive, and reading one after
    the Image is collected is an access violation, not an exception.
    """
    if luts is not None and image.PixelFormat().PixelFormatName() == ids_peak_ipl.PixelFormatName_BayerRG12:
        raw = np.array(image.get_numpy_2D_16(), copy=True)
        return raw_to_bgr8(raw, luts)
    converted = image.ConvertTo(ids_peak_ipl.PixelFormatName_BGR8)
    return converted.get_numpy_3D().copy()


