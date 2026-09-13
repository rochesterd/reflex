"""Headless tests for viewer.ViewerDialog against real recorded sessions.

No .show()/.exec() -- the dialog is constructed, driven directly, and torn
down, the same approach test_settings.py uses for PreviewDialog. The
teardown test is the important one: this dialog holds open PyAV decoders,
which is the same class of leak DECISIONS.md's "settings.py Preview leaked
the IDS device" entry describes.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialog, QMainWindow

from unittest.mock import patch

import viewer
from session_format import INSTRUMENT_STREAM, THIRD_PERSON_STREAM
from session_reader import Session
from test_session_reader import record_session
from viewer import SessionPickerDialog, ViewerDialog, _mmss

_qt_app = QApplication.instance() or QApplication([])


class DialogLifetimeTest(unittest.TestCase):
    """The kiosk runs unattended for days, and every Watch / Past recordings
    press opens a modal dialog parented to its window. Qt parent-child
    ownership would keep each one -- and the full-size QPixmap rendered into
    it -- alive for the life of the process. See DECISIONS.md's 2026-09-09
    entry.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.session_dir = record_session(cls._tmp.name, 1)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_open_session_does_not_leave_the_dialog_parented(self):
        parent = QMainWindow()
        self.addCleanup(parent.deleteLater)
        with patch.object(ViewerDialog, "exec", lambda self: 0):
            for _ in range(3):
                self.assertTrue(viewer.open_session(self.session_dir, parent=parent))
        _qt_app.processEvents()

        self.assertEqual([c for c in parent.children() if isinstance(c, QDialog)], [])

    def test_browse_sessions_does_not_leave_the_picker_parented(self):
        parent = QMainWindow()
        self.addCleanup(parent.deleteLater)
        with patch.object(SessionPickerDialog, "exec", lambda self: QDialog.DialogCode.Rejected):
            for _ in range(3):
                self.assertFalse(viewer.browse_sessions(self._tmp.name, parent=parent))
        _qt_app.processEvents()

        self.assertEqual([c for c in parent.children() if isinstance(c, QDialog)], [])


class MmSsTest(unittest.TestCase):
    def test_formats_as_minutes_and_seconds(self):
        self.assertEqual(_mmss(0), "0:00")
        self.assertEqual(_mmss(9.9), "0:09")
        self.assertEqual(_mmss(65), "1:05")
        self.assertEqual(_mmss(-3), "0:00")


class ViewerDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One real recording shared across the tests -- recording is the
        # slow part, and none of these mutate the session.
        cls._tmp = tempfile.TemporaryDirectory()
        cls.session_dir = record_session(cls._tmp.name, 3)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _dialog(self) -> ViewerDialog:
        dialog = ViewerDialog(Session.load(self.session_dir))
        self.addCleanup(dialog._shutdown)
        return dialog

    def test_opens_paused_at_the_start_with_a_frame_shown(self):
        dialog = self._dialog()

        self.assertFalse(dialog._playing)
        self.assertEqual(dialog.player.position, 0.0)
        self.assertGreater(dialog.player.duration, 1.0)
        self.assertFalse(dialog.video_label.pixmap().isNull())
        self.assertIn("/", dialog.time_label.text())

    def test_every_layout_mode_renders(self):
        dialog = self._dialog()
        seen = set()
        for index in range(dialog.layout_box.count()):
            dialog.layout_box.setCurrentIndex(index)
            dialog._render()
            pixmap = dialog.video_label.pixmap()
            self.assertFalse(pixmap.isNull(), dialog.layout_box.currentData())
            seen.add(dialog.layout_box.currentData())
        self.assertEqual(
            seen, {"side_by_side", "picture_in_picture", "instrument", "third_person"}
        )

    def test_single_camera_layouts_use_the_full_canvas(self):
        dialog = self._dialog()
        dialog.video_label.resize(800, 400)
        for mode in ("instrument", "third_person"):
            index = dialog.layout_box.findData(mode)
            dialog.layout_box.setCurrentIndex(index)
            dialog._render()
            size = dialog.video_label.pixmap().size()
            self.assertEqual((size.width(), size.height()), (800, 400), mode)

    def test_scrubbing_seeks_and_pauses_then_resumes(self):
        dialog = self._dialog()
        dialog._set_playing(True)
        self.assertTrue(dialog._playing)

        dialog._on_scrub_start()
        self.assertFalse(dialog._playing)  # paused while dragging
        self.assertTrue(dialog._resume_after_scrub)

        dialog._on_scrub_move(500)  # halfway
        self.assertAlmostEqual(dialog.player.position, dialog.player.duration / 2, delta=0.05)

        dialog._on_scrub_end()
        self.assertTrue(dialog._playing)  # resumed, because it was playing before

    def test_scrub_from_paused_stays_paused(self):
        dialog = self._dialog()
        dialog._on_scrub_start()
        dialog._on_scrub_move(250)
        dialog._on_scrub_end()
        self.assertFalse(dialog._playing)

    def test_play_from_the_end_restarts_from_the_beginning(self):
        dialog = self._dialog()
        dialog.player.seek(dialog.player.duration)
        dialog._toggle_play()
        self.assertTrue(dialog._playing)
        self.assertEqual(dialog.player.position, 0.0)

    def test_tick_advances_media_time_while_playing(self):
        dialog = self._dialog()
        dialog._set_playing(True)
        # Pretend playback started a second ago rather than sleeping.
        dialog._play_started_wall -= 1.0
        dialog._tick()
        self.assertAlmostEqual(dialog.player.position, 1.0, delta=0.1)

    def test_playback_stops_at_the_end(self):
        dialog = self._dialog()
        dialog._set_playing(True)
        dialog._play_started_wall -= dialog.player.duration + 5.0
        dialog._tick()
        self.assertFalse(dialog._playing)
        self.assertAlmostEqual(dialog.player.position, dialog.player.duration, delta=0.01)

    def test_reject_releases_the_decoders(self):
        """Esc routes through QDialog.reject(), which delivers no
        QCloseEvent -- teardown must hang off `finished` or the PyAV
        containers leak. Same bug class as the settings.py Preview leak."""
        dialog = ViewerDialog(Session.load(self.session_dir))
        self.assertTrue(dialog.player._cursors)

        dialog.reject()

        self.assertFalse(dialog.player._cursors)
        self.assertFalse(dialog.timer.isActive())

    def test_shutdown_is_idempotent(self):
        dialog = ViewerDialog(Session.load(self.session_dir))
        dialog._shutdown()
        dialog._shutdown()

    def test_status_line_names_both_streams(self):
        dialog = self._dialog()
        text = dialog.status_label.text()
        session = Session.load(self.session_dir)
        self.assertIn(session.streams[INSTRUMENT_STREAM].label, text)
        self.assertIn(session.streams[THIRD_PERSON_STREAM].label, text)


