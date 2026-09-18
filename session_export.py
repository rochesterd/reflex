"""Renders a recorded session's two streams into one shareable MP4, in
whichever layout was asked for.

This is the old live composite, produced on demand instead of on every
recording -- see DECISIONS.md's "Recorder/Viewer split" entries. It
reads only; the streams it composites are never modified, and the output
is written to a temporary file and moved into place, so a cancelled or
failed export can't leave something that looks like a finished one.

No Qt import: viewer.py runs this on a worker thread and reports progress
through the callbacks below.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import av

from compositor import (
    LAYOUT_INSTRUMENT,
    LAYOUT_PICTURE_IN_PICTURE,
    LAYOUT_SIDE_BY_SIDE,
    LAYOUT_THIRD_PERSON,
    compose_layout,
)
from session_format import INSTRUMENT_STREAM, THIRD_PERSON_STREAM
from session_reader import Session, SessionPlayer

logger = logging.getLogger(__name__)

DEFAULT_EXPORT_FPS = 30
DEFAULT_CRF = 23
DEFAULT_PRESET = "ultrafast"


class ExportCancelled(RuntimeError):
    """Raised out of export_session() when cancel_cb() asked it to stop."""


def _add_audio_track(container, session: Session):
    """An AAC track for the session's microphone, or None for a silent
    session. Added before the first video packet so the container's
    header lists both."""
    info = session.audio
    if info is None:
        return None
    try:
        with av.open(str(info.path)) as source:
            template = source.streams.audio[0]
            return container.add_stream_from_template(template)
    except Exception as exc:
        logger.warning("%s: audio track skipped: %s", info.path.name, exc)
        return None


def _copy_audio(container, out_stream, session: Session, duration: float) -> None:
    """Stream-copy the microphone track, already on the session clock, so
    it lands against the video at the same media time. No re-encode: the
    M4A is AAC already, and re-encoding would add nothing but time."""
    info = session.audio
    assert info is not None
    with av.open(str(info.path)) as source:
        in_stream = source.streams.audio[0]
        for packet in source.demux(in_stream):
            if packet.size == 0:
                continue
            if packet.pts is not None and float(packet.pts * in_stream.time_base) > duration + 1.0:
                break  # the mic ran on past the last frame; nothing to hear there
            packet.stream = out_stream
            container.mux(packet)


def _even(value: int) -> int:
    """libx264 with yuv420p needs even dimensions; a camera reporting an
    odd width/height would otherwise fail at encoder open."""
    return max(2, value - (value % 2))


def natural_layout_size(session: Session, layout: str) -> tuple[int, int]:
    """The full-resolution canvas for `layout` -- what an export should use
    so it doesn't throw away detail. Mirrors compositor's own defaults.
    """
    instrument = session.streams.get(INSTRUMENT_STREAM)
    third_person = session.streams.get(THIRD_PERSON_STREAM)
    iw, ih = (instrument.width, instrument.height) if instrument else (0, 0)
    tw, th = (third_person.width, third_person.height) if third_person else (0, 0)

    if layout == LAYOUT_INSTRUMENT:
        width, height = iw, ih
    elif layout == LAYOUT_THIRD_PERSON:
        width, height = tw, th
    elif layout == LAYOUT_PICTURE_IN_PICTURE:
        width, height = (iw, ih) if iw and ih else (tw, th)
    else:  # LAYOUT_SIDE_BY_SIDE
        width, height = iw + tw, max(ih, th)

    # A session with a stream of unknown size still has to export something.
    return (_even(width or 1280), _even(height or 720))


def default_export_name(layout: str) -> str:
    return f"{layout}.mp4"


def export_session(
    session: Session,
    out_path: Path | str,
    layout: str = LAYOUT_SIDE_BY_SIDE,
    fps: int = DEFAULT_EXPORT_FPS,
    out_size: tuple[int, int] | None = None,
    crf: int = DEFAULT_CRF,
    preset: str = DEFAULT_PRESET,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> Path:
    """Render `session` to a single constant-frame-rate MP4 at `out_path`.

    Constant rate, unlike the variable-rate stream files: this is one file
    for sharing or an LMS, where broad player compatibility matters more
    than preserving each camera's true cadence (which the session itself
    still has).

    progress_cb(done, total) is called per frame; cancel_cb() is polled per
    frame and raises ExportCancelled when it returns True. Raises
    ValueError rather than overwriting one of the session's own streams.
    """
    out_path = Path(out_path)
    stream_files = {info.path.resolve() for info in session.streams.values()}
    if out_path.resolve() in stream_files:
        raise ValueError(f"refusing to overwrite the session's own stream file: {out_path.name}")

    if out_size is None:
        out_size = natural_layout_size(session, layout)
    out_size = (_even(out_size[0]), _even(out_size[1]))

    # Written here, moved into place only on success -- a partial file must
    # never end up sitting there looking like a finished export. The real
    # suffix is kept (out.partial.mp4, not out.mp4.partial) because PyAV
    # infers the container format from the extension.
    partial_path = out_path.with_name(f"{out_path.stem}.partial{out_path.suffix}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with SessionPlayer(session) as player:
            total = max(1, int(round(player.duration * fps)))
            container = av.open(str(partial_path), mode="w")
            try:
                stream = container.add_stream("libx264", rate=fps)
                stream.width, stream.height = out_size
                stream.pix_fmt = "yuv420p"
                stream.codec_context.options = {"crf": str(crf), "preset": preset}
                audio_stream = _add_audio_track(container, session)

                for index in range(total):
                    if cancel_cb is not None and cancel_cb():
                        raise ExportCancelled(f"export of {session.directory.name} cancelled")
                    # Media time steps forward only, which is exactly what
                    # SessionPlayer.advance_to() is cheapest at.
                    player.advance_to(index / fps)
                    canvas = compose_layout(player.images(), layout, out_size)
                    frame = av.VideoFrame.from_ndarray(canvas, format="bgr24").reformat(format="yuv420p")
                    frame.pts = index
                    for packet in stream.encode(frame):
                        container.mux(packet)
                    if progress_cb is not None:
                        progress_cb(index + 1, total)

                for packet in stream.encode(None):
                    container.mux(packet)
                if audio_stream is not None:
                    _copy_audio(container, audio_stream, session, player.duration)
            finally:
                container.close()

        partial_path.replace(out_path)
    except BaseException:
        partial_path.unlink(missing_ok=True)
        raise

    logger.info("exported %s (%s, %dx%d) to %s", session.directory.name, layout, *out_size, out_path)
    return out_path
