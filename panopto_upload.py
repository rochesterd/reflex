"""Turns a recorded Session into one Panopto session with two streams.

The counterpart to session_export.export_session(): same place in the
flow, same progress_cb/cancel_cb signature so viewer.py's _ExportWorker
drives either, and the same refusal to leave something behind that looks
finished when it isn't.

What it deliberately does *not* do is composite. export_session() renders
one file because a drive or an LMS wants one file; Panopto takes both
streams and lines them up itself, which is why it was chosen over Canvas
(ROADMAP.md 2026-09-17). Compositing here would throw away the layout
switching that is the point.

Imports session_reader and panopto_api, and nothing else of the app: no
Qt, and no knowledge that HTTP exists. Everything above the wire lives
here and is testable against the in-memory double; everything on the wire
lives in panopto_api.py and is still unverified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from panopto_api import PanoptoClient
from session_format import INSTRUMENT_STREAM, THIRD_PERSON_STREAM
from session_reader import Session

logger = logging.getLogger(__name__)


class UploadCancelled(RuntimeError):
    """Raised when cancel_cb() went True. Mirrors
    session_export.ExportCancelled; kept separate so this module doesn't
    import the encoder to borrow one exception."""


@dataclass(frozen=True)
class UploadFile:
    role: str
    path: Path
    remote_name: str
    is_primary: bool
    offset_s: float
    size_bytes: int


@dataclass(frozen=True)
class UploadManifest:
    """What we intend to send, decided before a byte moves.

    Separated from the sending so it can be asserted on directly: the
    stream ordering and the offsets are the whole synchronization story,
    and a test that checks them needs no site and no network.
    """

    title: str
    description: str
    duration_s: float
    files: tuple[UploadFile, ...]

    @property
    def primary(self) -> UploadFile:
        return next(f for f in self.files if f.is_primary)

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)


def build_manifest(session: Session, title: str | None = None) -> UploadManifest:
    """Decide what goes up, in what order, at what offsets.

    The third-person camera is primary: it carries the room, and per the
    Notion brief it is the stream that will carry audio if audio is ever
    added. The instrument is secondary.

    A session missing one camera still uploads. Half a recording of an
    irreplaceable session is worth more than a refusal, and the student
    cannot re-run the hour -- so the surviving stream becomes primary
    rather than the upload failing.
    """
    files: list[UploadFile] = []
    for role in (THIRD_PERSON_STREAM, INSTRUMENT_STREAM):
        info = session.streams.get(role)
        if info is None:
            continue
        if not info.path.exists():
            logger.warning("session %s lists %s but the file is gone", session.directory, role)
            continue
        files.append(
            UploadFile(
                role=role,
                path=info.path,
                remote_name=info.path.name,
                # Provisional until the real primary is known (below).
                is_primary=False,
                # The offset the Viewer already aligns by: every PTS is
                # relative to one shared clock origin, so this is normally
                # 0.0 and is carried rather than assumed.
                offset_s=float(info.offset_s),
                size_bytes=info.path.stat().st_size,
            )
        )

    if not files:
        raise ValueError(f"{session.directory}: no stream files to upload")

    files[0] = replace(files[0], is_primary=True)

    unverified = [f.role for f in files if not session.streams[f.role].verified]
    if unverified:
        # Not a refusal: recorder.py keeps both the MKV and the MP4 when
        # verification fails, and the MP4 is usually still watchable. But
        # whoever opens it should know, so it goes in the description
        # rather than only into a log nobody reads.
        logger.warning("uploading unverified stream(s): %s", ", ".join(unverified))

    return UploadManifest(
        title=title or default_title(session),
        description=_describe(session, unverified),
        duration_s=float(session.duration_s),
        files=tuple(files),
    )


def default_title(session: Session) -> str:
    """What a student will scan a folder for: the instrument, then when.

    No student name, here or anywhere else in this module. Filing is by
    folder, which is the access decision; a name in the title would put
    one student's identity on a recording their classmate also appears in.
    """
    instrument = session.streams.get(INSTRUMENT_STREAM)
    label = instrument.label if instrument and instrument.label else session.instrument_key
    return f"{label} — {_local_stamp(session.session_start_utc)}"


def upload_session(
    session: Session,
    client: PanoptoClient,
    folder_id: str,
    title: str | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> str:
    """Upload `session` into `folder_id`; return the viewer URL.

    progress_cb(done, total) counts bytes across every file, so one
    progress bar covers the whole upload. cancel_cb() is polled by the
    transfer and raises UploadCancelled.

    Cancelling or failing must not leave a half session that looks
    complete: nothing is finished unless every file arrived, and the
    caller keeps the buffered recording either way -- app.py only marks a
    session exported on success.
    """
    manifest = build_manifest(session, title)
    _check_cancelled(cancel_cb)

    target = client.begin_upload(folder_id)
    logger.info(
        "uploading %s (%d files, %.1f MB) to folder %s",
        session.directory.name,
        len(manifest.files),
        manifest.total_bytes / 1e6,
        folder_id,
    )

    total = manifest.total_bytes
    sent_before = 0
    for upload_file in manifest.files:
        _check_cancelled(cancel_cb)

        def file_progress(done: int, _file_total: int, _base: int = sent_before) -> None:
            if progress_cb is not None:
                progress_cb(min(_base + done, total), total)

        client.put_upload_file(
            target,
            upload_file.path,
            upload_file.remote_name,
            progress_cb=file_progress,
            cancel_cb=cancel_cb,
        )
        sent_before += upload_file.size_bytes
        if progress_cb is not None:
            progress_cb(min(sent_before, total), total)

    _check_cancelled(cancel_cb)
    session_id = client.finish_upload(target)
    return client.viewer_url(session_id)


def _check_cancelled(cancel_cb: Callable[[], bool] | None) -> None:
    if cancel_cb is not None and cancel_cb():
        raise UploadCancelled()


def _describe(session: Session, unverified: list[str]) -> str:
    parts = [
        "Recorded with Reflex for self-review in the Clinical Training Center.",
        f"Session started {session.session_start_utc} (UTC).",
    ]
    dropped = sum(info.dropped_frames for info in session.streams.values())
    if dropped:
        parts.append(f"{dropped} dropped frame(s) across streams.")
    if unverified:
        parts.append(
            f"Stream(s) not verified after recording: {', '.join(unverified)}. "
            f"Playback may be incomplete."
        )
    if session.missing_streams:
        parts.append(f"Missing stream(s): {', '.join(session.missing_streams)}.")
    return " ".join(parts)


def _local_stamp(session_start_utc: str) -> str:
    """'2026-09-17 14:32'. Falls back to the raw string rather than
    raising: a title is not worth failing an upload over."""
    try:
        return datetime.fromisoformat(session_start_utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(session_start_utc)
