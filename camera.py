"""Camera abstraction: a background capture thread feeding a bounded queue
and a "latest frame" slot, so consumers can either drain every frame or
just grab whatever is newest without blocking on capture speed.
"""

from __future__ import annotations

import logging
import queue
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

# Dimension-preserving orientation fixes for a camera whose sensor is
# mounted rotated, or whose instrument optics mirror the image (the BIO
# does). This is the full Klein-four group of symmetries that keep width
# and height: identity, 180deg rotation, and the two axis flips. 90/270
# rotation is deliberately excluded -- it would make frames no longer match
# the camera's reported `.resolution`, which recorder.py/kiosk.py use to
# size the recording canvas. See DECISIONS.md's rotation/orientation
# entries.
ORIENTATION_NONE = "none"
ORIENTATION_ROTATE_180 = "rotate_180"
ORIENTATION_FLIP_HORIZONTAL = "flip_horizontal"  # mirror left<->right
ORIENTATION_FLIP_VERTICAL = "flip_vertical"  # mirror top<->bottom
VALID_ORIENTATIONS = (
    ORIENTATION_NONE,
    ORIENTATION_ROTATE_180,
    ORIENTATION_FLIP_HORIZONTAL,
    ORIENTATION_FLIP_VERTICAL,
)


def apply_orientation(image: np.ndarray, orientation: str) -> np.ndarray:
    """Return `image` transformed for `orientation` (one of
    VALID_ORIENTATIONS). The result may be a non-contiguous view; the
    caller makes it contiguous if a downstream consumer needs that."""
    if orientation == ORIENTATION_ROTATE_180:
        return image[::-1, ::-1]
    if orientation == ORIENTATION_FLIP_VERTICAL:
        return image[::-1, :]
    if orientation == ORIENTATION_FLIP_HORIZONTAL:
        return image[:, ::-1]
    return image


@dataclass
class Frame:
    image: np.ndarray  # BGR, HxWx3, uint8
    timestamp: float  # time.monotonic() at capture
    index: int  # source's own frame sequence number -- see BaseCamera._grab


class BaseCamera(ABC):
    """Owns a capture thread, a bounded frame queue, and a latest-frame slot.

    Subclasses implement _open, _close, _grab, and resolution. _grab is
    called repeatedly on the capture thread and should return a fresh
    (image, timestamp, index) triple or None if no frame was available.
    """

    def __init__(self, queue_size: int = 2, label: str | None = None, orientation: str | None = ORIENTATION_NONE):
        self._queue: queue.Queue[Frame] = queue.Queue(maxsize=queue_size)
        self._latest_lock = threading.Lock()
        self._latest: Frame | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # For log messages only -- distinguishes cam-a/cam-b (or a serial
        # number) in a shared log file where "SyntheticCamera" alone
        # wouldn't. Falls back to the class name if a subclass doesn't pass
        # one.
        self.label = label or type(self).__name__
        # Orientation fix applied to every captured frame before it's
        # queued, so recorder/preview/kiosk all see it the same way. None
        # means "not decided yet" -- a subclass (IdsCamera) resolves it from
        # a device-model preset in _open(), before the capture thread
        # starts. Validated here for an explicit value; a None left for
        # _open() to fill must be validated there too.
        if orientation is not None and orientation not in VALID_ORIENTATIONS:
            raise ValueError(f"orientation must be one of {VALID_ORIENTATIONS} or None, got {orientation!r}")
        self._orientation = orientation

    @abstractmethod
    def _open(self) -> None:
        """Acquire whatever resources are needed to start producing frames."""

    @abstractmethod
    def _close(self) -> None:
        """Release resources acquired in _open."""

    @abstractmethod
    def _grab(self) -> tuple[np.ndarray, float, int] | None:
        """Produce one (BGR image, monotonic timestamp, index) triple, or
        None if no frame was available.

        `index` must be the *source's* own frame sequence number (a real
        camera's own FrameID, or an equivalent counter for a synthetic
        source) rather than anything this class assigns itself. A consumer
        draining frames in order via read() detects gaps by comparing
        consecutive Frame.index values (see recorder.py) -- that only
        catches real drops if the number reported here reflects frames the
        source actually produced, including ones the source itself skipped
        without this class ever seeing them via a None return. Assigning a
        gapless counter here instead (as earlier versions of this class
        did) makes every source look drop-free by construction, silently
        hiding exactly the failure CLAUDE.md's Hardware section warns
        about: USB3 Vision degrading by dropping frames rather than
        raising an error. Not required to start at 0 or be gapless --
        recorder.py's frame counting does not depend on either.
        """

    @property
    @abstractmethod
    def resolution(self) -> tuple[int, int]:
        """(width, height) of frames this camera produces."""

    # Whether a student can brighten this camera's picture while
    # recording. A camera that offers nothing ignores the setting, so
    # callers never branch on camera type.
    BRIGHTNESS_ADJUSTABLE = False

    def set_brightness(self, amount: float) -> None:
        """Brighten the picture. `amount` runs 0.0 to 1.0, where 0.0 is the
        technician's calibration and 1.0 is the most this camera will give
        -- a range measured per model, not a hardware limit, so a student
        cannot reach anywhere nobody has looked.

        Each camera spends it on whatever lever it actually has, so the
        amount is deliberately unitless. Base: nothing to apply.
        """

    def start(self) -> None:
        if self._thread is not None:
            return
        self._open()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("%s: capture started", self.label)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        self._thread = None
        self._close()
        logger.info("%s: capture stopped", self.label)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = self._grab()
            except Exception:
                # Logged and skipped, not re-raised: an unhandled exception
                # here would silently kill the capture thread with nothing
                # in get_latest()/read() ever indicating why -- the caller
                # would only notice minutes later via a stall. This at
                # least leaves a record of the actual cause. See
                # DECISIONS.md's Frame.index entry for the related history
                # of source-side drops going undetected.
                logger.exception("%s: _grab() raised; skipping this frame", self.label)
                continue
            if result is None:
                continue
            image, timestamp, index = result
            if self._orientation and self._orientation != ORIENTATION_NONE:
                # Made contiguous so downstream cv2/PyAV consumers don't
                # trip over the negative strides a reversed view carries.
                image = np.ascontiguousarray(apply_orientation(image, self._orientation))
            frame = Frame(image=image, timestamp=timestamp, index=index)

            with self._latest_lock:
                self._latest = frame

            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(frame)
                except queue.Full:
                    pass

    def get_latest(self) -> Frame | None:
        """Return the most recently captured frame without blocking."""
        with self._latest_lock:
            return self._latest

    def read(self, timeout: float | None = None) -> Frame | None:
        """Pop the next queued frame, blocking up to timeout seconds."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def __enter__(self) -> "BaseCamera":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
