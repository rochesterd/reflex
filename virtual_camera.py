"""Publishes the two live feeds as one virtual webcam, for Panopto Capture.

Stream mode's core (DECISIONS.md 2026-09-18): instead of recording, Reflex
composes the instrument and third-person feeds into one canvas at a fixed
rate and hands each frame to the virtual camera filter the installer
registers (Unity Capture; PACKAGING.md 2c). A browser then sees it like
any webcam, and Panopto Capture -- signed in as the student -- does the
recording, the filing and the retention. Reflex records nothing in this
mode.

Two rules carry over from the recorder, one of them sharpened:

- **Display semantics.** Frames come from `get_latest()`, never `read()`:
  a virtual camera at 30 fps repeats a slow camera's frame and skips a
  fast one's, and neither is a drop worth counting. See CLAUDE.md on the
  two ways BaseCamera exposes frames.
- **Loud and early has to live in the picture.** With no Start button to
  disable, a camera that has stopped seeing would be recorded by Panopto
  as a frozen frame for an hour and look fine until playback. So a role
  the kiosk reports as absent or frozen is replaced by a slate -- a red
  panel saying so -- never by its last good frame. app.py feeds
  set_faults() from the same preflight that gates Start in record mode.

The backend is injectable: PyVirtualCamBackend drives the real driver via
pyvirtualcam, and tests use an in-memory one, for the reason
SyntheticCamera exists. pyvirtualcam is imported lazily -- a machine in
record mode never needs it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Protocol

import cv2
import numpy as np

from camera import BaseCamera
from compositor import LAYOUT_SIDE_BY_SIDE, compose_layout
from session_format import INSTRUMENT_STREAM, THIRD_PERSON_STREAM

logger = logging.getLogger(__name__)

# What a browser will take without argument; compose_layout letterboxes
# into it. Larger canvases are legal but Panopto Capture encodes around
# 1080p anyway, so nothing above this survives the trip.
DEFAULT_STREAM_SIZE = (1920, 1080)
DEFAULT_STREAM_FPS = 30

# The slate's colours, deliberately not brand colours: this is a fault
# indicator, like the banner reds in app.py, and must read as one.
_SLATE_BACKGROUND = (28, 28, 138)  # BGR: a dark red
_SLATE_TEXT = (255, 255, 255)
_SLATE_FALLBACK_SIZE = (640, 480)


class VirtualCameraUnavailable(RuntimeError):
    """The driver isn't installed or couldn't be opened. Raised at start,
    never swallowed: a kiosk configured to stream that silently doesn't
    is the black pane again."""


class VirtualCameraBackend(Protocol):
    """The whole surface a sink needs from a driver."""

    def open(self, width: int, height: int, fps: int) -> str:
        """Start publishing; return the device name a browser will list."""
        ...

    def send(self, bgr: np.ndarray) -> None: ...

    def close(self) -> None: ...


class PyVirtualCamBackend:
    """The real one: whichever DirectShow virtual camera is registered --
    the installer's Unity Capture filter, or an OBS one -- through
    pyvirtualcam, which finds it."""

    def __init__(self) -> None:
        self._camera = None

    def open(self, width: int, height: int, fps: int) -> str:
        try:
            import pyvirtualcam  # noqa: PLC0415 -- lazy; see the module docstring
        except ImportError as exc:
            raise VirtualCameraUnavailable(
                "streaming needs pyvirtualcam (pip install pyvirtualcam)"
            ) from exc
        try:
            self._camera = pyvirtualcam.Camera(
                width=width, height=height, fps=fps, fmt=pyvirtualcam.PixelFormat.BGR
            )
        except RuntimeError as exc:
            raise VirtualCameraUnavailable(
                f"no virtual camera is registered on this machine ({exc}). Reinstall Reflex, "
                f"which registers one, and restart."
            ) from exc
        return self._camera.device

    def send(self, bgr: np.ndarray) -> None:
        if self._camera is not None:
            self._camera.send(bgr)

    def close(self) -> None:
        if self._camera is not None:
            self._camera.close()
            self._camera = None


class VirtualCameraSink:
    """Composes the live feeds into the virtual camera at a fixed rate.

    `instrument` is a callable returning the currently selected instrument
    camera or None, so switching instruments needs no plumbing here: the
    next tick simply asks again. `set_faults()` names the roles to replace
    with a slate and why; app.py calls it every poll from the kiosk's
    preflight, so what the stream shows and what the status line says
    never disagree.
    """

    def __init__(
        self,
        instrument: Callable[[], BaseCamera | None],
        third_person: BaseCamera,
        backend: VirtualCameraBackend,
        *,
        layout: str = LAYOUT_SIDE_BY_SIDE,
        size: tuple[int, int] = DEFAULT_STREAM_SIZE,
        fps: int = DEFAULT_STREAM_FPS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._instrument = instrument
        self._third_person = third_person
        self._backend = backend
        self.layout = layout
        self.size = size
        self.fps = fps
        self._clock = clock
        self._sleep = sleep
        self._faults: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.device_name: str | None = None
        self.frames_sent = 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        width, height = self.size
        self.device_name = self._backend.open(width, height, self.fps)
        logger.info("streaming %s at %dx%d %dfps as %r", self.layout, width, height, self.fps, self.device_name)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="virtual-camera", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._thread = None
        self._backend.close()
        logger.info("streaming stopped after %d frames", self.frames_sent)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- what to show ----------------------------------------------------

    def set_faults(self, faults: dict[str, str]) -> None:
        """Roles to slate out, with the message to print. An empty dict
        means both cameras are believed to be seeing."""
        with self._lock:
            self._faults = dict(faults)

    def compose_frame(self) -> np.ndarray:
        """One canvas from the current state. Public so a test can check
        exactly what would be sent without running the thread."""
        with self._lock:
            faults = dict(self._faults)

        instrument_camera = self._instrument()
        images = {
            INSTRUMENT_STREAM: self._image_for(INSTRUMENT_STREAM, instrument_camera, faults),
            THIRD_PERSON_STREAM: self._image_for(THIRD_PERSON_STREAM, self._third_person, faults),
        }
        return compose_layout(images, self.layout, self.size)

    def _image_for(self, role: str, camera: BaseCamera | None, faults: dict[str, str]) -> np.ndarray:
        if role in faults:
            return slate(faults[role], _pane_size(camera))
        if camera is None:
            return slate("No instrument selected", _SLATE_FALLBACK_SIZE)
        frame = camera.get_latest()
        if frame is None:
            # Not delivering yet -- the same condition preflight reports as
            # "waiting for cameras"; app.py will name it next poll. Say
            # so now rather than showing black for a tick.
            return slate("Waiting for camera", _pane_size(camera))
        return frame.image

    # -- the loop --------------------------------------------------------

    def _run(self) -> None:
        period = 1.0 / self.fps
        next_due = self._clock()
        while not self._stop.is_set():
            try:
                self._backend.send(self.compose_frame())
                self.frames_sent += 1
            except Exception:  # noqa: BLE001 -- keep streaming; a bad frame is one frame
                logger.exception("virtual camera frame failed")
            next_due += period
            delay = next_due - self._clock()
            if delay > 0:
                self._sleep(delay)
            else:
                # Fell behind (a slow compose, a stalled driver). Resync
                # rather than bursting to catch up: a burst is what makes
                # a browser drop frames.
                next_due = self._clock()


def slate(message: str, size: tuple[int, int]) -> np.ndarray:
    """A pane-sized fault panel. Red, with the message and a fixed header
    line, sized so it reads on a phone-sized thumbnail as well as a
    projector: the whole point is that nobody can mistake it for video."""
    width, height = size
    canvas = np.empty((height, width, 3), dtype=np.uint8)
    canvas[:] = _SLATE_BACKGROUND

    scale = max(0.6, min(width, height) / 400.0)
    thickness = max(1, int(round(scale * 2)))
    lines = ["CAMERA NOT READY", message]
    line_height = int(40 * scale)
    y = height // 2 - line_height // 2
    for index, text in enumerate(lines):
        font_scale = scale * (1.2 if index == 0 else 0.8)
        (text_width, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        x = max(8, (width - text_width) // 2)
        cv2.putText(
            canvas, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale, _SLATE_TEXT, thickness, cv2.LINE_AA
        )
        y += line_height
    return canvas


def _pane_size(camera: BaseCamera | None) -> tuple[int, int]:
    """The slate takes the camera's own size, so a fault doesn't change
    the layout -- the panes stay where the student is used to them."""
    if camera is None:
        return _SLATE_FALLBACK_SIZE
    try:
        width, height = camera.resolution
        if width > 0 and height > 0:
            return int(width), int(height)
    except Exception:  # noqa: BLE001 -- a camera that can't say; use the fallback
        pass
    return _SLATE_FALLBACK_SIZE
