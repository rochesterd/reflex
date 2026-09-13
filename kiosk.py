"""Kiosk state machine: idle -> ready -> recording -> error -> idle.

No Qt dependency, deliberately. app.py is a thin PySide6 shell that polls
this class on a timer and reflects what it reports; every actual decision
(when Start may be pressed, when a stall counts as a failure, what the
post-session summary contains) lives here so it can be unit-tested
headlessly with SyntheticCamera, the same way test_recorder.py drives
Recorder headlessly.
"""

from __future__ import annotations

import enum
import logging
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from camera import BaseCamera
from recorder import Recorder

logger = logging.getLogger(__name__)

# --- Disk-space preflight -------------------------------------------------
#
# libx264 at crf=23 (Recorder's default) targets quality, not a fixed
# bitrate, but ~0.08 bits/pixel/frame is a standard rule of thumb for
# moderate-motion content at that quality. At a 2560x1080 canvas and 30fps
# (the fallback estimate used before real camera resolutions are known --
# see _estimated_canvas() below):
#
#   bits/sec  = 0.08 * (2560 * 1080) * 30        ~= 6.64 Mbps
#   bytes/sec = bits/sec / 8                      ~= 830 KB/s
#   15 minutes = bytes/sec * 900                  ~= 747 MB
#
# The minutes figure is MAX_SESSION_MINUTES, the hard cap below, not a
# guess at a typical session: a session can't run longer, so a preflight
# that passes has room for the longest one there can be.
#
# A session now writes one file per camera rather than one composite, but
# the estimate is unchanged: sum-of-widths x max-height is still the right
# proxy for total pixels/sec across both streams, whether they're stacked
# into one canvas or written separately.
#
# During finalization each stream briefly has both its .mkv (the
# interruption-safe capture copy) and its freshly-remuxed .mp4 on disk at
# full size, before verification deletes the .mkv -- so the *peak* a single
# session needs is about two copies of that estimate, even though a
# completed session settles back to one. That's where "2x" comes from
# below: it isn't a safety margin stacked on top, it's the transient peak
# one session hits. See DECISIONS.md for the full reasoning and thresholds.
BITS_PER_PIXEL_ESTIMATE = 0.08
REQUIRED_SPACE_MULTIPLIER = 2.0
FALLBACK_CANVAS_WIDTH = 2560
FALLBACK_CANVAS_HEIGHT = 1080

# The longest a recording may run. At this point it stops on its own, the
# same as pressing Stop: a normal, verified session, not an error. Also
# the minutes figure in the disk preflight above. See DECISIONS.md's
# "First round of student feedback" entry.
MAX_SESSION_MINUTES = 15.0

# How long a camera's frame index may stay unchanged during a recording
# before it counts as stalled. See DECISIONS.md.
DEFAULT_STALL_TIMEOUT_S = 2.0

# How long a camera may keep delivering *pixel-identical* frames before it
# counts as frozen. Longer than the stall timeout on purpose: a stalled
# camera has stopped delivering and is unambiguous, whereas this asks
# "is the picture actually changing?", so it trades a little lateness for
# a far smaller chance of accusing a live camera. Even so, ~20 consecutive
# byte-identical samples from a real sensor is essentially impossible --
# see _frame_signature(). See DECISIONS.md.
DEFAULT_FREEZE_TIMEOUT_S = 5.0

# Roughly how many points per axis _frame_signature() samples. Small
# enough to be cheap on a 2056x1542 frame (~65x65), while still covering
# thousands of independent pixels, so live sensor noise cannot hide from
# it.
_FRESHNESS_SAMPLES_PER_AXIS = 64


