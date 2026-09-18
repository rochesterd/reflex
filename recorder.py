"""Records two BaseCamera feeds as two separate variable-frame-rate video
files -- instrument.mp4 and third_person.mp4 -- on one shared clock, plus
a session.json manifest (format_version 2) that the Viewer reads.

No compositing happens here any more. Each frame is encoded at its
camera's native resolution with a presentation timestamp equal to its
grab time minus the session's origin, so a frame at time t in one file
and a frame at time t in the other were captured at the same instant.
That timestamp relationship *is* the synchronization; the Viewer lays the
two out side by side (or however) at watch time. See DECISIONS.md's
"Recorder/Viewer split, phase 1" entry for why this replaced the live
composite.

Each camera gets its own _StreamWriter with its own thread and encoder,
so a slow encode on one can't starve the other's queue. Writers drain
their camera with read() -- every frame, in order -- which is what lets
dropped_frames reflect real gaps in Frame.index (see CLAUDE.md's
Architecture section on read() vs get_latest()).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from fractions import Fraction
from itertools import islice
from pathlib import Path

import av
import cv2
import numpy as np

from camera import BaseCamera, Frame
from audio_capture import AudioCapture
from session_format import (
    AUDIO_STREAM,
    INSTRUMENT_STREAM,
    MANIFEST_NAME,
    SESSION_FORMAT_VERSION,
    THIRD_PERSON_STREAM,
)

logger = logging.getLogger(__name__)

# Millisecond PTS. Both the container stream *and* the encoder context
# must use this -- see DECISIONS.md: with only the stream set, PyAV leaves
# the encoder at 1/fps, ms PTS get rescaled to 1/30 ticks, two ~33ms-apart
# frames collapse into one tick, DTS goes non-monotonic and the MP4 remux
# fails with EINVAL (the MKV muxer tolerates it, so it only shows at remux).
_PTS_TIME_BASE = Fraction(1, 1000)

# Frames to actually decode from the head of a remuxed MP4 when verifying
# it before deleting the interim MKV -- enough to prove the leading
# keyframe survived (the failure mode DECISIONS.md's packet-filter entry
# describes: valid headers, zero decodable frames). ~0.5s at 30fps.
_MP4_VERIFY_DECODE_FRAMES = 15
# Video packets the remuxed MP4 may be short of what was encoded before
# verification fails it -- muxer edge-effect slack.
_MP4_VERIFY_PACKET_SLACK = 2

# How long a writer blocks on its camera queue per loop before re-checking
# its stop flag. Short, so stop() is responsive.
_READ_TIMEOUT_S = 0.1
# After a stop request, how many already-queued frames a writer will still
# absorb. Bounded (not "until empty") because the camera keeps running
# past the session and refilling its queue -- an unbounded drain against
# a producer that's faster than the encoder never returns. See
# DECISIONS.md's "_drain_remaining is one bounded pass" entry.
_STOP_DRAIN_MAX_FRAMES = 4
# Tolerance on the recorder-side rate limit: a frame is accepted once
# 0.9/fps has passed, not a strict 1/fps. A camera pacing itself at
# exactly the recording rate has jitter of a millisecond or two either
# way, and a strict threshold would reject a random ~half of its frames.
# Still limits anything meaningfully faster (a 90fps source at a 30fps
# target passes ~1 in 3).
_RATE_LIMIT_SLACK = 0.9


def _even(value: int) -> int:
    """libx264 with yuv420p refuses odd dimensions -- the encoder won't even
    open, so a camera reporting an odd width/height would record nothing at
    all. Rounded down here and the frames resized to match in
    _StreamWriter._encode(). session_export.py carries the identical helper
    for the same reason on the export side; kept separate rather than
    imported so the writer doesn't depend on the exporter.
    """
    return max(2, value - (value % 2))


def _mp4_verifies(mp4_path: Path, expected_frames: int) -> bool:
    """True if a remuxed MP4 is safe to treat as the sole copy of its
    stream: its first frames actually decode (catches the dropped-leading-
    keyframe remux failure) and it carries essentially all the packets
    that were encoded (catches truncation). Cheap -- a partial decode and
    a demux-only count, not a full decode a waiting student would feel.
    """
    if expected_frames == 0:
        return False  # nothing was recorded -- no basis to verify, so don't drop the MKV
    try:
        with av.open(str(mp4_path)) as container:
            stream = container.streams.video[0]
            packets = sum(1 for packet in container.demux(stream) if packet.size)
        with av.open(str(mp4_path)) as container:
            stream = container.streams.video[0]
            decoded = sum(1 for _ in islice(container.decode(stream), _MP4_VERIFY_DECODE_FRAMES))
    except Exception as exc:
        logger.error("%s: verification errored: %s", mp4_path.name, exc)
        return False

    if decoded == 0:
        logger.error("%s: decoded 0 frames from its first %d packets", mp4_path.name, _MP4_VERIFY_DECODE_FRAMES)
        return False
    if packets < expected_frames - _MP4_VERIFY_PACKET_SLACK:
        logger.error("%s: has %d video packets, expected ~%d", mp4_path.name, packets, expected_frames)
        return False
    return True


def _remux_to_mp4(mkv_path: Path, mp4_path: Path) -> None:
    input_ = av.open(str(mkv_path))
    output = av.open(str(mp4_path), mode="w")
    try:
        in_stream = input_.streams.video[0]
        out_stream = output.add_stream_from_template(in_stream)
        for packet in input_.demux(in_stream):
            # Skip only empty flush packets. Filtering on `packet.dts is
            # None` instead drops the leading keyframe here and produces
            # an MP4 that decodes zero frames. See DECISIONS.md.
            if packet.size == 0:
                continue
            packet.stream = out_stream
            output.mux(packet)
    finally:
        output.close()
        input_.close()


class _StreamWriter:
    """One camera -> one VFR video file, on its own thread."""

    def __init__(
        self,
        role: str,
        camera: BaseCamera,
        label: str,
        session_dir: Path,
        origin_monotonic: float,
        fps: int,
        codec: str,
        crf: int,
        preset: str,
    ):
        self.role = role
        self.camera = camera
        self.label = label
        self.mkv_path = session_dir / f"{role}.mkv"
        self.mp4_path = session_dir / f"{role}.mp4"
        self._origin = origin_monotonic
        self._fps = fps
        self._min_interval_s = (1.0 / fps) * _RATE_LIMIT_SLACK if fps > 0 else 0.0
        self._codec = codec
        self._crf = crf
        self._preset = preset

        self.width = 0
        self.height = 0
        self.frame_count = 0  # frames actually encoded
        self.dropped = 0  # gaps in the source's own Frame.index
        self.rate_limited = 0  # frames declined for arriving faster than fps
        self.first_timestamp: float | None = None
        self.mp4_verified = False
        # Set if the capture/encode loop raised mid-recording (a full disk,
        # an encoder fault). The MKV still holds everything written up to
        # that point -- it is kept, the stream is never claimed verified,
        # and the manifest carries this string so the kiosk summary can say
        # the recording ended early rather than reporting a truncated file
        # as complete. See DECISIONS.md's "Harden the recording path" entry.
        self._error: str | None = None

        self._last_index: int | None = None
        self._last_encoded_ts: float | None = None
        self._last_pts = -1
        self._warned_frame_size = False
        self._container = None
        self._stream = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        # Cameras are live by the time a real caller reaches here (kiosk.py
        # only starts a recording from READY), so .resolution is real.
        source_width, source_height = self.camera.resolution
        if source_width < 2 or source_height < 2:
            # Refuse to start rather than record something broken
            # (CLAUDE.md). _even() floors at 2, so without this a camera
            # reporting 0x0 -- a driver that hasn't filled in its frame
            # size -- would yield a 2x2 encoder and a "verified" recording
            # of nothing. kiosk.start_recording() turns this into a banner.
            raise ValueError(
                f"{self.role}: camera reported an unusable resolution "
                f"{source_width}x{source_height}"
            )
        self.width, self.height = _even(source_width), _even(source_height)
        if (self.width, self.height) != (source_width, source_height):
            logger.warning(
                "%s: camera reports %dx%d; encoding at %dx%d (libx264/yuv420p needs even dimensions)",
                self.role, source_width, source_height, self.width, self.height,
            )

        self._container = av.open(str(self.mkv_path), mode="w")
        self._stream = self._container.add_stream(self._codec, rate=self._fps)
        self._stream.width = self.width
        self._stream.height = self.height
        self._stream.pix_fmt = "yuv420p"
        self._stream.time_base = _PTS_TIME_BASE
        self._stream.codec_context.time_base = _PTS_TIME_BASE
        # g = a keyframe every `fps` frames: <=1s of media for a full-rate
        # stream, proportionally longer for a slower one, but always <=fps
        # frames of decode-forward after a Viewer seek. libx264's default
        # (~250) would make every scrub decode up to ~8s forward.
        self._stream.codec_context.options = {
            "crf": str(self._crf),
            "preset": self._preset,
            "g": str(self._fps),
        }

        # Discard anything queued from before the session's origin, so the
        # first encoded frame is genuinely post-Start rather than a stale
        # frame that would otherwise be clamped to pts 0.
        while self.camera.read(timeout=0) is not None:
            pass

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"writer-{self.role}")
        self._thread.start()

    def request_stop(self) -> None:
        self._stop_event.set()

    def abandon(self) -> None:
        """Close the container without remuxing or verifying, for a session
        that failed to start. There is nothing worth finalizing, and leaving
        the handle open would keep an orphan MKV locked in a session
        directory that will never get a manifest.
        """
        if self._container is not None:
            try:
                self._container.close()
            except Exception as exc:
                logger.warning("%s: closing an abandoned container failed: %s", self.role, exc)
            self._container = None

    def join(self, timeout: float) -> bool:
        """True if the writer thread has exited."""
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def finalize(self) -> None:
        """Flush the encoder, close the MKV, remux to MP4, verify, and
        delete the MKV if it verified. Only call once join() is True --
        touching the encoder while the writer thread may still be encoding
        is what produces a silently corrupt file.

        If the writer loop failed mid-recording (self._error), the encoder
        may be in an unusable state: flush and remux are still attempted
        best-effort, but the MKV is always kept and the stream is never
        claimed verified. The MKV is the interruption-safe copy precisely
        for this case.
        """
        self._thread = None
        try:
            for packet in self._stream.encode(None):
                self._container.mux(packet)
        except Exception as exc:
            logger.error("%s: flushing the encoder failed: %s", self.mkv_path.name, exc)
            if self._error is None:
                self._error = f"encoder flush failed: {exc}"
        finally:
            try:
                self._container.close()
            except Exception as exc:
                logger.error("%s: closing the container failed: %s", self.mkv_path.name, exc)

        if not self.mkv_path.exists():
            # PyAV never creates the container until a packet is written, so
            # a camera that delivered nothing for the whole session leaves no
            # file at all. Say that, rather than letting the remux below fail
            # with a confusing "no such file" -- and info() then omits `file`
            # so the manifest never names something that isn't there.
            self.mp4_verified = False
            if self._error is None:
                self._error = "no frames were captured from this camera"
            logger.error("%s: no frames captured; no file written", self.role)
            return

        try:
            _remux_to_mp4(self.mkv_path, self.mp4_path)
        except Exception as exc:
            logger.error("%s: remux to MP4 failed: %s", self.mp4_path.name, exc)
            self.mp4_verified = False
            if self._error is None:
                self._error = f"remux to MP4 failed: {exc}"
            return

        if self._error is not None:
            self.mp4_verified = False
            logger.error(
                "%s: writer failed mid-recording (%s); keeping %s as the recoverable copy",
                self.role, self._error, self.mkv_path.name,
            )
            return

        if _mp4_verifies(self.mp4_path, self.frame_count):
            self.mp4_verified = True
            self.mkv_path.unlink()
            logger.info("%s: verified (%d frames); removed interim %s", self.mp4_path.name, self.frame_count, self.mkv_path.name)
        else:
            self.mp4_verified = False
            logger.error("%s: did not verify; keeping %s as the recoverable copy", self.mp4_path.name, self.mkv_path.name)

    # --- capture thread ------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                frame = self.camera.read(timeout=_READ_TIMEOUT_S)
                if frame is not None:
                    self._absorb(frame)
            for _ in range(_STOP_DRAIN_MAX_FRAMES):
                frame = self.camera.read(timeout=0)
                if frame is None:
                    break
                self._absorb(frame)
        except Exception as exc:
            # A full disk or an encoder fault must not just kill this thread
            # silently -- BaseCamera._run() guards its capture loop for the
            # same reason. finalize() sees self._error, keeps the MKV, and
            # the manifest/summary report a recording that ended early
            # rather than a truncated file that looks complete. The other
            # stream's writer is a separate thread and keeps going.
            logger.exception("%s: writer loop failed after %d frames; stopping this stream", self.role, self.frame_count)
            self._error = f"{type(exc).__name__}: {exc}"

    def _absorb(self, frame: Frame) -> None:
        if self.first_timestamp is None:
            self.first_timestamp = frame.timestamp
        if self._last_index is not None and frame.index > self._last_index + 1:
            self.dropped += frame.index - self._last_index - 1
        self._last_index = frame.index

        # Recorder-side guarantee of "never faster than fps". The camera-
        # side caps are best-effort (a device may ignore them); this isn't.
        if self._last_encoded_ts is not None and frame.timestamp - self._last_encoded_ts < self._min_interval_s:
            self.rate_limited += 1
            return

        self._encode(frame)
        self._last_encoded_ts = frame.timestamp

    def _encode(self, frame: Frame) -> None:
        pts = int(round((frame.timestamp - self._origin) * 1000))
        if pts <= self._last_pts:
            pts = self._last_pts + 1  # bump, never drop: PTS must be strictly increasing
        self._last_pts = pts

        image = frame.image
        if image.shape[1] != self.width or image.shape[0] != self.height:
            # The encoder context is fixed at the resolution the camera
            # reported at start(); a frame that doesn't match it would make
            # stream.encode() raise. UvcCamera._try_reconnect() can reopen a
            # device at a different size after a USB drop -- resize to fit
            # rather than lose the rest of the recording. Rare and already
            # a degraded path, so a plain (possibly aspect-distorting)
            # resize is the right trade against dropping the stream.
            if not self._warned_frame_size:
                logger.warning(
                    "%s: got a %dx%d frame, encoder expects %dx%d; resizing to fit for the rest of the recording",
                    self.role, image.shape[1], image.shape[0], self.width, self.height,
                )
                self._warned_frame_size = True
            image = cv2.resize(image, (self.width, self.height))

        video_frame = av.VideoFrame.from_ndarray(image, format="bgr24").reformat(format="yuv420p")
        video_frame.pts = pts
        video_frame.time_base = _PTS_TIME_BASE
        for packet in self._stream.encode(video_frame):
            self._container.mux(packet)
        self.frame_count += 1

    # --- manifest ----------------------------------------------------------

    @property
    def duration_s(self) -> float:
        """Media time of the last frame written. Recorded in the manifest so
        a session list can show durations without opening every video."""
        return max(0.0, self._last_pts / 1000.0)

    def info(self) -> dict:
        # Point `file` at whatever actually exists to play. Normally that's
        # the verified MP4; if the remux failed it's the MKV, which the
        # Viewer and Export both open fine (PyAV reads either). If a camera
        # captured nothing there is no file at all -- `file` is null rather
        # than naming a phantom, so session_reader skips this stream instead
        # of refusing the whole session (which would throw away the other
        # camera's good recording). See DECISIONS.md's 2026-09-09 entry.
        mkv_exists = self.mkv_path.exists()
        if self.mp4_path.exists():
            playable_name: str | None = self.mp4_path.name
        elif mkv_exists:
            playable_name = self.mkv_path.name
        else:
            playable_name = None
        data = {
            "file": playable_name,
            "label": self.label,
            "width": self.width,
            "height": self.height,
            "duration_s": round(self.duration_s, 3),
            "frame_count": self.frame_count,
            "dropped_frames": self.dropped,
            "rate_limited_frames": self.rate_limited,
            "first_timestamp": self.first_timestamp,
            # Reserved for the per-stream inter-camera latency correction
            # (DECISIONS.md 2026-08-11); the Viewer subtracts this from the
            # stream's PTS. No measurement tooling yet, so always 0.0.
            "offset_s": 0.0,
            "verified": self.mp4_verified,
        }
        if self._error is not None:
            data["error"] = self._error
        if not self.mp4_verified and mkv_exists and self.mkv_path.name != playable_name:
            data["mkv"] = self.mkv_path.name
        return data


_AUDIO_CODEC = "aac"
# Silence is inserted when a block arrives later than its sample count
# predicts by more than this: an overflow lost samples, and the timeline
# must stay honest even if the audio has a hole in it.
_AUDIO_GAP_TOLERANCE_S = 0.040
_AUDIO_VERIFY_DECODE_FRAMES = 4


def _m4a_verifies(m4a_path: Path, expected_samples: int, samplerate: int) -> bool:
    """The audio counterpart of _mp4_verifies: the first packets decode,
    and the file carries about as much audio as was encoded."""
    if expected_samples == 0:
        return False
    try:
        with av.open(str(m4a_path)) as container:
            stream = container.streams.audio[0]
            packets = sum(1 for packet in container.demux(stream) if packet.size)
        with av.open(str(m4a_path)) as container:
            stream = container.streams.audio[0]
            decoded = sum(1 for _ in islice(container.decode(stream), _AUDIO_VERIFY_DECODE_FRAMES))
    except Exception as exc:
        logger.error("%s: verification errored: %s", m4a_path.name, exc)
        return False
    if decoded == 0:
        logger.error("%s: decoded 0 audio frames", m4a_path.name)
        return False
    # AAC packs 1024 samples a packet; allow the encoder's priming/flush slack.
    expected_packets = expected_samples // 1024
    if packets < expected_packets - 4:
        logger.error("%s: has %d audio packets, expected ~%d", m4a_path.name, packets, expected_packets)
        return False
    return True


def _remux_audio_to_m4a(mka_path: Path, m4a_path: Path) -> None:
    input_ = av.open(str(mka_path))
    output = av.open(str(m4a_path), mode="w")
    try:
        in_stream = input_.streams.audio[0]
        out_stream = output.add_stream_from_template(in_stream)
        for packet in input_.demux(in_stream):
            if packet.size == 0:
                continue
            packet.stream = out_stream
            output.mux(packet)
    finally:
        output.close()
        input_.close()


class _AudioWriter:
    """The microphone -> one AAC file, on its own thread, on the shared clock.

    Mirrors _StreamWriter: MKA written live and interruption-safe, remuxed
    to M4A, verified, MKA deleted. Every sample's position is derived from
    its block's monotonic timestamp against the session origin, exactly as
    a video frame's PTS is -- so audio and video line up by construction,
    not by a measured offset. Time base 1/samplerate; PTS is the sample
    index on the session clock.
    """

    def __init__(self, capture: AudioCapture, session_dir: Path, origin_monotonic: float):
        self.role = AUDIO_STREAM
        self.capture = capture
        self.label = "microphone"
        self.mka_path = session_dir / f"{AUDIO_STREAM}.mka"
        self.m4a_path = session_dir / f"{AUDIO_STREAM}.m4a"
        self._origin = origin_monotonic
        self.samplerate = capture.samplerate
        self.channels = capture.channels
        self.samples_written = 0
        self.silence_inserted = 0  # samples of silence filling gaps
        self.dropped_blocks = 0  # gaps in AudioBlock.index
        self.first_timestamp: float | None = None
        self.verified = False
        self._error: str | None = None
        self._last_index: int | None = None
        self._next_pts = 0  # sample index of the next sample to write
        self._container = None
        self._stream = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._container = av.open(str(self.mka_path), mode="w")
        self._stream = self._container.add_stream(_AUDIO_CODEC, rate=self.samplerate)
        self._stream.codec_context.format = "fltp"  # what libavcodec's aac takes
        layout = "mono" if self.channels == 1 else "stereo"
        self._stream.codec_context.layout = layout
        self._stream.time_base = Fraction(1, self.samplerate)
        self._stream.codec_context.time_base = Fraction(1, self.samplerate)
        # Discard what queued before the origin, as the video writers do.
        while self.capture.read(timeout=0) is not None:
            pass
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="writer-audio")
        self._thread.start()

    def request_stop(self) -> None:
        self._stop_event.set()

    def abandon(self) -> None:
        if self._container is not None:
            try:
                self._container.close()
            except Exception as exc:
                logger.warning("audio: closing an abandoned container failed: %s", exc)
            self._container = None

    def join(self, timeout: float) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def finalize(self) -> None:
        self._thread = None
        try:
            for packet in self._stream.encode(None):
                self._container.mux(packet)
        except Exception as exc:
            logger.error("%s: flushing the encoder failed: %s", self.mka_path.name, exc)
            if self._error is None:
                self._error = f"encoder flush failed: {exc}"
        finally:
            try:
                self._container.close()
            except Exception as exc:
                logger.error("%s: closing the container failed: %s", self.mka_path.name, exc)

        if not self.mka_path.exists():
            self.verified = False
            if self._error is None:
                self._error = "no audio was captured"
            logger.error("audio: nothing captured; no file written")
            return
        try:
            _remux_audio_to_m4a(self.mka_path, self.m4a_path)
        except Exception as exc:
            logger.error("%s: remux failed: %s", self.m4a_path.name, exc)
            self.verified = False
            if self._error is None:
                self._error = f"remux to M4A failed: {exc}"
            return
        if self._error is not None:
            self.verified = False
            logger.error("audio: writer failed mid-recording (%s); keeping %s", self._error, self.mka_path.name)
            return
        if _m4a_verifies(self.m4a_path, self.samples_written, self.samplerate):
            self.verified = True
            self.mka_path.unlink()
            logger.info("%s: verified (%.1fs); removed interim %s", self.m4a_path.name, self.duration_s, self.mka_path.name)
        else:
            self.verified = False
            logger.error("%s: did not verify; keeping %s", self.m4a_path.name, self.mka_path.name)

    # -- capture thread ---------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                block = self.capture.read(timeout=_READ_TIMEOUT_S)
                if block is not None:
                    self._absorb(block)
            for _ in range(_STOP_DRAIN_MAX_FRAMES):
                block = self.capture.read(timeout=0)
                if block is None:
                    break
                self._absorb(block)
        except Exception as exc:
            logger.exception("audio: writer loop failed after %d samples", self.samples_written)
            self._error = f"{type(exc).__name__}: {exc}"

    def _absorb(self, block) -> None:
        if self._last_index is not None and block.index > self._last_index + 1:
            self.dropped_blocks += block.index - self._last_index - 1
        self._last_index = block.index

        # Where this block's *first* sample belongs on the session clock.
        block_start_s = (block.timestamp - self._origin) - block.frames / self.samplerate
        if self.first_timestamp is None:
            self.first_timestamp = block.timestamp
            self._next_pts = max(0, int(round(block_start_s * self.samplerate)))
        else:
            expected_s = self._next_pts / self.samplerate
            behind_s = block_start_s - expected_s
            if behind_s > _AUDIO_GAP_TOLERANCE_S:
                # Samples went missing (an overflow, a stalled callback).
                # Fill with silence so what follows lands where it belongs.
                gap = int(round(behind_s * self.samplerate))
                self._encode(np.zeros((gap, self.channels), dtype=np.int16))
                self.silence_inserted += gap
        self._encode(block.samples)

    def _encode(self, samples: np.ndarray) -> None:
        if samples.shape[0] == 0:
            return
        # PyAV wants (channels, samples) for packed->planar conversion.
        frame = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(samples.T), format="s16", layout="mono" if self.channels == 1 else "stereo"
        )
        frame.sample_rate = self.samplerate
        frame.pts = self._next_pts
        frame.time_base = Fraction(1, self.samplerate)
        for packet in self._stream.encode(frame):
            self._container.mux(packet)
        self._next_pts += samples.shape[0]
        self.samples_written += samples.shape[0]

    # -- manifest ---------------------------------------------------------

    @property
    def duration_s(self) -> float:
        return max(0.0, self._next_pts / self.samplerate)

    def info(self) -> dict:
        mka_exists = self.mka_path.exists()
        if self.m4a_path.exists():
            playable: str | None = self.m4a_path.name
        elif mka_exists:
            playable = self.mka_path.name
        else:
            playable = None
        data = {
            "file": playable,
            "kind": "audio",
            "label": self.label,
            "samplerate": self.samplerate,
            "channels": self.channels,
            "duration_s": round(self.duration_s, 3),
            "samples": self.samples_written,
            "silence_inserted_samples": self.silence_inserted,
            "dropped_blocks": self.dropped_blocks,
            "overflows": self.capture.overflows,
            "first_timestamp": self.first_timestamp,
            "offset_s": 0.0,
            "verified": self.verified,
        }
        if self._error is not None:
            data["error"] = self._error
        if not self.verified and mka_exists and self.mka_path.name != playable:
            data["mka"] = self.mka_path.name
        return data


class Recorder:
    """Records the selected instrument camera and the third-person camera
    as two synchronized VFR files -- and the microphone, if one is given,
    as a third file on the same clock -- in a fresh session directory."""

    def __init__(
        self,
        instrument_camera: BaseCamera,
        third_person_camera: BaseCamera,
        instrument_key: str,
        instrument_label: str | None = None,
        third_person_label: str | None = None,
        output_root: str | Path = "sessions",
        fps: int = 30,
        codec: str = "libx264",
        crf: int = 23,
        preset: str = "ultrafast",
        audio: AudioCapture | None = None,
    ):
        self.instrument_camera = instrument_camera
        # None means a silent session, which every session before audio
        # existed is. The capture is started and owned by whoever made it
        # (kiosk.py), the way the third-person camera is.
        self.audio = audio
        self.third_person_camera = third_person_camera
        self.instrument_key = instrument_key
        self.instrument_label = instrument_label or instrument_key
        self.third_person_label = third_person_label or THIRD_PERSON_STREAM
        self.output_root = Path(output_root)
        self.fps = fps
        self.codec = codec
        self.crf = crf
        self.preset = preset

        self.session_dir: Path | None = None
        self._writers: list = []  # _StreamWriter and, if configured, one _AudioWriter
        self._start_wall: datetime | None = None
        self._origin_monotonic: float | None = None

    def start(self) -> None:
        self.session_dir = self._make_session_dir()
        self._start_wall = datetime.now(timezone.utc)
        self._origin_monotonic = time.monotonic()

        self._writers = [
            _StreamWriter(
                INSTRUMENT_STREAM, self.instrument_camera, self.instrument_label, self.session_dir,
                self._origin_monotonic, self.fps, self.codec, self.crf, self.preset,
            ),
            _StreamWriter(
                THIRD_PERSON_STREAM, self.third_person_camera, self.third_person_label, self.session_dir,
                self._origin_monotonic, self.fps, self.codec, self.crf, self.preset,
            ),
        ]
        if self.audio is not None:
            self._writers.append(_AudioWriter(self.audio, self.session_dir, self._origin_monotonic))
        started: list = []
        try:
            for writer in self._writers:
                writer.start()
                started.append(writer)
        except Exception:
            # Don't leave a half-started session behind: an already-running
            # writer thread would go on filling an MKV that nothing will
            # ever finalize, in a directory that will never get a manifest.
            for writer in started:
                writer.request_stop()
            for writer in started:
                writer.join(timeout=2.0)
                writer.abandon()
            self._writers = []
            # Remove the directory just created if nothing landed in it: a
            # repeated failed Start would otherwise litter the buffer with
            # empty folders, which nothing else would clean up
            # (no session.json means "a failed session a technician should
            # look at").
            try:
                if not any(self.session_dir.iterdir()):
                    self.session_dir.rmdir()
            except OSError as exc:
                logger.warning("could not remove the empty session dir %s: %s", self.session_dir, exc)
            raise

    def failed_stream(self) -> tuple[str, str] | None:
        """(role, error) of the first writer that raised mid-recording, or
        None while both are healthy. kiosk.poll_recording() checks this so a
        dead writer stops the session loudly and immediately, rather than
        one stream running on frozen until Stop. See DECISIONS.md's "Harden
        the recording path" entry.
        """
        for writer in self._writers:
            if writer._error is not None:
                return writer.role, writer._error
        return None

    def stop(self) -> dict:
        for writer in self._writers:
            writer.request_stop()
        stuck = [writer.role for writer in self._writers if not writer.join(timeout=10.0)]
        if stuck:
            # Loud and early (see CLAUDE.md) rather than flushing an encoder
            # another thread may still be writing to -- that race is what
            # produces a corrupt/short file with no warning. Logged as well
            # as raised: kiosk.py's _fail() catches broadly to still record
            # a summary, and would otherwise leave no trace of why.
            logger.error(
                "session_dir=%s: writer thread(s) %s did not stop within 10s; refusing to finalize",
                self.session_dir, stuck,
            )
            raise RuntimeError(
                f"Recorder writer thread(s) {stuck} did not stop within 10s; refusing to finalize "
                "while they may still be writing, to avoid a silently corrupt recording."
            )

        for writer in self._writers:
            writer.finalize()

        session_info = self._build_session_info()
        with open(self.session_dir / MANIFEST_NAME, "w", encoding="utf-8") as f:
            json.dump(session_info, f, indent=2)
        return session_info

    def _make_session_dir(self) -> Path:
        base = datetime.now().strftime("%Y-%m-%d_%H%M")
        candidate = self.output_root / base
        suffix = 1
        while candidate.exists():
            suffix += 1
            candidate = self.output_root / f"{base}_{suffix}"
        candidate.mkdir(parents=True, exist_ok=False)
        return candidate

    def _build_session_info(self) -> dict:
        return {
            "format_version": SESSION_FORMAT_VERSION,
            "session_start_utc": self._start_wall.isoformat(),
            "instrument": self.instrument_key,
            # t=0 for every PTS in every stream. Two frames with equal PTS
            # in different files were grabbed at the same instant.
            "clock": {"origin_monotonic": self._origin_monotonic},
            "fps": self.fps,
            "duration_s": round(
                max((w.duration_s for w in self._writers if isinstance(w, _StreamWriter)), default=0.0), 3
            ),
            "streams": {writer.role: writer.info() for writer in self._writers},
        }
