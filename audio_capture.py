"""Microphone capture on the same clock as the cameras.

`AudioCapture` is to a microphone what `BaseCamera` is to a camera: it
owns the device, runs the capture in the background, stamps every block
with `time.monotonic()` as it arrives, and hands blocks out through a
bounded queue plus a latest-level slot. Consumers pick the same way:

- **`read(timeout)`** pops the next block, in order, for the recorder --
  gaps show up as `AudioBlock.index` jumps and as the capture's own
  overflow count, so the manifest can report real dropped audio.
- **`level()`** peeks the most recent block's loudness for a meter,
  without touching the queue.

Timestamps are what make the sync story hold: a block's `timestamp` is
the instant its *last* sample arrived, on the clock `Frame.timestamp`
uses, so the recorder can place it against `clock.origin_monotonic`
exactly as it places a video frame. Nothing here assumes the device's
sample clock and the monotonic clock agree; the recorder corrects drift
by inserting silence where the timestamps say samples are missing.

sounddevice (PortAudio) is imported lazily, so a machine with no `audio`
section -- every dev machine, and stream mode, where the browser owns the
microphone -- never needs it.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_SAMPLERATE = 48000
DEFAULT_CHANNELS = 1
# ~20 ms blocks: small enough that a block's timestamp is a usable
# sample-accurate anchor, large enough not to starve the callback.
DEFAULT_BLOCKSIZE = 960
# Bounded, like a camera's queue: prefer dropping audio over blocking
# PortAudio's callback, which would stall the device.
QUEUE_DEPTH = 200


class AudioUnavailable(RuntimeError):
    """No such device, or PortAudio couldn't open it. Raised at start,
    never swallowed: a session recorded silent is the black pane again."""


@dataclass(frozen=True)
class AudioBlock:
    index: int  # self-counted, like a UVC camera's Frame.index
    timestamp: float  # monotonic, when the block's last sample arrived
    samples: np.ndarray  # int16, shape (frames, channels)

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])


def list_input_devices() -> list[tuple[int, str]]:
    """(index, name) of every device that can capture, for settings.py.
    Names, not indices, are what config.json stores -- an index changes
    when a USB device is re-plugged, the same reason cameras go by serial."""
    import sounddevice as sd  # noqa: PLC0415 -- lazy; see the module docstring

    return [
        (index, str(device["name"]))
        for index, device in enumerate(sd.query_devices())
        if int(device["max_input_channels"]) > 0
    ]


def resolve_device(name: str | None) -> int | None:
    """The index for a configured device name, or None for the default.
    Exact match first, then the first prefix match: Windows truncates
    long device names differently between host APIs."""
    if not name:
        return None
    devices = list_input_devices()
    for index, device_name in devices:
        if device_name == name:
            return index
    for index, device_name in devices:
        if device_name.startswith(name) or name.startswith(device_name):
            return index
    raise AudioUnavailable(f"no audio input device named {name!r}")


class AudioCapture:
    def __init__(
        self,
        device: str | int | None = None,
        *,
        samplerate: int = DEFAULT_SAMPLERATE,
        channels: int = DEFAULT_CHANNELS,
        blocksize: int = DEFAULT_BLOCKSIZE,
        name: str = "microphone",
    ):
        self.device = device
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.name = name
        self._queue: queue.Queue[AudioBlock] = queue.Queue(maxsize=QUEUE_DEPTH)
        self._stream = None
        self._index = 0
        self._latest: AudioBlock | None = None
        self._lock = threading.Lock()
        self.overflows = 0  # PortAudio reported input overflow (device-side loss)
        self.dropped = 0  # blocks we discarded because the queue was full

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:
            raise AudioUnavailable("audio needs sounddevice (pip install sounddevice)") from exc

        device = resolve_device(self.device) if isinstance(self.device, str) else self.device
        try:
            self._stream = sd.InputStream(
                device=device,
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="int16",
                blocksize=self.blocksize,
                callback=self._on_block,
            )
            self._stream.start()
        except Exception as exc:  # sd.PortAudioError, ValueError for a bad device
            self._stream = None
            raise AudioUnavailable(f"{self.name}: could not open {self.device!r}: {exc}") from exc
        logger.info("%s: capture started (%s, %d Hz, %d ch)", self.name, self.device, self.samplerate, self.channels)

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None
        logger.info("%s: capture stopped (%d overflows, %d dropped blocks)", self.name, self.overflows, self.dropped)

    @property
    def running(self) -> bool:
        return self._stream is not None

    # -- the callback (PortAudio's thread) -------------------------------

    def _on_block(self, indata, frames, _time_info, status, timestamp: float | None = None) -> None:
        # Stamped on arrival: the block's last sample is "now", give or
        # take the device's latency, which is the same for every block and
        # so cancels out of everything that matters.
        if timestamp is None:
            timestamp = time.monotonic()
        if status and status.input_overflow:
            self.overflows += 1
        block = AudioBlock(index=self._index, timestamp=timestamp, samples=np.array(indata, dtype=np.int16))
        self._index += 1
        with self._lock:
            self._latest = block
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            self.dropped += 1

    # -- consumers -------------------------------------------------------

    def read(self, timeout: float | None = 0.0) -> AudioBlock | None:
        """The next block, in order, or None if none arrived in `timeout`."""
        try:
            if timeout is None or timeout <= 0:
                return self._queue.get_nowait()
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_latest(self) -> AudioBlock | None:
        with self._lock:
            return self._latest

    def level(self) -> float:
        """RMS of the latest block, 0.0-1.0. For a meter; peeks, never pops."""
        block = self.get_latest()
        if block is None or block.frames == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(block.samples.astype(np.float64) ** 2)))
        return min(1.0, rms / 32768.0)

    def push(self, samples: np.ndarray, timestamp: float | None = None) -> None:
        """Feed a block by hand -- the same path the PortAudio callback
        takes, with the timestamp a test wants rather than the clock's."""
        self._on_block(samples, samples.shape[0], None, None, timestamp=timestamp)


class SyntheticAudio(AudioCapture):
    """A tone, delivered at real time, with no microphone: what
    SyntheticCamera is for a camera. The recorder, export and viewer are
    all exercised end to end against it."""

    def __init__(self, *, frequency_hz: float = 440.0, amplitude: float = 0.3, **kwargs):
        super().__init__(device=None, name="synthetic-audio", **kwargs)
        self.frequency_hz = frequency_hz
        self.amplitude = amplitude
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._phase = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()
        self._stream = self  # so `running` reads True without a device

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._thread = None
        self._stream = None

    def _run(self) -> None:
        period = self.blocksize / self.samplerate
        next_due = time.monotonic()
        t = np.arange(self.blocksize) / self.samplerate
        while not self._stop.is_set():
            phase = self._phase / self.samplerate
            tone = np.sin(2 * np.pi * self.frequency_hz * (t + phase)) * self.amplitude * 32767
            samples = np.repeat(tone.astype(np.int16)[:, None], self.channels, axis=1)
            self._phase += self.blocksize
            next_due += period
            delay = next_due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._on_block(samples, self.blocksize, None, None)
