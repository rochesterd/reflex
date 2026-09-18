"""Tests for panopto_upload against real recorded sessions.

Records two SyntheticCameras through the real Recorder, reads the result
back with Session.load(), and uploads it to an in-memory Panopto. That is
the integration shape CLAUDE.md asks for, and it is what makes these tests
worth anything: the manifest's offsets and stream ordering are the whole
synchronization story, and they are read off a session that was genuinely
encoded rather than hand-built.

The bytes never leave the machine and no credentials exist. What is still
unproven here is Panopto's wire format -- see test_panopto_api.py.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

from panopto_api import (
    MANIFEST_FILENAME,
    UPLOAD_STATE_COMPLETE,
    PanoptoClient,
    UploadAborted,
)
from panopto_upload import (
    UCS_NAMESPACE,
    UploadCancelled,
    build_manifest,
    build_ucs_manifest,
    default_title,
    upload_session,
)
from session_format import AUDIO_STREAM, INSTRUMENT_STREAM, THIRD_PERSON_STREAM
from session_reader import Session
from test_panopto_api import HOST, FakePanoptoSite, make_login
from test_session_reader import record_session

CHUNK = 64 * 1024


class InMemoryUploadClient(PanoptoClient):
    """Everything real except the S3 transfer.

    begin_upload and finish_upload run the actual client code against the
    fake site; only the byte transfer is replaced, since the real one is
    boto3 talking to an S3 endpoint. Chunked, and polls cancel_cb between
    chunks, because a student cancelling a large upload should stop within
    a chunk rather than at the end of a file.
    """

    def __init__(self, site: FakePanoptoSite):
        login = make_login(site)
        login.sign_in()
        super().__init__(HOST, login, site)
        self.site = site
        self.received: dict[str, bytes] = {}

    def put_upload_file(self, target, local_path, remote_name, progress_cb=None, cancel_cb=None):
        data = Path(local_path).read_bytes()
        sent = 0
        while sent < len(data):
            if cancel_cb is not None and cancel_cb():
                raise UploadAborted()
            sent = min(sent + CHUNK, len(data))
            if progress_cb is not None:
                progress_cb(sent, len(data))
        self.received[remote_name] = data
        self.site.uploads[target.upload_id]["files"].append(remote_name)

    @property
    def finished(self) -> list[str]:
        """Upload ids the real finish_upload actually completed."""
        return [
            upload_id
            for upload_id, record in self.site.uploads.items()
            if record.get("State") == UPLOAD_STATE_COMPLETE
        ]


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.session_dir = record_session(cls._tmp.name, 2)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        # A fresh copy per test: several of these delete stream files to
        # reach the half-session paths, and a shared recording would leave
        # whichever test ran next with nothing to read.
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        copy = Path(work.name) / self.session_dir.name
        shutil.copytree(self.session_dir, copy)
        self.session = Session.load(copy)

    def test_third_person_is_primary_and_the_instrument_follows(self):
        # Per the Notion brief: the room camera is the primary stream.
        manifest = build_manifest(self.session)
        self.assertEqual([f.role for f in manifest.files], [THIRD_PERSON_STREAM, INSTRUMENT_STREAM])
        self.assertEqual(manifest.primary.role, THIRD_PERSON_STREAM)
        self.assertEqual(sum(1 for f in manifest.files if f.is_primary), 1)

    def test_offsets_come_from_the_session_not_from_an_assumption(self):
        self.session.streams[INSTRUMENT_STREAM] = replace(
            self.session.streams[INSTRUMENT_STREAM], offset_s=0.25
        )
        manifest = build_manifest(self.session)
        offsets = {f.role: f.offset_s for f in manifest.files}
        self.assertEqual(offsets[INSTRUMENT_STREAM], 0.25)
        self.assertEqual(offsets[THIRD_PERSON_STREAM], 0.0)

    def test_total_bytes_is_what_is_actually_on_disk(self):
        manifest = build_manifest(self.session)
        on_disk = sum(info.path.stat().st_size for info in self.session.streams.values())
        self.assertEqual(manifest.total_bytes, on_disk)

    def test_a_session_missing_a_camera_still_uploads(self):
        # An irreplaceable half-session must not be refused: the student
        # cannot record the hour again.
        gone = self.session.streams[THIRD_PERSON_STREAM].path
        gone.unlink()

        manifest = build_manifest(self.session)
        self.assertEqual([f.role for f in manifest.files], [INSTRUMENT_STREAM])
        self.assertEqual(manifest.primary.role, INSTRUMENT_STREAM)

    def test_a_session_with_no_files_left_is_refused(self):
        for info in self.session.streams.values():
            info.path.unlink()
        with self.assertRaises(ValueError):
            build_manifest(self.session)

    def test_unverified_streams_are_declared_in_the_description(self):
        self.session.streams[INSTRUMENT_STREAM] = replace(
            self.session.streams[INSTRUMENT_STREAM], verified=False
        )
        manifest = build_manifest(self.session)
        self.assertIn("not verified", manifest.description)
        self.assertIn(INSTRUMENT_STREAM, manifest.description)

    def test_default_title_names_the_instrument_and_carries_no_student(self):
        title = default_title(self.session)
        self.assertIn("BI900", title)
        self.assertRegex(title, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def test_an_explicit_title_wins(self):
        self.assertEqual(build_manifest(self.session, "Retake").title, "Retake")


class AudioManifestTest(unittest.TestCase):
    """A microphone track is a third entry in the UCS manifest, typed
    Audio, and never becomes the Primary."""

    @classmethod
    def setUpClass(cls):
        from test_audio_playback import record_with_audio

        cls._tmp = tempfile.TemporaryDirectory()
        cls.session = record_with_audio(cls._tmp.name, 1.0)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_audio_is_listed_as_an_audio_entry_after_the_videos(self):
        manifest = build_manifest(self.session)
        roles = [f.role for f in manifest.files]
        self.assertEqual(roles, [THIRD_PERSON_STREAM, INSTRUMENT_STREAM, AUDIO_STREAM])
        self.assertEqual(manifest.primary.role, THIRD_PERSON_STREAM)

        root = ET.fromstring(build_ucs_manifest(manifest))
        types = [
            v.find(f"{{{UCS_NAMESPACE}}}Type").text
            for v in root.findall(f"{{{UCS_NAMESPACE}}}Videos/{{{UCS_NAMESPACE}}}Video")
        ]
        self.assertEqual(types, ["Primary", "Secondary", "Audio"])


class UploadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.session_dir = record_session(cls._tmp.name, 2)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.session = Session.load(self.session_dir)
        self.site = FakePanoptoSite()
        self.site.add_folder("parent", "Slit lamp practice")
        self.client = InMemoryUploadClient(self.site)

    def test_both_streams_arrive_whole_and_the_viewer_url_comes_back(self):
        url = upload_session(self.session, self.client, "parent")

        self.assertEqual(
            set(self.client.received),
            {"third_person.mp4", "instrument.mp4", MANIFEST_FILENAME},
        )
        for role, info in self.session.streams.items():  # the videos, byte for byte
            self.assertEqual(
                self.client.received[info.path.name],
                info.path.read_bytes(),
                f"{role} did not arrive byte-for-byte",
            )
        self.assertIn("session-upload-1", url)
        self.assertTrue(url.startswith(f"https://{HOST}/"))

    def test_progress_is_monotonic_and_ends_at_the_total(self):
        seen: list[tuple[int, int]] = []
        upload_session(self.session, self.client, "parent", progress_cb=lambda d, t: seen.append((d, t)))

        self.assertTrue(seen)
        self.assertEqual({t for _, t in seen}, {build_manifest(self.session).total_bytes})
        dones = [d for d, _ in seen]
        self.assertEqual(dones, sorted(dones), "progress went backwards")
        self.assertEqual(dones[-1], seen[-1][1], "progress did not reach the total")

    def test_progress_spans_both_files_rather_than_restarting(self):
        # One bar for the whole upload: the second file must continue from
        # where the first ended, not reset to zero.
        first_size = self.session.streams[THIRD_PERSON_STREAM].path.stat().st_size
        seen: list[int] = []
        upload_session(self.session, self.client, "parent", progress_cb=lambda d, t: seen.append(d))
        self.assertTrue(any(d > first_size for d in seen))

    def test_cancelling_before_the_first_byte_uploads_nothing(self):
        with self.assertRaises(UploadCancelled):
            upload_session(self.session, self.client, "parent", cancel_cb=lambda: True)

        self.assertEqual(self.client.received, {})
        self.assertEqual(self.client.finished, [])

    def test_cancelling_mid_upload_never_finishes_the_session(self):
        # A cancelled upload must not leave something that looks complete,
        # the same rule export_session follows with its .partial.mp4.
        calls = {"n": 0}

        def cancel_after_a_chunk() -> bool:
            calls["n"] += 1
            return calls["n"] > 2

        with self.assertRaises(UploadCancelled):
            upload_session(self.session, self.client, "parent", cancel_cb=cancel_after_a_chunk)

        self.assertEqual(self.client.finished, [])
        self.assertEqual(self.site.uploads["upload-1"]["files"], [])

    def test_the_manifest_is_sent_last_so_it_never_describes_missing_files(self):
        order: list[str] = []
        original = self.client.put_upload_file

        def watched(target, local_path, remote_name, **kwargs):
            order.append(remote_name)
            return original(target, local_path, remote_name, **kwargs)

        self.client.put_upload_file = watched
        upload_session(self.session, self.client, "parent")
        self.assertEqual(order[-1], MANIFEST_FILENAME)

    def test_the_manifest_describes_both_streams_with_their_offsets(self):
        upload_session(self.session, self.client, "parent")
        root = ET.fromstring(self.client.received[MANIFEST_FILENAME].decode("utf-8"))

        videos = root.findall(f"{{{UCS_NAMESPACE}}}Videos/{{{UCS_NAMESPACE}}}Video")
        described = {
            video.find(f"{{{UCS_NAMESPACE}}}File").text: (
                video.find(f"{{{UCS_NAMESPACE}}}Type").text,
                video.find(f"{{{UCS_NAMESPACE}}}Start").text,
            )
            for video in videos
        }
        self.assertEqual(
            described,
            {"third_person.mp4": ("Primary", "PT0S"), "instrument.mp4": ("Secondary", "PT0S")},
        )

    def test_every_manifest_element_is_in_the_ucs_namespace(self):
        # A child in no namespace (<Title xmlns="">) parses fine and then
        # fails to match the schema on Panopto's side, which is the kind of
        # failure that only shows up after a student has walked away.
        upload_session(self.session, self.client, "parent")
        root = ET.fromstring(self.client.received[MANIFEST_FILENAME].decode("utf-8"))
        for element in root.iter():
            self.assertTrue(
                element.tag.startswith(f"{{{UCS_NAMESPACE}}}"),
                f"{element.tag} is outside the UCS namespace",
            )

    def test_upload_lands_in_the_folder_it_was_given(self):
        upload_session(self.session, self.client, "parent")
        self.assertEqual(self.site.uploads["upload-1"]["FolderId"], "parent")


if __name__ == "__main__":
    unittest.main()
