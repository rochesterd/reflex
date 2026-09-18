"""Reads a recorded session back: the session.json manifest, and a
timestamp-aligned player over its per-camera video files.

No Qt import, deliberately -- viewer.py is a thin PySide6 shell over this,
the same split app.py/kiosk.py already use, so the playback logic is
unit-testable headlessly against real recorded sessions.

Alignment is purely by presentation timestamp. Every stream was written
with PTS = (grab time - session clock origin), so asking each stream for
"the last frame at or before media time t" yields frames that really were
captured at the same instant -- no frame pairing, no offset search. A
slower camera simply holds its last frame between its own frames, which
is exactly what it was doing in the room. See DECISIONS.md's
"Recorder/Viewer split" entries.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import av
import numpy as np

from session_format import (
    AUDIO_STREAM,
    INSTRUMENT_STREAM,
    MANIFEST_NAME,
    SESSION_FORMAT_VERSION,
    THIRD_PERSON_STREAM,
)

logger = logging.getLogger(__name__)


class SessionError(RuntimeError):
    """A session directory is missing, unreadable, or not a format this
    Viewer understands."""


@dataclass
class StreamInfo:
    role: str
    path: Path
    label: str
    width: int
    height: int
    duration_s: float
    frame_count: int
    dropped_frames: int
    verified: bool
    # "video" or "audio". Audio streams have no width/height/frame_count
    # worth reading; they carry samplerate and channels instead.
    kind: str = "video"
    samplerate: int = 0
    channels: int = 0
    # Per-stream inter-camera latency correction, subtracted from this
    # stream's PTS at alignment time (DECISIONS.md 2026-08-11). Always 0.0
    # until the measurement tooling exists; the plumbing is here so adding
    # it later needs no change to playback.
    offset_s: float = 0.0


@dataclass
class Session:
    directory: Path
    instrument_key: str
    session_start_utc: str
    streams: dict[str, StreamInfo]
    # From the manifest, so a session list can show durations without
    # opening any video. SessionPlayer re-derives this from the containers
    # themselves, which is authoritative for playback.
    duration_s: float = 0.0
    # Roles the manifest declared but whose file isn't on disk -- a camera
    # that captured nothing (the recorder writes no file at all then), or a
    # session copied without all of its files. Recorded rather than dropped
    # silently so the Viewer can say a pane is missing.
    missing_streams: tuple[str, ...] = ()

    @property
    def instrument(self) -> StreamInfo | None:
        return self.streams.get(INSTRUMENT_STREAM)

    @property
    def third_person(self) -> StreamInfo | None:
        return self.streams.get(THIRD_PERSON_STREAM)

    @property
    def audio(self) -> StreamInfo | None:
        """The microphone track, or None for a silent session -- which is
        every session recorded before audio existed, and any recorded on a
        machine with no microphone configured."""
        return self.streams.get(AUDIO_STREAM)

    @property
    def video_streams(self) -> dict[str, StreamInfo]:
        return {role: info for role, info in self.streams.items() if info.kind == "video"}

    @classmethod
    def load(cls, session_dir: Path | str) -> "Session":
        session_dir = Path(session_dir)
        manifest_path = session_dir / MANIFEST_NAME
        if not manifest_path.is_file():
            raise SessionError(f"{session_dir} has no {MANIFEST_NAME} - not a recorded session.")
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"{manifest_path} could not be read: {exc}") from exc

        version = raw.get("format_version")
        if version != SESSION_FORMAT_VERSION:
            # Deliberately loud rather than a best-effort fallback: nothing
            # in the pre-split composite.mp4 format is in circulation, so
            # there's no migration path to honour and guessing would only
            # hide a real problem. See DECISIONS.md.
            raise SessionError(
                f"{manifest_path}: unsupported format_version {version!r} "
                f"(this build reads {SESSION_FORMAT_VERSION})."
            )

        streams_raw = raw.get("streams")
        if not isinstance(streams_raw, dict) or not streams_raw:
            raise SessionError(f"{manifest_path}: 'streams' must be a non-empty object.")

        streams: dict[str, StreamInfo] = {}
        missing: list[str] = []
        for role, entry in streams_raw.items():
            filename = entry.get("file") if isinstance(entry, dict) else None
            path = session_dir / filename if filename else None
            if path is None or not path.is_file():
                # Skipped, not fatal. One camera that captured nothing (the
                # recorder writes no file at all then, and records the
                # reason) must not make the *other* camera's recording
                # unopenable -- refusing the whole session would throw away
                # good, irreplaceable data over a partial failure. The
                # Viewer says which pane is missing.
                logger.warning(
                    "%s: stream %r names %r, which is missing - skipping it",
                    manifest_path, role, filename,
                )
                missing.append(role)
                continue
            streams[role] = StreamInfo(
                role=role,
                path=path,
                label=entry.get("label") or role,
                width=int(entry.get("width", 0)),
                height=int(entry.get("height", 0)),
                duration_s=float(entry.get("duration_s", 0.0)),
                frame_count=int(entry.get("frame_count", 0)),
                dropped_frames=int(entry.get("dropped_frames", 0)),
                verified=bool(entry.get("verified", False)),
                offset_s=float(entry.get("offset_s", 0.0)),
                kind=str(entry.get("kind", "video")),
                samplerate=int(entry.get("samplerate", 0)),
                channels=int(entry.get("channels", 0)),
            )

        if not streams:
            # Nothing at all to play -- that *is* fatal, and distinct from
            # losing one of two panes.
            raise SessionError(
                f"{manifest_path}: none of its stream files are present "
                f"(missing: {', '.join(missing) or 'unknown'})."
            )

        return cls(
            directory=session_dir,
            instrument_key=raw.get("instrument", INSTRUMENT_STREAM),
            session_start_utc=raw.get("session_start_utc", ""),
            streams=streams,
            missing_streams=tuple(missing),
            duration_s=float(raw.get("duration_s", 0.0)),
        )


class _StreamCursor:
    """A decode cursor over one stream, holding the frame that belongs on
    screen at the current media time.

    Decoding and *presenting* are deliberately separate: advancing past
    several frames still decodes each one (H.264 P-frames leave no choice)
    but only the frame actually shown pays for to_ndarray(). So if the UI
    falls behind, it skips presentation, not decoding, and media time
    stays honest.
    """

    def __init__(self, info: StreamInfo):
        self.info = info
        self._container = av.open(str(info.path))
        self._stream = self._container.streams.video[0]
        self._time_base = self._stream.time_base
        self._iter = self._container.decode(self._stream)
        self._current = None  # av.VideoFrame on screen
        self._pending = next(self._iter, None)  # decoded, not yet due
        self._image: np.ndarray | None = None

    @property
    def duration(self) -> float:
        """Media time of this stream's last frame, offset-corrected."""
        if self._stream.duration is not None:
            return float(self._stream.duration * self._time_base) - self.info.offset_s
        if self._container.duration is not None:
            return float(self._container.duration / av.time_base) - self.info.offset_s
        return 0.0

    def _pts_seconds(self, frame) -> float:
        # offset_s shifts this stream's own timeline onto shared media time.
        return float(frame.pts * self._time_base) - self.info.offset_s

    def advance_to(self, t: float) -> bool:
        """Move the cursor to the last frame at or before media time `t`,
        or to this stream's first frame if `t` precedes it. Returns True if
        the on-screen frame changed."""
        changed = False
        while self._pending is not None and self._pts_seconds(self._pending) <= t:
            self._current = self._pending
            self._pending = next(self._iter, None)
            changed = True
        if self._current is None and self._pending is not None:
            # Nothing is due yet: t precedes this stream's first frame.
            # That gap is normal, not an error -- the recorder discards
            # frames captured before the session origin, and the two
            # cameras don't deliver their first frame at the same instant.
            # Showing the first frame is right; black would misrepresent a
            # camera that was running the whole time.
            self._current = self._pending
            self._pending = next(self._iter, None)
            changed = True
        if changed:
            self._image = None
        return changed

    def seek(self, t: float) -> None:
        """Jump to the keyframe at or before `t`, then decode forward to
        it. Bounded by the GOP the recorder wrote (recording.fps frames)."""
        target = max(0.0, t) + self.info.offset_s
        self._container.seek(int(target / self._time_base), stream=self._stream, backward=True, any_frame=False)
        self._iter = self._container.decode(self._stream)
        self._current = None
        self._image = None
        self._pending = next(self._iter, None)
        self.advance_to(t)

    @property
    def image(self) -> np.ndarray | None:
        """The on-screen frame as BGR, converted lazily and cached."""
        if self._current is None:
            return None
        if self._image is None:
            self._image = self._current.to_ndarray(format="bgr24")
        return self._image

    def close(self) -> None:
        self._container.close()


