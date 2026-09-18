"""Session viewer: plays back a recorded session, laying its two streams
out on demand rather than however they were composited at record time
(they aren't -- see recorder.py), and exporting one shareable file when
asked.

A thin PySide6 shell over session_reader.SessionPlayer and
session_export.export_session, the same split app.py has over kiosk.py.
Opened modally by app.py when a recording stops. There is no standalone
viewer: see DECISIONS.md's 2026-09-14 "The standalone viewer is gone".

Student-facing, same audience as recording (CLAUDE.md's "Who uses it"):
play/pause, scrub, a layout picker, and Export. Nothing here can modify or
delete a recording -- Export only ever writes a new file.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QInputDialog,
    QProgressDialog,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from compositor import LAYOUT_MODES, LAYOUT_TITLES, compose_layout
from qt_image import bgr_to_pixmap
import reflex_style
from session_buffer import default_export_dir, removable_drives_detailed
from session_export import ExportCancelled, default_export_name, export_session
from audio_playback import AudioPlayer
from session_reader import Session, SessionError, SessionPlayer

logger = logging.getLogger(__name__)

TICK_MS = 33  # ~30Hz, matching the recording rate ceiling
SLIDER_STEPS = 1000
DEFAULT_CANVAS = (1280, 620)
EXPORT_FPS = 30
# How long to wait for the export thread to notice a cancel before giving
# up on it. It checks per frame, so this is generous.
_EXPORT_JOIN_TIMEOUT_S = 10.0


def _mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:d}:{seconds % 60:02d}"


def _default_panopto_session(panopto):
    """A real login and client for config.PanoptoConfig. Imported here so
    viewer.py stays importable, and testable, with no Panopto at all."""
    from panopto_api import PanoptoClient, UserLogin

    login = UserLogin(
        panopto.host,
        panopto.client_id,
        panopto.client_secret,
        redirect_port=panopto.redirect_port,
    )
    return login, PanoptoClient(panopto.host, login)


class _ExportWorker(QObject):
    """Runs export_session on a plain thread, reporting back through Qt
    signals (emitting a signal from another thread is queued, so the slots
    still run on the UI thread)."""

    progress = Signal(int, int)
    done = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, session: Session, out_path: Path, layout: str, fps: int):
        super().__init__()
        self._session = session
        self._out_path = out_path
        self._layout = layout
        self._fps = fps
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            path = export_session(
                self._session,
                self._out_path,
                layout=self._layout,
                fps=self._fps,
                progress_cb=self.progress.emit,
                cancel_cb=self._cancel.is_set,
            )
        except ExportCancelled:
            self.cancelled.emit()
        except Exception as exc:  # a bad codec, a full disk, a read-only folder
            logger.exception("export failed")
            self.failed.emit(str(exc))
        else:
            self.done.emit(str(path))


class _PanoptoWorker(QObject):
    """Signs the student in, uploads, and signs them out, on a plain thread.

    Same shape as _ExportWorker so the progress dialog and outcome handling
    read the same. The sign-out is in a `finally`: whatever happened, the
    token must not outlive this run on a shared machine.
    """

    progress = Signal(int, int)
    stage = Signal(str)
    done = Signal(str)
    failed = Signal(str)
    sign_in_failed = Signal(str)
    cancelled = Signal()

    def __init__(self, session: Session, login, client, folder_id: str):
        super().__init__()
        self._session = session
        self._login = login
        self._client = client
        self._folder_id = folder_id
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        from panopto_api import LoginCancelled, PanoptoAuthError, PanoptoError
        from panopto_upload import UploadCancelled, upload_session

        try:
            self.stage.emit("Sign in using the browser window that just opened...")
            self._login.sign_in(cancel_cb=self._cancel.is_set)
            self.stage.emit("Sending to Panopto...")
            url = upload_session(
                self._session,
                self._client,
                self._folder_id,
                progress_cb=self.progress.emit,
                cancel_cb=self._cancel.is_set,
            )
        except (LoginCancelled, UploadCancelled):
            self.cancelled.emit()
        except PanoptoAuthError as exc:
            logger.warning("Panopto sign-in problem: %s", exc)
            self.sign_in_failed.emit(str(exc))
        except PanoptoError as exc:
            logger.warning("Panopto upload failed: %s", exc)
            self.failed.emit(str(exc))
        except Exception as exc:  # a missing boto3, a read error mid-transfer
            logger.exception("Panopto upload failed")
            self.failed.emit(str(exc))
        else:
            self.done.emit(url)
        finally:
            self._login.sign_out()


class ViewerDialog(QDialog):
    """Playback window for one session.

    A QDialog rather than a QMainWindow so app.py can open it modally with
    .exec(): the kiosk's own controls are then unreachable while it's up.

    Teardown hangs off the `finished` signal, not closeEvent: Esc routes
    through QDialog.reject(), which never delivers a QCloseEvent, and this
    dialog holds open PyAV decoders. That's the same leak DECISIONS.md's
    "settings.py Preview leaked the IDS device" entry is about.
    """

    def __init__(
        self,
        session: Session,
        parent=None,
        on_export=None,
        confirm_close=None,
        panopto=None,
        panopto_session_factory=_default_panopto_session,
    ):
        super().__init__(parent)
        self.session = session
        # config.PanoptoConfig, or None on a machine that doesn't upload --
        # the normal state, in which the button simply isn't there. The
        # factory is injectable so tests can hand in a login and client
        # backed by an in-memory Panopto.
        self._panopto = panopto
        self._panopto_session_factory = panopto_session_factory
        # Called with the written path -- or, for Panopto, the viewer URL --
        # when an export finishes. The kiosk uses it to tell an exported
        # session from one its buffer is about to delete (see app.py).
        self._on_export = on_export
        # Called with this dialog when something tries to close it; return
        # False to keep it open. The kiosk uses it to ask about saving
        # while the student can still act on the answer -- see app.py's
        # _review_last_session() and DECISIONS.md 2026-09-14.
        self._confirm_close = confirm_close
        self._close_allowed = False
        self.setWindowTitle(f"Recording - {session.directory.name}")
        self.setSizeGripEnabled(True)

        self.player = SessionPlayer(session)
        # The microphone track, following the video clock: play/pause/seek
        # below drive it. None for a silent session, and also when the
        # speaker can't be opened -- a review without sound beats no review.
        self.audio: AudioPlayer | None = None
        if session.audio is not None:
            try:
                self.audio = AudioPlayer(session.audio.path, session.audio.offset_s)
            except Exception as exc:  # noqa: BLE001 -- no output device, a bad file
                logger.warning("audio playback unavailable: %s", exc)
                self.audio = None
        self._playing = False
        self._play_started_wall = 0.0
        self._play_started_media = 0.0
        self._scrubbing = False
        self._resume_after_scrub = False
        self._dirty = True
        self.finished.connect(self._shutdown)

        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setMinimumSize(640, 300)
        self.video_label.setStyleSheet("background-color: black;")

        self.play_button = QPushButton("Play")
        self.play_button.setMinimumHeight(40)
        self.play_button.clicked.connect(self._toggle_play)

        instrument = session.instrument
        third = session.third_person
        names = {
            "instrument": instrument.label if instrument else "Instrument",
            "third_person": third.label if third else "Third-person",
        }
        self.layout_box = QComboBox()
        for mode in LAYOUT_MODES:
            self.layout_box.addItem(LAYOUT_TITLES[mode].format(**names), mode)
        self.layout_box.currentIndexChanged.connect(self._on_layout_changed)

        self.export_button = QPushButton("Export video...")
        self.export_button.setMinimumHeight(40)
        self.export_button.setToolTip("Save the current view as a single video file")
        self.export_button.clicked.connect(self._on_export_clicked)

        self.panopto_button = QPushButton("Send to Panopto...")
        self.panopto_button.setMinimumHeight(40)
        self.panopto_button.setToolTip("Sign in and upload both views to your course folder")
        self.panopto_button.clicked.connect(self._on_panopto_clicked)
        self.panopto_button.setVisible(self._panopto is not None)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, SLIDER_STEPS)
        self.slider.sliderPressed.connect(self._on_scrub_start)
        self.slider.sliderMoved.connect(self._on_scrub_move)
        self.slider.sliderReleased.connect(self._on_scrub_end)

        self.time_label = QLabel()
        self.time_label.setMinimumWidth(90)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.status_label = QLabel(self._describe_session())
        self.status_label.setObjectName(reflex_style.SECONDARY)
        self.status_label.setWordWrap(True)

        controls = QHBoxLayout()
        controls.addWidget(self.play_button)
        controls.addWidget(self.slider, stretch=1)
        controls.addWidget(self.time_label)
        controls.addWidget(QLabel("View:"))
        controls.addWidget(self.layout_box)
        controls.addWidget(self.export_button)
        controls.addWidget(self.panopto_button)

        layout = QVBoxLayout()
        layout.addWidget(self.video_label, stretch=1)
        layout.addLayout(controls)
        layout.addWidget(self.status_label)
        self.setLayout(layout)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)

        self._sync_position_ui()
        self._render()

    # --- session description ------------------------------------------------

    def _describe_session(self) -> str:
        parts = [f"{_mmss(self.player.duration)} recording"]
        for stream in self.session.streams.values():
            note = "" if stream.verified else "  (unverified - see the .mkv beside it)"
            parts.append(f"{stream.label}: {stream.width}x{stream.height}, {stream.frame_count} frames{note}")
        for role in self.session.missing_streams:
            # Say the pane is absent rather than let it render as black and
            # look like a bug in playback.
            parts.append(f"{role}: no video recorded")
        if self.session.audio is not None:
            parts.append("with sound" if self.audio is not None else "sound recorded, but no speaker")
        return "   |   ".join(parts)

    # --- playback -------------------------------------------------------------

    def _toggle_play(self) -> None:
        if not self._playing and self.player.position >= self.player.duration:
            self.player.seek(0.0)  # replay from the top rather than sitting at the end
            if self.audio is not None:
                self.audio.seek(0.0)
        self._set_playing(not self._playing)

    def _set_playing(self, playing: bool) -> None:
        self._playing = playing
        self.play_button.setText("Pause" if playing else "Play")
        if playing:
            self._play_started_wall = time.monotonic()
            self._play_started_media = self.player.position
            if self.audio is not None:
                self.audio.play(self.player.position)
        elif self.audio is not None:
            self.audio.pause()
        self._dirty = True

    def _tick(self) -> None:
        if self._playing and not self._scrubbing:
            elapsed = time.monotonic() - self._play_started_wall
            target = self._play_started_media + elapsed
            if target >= self.player.duration:
                target = self.player.duration
                self._set_playing(False)
            # advance_to only moves forward and decodes what it must -- if
            # the UI falls behind, frames are skipped for *presentation*,
            # so media time never drifts from wall-clock.
            if self.player.advance_to(target):
                self._dirty = True
            self._sync_position_ui()
        if self._dirty:
            self._render()
            self._dirty = False

    def _sync_position_ui(self) -> None:
        duration = self.player.duration
        if not self._scrubbing:
            fraction = (self.player.position / duration) if duration > 0 else 0.0
            self.slider.blockSignals(True)
            self.slider.setValue(int(fraction * SLIDER_STEPS))
            self.slider.blockSignals(False)
        self.time_label.setText(f"{_mmss(self.player.position)} / {_mmss(duration)}")

    # --- scrubbing -----------------------------------------------------------

    def _on_scrub_start(self) -> None:
        self._scrubbing = True
        self._resume_after_scrub = self._playing
        self._set_playing(False)

    def _on_scrub_move(self, value: int) -> None:
        self.player.seek(self.player.duration * (value / SLIDER_STEPS))
        if self.audio is not None:
            self.audio.seek(self.player.position)
        self.time_label.setText(f"{_mmss(self.player.position)} / {_mmss(self.player.duration)}")
        self._dirty = True

    def _on_scrub_end(self) -> None:
        self._scrubbing = False
        if self._resume_after_scrub:
            self._set_playing(True)
        self._sync_position_ui()

    def _on_layout_changed(self, _index: int) -> None:
        self._dirty = True

    # --- export ----------------------------------------------------------------

    def _on_export_clicked(self) -> None:
        layout_mode = self.layout_box.currentData()
        destination = self._choose_destination()
        if destination is None:
            return  # the student backed out of the drive chooser
        suggested = destination / default_export_name(layout_mode)
        chosen, _filter = QFileDialog.getSaveFileName(
            self, "Export video", str(suggested), "MP4 video (*.mp4)"
        )
        if not chosen:
            return
        self._run_export(Path(chosen), layout_mode)

    def _choose_destination(self) -> Path | None:
        """Which drive the save dialog should open on, or None to abort.

        Deliberately not the session folder: on a kiosk that folder is a
        temporary buffer, so saving into it saves nothing (session_buffer.py).
        With one drive plugged in there is no question to ask. With two or
        more there is a real one -- a student's own stick beside somebody
        else's, or a card reader -- and picking the first by drive letter
        would quietly write a peer's recording onto a stranger's drive. The
        save dialog can still go anywhere; this only decides where it opens.
        """
        drives = removable_drives_detailed()
        if len(drives) < 2:
            return default_export_dir()

        choices = [drive.describe() for drive in drives]
        picked, ok = QInputDialog.getItem(
            self,
            "Which drive?",
            "More than one drive is plugged in. Choose yours:",
            choices,
            0,
            False,  # not editable: these are the drives that exist
        )
        if not ok:
            return None
        return drives[choices.index(picked)].path

    def _run_export(self, out_path: Path, layout_mode: str) -> None:
        """Export on a worker thread behind a cancellable progress dialog.
        Playback pauses first: exporting decodes the same streams this
        window is playing, and there's no reason to fight over them."""
        self._set_playing(False)

        progress = QProgressDialog("Exporting video...", "Cancel", 0, 100, self)
        progress.setWindowTitle("Export")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        worker = _ExportWorker(self.session, out_path, layout_mode, EXPORT_FPS)
        outcome: dict[str, str] = {}

        def settle(kind: str, payload: str = "") -> None:
            outcome["kind"] = kind
            outcome["payload"] = payload
            progress.reset()
            progress.close()

        worker.progress.connect(lambda done, total: self._on_export_progress(progress, done, total))
        worker.done.connect(lambda path: settle("done", path))
        worker.failed.connect(lambda message: settle("failed", message))
        worker.cancelled.connect(lambda: settle("cancelled"))
        progress.canceled.connect(worker.cancel)

        thread = threading.Thread(target=worker.run, daemon=True, name="export")
        thread.start()
        progress.exec()

        # Cancelling closes the dialog immediately, so the worker may still
        # be unwinding (it deletes its partial file on the way out). Any
        # other way of getting here with no outcome means the dialog closed
        # early -- stop the export either way rather than leaving it
        # running against a window that's gone.
        if not outcome:
            worker.cancel()
        thread.join(timeout=_EXPORT_JOIN_TIMEOUT_S)
        QApplication.processEvents()  # let a signal emitted at the end land

        self._report_export(outcome, out_path)

    @staticmethod
    def _on_export_progress(progress: QProgressDialog, done: int, total: int) -> None:
        progress.setMaximum(total)
        progress.setValue(done)
        progress.setLabelText(f"Exporting video...  {done} / {total} frames")

    def _report_export(self, outcome: dict[str, str], out_path: Path) -> None:
        kind = outcome.get("kind")
        if kind == "done":
            if self._on_export is not None:
                self._on_export(Path(outcome["payload"]))
            QMessageBox.information(self, "Export complete", f"Saved to:\n{outcome['payload']}")
        elif kind == "failed":
            QMessageBox.warning(self, "Export failed", outcome["payload"])
        elif kind == "cancelled":
            self.status_label.setText(f"Export cancelled.   |   {self._describe_session()}")
        else:
            # The thread outlived the join -- say so rather than claiming
            # either success or failure.
            logger.warning("export of %s did not report an outcome in time", out_path)
            QMessageBox.warning(
                self,
                "Export unfinished",
                f"The export is taking longer than expected and was left running.\n"
                f"Check whether {out_path.name} appears in {out_path.parent}.",
            )

    # --- Panopto ------------------------------------------------------------

    def _on_panopto_clicked(self) -> None:
        if self._panopto is None:
            return
        self._run_panopto_upload()

    def _run_panopto_upload(self) -> None:
        """Sign in, upload, sign out, behind one cancellable progress dialog.

        Failure is loud and in-session: a warning naming what failed and a
        Retry, with the recording still in the buffer and still unexported,
        so closing the viewer keeps asking. Nothing is queued and nothing
        survives the app exiting -- the decided behaviour (ROADMAP.md
        2026-09-18), not an oversight.
        """
        self._set_playing(False)

        progress = QProgressDialog("Connecting to Panopto...", "Cancel", 0, 100, self)
        progress.setWindowTitle("Send to Panopto")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        login, client = self._panopto_session_factory(self._panopto)
        worker = _PanoptoWorker(self.session, login, client, self._panopto.assignment_folder_id)
        outcome: dict[str, str] = {}

        def settle(kind: str, payload: str = "") -> None:
            outcome["kind"] = kind
            outcome["payload"] = payload
            progress.reset()
            progress.close()

        worker.stage.connect(progress.setLabelText)
        worker.progress.connect(lambda done, total: self._on_upload_progress(progress, done, total))
        worker.done.connect(lambda url: settle("done", url))
        worker.failed.connect(lambda message: settle("failed", message))
        worker.sign_in_failed.connect(lambda message: settle("sign_in_failed", message))
        worker.cancelled.connect(lambda: settle("cancelled"))
        progress.canceled.connect(worker.cancel)

        thread = threading.Thread(target=worker.run, daemon=True, name="panopto")
        thread.start()
        progress.exec()

        if not outcome:
            worker.cancel()
        thread.join(timeout=_EXPORT_JOIN_TIMEOUT_S)
        QApplication.processEvents()

        self._report_panopto(outcome)

    @staticmethod
    def _on_upload_progress(progress: QProgressDialog, done: int, total: int) -> None:
        progress.setMaximum(total)
        progress.setValue(done)
        progress.setLabelText(f"Sending to Panopto...  {done // 1_000_000} / {total // 1_000_000} MB")

    def _report_panopto(self, outcome: dict[str, str]) -> None:
        kind = outcome.get("kind")
        if kind == "done":
            if self._on_export is not None:
                self._on_export(outcome["payload"])
            QMessageBox.information(
                self,
                "Sent to Panopto",
                "Your recording is on its way. It will appear in your course folder "
                "once Panopto has finished processing it.",
            )
        elif kind == "sign_in_failed":
            self._offer_retry("Couldn't sign in", outcome["payload"])
        elif kind == "failed":
            self._offer_retry("Upload failed", outcome["payload"])
        elif kind == "cancelled":
            self.status_label.setText(f"Upload cancelled.   |   {self._describe_session()}")
        else:
            logger.warning("Panopto upload did not report an outcome in time")
            QMessageBox.warning(
                self,
                "Upload unfinished",
                "The upload is taking longer than expected. Your recording is still "
                "here -- try again, or save it to a drive.",
            )

    def _offer_retry(self, title: str, detail: str) -> None:
        """Say what went wrong and let the student go again. The recording
        is not touched either way: a student who gives up still has the
        drive export, and closing still asks."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(f"{detail}\n\nYour recording is still here.")
        retry = box.addButton("Try again", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(retry)
        box.exec()
        if box.clickedButton() is retry:
            self._run_panopto_upload()

    # --- rendering ------------------------------------------------------------

    def _canvas_size(self) -> tuple[int, int]:
        size = self.video_label.size()
        return (max(160, size.width()), max(120, size.height()))

    def _render(self) -> None:
        canvas = compose_layout(self.player.images(), self.layout_box.currentData(), self._canvas_size())
        self.video_label.setPixmap(bgr_to_pixmap(canvas))

    def resizeEvent(self, event) -> None:
        self._dirty = True
        super().resizeEvent(event)

    # --- teardown --------------------------------------------------------------

    def _shutdown(self, *_args) -> None:
        """Stop the timer and release the decoders. Idempotent -- reached
        from both `finished` and closeEvent."""
        audio = getattr(self, "audio", None)
        if audio is not None:
            audio.close()
            self.audio = None
        timer = getattr(self, "timer", None)
        if timer is not None:
            timer.stop()
        player = getattr(self, "player", None)
        if player is not None:
            player.close()

    def _may_close(self) -> bool:
        """Whether this dialog may close. Asked at most once: the answer
        latches, because the window's X reaches here twice (closeEvent,
        then the reject() it delegates to) and a student must not be put
        the same question twice for one click."""
        if self._close_allowed or self._confirm_close is None:
            return True
        self._close_allowed = bool(self._confirm_close(self))
        return self._close_allowed

    def done(self, result: int) -> None:
        """Every way a QDialog closes funnels through here -- Esc and
        reject() included, neither of which delivers a QCloseEvent."""
        if not self._may_close():
            return
        super().done(result)

    def closeEvent(self, event) -> None:
        # Gate *before* _shutdown(): it releases the PyAV decoders, and a
        # dialog that then stays open would be showing a dead session.
        if not self._may_close():
            event.ignore()
            return
        self._shutdown()
        super().closeEvent(event)


def _release(dialog: QDialog) -> None:
    """Schedule a finished modal dialog for deletion.

    Qt parent-child ownership keeps a dialog alive for the life of its
    parent, so without this every recording reviewed would leave another
    window -- and the full-size QPixmap rendered into it -- attached
    to the kiosk window. The kiosk runs unattended for days at a time, so
    that accumulates. See DECISIONS.md's 2026-09-09 entry.

    setParent(None) before deleteLater(), not either alone: deleteLater()
    only fires when control returns to the event loop *at the level where
    it was called*, which is a fragile thing to rely on right after a
    nested exec(). Unparenting drops it from the kiosk window's children
    immediately and hands ownership back to Python's refcount; deleteLater
    then cleans up the C++ side on the next pass either way. The dialog is
    already closed here, so unparenting can't make it show.
    """
    dialog.setParent(None)
    dialog.deleteLater()


def open_session(
    session_dir: Path | str, parent=None, on_export=None, confirm_close=None, panopto=None
) -> bool:
    """Load and show a session modally, reporting a bad session with a
    dialog rather than a traceback. True if it opened.

    `on_export` is called with the written path each time an export
    succeeds -- the kiosk's way of learning that a session has been taken
    somewhere that outlives its buffer. `confirm_close` can refuse a close,
    which is how the kiosk asks about saving without the window going away
    first.
    """
    try:
        session = Session.load(session_dir)
    except SessionError as exc:
        QMessageBox.warning(parent, "Can't open this recording", str(exc))
        logger.warning("could not open session %s: %s", session_dir, exc)
        return False
    dialog = ViewerDialog(
        session, parent=parent, on_export=on_export, confirm_close=confirm_close, panopto=panopto
    )
    dialog.resize(*DEFAULT_CANVAS)
    try:
        dialog.exec()
    finally:
        # _shutdown is idempotent and normally already ran via `finished`.
        # Called again here so releasing the PyAV decoders never depends on
        # a signal having fired -- the same leak class as DECISIONS.md's
        # "settings.py Preview leaked the IDS device" entry, which is why
        # this dialog's teardown hangs off `finished` in the first place.
        dialog._shutdown()
        _release(dialog)
    return True