def _frame_signature(image):
    """A cheap, *exact-valued* subsample of a frame, for telling a live
    stream from one that has frozen on a single image.

    Strided indexing, never interpolation: the comparison is exact
    equality, so a resampled or averaged signature could smooth away the
    one-bit differences that prove a sensor is live. Exact equality is
    what makes this safe to act on -- a real sensor emits noise on every
    frame, so two byte-identical frames mean the pixels are not coming
    from a sensor at all (a blocked camera being fed a substitute image,
    a driver replaying its last good frame, a hung capture path). A
    difference *threshold* would have to guess how dark and how static a
    real scene may legitimately be; equality does not.
    """
    height, width = image.shape[:2]
    step_y = max(1, height // _FRESHNESS_SAMPLES_PER_AXIS)
    step_x = max(1, width // _FRESHNESS_SAMPLES_PER_AXIS)
    # Copied, not a view: the caller holds this across polls, and a camera
    # implementation is free to reuse its frame buffer.
    return image[::step_y, ::step_x].copy()


def estimate_recording_bytes(
    width: int,
    height: int,
    fps: float,
    minutes: float = MAX_SESSION_MINUTES,
    bits_per_pixel: float = BITS_PER_PIXEL_ESTIMATE,
) -> float:
    bytes_per_second = (bits_per_pixel * width * height * fps) / 8.0
    return bytes_per_second * minutes * 60.0


def _existing_ancestor(path: Path) -> Path:
    """Walk up from `path` to the nearest directory that actually exists,
    so shutil.disk_usage has something to measure before sessions/ is
    ever created.
    """
    path = path.resolve()
    while not path.exists():
        parent = path.parent
        if parent == path:
            return Path(path.anchor or ".")
        path = parent
    return path


class State(enum.Enum):
    IDLE = "idle"
    READY = "ready"
    RECORDING = "recording"
    ERROR = "error"


@dataclass
class PreflightStatus:
    cameras_ready: bool
    disk_ok: bool
    free_bytes: int
    required_bytes: float
    # Cameras delivering frames whose pixels have stopped changing. Kept
    # separate from cameras_ready so the UI can say *which* problem it is:
    # "waiting for cameras" and "the camera is on but the picture is
    # frozen" need different words and different fixes.
    frozen_cameras: tuple[str, ...] = ()
    # Set when the disk-space check itself couldn't run -- the configured
    # sessions_dir is on a drive that isn't there (a removed USB disk, an
    # offline network share). disk_ok is False alongside it; the UI shows a
    # "is that drive connected?" message rather than a bogus "0 MB free".
    disk_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.cameras_ready and self.disk_ok and not self.frozen_cameras


def set_keep_awake(active: bool) -> None:
    """Ask Windows not to sleep while a recording is in progress.

    Scoped to recording rather than the app's whole lifetime: a kiosk that
    never sleeps is a facilities decision, while an in-progress capture is
    irreplaceable and a machine that suspends mid-session can take the USB
    cameras with it. Students reported the screen sleeping about two
    minutes into a session -- the capture threads carried on correctly, but
    a preview that vanishes looks like a fault, and a full sleep would not
    have been survivable.

    Best-effort, like every other platform call here: not Windows, or the
    call fails, and recording proceeds exactly as before.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # ES_CONTINUOUS keeps the state until it is cleared; the other two
        # say the system and its display are both still needed.
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if active else 0)
        if ctypes.windll.kernel32.SetThreadExecutionState(flags) == 0:
            logger.warning("could not ask Windows to stay awake (recording continues regardless)")
    except Exception:
        logger.exception("keeping the machine awake failed; recording continues regardless")


class KioskController:
    """third_person_camera runs for the controller's whole lifetime, same
    as both cameras used to. `instruments` holds one BaseCamera per
    selectable instrument (e.g. "slit_lamp", "bio"), constructed but not
    started -- only one is ever running at a time, since only one
    instrument is ever physically in use in a session. select_instrument()
    owns starting/stopping them; nothing else does. See CLAUDE.md's
    Architecture section and DECISIONS.md's "Third-person UVC camera"
    entry.
    """

    def __init__(
        self,
        third_person_camera: BaseCamera,
        instruments: dict[str, BaseCamera],
        third_person_name: str = "third_person",
        # Display labels written into session.json so a session stays
        # self-describing after a config change -- see recorder.py.
        instrument_labels: dict[str, str] | None = None,
        third_person_label: str | None = None,
        output_root: str | Path = "sessions",
        # Only feeds the disk-space estimate (_estimated_canvas()); the
        # recorder no longer has a canvas. None means "derive from the two
        # live cameras' own resolution". Still overridable for tests.
        width: int | None = None,
        height: int | None = None,
        fps: int = 30,
        stall_timeout_s: float = DEFAULT_STALL_TIMEOUT_S,
        freeze_timeout_s: float = DEFAULT_FREEZE_TIMEOUT_S,
        max_session_minutes: float = MAX_SESSION_MINUTES,
        bits_per_pixel: float = BITS_PER_PIXEL_ESTIMATE,
        disk_usage_fn: Callable[[str], object] = shutil.disk_usage,
        recorder_factory: Callable[[BaseCamera, str], Recorder] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.third_person_camera = third_person_camera
        self.instruments = instruments
        self.output_root = Path(output_root)
        self.width = width
        self.height = height
        self.fps = fps
        self.stall_timeout_s = stall_timeout_s
        self.freeze_timeout_s = freeze_timeout_s
        self.max_session_minutes = max_session_minutes

        self._third_person_name = third_person_name
        self._instrument_labels = instrument_labels or {}
        self._third_person_label = third_person_label
        self._disk_usage_fn = disk_usage_fn
        self._clock = clock
        self._bits_per_pixel = bits_per_pixel
        self._recorder_factory = recorder_factory or (
            lambda instrument_camera, instrument_name: Recorder(
                instrument_camera,
                self.third_person_camera,
                instrument_key=instrument_name,
                instrument_label=self._instrument_labels.get(instrument_name),
                third_person_label=self._third_person_label,
                output_root=str(self.output_root),
                fps=self.fps,
            )
        )

        self.state = State.IDLE
        self.error_message: str | None = None
        self.last_session_info: dict | None = None
        self.last_session_dir: Path | None = None
        self.selected_instrument: str | None = None
        # True when the last session ended at max_session_minutes rather
        # than on Stop, so the UI can say why it stopped by itself.
        self.stopped_at_time_limit = False

        self._recorder: Recorder | None = None
        self._recording_started_at: float | None = None
        self._last_index: dict[str, int] = {"instrument": -1, "third_person": -1}
        self._last_progress_time: dict[str, float] = {}
        # Freshness: the last frame signature seen per camera, and when it
        # last actually changed. See _update_freshness().
        self._last_signature: dict[str, np.ndarray] = {}
        self._last_change_time: dict[str, float] = {}
        self._last_fresh_index: dict[str, int] = {}

    # --- Instrument selection -------------------------------------------

    def select_instrument(self, key: str) -> None:
        """Stop whichever instrument camera was running (if any) and start
        the newly chosen one. Only one instrument camera is ever live at a
        time -- see the class docstring. A no-op if `key` is already
        selected, so callers (app.py's picker buttons) don't need to guard
        against re-clicking the active choice.
        """
        if key not in self.instruments:
            raise ValueError(f"unknown instrument {key!r}")
        if self.state == State.RECORDING:
            raise RuntimeError("cannot change instrument while recording")
        if key == self.selected_instrument:
            return
        if self.selected_instrument is not None:
            self.instruments[self.selected_instrument].stop()
        # Cleared before start(), not after: a real camera's start() can
        # raise (wrong serial, device busy elsewhere -- see IdsCamera). If
        # it does, the old camera is already stopped, so leaving
        # selected_instrument pointing at it would make _cameras_ready()
        # trust a frozen last frame from a camera that isn't actually
        # running. None here means "nothing confirmed running," which is
        # the truth until start() below succeeds.
        self.selected_instrument = None
        self.instruments[key].start()
        self.selected_instrument = key
        # A newly started camera has no history worth carrying over.
        self._reset_freshness()
        logger.info("instrument selected: %s", key)

    # --- Idle / ready -------------------------------------------------

    def poll_preflight(self) -> PreflightStatus:
        """Call regularly while not recording. Moves between IDLE and
        READY based on current camera/disk conditions; also where an ERROR
        state resolves back to IDLE/READY once conditions look healthy
        again. error_message / last_session_info are left in place so the
        UI can keep showing the last failure until the next session starts.
        """
        width, height = self._estimated_canvas()
        required_bytes = REQUIRED_SPACE_MULTIPLIER * estimate_recording_bytes(
            width, height, self.fps, minutes=self.max_session_minutes, bits_per_pixel=self._bits_per_pixel
        )
        try:
            free_bytes = self._disk_usage_fn(str(_existing_ancestor(self.output_root))).free
            disk_ok = free_bytes >= required_bytes
            disk_error: str | None = None
        except OSError as exc:
            # The configured sessions_dir is on a drive that isn't present
            # (a removed USB disk, an offline network share). Report it as
            # "not ready" with a reason rather than letting this raise every
            # poll and wedge the kiosk -- at startup it would otherwise be a
            # bare crash with only a log line.
            logger.warning("disk-space check failed for %s: %s", self.output_root, exc)
            free_bytes, disk_ok, disk_error = 0, False, str(exc)

        status = PreflightStatus(
            cameras_ready=self._cameras_ready(),
            disk_ok=disk_ok,
            free_bytes=free_bytes,
            required_bytes=required_bytes,
            # Refuse to start over recording something broken (CLAUDE.md):
            # a frozen camera is worse than no camera, because it looks
            # fine right up until someone plays the session back.
            frozen_cameras=self._update_freshness(self._clock()),
            disk_error=disk_error,
        )
        if self.state != State.RECORDING:
            new_state = State.READY if status.ok else State.IDLE
            if new_state != self.state:
                logger.info(
                    "%s -> %s (cameras_ready=%s, disk_ok=%s, frozen=%s)",
                    self.state.value,
                    new_state.value,
                    status.cameras_ready,
                    status.disk_ok,
                    status.frozen_cameras or "none",
                )
            self.state = new_state
        return status

    def _estimated_canvas(self) -> tuple[int, int]:
        """Best-available canvas size for the disk-space estimate. An
        explicit width/height override wins outright (tests rely on this
        to keep synthetic recordings small); otherwise prefers the two
        cameras' real resolutions once both are live, mirroring
        compositor.side_by_side's own sum-of-widths/max-of-heights default
        so the estimate matches what Recorder will actually encode.
        Before that (no instrument selected yet, or its camera hasn't
        confirmed live), falls back to a conservative default -- Start
        stays gated on cameras_ready regardless, so an imprecise estimate
        here never lets a session start on a false "ready."
        """
        if self.width is not None and self.height is not None:
            return self.width, self.height

        third_width, third_height = self.third_person_camera.resolution
        instrument_width, instrument_height = 0, 0
        if self.selected_instrument is not None:
            instrument_width, instrument_height = self.instruments[self.selected_instrument].resolution

        if third_width and third_height and instrument_width and instrument_height:
            return third_width + instrument_width, max(third_height, instrument_height)
        return FALLBACK_CANVAS_WIDTH, FALLBACK_CANVAS_HEIGHT

    def _active_cameras(self) -> dict[str, BaseCamera]:
        """The cameras that should be producing frames right now, keyed the
        way stall/freshness reporting names them. The instrument is absent
        until one is selected -- nothing is wrong with a camera that was
        never started."""
        cameras = {"third_person": self.third_person_camera}
        if self.selected_instrument is not None:
            cameras["instrument"] = self.instruments[self.selected_instrument]
        return cameras

    def _reset_freshness(self) -> None:
        self._last_signature.clear()
        self._last_change_time.clear()
        self._last_fresh_index.clear()

    def _update_freshness(self, now: float) -> tuple[str, ...]:
        """Sample each active camera and return the keys whose picture has
        not changed for freeze_timeout_s.

        This is the check that catches a camera which is *delivering* but
        not *seeing*: a blocked or switched-off webcam whose driver keeps
        the stream alive on a substitute image, a capture path replaying
        its last good frame. Nothing else notices, because reads succeed
        and Frame.index keeps advancing -- for the UVC camera that counter
        is self-assigned (UVC exposes no source counter, see
        DECISIONS.md), so it advances even when the pixels are dead. Left
        undetected this records a whole session of frozen video and
        reports it as healthy, which is the failure CLAUDE.md calls the
        worst outcome.
        """
        frozen: list[str] = []
        active = self._active_cameras()
        for key in list(self._last_signature):
            if key not in active:  # e.g. the instrument was deselected
                self._last_signature.pop(key, None)
                self._last_change_time.pop(key, None)
                self._last_fresh_index.pop(key, None)

        for key, camera in active.items():
            frame = camera.get_latest()
            if frame is None:
                continue  # not delivering at all -- that's cameras_ready/stall territory
            if self._last_fresh_index.get(key) == frame.index:
                # The same frame we already judged. Polling is simply
                # faster than this camera delivers (the slit lamp runs at
                # ~11fps against a 4Hz poll), which is not evidence of
                # anything -- comparing a frame with itself would accuse
                # every slow camera of being frozen. A camera that has
                # genuinely stopped delivering is the stall check's job.
                continue
            self._last_fresh_index[key] = frame.index
            signature = _frame_signature(frame.image)
            previous = self._last_signature.get(key)
            if previous is None or previous.shape != signature.shape or not np.array_equal(previous, signature):
                self._last_signature[key] = signature
                self._last_change_time[key] = now
            elif now - self._last_change_time[key] >= self.freeze_timeout_s:
                frozen.append(key)
        return tuple(frozen)

    def _cameras_ready(self) -> bool:
        # No instrument picked yet counts as not ready, the same as a
        # picked-but-not-yet-live instrument camera -- Start stays
        # disabled either way, and app.py's status line prompts for a
        # selection using the same "waiting for..." messaging.
        if self.selected_instrument is None:
            return False
        instrument_camera = self.instruments[self.selected_instrument]
        return self.third_person_camera.get_latest() is not None and instrument_camera.get_latest() is not None

    # --- Recording ------------------------------------------------------

    def start_recording(self) -> None:
        if self.state != State.READY:
            raise RuntimeError(f"cannot start recording from state {self.state}")

        instrument_camera = self.instruments[self.selected_instrument]
        try:
            self._recorder = self._recorder_factory(instrument_camera, self.selected_instrument)
            self._recorder.start()
        except Exception as exc:
            # A sessions_dir that can't be created (a technician pointed it
            # at a file, or somewhere unwritable), a full disk, an encoder
            # that won't open. The disk preflight can't see any of these --
            # it measures free space on the nearest *existing* ancestor --
            # so this is where they surface. Land in ERROR with the reason
            # rather than propagating into the Qt event loop, where the
            # student would just see Start do nothing at all, forever.
            logger.exception("could not start recording")
            self._recorder = None
            self.error_message = f"Could not start recording: {exc}"
            self.last_session_info = None
            self.last_session_dir = None
            self.state = State.ERROR
            return

        self.error_message = None
        self.last_session_info = None
        self.last_session_dir = None
        self.stopped_at_time_limit = False

        now = self._clock()
        self._recording_started_at = now
        for key, camera in (("instrument", instrument_camera), ("third_person", self.third_person_camera)):
            frame = camera.get_latest()
            self._last_index[key] = frame.index if frame is not None else -1
            self._last_progress_time[key] = now
        # Start the freeze clock at the session, not at whatever preflight
        # last saw -- otherwise a session could inherit a nearly-expired
        # timer and fail in its first moments.
        self._reset_freshness()
        self._update_freshness(now)

        self.state = State.RECORDING
        set_keep_awake(True)
        logger.info(
            "recording started: session_dir=%s instrument=%s",
            self._recorder.session_dir,
            self.selected_instrument,
        )

    def recording_elapsed_s(self) -> float | None:
        """Seconds since Start, on the controller's clock; None when not
        recording. What the UI counts up against max_session_minutes."""
        if self.state != State.RECORDING or self._recording_started_at is None:
            return None
        return self._clock() - self._recording_started_at

    def poll_recording(self) -> None:
        """Call regularly while RECORDING. Stops the recording and moves to
        ERROR immediately - loud and early, per CLAUDE.md, rather than
        letting a broken recording run to completion - if a writer thread
        failed, if a camera's frame index hasn't advanced for
        stall_timeout_s, or if a camera's picture has stopped changing for
        freeze_timeout_s.

        Also where a session reaches max_session_minutes. That is a normal
        stop (stop_recording(), landing in IDLE), with
        stopped_at_time_limit set so the UI can say so.
        """
        if self.state != State.RECORDING:
            return

        instrument_camera = self.instruments[self.selected_instrument]
        # The technician-set labels, not the internal role keys -- these
        # strings go straight into the error banner a student reads.
        display_names = {
            "instrument": self._instrument_labels.get(self.selected_instrument, self.selected_instrument),
            "third_person": self._third_person_label or self._third_person_name,
        }

        # A writer thread that hit a full disk / encoder fault. The camera
        # is still fine so the stall/freeze checks below won't catch it --
        # stop now rather than let one stream run on frozen until Stop.
        failed = self._recorder.failed_stream()
        if failed:
            role, detail = failed
            self._fail(
                f"Recording of {display_names.get(role, role)} stopped unexpectedly: {detail}. "
                "Recording stopped; the frames captured so far are kept."
            )
            return

        now = self._clock()
        # After the writer check (a failed stream is still a failure, cap
        # or not) and before the stall/freeze checks, so a session that
        # ran its full length is never reported as broken.
        if now - self._recording_started_at >= self.max_session_minutes * 60.0:
            logger.info("recording reached the %g-minute limit; stopping", self.max_session_minutes)
            self.stop_recording()
            # stop_recording() lands in ERROR if it couldn't finalize, and
            # that is reported as a failure, not as reaching the limit.
            self.stopped_at_time_limit = self.state == State.IDLE
            return

        stalled = []
        for key, camera in (("instrument", instrument_camera), ("third_person", self.third_person_camera)):
            frame = camera.get_latest()
            index = frame.index if frame is not None else -1
            if index != self._last_index[key]:
                self._last_index[key] = index
                self._last_progress_time[key] = now
                continue
            if now - self._last_progress_time[key] >= self.stall_timeout_s:
                stalled.append(key)

        if stalled:
            names = ", ".join(display_names[key] for key in stalled)
            self._fail(
                f"No new frames from {names} for {self.stall_timeout_s:.1f}s+. "
                "Recording stopped."
            )
            return

        frozen = self._update_freshness(now)
        if frozen:
            names = ", ".join(display_names.get(key, key) for key in frozen)
            self._fail(
                f"The picture from {names} stopped changing {self.freeze_timeout_s:.0f}s ago - "
                "the camera is delivering frames but not seeing anything. It may be "
                "switched off, blocked, or covered. Recording stopped."
            )

    def stop_recording(self) -> dict:
        set_keep_awake(False)
        if self.state != State.RECORDING:
            raise RuntimeError(f"cannot stop recording from state {self.state}")

        recorder = self._recorder
        self.last_session_dir = recorder.session_dir
        try:
            session_info = recorder.stop()
        except Exception as exc:
            # A stuck writer thread, or a full disk during the MKV->MP4
            # remux/manifest write. The raw capture files are still on disk
            # (that's what the MKV is for) -- surface the failure loudly and
            # land in ERROR rather than propagating into the Qt event loop
            # and leaving the controller stuck in RECORDING. Mirrors _fail().
            logger.exception("recorder.stop() failed during a normal Stop")
            self._recorder = None
            self.last_session_info = {"error": f"the recording could not be finalized: {exc}"}
            self.error_message = (
                f"The recording was stopped but could not be finalized: {exc} "
                f"The raw capture files are in {recorder.session_dir}."
            )
            self.state = State.ERROR
            return self.last_session_info

        self._recorder = None
        self.last_session_info = session_info
        self.error_message = None
        self.state = State.IDLE
        streams = session_info.get("streams", {})
        logger.info(
            "recording stopped: session_dir=%s frames=%s dropped=%s verified=%s",
            self.last_session_dir,
            {role: s.get("frame_count") for role, s in streams.items()},
            {role: s.get("dropped_frames") for role, s in streams.items()},
            {role: s.get("verified") for role, s in streams.items()},
        )
        return session_info

    def _fail(self, message: str) -> None:
        set_keep_awake(False)
        logger.error("recording failed: %s", message)
        self.error_message = message
        if self._recorder is not None:
            try:
                self.last_session_info = self._recorder.stop()
            except Exception as exc:
                self.last_session_info = {"error": f"recorder failed to stop cleanly: {exc}"}
            self.last_session_dir = self._recorder.session_dir
            self._recorder = None
        self.state = State.ERROR