class SessionPlayer:
    """Timestamp-aligned playback across a session's streams.

    Holds an open decoder per stream, so it must be closed (or used as a
    context manager) -- viewer.py ties this to the window's `finished`
    signal, not `closeEvent`, for the reason DECISIONS.md's PreviewDialog
    entry gives.
    """

    def __init__(self, session: Session):
        self.session = session
        self._cursors: dict[str, _StreamCursor] = {}
        try:
            for role, info in session.video_streams.items():
                self._cursors[role] = _StreamCursor(info)
        except Exception:
            self.close()  # don't leak the containers already opened
            raise
        self.duration = max((c.duration for c in self._cursors.values()), default=0.0)
        self._position = 0.0
        self.advance_to(0.0)

    @property
    def position(self) -> float:
        return self._position

    def advance_to(self, t: float) -> bool:
        """Present media time `t`. Only moves forward -- use seek() to go
        back. Returns True if any stream's frame changed."""
        t = max(0.0, min(t, self.duration))
        # A list, not a generator: any() short-circuits, which would leave
        # every stream after the first one that moved un-advanced -- one
        # pane playing and the other frozen.
        changed = [cursor.advance_to(t) for cursor in self._cursors.values()]
        self._position = t
        return any(changed)

    def seek(self, t: float) -> None:
        t = max(0.0, min(t, self.duration))
        for cursor in self._cursors.values():
            cursor.seek(t)
        self._position = t

    def images(self) -> dict[str, np.ndarray | None]:
        """The frame on screen for each stream at the current position."""
        return {role: cursor.image for role, cursor in self._cursors.items()}

    def close(self) -> None:
        for cursor in self._cursors.values():
            cursor.close()
        self._cursors.clear()

    def __enter__(self) -> "SessionPlayer":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
