"""Tests for secret_store against real DPAPI -- no mocking of crypt32.

Mocking the thing under test would prove only that ctypes was called. What
matters is that the plaintext genuinely does not appear on disk and that a
blob written by this machine reads back, which only the real API can show.

These tests are Windows-only, like the app.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from secret_store import (
    SecretError,
    protect,
    read_secret,
    unprotect,
    write_secret,
)

SECRET = "panopto-client-secret-9f3c"


class RoundTripTest(unittest.TestCase):
    def test_a_protected_value_reads_back(self):
        self.assertEqual(unprotect(protect(SECRET)), SECRET)

    def test_the_ciphertext_does_not_contain_the_plaintext(self):
        blob = protect(SECRET)
        self.assertNotIn(SECRET.encode("utf-8"), blob)
        self.assertNotIn(SECRET.encode("utf-16-le"), blob)

    def test_non_ascii_survives(self):
        self.assertEqual(unprotect(protect("pässwörd–ünicode")), "pässwörd–ünicode")

    def test_an_empty_secret_still_round_trips(self):
        # Not a valid credential, but the store shouldn't be the thing that
        # decides that -- config.py reports it with a message that helps.
        self.assertEqual(unprotect(protect("")), "")

    def test_a_corrupt_blob_is_refused_with_a_fix(self):
        blob = bytearray(protect(SECRET))
        blob[len(blob) // 2] ^= 0xFF
        with self.assertRaises(SecretError) as caught:
            unprotect(bytes(blob))
        self.assertIn("Settings", str(caught.exception))

    def test_entropy_means_a_foreign_dpapi_blob_will_not_decrypt(self):
        # Another app's blob on the same machine must not be readable as
        # ours, or a mistaken path would silently yield someone else's data.
        import ctypes

        from secret_store import CRYPTPROTECT_LOCAL_MACHINE, _Blob

        data_in = _Blob.of(b"someone else's secret")
        data_out = _Blob()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(data_in), None, None, None, None,
            CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(data_out),
        )
        self.assertTrue(ok)
        foreign = data_out.value()
        ctypes.windll.kernel32.LocalFree(data_out.pbData)

        with self.assertRaises(SecretError):
            unprotect(foreign)


class FileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "panopto.secret"

    def test_written_then_read(self):
        write_secret(self.path, SECRET)
        self.assertEqual(read_secret(self.path), SECRET)

    def test_the_file_never_holds_the_plaintext(self):
        write_secret(self.path, SECRET)
        self.assertNotIn(SECRET.encode("utf-8"), self.path.read_bytes())

    def test_rewriting_replaces_rather_than_appends(self):
        write_secret(self.path, SECRET)
        first = self.path.stat().st_size
        write_secret(self.path, "a-different-secret")
        self.assertEqual(read_secret(self.path), "a-different-secret")
        self.assertAlmostEqual(self.path.stat().st_size, first, delta=32)

    def test_no_partial_file_is_left_behind(self):
        write_secret(self.path, SECRET)
        leftovers = [p.name for p in self.path.parent.iterdir() if "partial" in p.name]
        self.assertEqual(leftovers, [])

    def test_a_missing_file_is_a_clear_error(self):
        with self.assertRaises(SecretError):
            read_secret(self.path)

    def test_an_empty_file_is_a_clear_error(self):
        self.path.write_bytes(b"")
        with self.assertRaises(SecretError) as caught:
            read_secret(self.path)
        self.assertIn("Settings", str(caught.exception))

    def test_the_parent_directory_is_created(self):
        nested = Path(self._tmp.name) / "Reflex" / "panopto.secret"
        write_secret(nested, SECRET)
        self.assertEqual(read_secret(nested), SECRET)


if __name__ == "__main__":
    unittest.main()
