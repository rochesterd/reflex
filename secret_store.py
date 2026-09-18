"""Machine-bound storage for the one secret Reflex holds: Panopto's client
secret.

ctypes over inbox `crypt32.dll` -- DPAPI, no third-party crypto and no new
dependency, the same boundary winusb.py draws around `winusb.dll`.

**What this protects against, and what it does not.** The blob is encrypted
to *this machine* (CRYPTPROTECT_LOCAL_MACHINE), so a copy of the file is
worthless anywhere else: off a stolen disk, out of a backup, in a support
email, on a technician's USB stick. It does **not** make the secret
unreadable to code running on the kiosk as the account app.exe runs as --
machine scope means any process on the box can decrypt.

That is acceptable because the secret is low-value by design: it belongs
to an authorization-code client and cannot mint a token without a student
signing in (DECISIONS.md 2026-09-18). The store exists so a secret never
sits in a JSON file, not because the kiosk's safety depends on it.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)

CRYPTPROTECT_UI_FORBIDDEN = 0x01
CRYPTPROTECT_LOCAL_MACHINE = 0x04

# Mixed into the encryption so a blob from this app is not interchangeable
# with some other DPAPI blob on the same machine. Not a key and not a
# secret -- it is in the source, deliberately; it only scopes the ciphertext.
_ENTROPY = b"NECO Reflex / Panopto client secret / v1"

_DESCRIPTION = "NECO Reflex Panopto credential"


class SecretError(RuntimeError):
    """The secret could not be stored or read back. Always loud: a kiosk
    that silently loses its credential looks exactly like one that was
    never configured."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    @classmethod
    def of(cls, data: bytes) -> "_Blob":
        buffer = ctypes.create_string_buffer(data, len(data))
        return cls(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    def value(self) -> bytes:
        return ctypes.string_at(self.pbData, self.cbData)


def _crypt32():
    try:
        return ctypes.windll.crypt32
    except AttributeError as exc:  # pragma: no cover -- Windows-only app
        raise SecretError("DPAPI is only available on Windows") from exc


def protect(plaintext: str) -> bytes:
    """Encrypt to this machine. Raises SecretError rather than returning
    something that isn't ciphertext."""
    data_in = _Blob.of(plaintext.encode("utf-8"))
    entropy = _Blob.of(_ENTROPY)
    data_out = _Blob()
    ok = _crypt32().CryptProtectData(
        ctypes.byref(data_in),
        _DESCRIPTION,
        ctypes.byref(entropy),
        None,
        None,
        CRYPTPROTECT_LOCAL_MACHINE | CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(data_out),
    )
    if not ok:
        raise SecretError(f"CryptProtectData failed (error {ctypes.GetLastError()})")
    try:
        return data_out.value()
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.pbData)


def unprotect(blob: bytes) -> str:
    """Decrypt a blob produced by protect() on this machine."""
    data_in = _Blob.of(blob)
    entropy = _Blob.of(_ENTROPY)
    data_out = _Blob()
    ok = _crypt32().CryptUnprotectData(
        ctypes.byref(data_in),
        None,
        ctypes.byref(entropy),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(data_out),
    )
    if not ok:
        raise SecretError(
            f"CryptUnprotectData failed (error {ctypes.GetLastError()}). The stored "
            f"credential was written on a different machine, or is corrupt -- "
            f"re-enter it in Settings."
        )
    try:
        return data_out.value().decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.pbData)


def write_secret(path: Path | str, plaintext: str) -> None:
    """Encrypt `plaintext` to `path`, then lock the file down.

    Written whole and replaced, never appended to: a half-written
    credential file is indistinguishable from a corrupt one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_bytes(protect(plaintext))
    temporary.replace(path)
    restrict_access(path)


def read_secret(path: Path | str) -> str:
    path = Path(path)
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise SecretError(f"could not read {path}: {exc}") from exc
    if not blob:
        raise SecretError(f"{path} is empty -- re-enter the credential in Settings.")
    return unprotect(blob)


def restrict_access(path: Path | str) -> None:
    """Drop inherited permissions and leave SYSTEM, Administrators and the
    local Users group (which is what app.exe runs as on a kiosk).

    Users must keep *read* or the app cannot upload at all -- see this
    module's docstring on why that ceiling is inherent. What this removes
    is everything wider than the machine: inherited grants from a parent
    folder someone widened, and any write access for a non-administrator,
    so the credential cannot be swapped for an attacker's.

    Best-effort by design: a failure here is logged, not raised. Refusing
    to save a credential because icacls returned non-zero would leave the
    technician with no integration and no way forward.
    """
    path = Path(path)
    commands = [
        ["icacls", str(path), "/inheritance:r"],
        ["icacls", str(path), "/grant:r", "*S-1-5-18:(F)"],  # SYSTEM
        ["icacls", str(path), "/grant:r", "*S-1-5-32-544:(F)"],  # Administrators
        ["icacls", str(path), "/grant:r", "*S-1-5-32-545:(R)"],  # Users: read only
    ]
    for command in commands:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:  # pragma: no cover -- icacls is inbox
            logger.warning("could not lock down %s: %s", path, exc)
            return
        if result.returncode != 0:
            logger.warning(
                "could not lock down %s: %s", path, (result.stderr or result.stdout).strip()
            )
            return
    logger.info("restricted access to %s", path)
