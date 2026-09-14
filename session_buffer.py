"""Where a recording lives while the student who made it is still here.

Reflex keeps no library of recordings. Every session contains two students
-- the one performing the skill and the peer being examined -- and an eye
and a face are PII, so what the kiosk holds is a *buffer*, not a folder: it
exists while the app does, and what a student takes away is a file they
exported deliberately. Nothing accumulates, which is why there is nothing
to sweep. See DECISIONS.md's 2026-09-13 entry.

Three things clear it, and the third is the point of the design:

1. app start, so a session can never outlive the app that made it;
2. app exit, so the usual case leaves nothing behind;
3. a logon task the installer creates, for the case neither of those runs
   -- a crash, a power cut, a forced reboot. An app cannot clean up after
   its own crash, so something outside it has to.

This module is 1 and 2; `packaging/reflex.iss` is 3.

No Qt and no camera imports: `app.py`, `recorder.py`'s caller and the
viewer all need this.
"""
from __future__ import annotations

import ctypes
import logging
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Under the user's temp directory rather than Documents or ProgramData:
# temp is the one location whose contract is "this may be deleted", which
# is exactly the contract we want -- and Windows' own cleanup becomes a
# free fourth backstop behind the three above.
BUFFER_DIR_NAME = "Reflex"
BUFFER_SUBDIR = "buffer"

_DRIVE_REMOVABLE = 2
# GetVolumeInformationW's lpVolumeNameBuffer, per MSDN: a volume label is
# at most MAX_PATH (260) characters plus the terminator.
_VOLUME_NAME_MAX = 261


@dataclass(frozen=True)
class Drive:
    """A removable drive, as a student would recognise it."""

    path: Path
    label: str  # the volume label, "" if the drive has none
    free_bytes: int | None  # None if the drive would not answer

    def describe(self) -> str:
        """What to show in a chooser. The letter is always present and
        always unique, so two identically-labelled sticks stay tellable
        apart -- which is the whole reason the chooser exists."""
        name = f"{self.label} ({self.path.drive})" if self.label else str(self.path)
        if self.free_bytes is None:
            return name
        return f"{name} - {self.free_bytes / 1e9:.1f} GB free"


def buffer_root() -> Path:
    """The folder sessions are recorded into. Not created here: the
    recorder creates its own session folder, and the disk preflight walks
    up to an existing ancestor (see kiosk.py's _existing_ancestor)."""
    return Path(tempfile.gettempdir()) / BUFFER_DIR_NAME / BUFFER_SUBDIR


def clear_buffer(root: Path | str | None = None) -> int:
    """Delete everything in the buffer, returning how many entries went.

    Never raises. This runs at startup, where a failure must not stop a
    student recording, and at exit, where there is nobody left to tell.
    A file that won't delete (a decoder still holding it open, a virus
    scanner mid-scan) is logged and skipped -- the next clear gets it.
    """
    root = Path(root) if root is not None else buffer_root()
    if not _is_temporary(root):
        # A guard, not a policy: this function deletes folders outright,
        # and the one thing that must never happen is it being pointed at
        # somewhere real. The buffer lives under temp by construction, so
        # anything else is a wiring bug -- say so and delete nothing.
        logger.error(
            "refusing to clear %s: the recording buffer must live under %s", root, tempfile.gettempdir()
        )
        return 0
    if not root.is_dir():
        return 0

    removed = 0
    for child in sorted(root.iterdir()):
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError as exc:
            logger.warning("could not clear %s from the recording buffer: %s", child, exc)
            continue
        removed += 1

    if removed:
        logger.info("cleared %d item(s) from the recording buffer at %s", removed, root)
    return removed


def _is_temporary(path: Path) -> bool:
    """True if `path` is inside the system temp directory."""
    try:
        Path(path).resolve().relative_to(Path(tempfile.gettempdir()).resolve())
    except (ValueError, OSError):
        return False
    return True


def removable_drives() -> list[Path]:
    """Drives Windows reports as removable, in letter order. Empty on any
    other platform, and empty rather than raising if the call fails --
    this only picks a *default* for a file dialog the student can steer."""
    if sys.platform != "win32":
        return []
    try:
        kernel32 = ctypes.windll.kernel32
        mask = int(kernel32.GetLogicalDrives())
    except (AttributeError, OSError) as exc:
        logger.debug("could not enumerate drives: %s", exc)
        return []

    drives: list[Path] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = f"{chr(ord('A') + index)}:\\"
        try:
            kind = int(kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
        except OSError as exc:
            logger.debug("could not type drive %s: %s", root, exc)
            continue
        if kind == _DRIVE_REMOVABLE:
            drives.append(Path(root))
    return drives


def removable_drives_detailed() -> list[Drive]:
    """Every removable drive with the label and free space a student
    picks by. A drive that will not answer still appears -- a card reader
    with no card is worth showing as a wrong choice, not hiding."""
    drives = []
    for root in removable_drives():
        try:
            free = shutil.disk_usage(str(root)).free
        except OSError as exc:
            logger.debug("could not measure %s: %s", root, exc)
            free = None
        drives.append(Drive(path=root, label=_volume_label(root), free_bytes=free))
    return drives


def _volume_label(root: Path) -> str:
    """The drive's own name ("KINGSTON"), or "" if it has none or the
    call fails. Cosmetic, so every failure is an empty string."""
    if sys.platform != "win32":
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(_VOLUME_NAME_MAX)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(str(root)), buffer, len(buffer), None, None, None, None, 0
        )
    except (AttributeError, OSError) as exc:
        logger.debug("could not read the volume label of %s: %s", root, exc)
        return ""
    return buffer.value if ok else ""


def default_export_dir() -> Path:
    """Where the Export dialog should open. A student's own drive if one
    is plugged in -- that is the whole point of exporting -- and their
    home folder if not, so the dialog still opens somewhere they can find
    rather than inside the buffer they are trying to leave."""
    drives = removable_drives()
    return drives[0] if drives else Path.home()
