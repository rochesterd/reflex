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
from device_presets import orientation_for_model, pixel_clock_hz_for_model
from exposure_calibration import (
    DEFAULT_MAX_ITERATIONS,
    METERING_HIGHLIGHT,
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
        binning: int | None = None,
        pixel_format: str | None = None,
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
        self._binning = binning
        # None keeps whatever the camera powers up in (BayerRG8 on both of
        # ours). Set before buffers are announced, since payload size
        # depends on it. _grab() reads each buffer's own format, so a
        # higher-depth capture still reaches consumers as BGR8.
        self._pixel_format = pixel_format
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
            self._apply_binning()
            self._apply_pixel_format()

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
                self.set_exposure_time_us(
                    min(exposure_max, max(exposure_min, self._exposure_time_us))
                )
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
                self._converge_auto_nodes(auto_converge_nodes)

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

    def gain_range(self) -> tuple[float, float]:
        node = self._node_map.FindNode("Gain")
        return float(node.Minimum()), float(node.Maximum())

    def _apply_pixel_format(self) -> None:
        """Best-effort, like the other optional nodes: a camera that doesn't
        offer the requested format keeps its current one rather than failing
        to open."""
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
        metering: str = METERING_HIGHLIGHT,
    ) -> bool:
        """One-shot software auto-exposure, run once when a technician
        clicks settings.py's Auto-Calibrate button (see
        supports_manual_calibration()) -- not a continuous loop during
        real recording. See exposure_calibration.py
        for the actual median-brightness/correction-step math and
        DECISIONS.md's 2026-08-25 calibration entry for the design rationale.

        Returns True once within `tolerance` of `target`; False if
        `max_iterations` ran out first (e.g. a scene brighter/darker than
        the achievable ExposureTime/Gain range can reach) -- not raised,
        since settings.py's sliders remain a valid manual fallback either
        way. Only raises IdsCameraCalibrationError if a live frame never
        arrives at all, which points at the camera/scene, not the
        algorithm.
        """
        default_target, default_tolerance = metering_target(metering)
        target = default_target if target is None else target
        tolerance = default_tolerance if tolerance is None else tolerance

        self._ensure_manual_exposure()
        self._ensure_manual_gain()
        exposure_range = self.exposure_time_range_us()
        gain_range = self.gain_range()
        # Exposure is a frame-rate budget. Without this the search spends
        # the whole frame interval to avoid gain -- see
        # next_exposure_gain()'s docstring and DECISIONS.md. Falls back to
        # this camera's own acquisition target if the caller didn't name
        # one; None on both means the old unbounded behaviour.
        fps = target_fps if target_fps is not None else self._target_fps
        max_exposure_us = exposure_budget_us(fps) if fps else None

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
        converted = image.ConvertTo(ids_peak_ipl.PixelFormatName_BGR8)
        # Copy out of the converted Image's own buffer before it goes out
        # of scope, rather than trust an unverified zero-copy lifetime.
        array = converted.get_numpy_3D().copy()
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
