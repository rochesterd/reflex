"""A fake camera that generates test-pattern frames, for exercising
capture/compositing code without real hardware.
"""

from __future__ import annotations

import random
import time

import cv2
import numpy as np

from camera import BaseCamera, ORIENTATION_NONE

SWEEP_PERIOD_S = 2.0


class SyntheticCamera(BaseCamera):
    def __init__(
        self,
        width: int,
        height: int,
        name: str = "synthetic",
        fps: float = 30.0,
        latency: float = 0.0,
        drop_rate: float = 0.0,
        queue_size: int = 2,
        orientation: str | None = ORIENTATION_NONE,
    ):
        """
        width, height: frame resolution.
        name: label burned into the frame, useful when compositing two feeds.
        fps: target capture rate.
        latency: artificial per-frame delay (seconds) simulating slow hardware.
        drop_rate: probability in [0, 1] that a captured frame is discarded,
            simulating an unreliable camera.
        """
        super().__init__(queue_size=queue_size, label=name, orientation=orientation)
        self._width = width
        self._height = height
        self._name = name
        self._frame_period = 1.0 / fps if fps > 0 else 0.0
        self._latency = latency
        self._drop_rate = drop_rate
        self._start_time = 0.0
        self._counter = 0

    @property
    def resolution(self) -> tuple[int, int]:
        return (self._width, self._height)

    @property
    def drop_rate(self) -> float:
        return self._drop_rate

    @drop_rate.setter
    def drop_rate(self, value: float) -> None:
        """Mutable so tests can inject a fault mid-run, e.g. set to 1.0 to
        simulate a camera going silent partway through a recording.
        """
        self._drop_rate = value

    @property
    def latency(self) -> float:
        return self._latency

    @latency.setter
    def latency(self, value: float) -> None:
        self._latency = value

    def _open(self) -> None:
        self._start_time = time.monotonic()
        self._counter = 0

    def _close(self) -> None:
        pass

    def _grab(self) -> tuple[np.ndarray, float, int] | None:
        if self._latency > 0:
            time.sleep(self._latency)

        frame_index = self._counter
        target_time = self._start_time + frame_index * self._frame_period
        now = time.monotonic()
        if target_time > now:
            time.sleep(target_time - now)

        elapsed = time.monotonic() - self._start_time
        self._counter += 1

        if self._drop_rate > 0 and random.random() < self._drop_rate:
            # frame_index is deliberately not returned here: this slot was
            # "produced" (the counter advanced past it) but never
            # delivered, the same way a real camera's FrameID can skip a
            # value it silently dropped on the wire. See camera.py's
            # BaseCamera._grab docstring.
            return None

        image = self._render(elapsed, frame_index)
        return image, time.monotonic(), frame_index

    # Mirrors what a real instrument camera offers, so the brightness
    # control is visible and exercisable with no hardware attached -- which
    # is how most of this app is developed. See CLAUDE.md's Environment
    # section.
    BRIGHTNESS_LEVELS = 3
    _BRIGHTNESS_GAIN = (1.0, 1.6, 2.4)

    def set_brightness_level(self, level: int) -> None:
        self._brightness_level = max(0, min(self.BRIGHTNESS_LEVELS - 1, int(level)))

    def _render(self, elapsed: float, frame_index: int) -> np.ndarray:
        w, h = self._width, self._height
        image = np.zeros((h, w, 3), dtype=np.uint8)
        image[:] = (30, 30, 30)

        # Bar sweeping left-to-right on a SWEEP_PERIOD_S cycle.
        phase = (elapsed % SWEEP_PERIOD_S) / SWEEP_PERIOD_S
        bar_width = max(1, w // 20)
        bar_x = int(phase * (w - bar_width))
        cv2.rectangle(
            image,
            (bar_x, 0),
            (bar_x + bar_width, h),
            (60, 180, 255),
            thickness=-1,
        )

        scale = max(0.5, min(w, h) / 800.0)
        thickness = max(1, int(scale * 2))
        cv2.putText(
            image,
            f"{self._name} {w}x{h}",
            (20, int(50 * scale) + 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )
        cv2.putText(
            image,
            f"frame {frame_index}",
            (20, int(50 * scale) + 20 + int(50 * scale)),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )
        cv2.putText(
            image,
            f"t={elapsed:8.3f}s",
            (20, int(50 * scale) + 20 + int(100 * scale)),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )

        gain = self._BRIGHTNESS_GAIN[getattr(self, "_brightness_level", 0)]
        if gain != 1.0:
            image = np.clip(image.astype(np.float32) * gain, 0, 255).astype(np.uint8)
        return image