class ViewerExportTest(unittest.TestCase):
    """The export *engine* is covered by test_session_export.py; these
    cover the wiring -- that the save dialog is honoured, that cancelling
    it does nothing, and that each outcome is reported rather than
    silently swallowed."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.session_dir = record_session(cls._tmp.name, 2)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _dialog(self) -> ViewerDialog:
        dialog = ViewerDialog(Session.load(self.session_dir))
        self.addCleanup(dialog._shutdown)
        return dialog

    def test_export_uses_the_chosen_path_and_current_layout(self):
        dialog = self._dialog()
        dialog.layout_box.setCurrentIndex(dialog.layout_box.findData("instrument"))
        target = Path(self._tmp.name) / "chosen.mp4"

        with patch("viewer.QFileDialog.getSaveFileName", return_value=(str(target), "")):
            with patch.object(ViewerDialog, "_run_export") as mock_run:
                dialog._on_export_clicked()

        mock_run.assert_called_once_with(target, "instrument")

    def test_export_suggests_a_name_on_the_export_drive_not_the_buffer(self):
        """The session folder is a buffer the app deletes, so suggesting a
        name inside it would suggest saving nowhere. See session_buffer.py."""
        dialog = self._dialog()
        dialog.layout_box.setCurrentIndex(dialog.layout_box.findData("side_by_side"))
        drive = Path(self._tmp.name) / "student-drive"

        with patch("viewer.default_export_dir", return_value=drive):
            with patch("viewer.QFileDialog.getSaveFileName", return_value=("", "")) as mock_dialog:
                dialog._on_export_clicked()

        suggested = Path(mock_dialog.call_args.args[2])
        self.assertEqual(suggested.name, "side_by_side.mp4")
        self.assertEqual(suggested.parent, drive)
        self.assertNotEqual(suggested.parent, self.session_dir)

    def test_a_finished_export_is_reported_to_the_caller(self):
        """How app.py learns a session has been taken somewhere that
        outlives the buffer (see app.py's _unexported_session)."""
        exported = []
        dialog = ViewerDialog(Session.load(self.session_dir), on_export=exported.append)
        self.addCleanup(dialog._shutdown)
        out = Path(self._tmp.name) / "out.mp4"

        with patch("viewer.QMessageBox.information"):
            dialog._report_export({"kind": "done", "payload": str(out)}, out)
        self.assertEqual(exported, [out])

        dialog._report_export({"kind": "cancelled"}, out)
        self.assertEqual(exported, [out])

    def test_cancelling_the_save_dialog_exports_nothing(self):
        dialog = self._dialog()
        with patch("viewer.QFileDialog.getSaveFileName", return_value=("", "")):
            with patch.object(ViewerDialog, "_run_export") as mock_run:
                dialog._on_export_clicked()
        mock_run.assert_not_called()

    def test_each_outcome_is_reported(self):
        dialog = self._dialog()
        out = Path(self._tmp.name) / "out.mp4"

        with patch("viewer.QMessageBox.information") as info:
            dialog._report_export({"kind": "done", "payload": str(out)}, out)
        info.assert_called_once()

        with patch("viewer.QMessageBox.warning") as warn:
            dialog._report_export({"kind": "failed", "payload": "disk full"}, out)
        self.assertIn("disk full", warn.call_args.args[-1])

        dialog._report_export({"kind": "cancelled"}, out)
        self.assertIn("cancelled", dialog.status_label.text().lower())

        # No outcome at all must not be reported as success.
        with patch("viewer.QMessageBox.warning") as warn:
            dialog._report_export({}, out)
        warn.assert_called_once()


class SessionPickerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _picker(self, directory=None) -> SessionPickerDialog:
        picker = SessionPickerDialog(directory or self.root)
        self.addCleanup(picker.deleteLater)
        return picker

    def test_lists_recordings_newest_first_with_label_and_duration(self):
        first = record_session(str(self.root), 1)
        second = record_session(str(self.root), 1)

        picker = self._picker()

        self.assertEqual(picker.list_widget.count(), 2)
        rows = [picker.list_widget.item(i).text() for i in range(2)]
        self.assertIn(second.name.replace("_", " "), rows[0])
        self.assertIn(first.name.replace("_", " "), rows[1])
        self.assertIn("BI900", rows[0])  # the instrument label from the manifest
        self.assertIn("0:0", rows[0])  # a mm:ss duration

    def test_empty_folder_shows_a_placeholder_and_disables_open(self):
        picker = self._picker()

        self.assertEqual(picker.list_widget.count(), 1)
        self.assertIn("No recordings", picker.list_widget.item(0).text())
        self.assertIsNone(picker._selected_directory())

    def test_selecting_and_accepting_returns_the_directory(self):
        session_dir = record_session(str(self.root), 1)
        picker = self._picker()

        picker.list_widget.setCurrentRow(0)
        picker._accept_selection()

        self.assertEqual(picker.selected_directory, session_dir)

    def test_browsing_to_a_folder_of_recordings_reloads_the_list(self):
        other = self.root / "elsewhere"
        other.mkdir()
        record_session(str(other), 1)
        picker = self._picker()
        self.assertEqual(picker.list_widget.count(), 1)  # placeholder only

        with patch("viewer.QFileDialog.getExistingDirectory", return_value=str(other)):
            picker._on_browse()

        self.assertIsNone(picker.selected_directory)  # a folder, not a pick
        self.assertEqual(picker.list_widget.count(), 1)
        self.assertIn("BI900", picker.list_widget.item(0).text())

    def test_browsing_straight_to_one_recording_accepts_it(self):
        """Pointing at a single session folder rather than the folder
        containing several is an easy slip -- take it as a pick."""
        session_dir = record_session(str(self.root), 1)
        picker = self._picker()

        with patch("viewer.QFileDialog.getExistingDirectory", return_value=str(session_dir)):
            picker._on_browse()

        self.assertEqual(picker.selected_directory, session_dir)

    def test_cancelling_the_browse_changes_nothing(self):
        picker = self._picker()
        before = picker.folder_label.text()

        with patch("viewer.QFileDialog.getExistingDirectory", return_value=""):
            picker._on_browse()

        self.assertEqual(picker.folder_label.text(), before)
        self.assertIsNone(picker.selected_directory)

    def test_missing_folder_is_not_an_error(self):
        picker = self._picker(self.root / "does-not-exist")
        self.assertIn("No recordings", picker.list_widget.item(0).text())


if __name__ == "__main__":
    unittest.main()
