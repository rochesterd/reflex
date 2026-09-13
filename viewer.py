"""Session viewer: plays back a recorded session, laying its two streams
out on demand rather than however they were composited at record time
(they aren't -- see recorder.py), and exporting one shareable file when
asked.

A thin PySide6 shell over session_reader.SessionPlayer and
session_export.export_session, the same split app.py has over kiosk.py.
Runs two ways from one class:

- inside the kiosk, opened modally by app.py's Watch / Past recordings
  buttons, and
- as a standalone viewer.exe for reviewing a session on another machine.

Student-facing, same audience as recording (CLAUDE.md's "Who uses it"):
play/pause, scrub, a layout picker, and Export. Nothing here can modify or
delete a recording -- Export only ever writes a new file.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from app_icon import ICON_VIEWER, icon_path
from compositor import LAYOUT_MODES, LAYOUT_TITLES, compose_layout
from session_buffer import default_export_dir
from qt_image import bgr_to_pixmap
import reflex_style
from session_export import ExportCancelled, default_export_name, export_session
from session_reader import Session, SessionError, SessionPlayer, list_sessions

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


class ViewerDialog(QDialog):
    """Playback window for one session.

    A QDialog rather than a QMainWindow so app.py can open it modally with
    .exec() (the kiosk's own controls are then unreachable while it's up)
    while main() can still show it as a standalone top-level window.

    Teardown hangs off the `finished` signal, not closeEvent: Esc routes
    through QDialog.reject(), which never delivers a QCloseEvent, and this
    dialog holds open PyAV decoders. That's the same leak DECISIONS.md's
    "settings.py Preview leaked the IDS device" entry is about.
    """

    def __init__(self, session: Session, parent=None, on_export=None):
        super().__init__(parent)
        self.session = session
        # Called with the written path when an export finishes. The kiosk
        # uses it to tell an exported session from one its buffer is about
        # to delete (see app.py); standalone viewer.exe passes nothing.
        self._on_export = on_export
        self.setWindowTitle(f"Recording - {session.directory.name}")
        self.setSizeGripEnabled(True)

        self.player = SessionPlayer(session)
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
        return "   |   ".join(parts)

    # --- playback -------------------------------------------------------------

    def _toggle_play(self) -> None:
        if not self._playing and self.player.position >= self.player.duration:
            self.player.seek(0.0)  # replay from the top rather than sitting at the end
        self._set_playing(not self._playing)

    def _set_playing(self, playing: bool) -> None:
        self._playing = playing
        self.play_button.setText("Pause" if playing else "Play")
        if playing:
            self._play_started_wall = time.monotonic()
            self._play_started_media = self.player.position
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
        # Deliberately not the session folder: on a kiosk that folder is a
        # temporary buffer, so saving into it saves nothing. Default to a
        # removable drive when one is plugged in. See session_buffer.py.
        suggested = default_export_dir() / default_export_name(layout_mode)
        chosen, _filter = QFileDialog.getSaveFileName(
            self, "Export video", str(suggested), "MP4 video (*.mp4)"
        )
        if not chosen:
            return
        self._run_export(Path(chosen), layout_mode)

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
        timer = getattr(self, "timer", None)
        if timer is not None:
            timer.stop()
        player = getattr(self, "player", None)
        if player is not None:
            player.close()

    def closeEvent(self, event) -> None:
        self._shutdown()
        super().closeEvent(event)


class SessionPickerDialog(QDialog):
    """Pick a recording to watch.

    Lists what's in a folder, newest first, and offers a browse button --
    which the standalone viewer genuinely needs: on a review machine there
    may be no config.json at all, and recordings will have been copied to
    a USB stick or Downloads rather than sitting in the default location
    (see DECISIONS.md's "Recorder/Viewer split, phase 4" entry).
    """

    def __init__(self, sessions_dir: Path | str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Past recordings")
        self.setMinimumSize(560, 380)
        self.selected_directory: Path | None = None
        self._sessions_dir = Path(sessions_dir)

        self.folder_label = QLabel()
        self.folder_label.setWordWrap(True)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(lambda _item: self._accept_selection())
        self.list_widget.itemSelectionChanged.connect(self._update_buttons)

        self.browse_button = QPushButton("Open a recording folder...")
        self.browse_button.clicked.connect(self._on_browse)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._accept_selection)
        self.buttons.rejected.connect(self.reject)

        top = QHBoxLayout()
        top.addWidget(self.folder_label, stretch=1)
        top.addWidget(self.browse_button)

        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addWidget(self.list_widget, stretch=1)
        layout.addWidget(self.buttons)
        self.setLayout(layout)

        self._reload()

    def _reload(self) -> None:
        self.list_widget.clear()
        sessions = list_sessions(self._sessions_dir)
        self.folder_label.setText(f"Recordings in {self._sessions_dir}")
        for session in sessions:
            item = QListWidgetItem(self._describe(session))
            item.setData(Qt.ItemDataRole.UserRole, str(session.directory))
            self.list_widget.addItem(item)
        if sessions:
            self.list_widget.setCurrentRow(0)
        else:
            placeholder = QListWidgetItem("No recordings in this folder.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(placeholder)
        self._update_buttons()

    @staticmethod
    def _describe(session: Session) -> str:
        date_part = session.directory.name.replace("_", " ")
        instrument = session.instrument
        label = instrument.label if instrument else session.instrument_key
        return f"{date_part}    {label}    {_mmss(session.duration_s)}"

    def _selected_directory(self) -> Path | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return Path(value) if value else None

    def _update_buttons(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Open).setEnabled(
            self._selected_directory() is not None
        )

    def _accept_selection(self) -> None:
        directory = self._selected_directory()
        if directory is None:
            return
        self.selected_directory = directory
        self.accept()

    def _on_browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose a folder containing recordings", str(self._sessions_dir)
        )
        if not chosen:
            return
        chosen = Path(chosen)
        # Tolerate being pointed straight at one recording rather than at
        # the folder containing several -- an easy and understandable slip.
        if (chosen / "session.json").is_file():
            self.selected_directory = chosen
            self.accept()
            return
        self._sessions_dir = chosen
        self._reload()


def _release(dialog: QDialog) -> None:
    """Schedule a finished modal dialog for deletion.

    Qt parent-child ownership keeps a dialog alive for the life of its
    parent, so without this every Watch / Past recordings press would leave
    another window -- and the full-size QPixmap rendered into it -- attached
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


def open_session(session_dir: Path | str, parent=None, on_export=None) -> bool:
    """Load and show a session modally, reporting a bad session with a
    dialog rather than a traceback. True if it opened.

    `on_export` is called with the written path each time an export
    succeeds -- the kiosk's way of learning that a session has been taken
    somewhere that outlives its buffer.
    """
    try:
        session = Session.load(session_dir)
    except SessionError as exc:
        QMessageBox.warning(parent, "Can't open this recording", str(exc))
        logger.warning("could not open session %s: %s", session_dir, exc)
        return False
    dialog = ViewerDialog(session, parent=parent, on_export=on_export)
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


def browse_sessions(sessions_dir: Path | str, parent=None) -> bool:
    """Show the picker, then open whatever was chosen."""
    picker = SessionPickerDialog(sessions_dir, parent=parent)
    try:
        accepted = picker.exec() == QDialog.DialogCode.Accepted
        chosen = picker.selected_directory
    finally:
        _release(picker)
    if not accepted or chosen is None:
        return False
    return open_session(chosen, parent=parent)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841 - keeps Qt alive
    # Standalone viewer.exe only -- opened as a dialog from app.py this
    # main() never runs, and the dialog inherits the kiosk's icon and style.
    app.setWindowIcon(QIcon(str(icon_path(ICON_VIEWER))))
    reflex_style.apply(app)

    if len(sys.argv) > 1:
        return 0 if open_session(Path(sys.argv[1])) else 1
    # There is no recordings folder any more -- the kiosk records into a
    # buffer it deletes (session_buffer.py). Open where a person keeps
    # files; the picker's "Open a recording folder..." does the rest.
    return 0 if browse_sessions(Path.home()) else 1


if __name__ == "__main__":
    sys.exit(main())
