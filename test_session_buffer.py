"""Tests for session_buffer.py.

Real folders in a real temp directory, not mocks: what is being tested is
that files actually stop existing, and that the one thing this module must
never do -- delete something outside the buffer -- it doesn't.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_buffer import (
    Drive,
    buffer_root,
    clear_buffer,
    default_export_dir,
    removable_drives,
    removable_drives_detailed,
)


class BufferRootTest(unittest.TestCase):
    def test_the_buffer_lives_under_the_system_temp_directory(self):
        """Temp is the one location whose contract is 'this may be
        deleted', which is the contract the buffer wants."""
        root = buffer_root()
        self.assertEqual(
            root.resolve().parts[: len(Path(tempfile.gettempdir()).resolve().parts)],
            Path(tempfile.gettempdir()).resolve().parts,
        )

    def test_it_is_not_created_as_a_side_effect_of_asking(self):
        """The recorder creates its own session folder; asking where the
        buffer is must not litter temp on a machine that never records."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch("tempfile.gettempdir", return_value=tmp):
                root = buffer_root()
            self.assertFalse(root.exists())


class ClearBufferTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Inside the real temp directory, so the _is_temporary guard
        # allows it -- the same position a real buffer is in.
        self.root = Path(self._tmp.name) / "buffer"
        self.root.mkdir()

    def _session(self, name: str) -> Path:
        session = self.root / name
        session.mkdir()
        (session / "instrument.mp4").write_bytes(b"x" * 16)
        (session / "session.json").write_text("{}", encoding="utf-8")
        return session

    def test_it_removes_sessions_and_reports_how_many(self):
        first, second = self._session("2026-01-01_1200"), self._session("2026-01-01_1300")

        self.assertEqual(clear_buffer(self.root), 2)

        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        # The buffer folder itself stays: the recorder writes into it next.
        self.assertTrue(self.root.is_dir())

    def test_it_removes_loose_files_as_well_as_session_folders(self):
        """A crashed session can leave a half-written .mkv with no folder
        of its own; 'nothing outlives the app' means that too."""
        (self.root / "stray.mkv").write_bytes(b"x")
        self._session("2026-01-01_1200")

        self.assertEqual(clear_buffer(self.root), 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_missing_buffer_is_not_an_error(self):
        """The normal first-run case: nothing has recorded yet."""
        self.assertEqual(clear_buffer(self.root / "never-created"), 0)

    def test_one_undeletable_entry_does_not_stop_the_rest(self):
        """This runs at startup, where a failure must not stop a student
        recording, and at exit, where there is nobody left to tell."""
        kept, gone = self._session("2026-01-01_1200"), self._session("2026-01-01_1300")
        real_rmtree = __import__("shutil").rmtree

        def rmtree(path, *args, **kwargs):
            if Path(path) == kept:
                raise OSError("file in use")
            return real_rmtree(path, *args, **kwargs)

        with patch("session_buffer.shutil.rmtree", side_effect=rmtree):
            removed = clear_buffer(self.root)

        self.assertEqual(removed, 1)
        self.assertTrue(kept.exists())
        self.assertFalse(gone.exists())

    def test_it_refuses_to_clear_anything_outside_temp(self):
        """The guard that stops a mis-wired output_root from deleting real
        recordings. Nothing is removed and nothing is raised."""
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "sessions"
            outside.mkdir()
            (outside / "2026-01-01_1200").mkdir()

            with patch("tempfile.gettempdir", return_value=str(Path(elsewhere) / "temp")):
                removed = clear_buffer(outside)

            self.assertEqual(removed, 0)
            self.assertTrue((outside / "2026-01-01_1200").exists())


class DriveDescriptionTest(unittest.TestCase):
    """With two sticks plugged in a student picks by what is written on
    them, so the description carries the label -- and the drive letter,
    which is the only part guaranteed to be unique."""

    def test_it_reads_like_the_drive_in_their_hand(self):
        drive = Drive(path=Path("E:/"), label="KINGSTON", free_bytes=14_200_000_000)
        self.assertEqual(drive.describe(), "KINGSTON (E:) - 14.2 GB free")

    def test_an_unlabelled_drive_still_names_itself(self):
        drive = Drive(path=Path("F:/"), label="", free_bytes=None)
        self.assertEqual(drive.describe(), str(drive.path))

    def test_details_are_gathered_per_drive(self):
        with patch("session_buffer.removable_drives", return_value=[Path("E:/"), Path("F:/")]):
            with patch("session_buffer._volume_label", side_effect=["KINGSTON", ""]):
                with patch("session_buffer.shutil.disk_usage") as usage:
                    usage.side_effect = [
                        type("U", (), {"free": 8_000_000_000})(),
                        OSError("no media"),  # a card reader with no card in it
                    ]
                    drives = removable_drives_detailed()

        self.assertEqual([d.path for d in drives], [Path("E:/"), Path("F:/")])
        self.assertEqual(drives[0].label, "KINGSTON")
        # A drive that will not answer is still offered: a wrong choice the
        # student can see beats one that mysteriously isn't listed.
        self.assertIsNone(drives[1].free_bytes)


class ExportDestinationTest(unittest.TestCase):
    """Only picks the *default* a student sees in a save dialog, so every
    failure here has to be a quiet fallback, never an exception."""

    def test_a_removable_drive_wins_when_one_is_plugged_in(self):
        with patch("session_buffer.removable_drives", return_value=[Path("E:\\"), Path("F:\\")]):
            self.assertEqual(default_export_dir(), Path("E:\\"))

    def test_it_falls_back_to_home_with_no_removable_drive(self):
        with patch("session_buffer.removable_drives", return_value=[]):
            self.assertEqual(default_export_dir(), Path.home())

    def test_enumeration_failure_is_not_an_error(self):
        with patch("session_buffer.sys.platform", "linux"):
            self.assertEqual(removable_drives(), [])


if __name__ == "__main__":
    unittest.main()
