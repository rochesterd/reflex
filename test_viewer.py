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
from config import PanoptoConfig
from panopto_api import MANIFEST_FILENAME
from session_format import INSTRUMENT_STREAM, THIRD_PERSON_STREAM
from session_buffer import Drive
from session_reader import Session
from test_panopto_api import HOST as PANOPTO_HOST, TEST_REDIRECT_PORT, FakePanoptoSite, make_login
from test_panopto_upload import InMemoryUploadClient
from test_session_reader import record_session
from viewer import ViewerDialog, _mmss

_qt_app = QApplication.instance() or QApplication([])


class DialogLifetimeTest(unittest.TestCase):
    """The kiosk runs unattended for days, and every recording it stops
    opens a modal dialog parented to its window. Qt parent-child
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

    def test_one_drive_asks_nothing(self):
        dialog = self._dialog()
        with patch("viewer.removable_drives_detailed", return_value=[_drive("E:")]):
            with patch("viewer.default_export_dir", return_value=Path("E:/")):
                with patch("viewer.QInputDialog.getItem") as chooser:
                    self.assertEqual(dialog._choose_destination(), Path("E:/"))
        chooser.assert_not_called()

    def test_two_drives_let_the_student_pick(self):
        """Taking the first by drive letter would quietly write a peer's
        recording onto a stranger's stick."""
        dialog = self._dialog()
        drives = [_drive("E:", "SCHOOL"), _drive("F:", "MINE")]
        with patch("viewer.removable_drives_detailed", return_value=drives):
            with patch(
                "viewer.QInputDialog.getItem", return_value=(drives[1].describe(), True)
            ) as chooser:
                destination = dialog._choose_destination()

        self.assertEqual(destination, drives[1].path)
        self.assertEqual(chooser.call_args.args[3], [d.describe() for d in drives])

    def test_backing_out_of_the_chooser_exports_nothing(self):
        dialog = self._dialog()
        drives = [_drive("E:"), _drive("F:")]
        with patch("viewer.removable_drives_detailed", return_value=drives):
            with patch("viewer.QInputDialog.getItem", return_value=("", False)):
                self.assertIsNone(dialog._choose_destination())
                with patch("viewer.QFileDialog.getSaveFileName") as save:
                    with patch.object(ViewerDialog, "_run_export") as mock_run:
                        dialog._on_export_clicked()
        save.assert_not_called()
        mock_run.assert_not_called()

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


def _panopto_config():
    return PanoptoConfig(
        host=PANOPTO_HOST,
        client_id="reflex-kiosk",
        assignment_folder_id="assign-1",
        redirect_port=TEST_REDIRECT_PORT,
    )


class _FakeMessageBox:
    """Stands in for viewer.QMessageBox in _offer_retry: records what was
    shown and answers with whichever button the test chose."""

    Icon = viewer.QMessageBox.Icon
    ButtonRole = viewer.QMessageBox.ButtonRole
    shown: list[dict] = []
    answer = "Not now"

    def __init__(self, _parent=None):
        self._buttons: dict[str, object] = {}
        self._record: dict = {}
        _FakeMessageBox.shown.append(self._record)

    def setIcon(self, *_a): ...
    def setWindowTitle(self, title): self._record["title"] = title
    def setText(self, text): self._record["text"] = text
    def setDefaultButton(self, *_a): ...

    def addButton(self, label, _role):
        button = object()
        self._buttons[label] = button
        return button

    def exec(self): ...

    def clickedButton(self):
        return self._buttons.get(_FakeMessageBox.answer)


