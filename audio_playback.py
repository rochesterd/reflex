"""Plays a session's microphone track against the viewer's media clock.

The viewer's clock is the master: video frames are presented at media
time `t`, and audio has to come out of the speaker at the same `t`. This
does that the simple way -- decode the whole track into memory once, then
let the output device pull samples from a cursor that play()/seek() place
by media time. A 15-minute mono track at 48 kHz is ~86 MB, which a kiosk
has; streaming decode would add a thread and a ring buffer to save memory
nobody is short of.

The cursor free-runs between seeks on the device's own clock. Over a
session that clock and the viewer's monotonic one drift by milliseconds,
not frames, so there is no per-tick correction -- a correction would be
audible as a click, and the drift is not.

No Qt. The output backend is injectable so tests drive the callback by
hand; the real one is sounddevice.OutputStream, imported lazily so a
silent session, and every machine without sounddevice, never touches it.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Protocol

import av
import numpy as np

logger = logging.getLogger(__name__)


class OutputBackend(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


def _sounddevice_output(samplerate: int, channels: int, callback) -> OutputBackend:
    import sounddevice as sd  # noqa: PLC0415 -- lazy; see the module docstring

    def on_pull(outdata, frames, _time_info, _status) -> None:
        callback(outdata, frames)

    return sd.OutputStream(samplerate=samplerate, channels=channels, dtype="int16", callback=on_pull)


def decode_track(path: Path | str) -> tuple[np.ndarray, int, float]:
    """(pcm int16 (samples, channels), samplerate, start_s) for a whole
    file. start_s is the media time of pcm[0] on the file's own timeline,
    which for AAC is slightly negative (encoder priming) -- callers add
    the stream's offset_s to land it on the session clock."""
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        samplerate = int(stream.rate)
        time_base = stream.time_base
        chunks: list[np.ndarray] = []
        start_s: float | None = None
        for frame in container.decode(stream):
            if start_s is None and frame.pts is not None:
                start_s = float(frame.pts * time_base)
            data = frame.to_ndarray()  # planar or packed, float or int
            if data.dtype != np.int16:
                data = np.clip(data, -1.0, 1.0) * 32767.0
                data = data.astype(np.int16)
            if data.ndim == 2 and frame.format.is_planar:
                data = data.T  # (channels, samples) -> (samples, channels)
            elif data.ndim == 2 and data.shape[0] == 1 and frame.layout.nb_channels > 1:
                data = data.reshape(-1, frame.layout.nb_channels)  # packed
            elif data.ndim == 1:
                data = data[:, None]
            chunks.append(np.ascontiguousarray(data))
    if not chunks:
        return np.zeros((0, 1), dtype=np.int16), samplerate, 0.0
    return np.concatenate(chunks, axis=0), samplerate, start_s or 0.0


class AudioPlayer:
    def __init__(
        self,
        path: Path | str,
        offset_s: float = 0.0,
        *,
        output_factory: Callable[[int, int, Callable], OutputBackend] = _sounddevice_output,
    ):
        self.pcm, self.samplerate, start_s = decode_track(path)
        self.channels = int(self.pcm.shape[1]) if self.pcm.ndim == 2 else 1
        # Media time of pcm[0] on the session clock.
        self._start_s = start_s - offset_s
        self._cursor = 0
        self._playing = False
        self._lock = threading.Lock()
        self._output = output_factory(self.samplerate, self.channels, self._pull)
        self._output.start()  # runs silent until play(); starting once avoids a click per Play

    @property
    def duration(self) -> float:
        return self._start_s + self.pcm.shape[0] / self.samplerate

    @property
    def position(self) -> float:
        """Media time of the next sample to be played."""
        with self._lock:
            return self._start_s + self._cursor / self.samplerate

    @property
    def playing(self) -> bool:
        return self._playing

    def play(self, media_time: float) -> None:
        self.seek(media_time)
        self._playing = True

    def pause(self) -> None:
        self._playing = False

    def seek(self, media_time: float) -> None:
        sample = int(round((media_time - self._start_s) * self.samplerate))
        with self._lock:
            self._cursor = max(0, min(sample, self.pcm.shape[0]))

    def _pull(self, outdata, frames: int) -> None:
        """The device asks for `frames` samples. Silence when paused or past
        the end; otherwise the next slice, and the cursor moves on."""
        if not self._playing:
            outdata[:] = 0
            return
        with self._lock:
            start = self._cursor
            end = min(start + frames, self.pcm.shape[0])
            self._cursor = end
        available = end - start
        if available > 0:
            outdata[:available] = self.pcm[start:end]
        if available < frames:
            outdata[available:] = 0
            self._playing = False  # ran off the end: this pull was the last with sound in it

    def close(self) -> None:
        self._playing = False
        try:
            self._output.stop()
            self._output.close()
        except Exception as exc:  # noqa: BLE001 -- closing is best effort
            logger.warning("audio output close failed: %s", exc)