class ViewerPanoptoTest(unittest.TestCase):
    """The Send-to-Panopto wiring. The sign-in and upload engines are
    covered by test_panopto_api.py and test_panopto_upload.py; these cover
    that the button exists only when configured, that the worker signs
    out on every path, and that each outcome reaches the student -- with a
    Retry, and never a silent loss."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.session_dir = record_session(cls._tmp.name, 2)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.site = FakePanoptoSite()
        self.site.add_folder("assign-1", "Slit lamp practice")
        self.login = make_login(self.site)
        self.client = InMemoryUploadClient(self.site)
        self.exported: list = []

    def _dialog(self, configured: bool = True) -> ViewerDialog:
        dialog = ViewerDialog(
            Session.load(self.session_dir),
            on_export=self.exported.append,
            panopto=_panopto_config() if configured else None,
            panopto_session_factory=lambda _cfg: (self.login, self.client),
        )
        self.addCleanup(dialog._shutdown)
        return dialog

    def _run_worker(self):
        worker = viewer._PanoptoWorker(
            Session.load(self.session_dir), self.login, self.client, "assign-1"
        )
        outcomes: list[tuple[str, str]] = []
        worker.done.connect(lambda url: outcomes.append(("done", url)))
        worker.failed.connect(lambda msg: outcomes.append(("failed", msg)))
        worker.sign_in_failed.connect(lambda msg: outcomes.append(("sign_in_failed", msg)))
        worker.cancelled.connect(lambda: outcomes.append(("cancelled", "")))
        return worker, outcomes

    # -- presence --------------------------------------------------------

    def test_the_button_exists_only_when_panopto_is_configured(self):
        self.assertTrue(self._dialog(configured=False).panopto_button.isHidden())
        self.assertFalse(self._dialog(configured=True).panopto_button.isHidden())

    def test_clicking_without_config_does_nothing(self):
        dialog = self._dialog(configured=False)
        with patch.object(ViewerDialog, "_run_panopto_upload") as run:
            dialog._on_panopto_clicked()
        run.assert_not_called()

    # -- the worker ------------------------------------------------------

    def test_a_full_run_signs_in_uploads_and_signs_out(self):
        worker, outcomes = self._run_worker()
        worker.run()

        self.assertEqual(outcomes[0][0], "done")
        self.assertTrue(outcomes[0][1].startswith(f"https://{PANOPTO_HOST}/"))
        self.assertEqual(
            set(self.client.received), {"third_person.mp4", "instrument.mp4", MANIFEST_FILENAME}
        )
        self.assertEqual(self.site.uploads["upload-1"]["FolderId"], "assign-1")
        self.assertFalse(self.login.signed_in, "the token outlived the upload")

    def test_a_refused_sign_in_is_its_own_outcome(self):
        self.site.refuse_login = True
        worker, outcomes = self._run_worker()
        worker.run()
        self.assertEqual(outcomes[0][0], "sign_in_failed")
        self.assertEqual(self.client.received, {})

    def test_an_upload_failure_is_reported_and_still_signs_out(self):
        original = self.site.request

        def broken_upload(method, url, **kwargs):
            if url.endswith("/sessionUpload"):
                return viewer_test_json(500, {"Message": "storage unavailable"})
            return original(method, url, **kwargs)

        self.site.request = broken_upload
        worker, outcomes = self._run_worker()
        worker.run()
        self.assertEqual(outcomes[0][0], "failed")
        self.assertIn("500", outcomes[0][1])
        self.assertFalse(self.login.signed_in)

    def test_cancelling_before_sign_in_completes_is_quiet(self):
        worker, outcomes = self._run_worker()
        self.login._open_url = lambda _url: None  # the browser never comes back
        worker.cancel()
        worker.run()
        self.assertEqual(outcomes, [("cancelled", "")])
        self.assertEqual(self.client.received, {})
        self.assertFalse(self.login.signed_in)

    # -- reporting -------------------------------------------------------

    def test_success_is_reported_to_the_kiosk_with_the_url(self):
        dialog = self._dialog()
        with patch("viewer.QMessageBox.information") as info:
            dialog._report_panopto({"kind": "done", "payload": "https://x/viewer?id=1"})
        self.assertEqual(self.exported, ["https://x/viewer?id=1"])
        info.assert_called_once()

    def test_failures_offer_a_retry_and_do_not_mark_exported(self):
        dialog = self._dialog()
        for kind in ("failed", "sign_in_failed"):
            with self.subTest(kind=kind):
                with patch.object(ViewerDialog, "_offer_retry") as offer:
                    dialog._report_panopto({"kind": kind, "payload": "nope"})
                offer.assert_called_once()
                self.assertIn("nope", offer.call_args.args[1])
        self.assertEqual(self.exported, [])

    def test_retry_runs_the_upload_again_and_not_now_does_not(self):
        dialog = self._dialog()
        _FakeMessageBox.shown.clear()
        with patch("viewer.QMessageBox", _FakeMessageBox):
            _FakeMessageBox.answer = "Try again"
            with patch.object(ViewerDialog, "_run_panopto_upload") as run:
                dialog._offer_retry("Upload failed", "storage unavailable")
            run.assert_called_once()

            _FakeMessageBox.answer = "Not now"
            with patch.object(ViewerDialog, "_run_panopto_upload") as run:
                dialog._offer_retry("Upload failed", "storage unavailable")
            run.assert_not_called()

        # The student is told the recording is still there, every time.
        for record in _FakeMessageBox.shown:
            self.assertIn("still here", record["text"])

    def test_cancel_and_no_outcome_are_not_reported_as_success(self):
        dialog = self._dialog()
        dialog._report_panopto({"kind": "cancelled"})
        self.assertIn("cancelled", dialog.status_label.text().lower())

        with patch("viewer.QMessageBox.warning") as warn:
            dialog._report_panopto({})
        warn.assert_called_once()
        self.assertEqual(self.exported, [])


def viewer_test_json(status: int, payload: dict):
    from panopto_api import HttpResponse
    import json as _json

    return HttpResponse(status=status, body=_json.dumps(payload).encode("utf-8"))



class ViewerAudioTest(unittest.TestCase):
    """The viewer drives the audio player from its own transport: play
    starts sound at the video's position, pause silences it, a scrub moves
    it, and a silent session simply has none."""

    @classmethod
    def setUpClass(cls):
        from test_audio_playback import record_with_audio

        cls._tmp = tempfile.TemporaryDirectory()
        cls.audible = record_with_audio(cls._tmp.name, 1.5)
        cls.silent_dir = record_session(cls._tmp.name, 1)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _dialog(self, session) -> ViewerDialog:
        class FakeAudio:
            def __init__(self, path, offset_s=0.0):
                self.calls: list[tuple] = []
                self.closed = False
            def play(self, t): self.calls.append(("play", round(t, 2)))
            def pause(self): self.calls.append(("pause",))
            def seek(self, t): self.calls.append(("seek", round(t, 2)))
            def close(self): self.closed = True

        with patch("viewer.AudioPlayer", FakeAudio):
            dialog = ViewerDialog(session)
        self.addCleanup(dialog._shutdown)
        return dialog

    def test_a_silent_session_has_no_audio_player(self):
        dialog = self._dialog(Session.load(self.silent_dir))
        self.assertIsNone(dialog.audio)
        self.assertNotIn("sound", dialog.status_label.text())

    def test_an_audible_session_gets_a_player_and_says_so(self):
        dialog = self._dialog(self.audible)
        self.assertIsNotNone(dialog.audio)
        self.assertIn("with sound", dialog.status_label.text())

    def test_transport_drives_the_audio(self):
        dialog = self._dialog(self.audible)
        dialog._set_playing(True)
        self.assertEqual(dialog.audio.calls[-1], ("play", 0.0))
        dialog._set_playing(False)
        self.assertEqual(dialog.audio.calls[-1], ("pause",))
        dialog._on_scrub_move(viewer.SLIDER_STEPS // 2)
        self.assertEqual(dialog.audio.calls[-1][0], "seek")
        self.assertAlmostEqual(dialog.audio.calls[-1][1], round(dialog.player.position, 2), places=2)

    def test_shutdown_closes_the_audio(self):
        dialog = self._dialog(self.audible)
        audio = dialog.audio
        dialog._shutdown()
        self.assertTrue(audio.closed)
        self.assertIsNone(dialog.audio)

    def test_a_speaker_that_cannot_open_does_not_lose_the_viewer(self):
        def broken(path, offset_s=0.0):
            raise RuntimeError("no output device")

        with patch("viewer.AudioPlayer", broken):
            dialog = ViewerDialog(self.audible)
        self.addCleanup(dialog._shutdown)
        self.assertIsNone(dialog.audio)
        self.assertIn("no speaker", dialog.status_label.text())


def _drive(letter: str, label: str = "") -> Drive:
    return Drive(path=Path(f"{letter}/"), label=label, free_bytes=8_000_000_000)


class ViewerCloseGateTest(unittest.TestCase):
    """`confirm_close` can refuse a close, which is how the kiosk asks
    about saving without the window going away first. See viewer.py's
    _may_close() and DECISIONS.md 2026-09-14."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.session_dir = record_session(cls._tmp.name, 1)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _dialog(self, confirm_close):
        dialog = ViewerDialog(Session.load(self.session_dir), confirm_close=confirm_close)
        self.addCleanup(dialog._shutdown)
        return dialog

    def test_refusing_keeps_the_window_open_on_every_close_path(self):
        """Esc and reject() never deliver a QCloseEvent, so gating
        closeEvent alone would let the keyboard through."""
        asked = []
        dialog = self._dialog(lambda _d: (asked.append(1), False)[1])
        dialog.show()

        dialog.close()
        self.assertTrue(dialog.isVisible())

        dialog.reject()
        self.assertTrue(dialog.isVisible())

        self.assertEqual(len(asked), 2)  # once per deliberate attempt

    def test_a_refused_close_leaves_the_decoders_alone(self):
        """closeEvent releases the PyAV decoders. Gating after that would
        leave a window open on a dead session."""
        dialog = self._dialog(lambda _d: False)
        dialog.show()

        dialog.close()

        self.assertTrue(dialog.isVisible())
        # Still playable: the player is intact and still answers.
        self.assertTrue(any(v is not None for v in dialog.player.images().values()))

    def test_one_click_on_the_x_asks_once(self):
        """closeEvent delegates to reject(), so the question reaches this
        dialog twice for one click unless the answer latches."""
        asked = []
        dialog = self._dialog(lambda _d: (asked.append(1), True)[1])
        dialog.show()

        dialog.close()

        self.assertFalse(dialog.isVisible())
        self.assertEqual(len(asked), 1)

    def test_no_gate_means_the_old_behaviour(self):
        """Without a gate the dialog closes like any other."""
        dialog = self._dialog(None)
        dialog.show()
        dialog.close()
        self.assertFalse(dialog.isVisible())


if __name__ == "__main__":
    unittest.main()
