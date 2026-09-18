"""Headless tests for settings.py's SettingsWindow/DeviceRow. Enumeration
and camera-construction functions are injected with fakes -- no real
hardware or the IDS SDK is needed (this dev machine has neither), matching
test_app.py's approach of exercising real failure/success paths against
fake BaseCamera-shaped objects rather than mocking internals.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QLineEdit

from config import (
    AudioConfig,
    PanoptoConfig,
    StreamingConfig,
    load_config,
    panopto_secret_path,
)
from device_presets import CUSTOM_PROFILE_ID
from secret_store import read_secret
from settings import AudioSection, DeviceRow, PanoptoSection, SettingsWindow, StreamingSection
from synthetic_camera import SyntheticCamera
from uvc_enumeration import UvcDeviceInfo

_qt_app = QApplication.instance() or QApplication([])


@dataclass
class _FakeIdsDevice:
    serial: str
    model_name: str


SLIT_LAMP_DEVICE = _FakeIdsDevice(serial="111", model_name="UI325xCP-C")  # what the camera reports
BIO_DEVICE = _FakeIdsDevice(serial="222", model_name="U3-327xCP-C")
THIRD_PERSON_DEVICE = UvcDeviceInfo(index=0, name="HD USB Camera", vid_pid="32E4:9310")

# What winusb.find_by_vid_pid() returns: (instance_id, device_path).
# _net2860_winusb_candidates() only counts these, so the values are
# placeholders rather than realistic device paths.
ONE_WINUSB_DEVICE = ("legacy-bio-instance", "legacy-bio-path")

VALID_CONFIG = {
    "instruments": {
        "slit_lamp": {
            "kind": "ids",
            "serial": "111",
            "label": "Slit Lamp",
            "profile": "haag_streit_bi900_slit_lamp",
        },
        "bio": {"kind": "ids", "serial": "222", "label": "BIO", "profile": "keeler_vantage_plus_digital"},
    },
    "third_person": {"kind": "uvc", "vid_pid": "32E4:9310", "friendly_name": "HD USB Camera"},
}


def _select(row: DeviceRow, key: str) -> None:
    for i in range(row.combo.count()):
        if row.combo.itemData(i) not in (None, -1) and row._candidates[row.combo.itemData(i)].key == key:
            row.combo.setCurrentIndex(i)
            return
    raise AssertionError(f"no candidate with key {key!r} in row {row.role_key!r}")


class SettingsWindowTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.config_path = Path(self._tmpdir.name) / "config.json"

    def _make_window(
        self,
        ids_devices: list = (),
        uvc_devices: list = (),
        # Injected and empty by default so these tests never depend on
        # whether a legacy BIO happens to be plugged into the machine
        # running them -- unlike the vendor-driver candidate, the WinUSB one
        # is discovered rather than always offered.
        winusb_devices: list = (),
        instrument_preview_factory=lambda candidate: SyntheticCamera(160, 120, fps=30),
        uvc_preview_factory=lambda candidate: SyntheticCamera(160, 120, fps=30),
    ) -> SettingsWindow:
        return SettingsWindow(
            config_path=self.config_path,
            list_ids_devices_fn=lambda: list(ids_devices),
            list_uvc_devices_fn=lambda: list(uvc_devices),
            list_net2860_winusb_fn=lambda: list(winusb_devices),
            instrument_preview_camera_factory=instrument_preview_factory,
            uvc_preview_camera_factory=uvc_preview_factory,
        )

    def test_fresh_start_shows_not_connected_and_save_disabled(self):
        window = self._make_window()

        for row in window._all_rows():
            self.assertIsNone(row.selected_key())
        self.assertFalse(window.save_button.isEnabled())
        self.assertTrue(window.warning_label.isHidden())

    def test_valid_existing_config_prepopulates_selection_and_labels(self):
        self.config_path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
        window = self._make_window(
            ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE]
        )

        self.assertEqual(window._instrument_rows["slit_lamp"].selected_key(), "111")
        self.assertEqual(window._instrument_rows["slit_lamp"].label_text(), "Slit Lamp")
        self.assertEqual(window._instrument_rows["bio"].selected_key(), "222")
        self.assertEqual(window._third_person_row.selected_key(), "32E4:9310")
        self.assertTrue(window.save_button.isEnabled())

    def test_configured_device_no_longer_present_shows_not_connected(self):
        self.config_path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
        # Only bio is currently enumerated -- slit_lamp's configured serial is absent.
        window = self._make_window(ids_devices=[BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE])

        self.assertIsNone(window._instrument_rows["slit_lamp"].selected_key())
        self.assertEqual(window._instrument_rows["slit_lamp"].label_text(), "Slit Lamp")  # label still loaded
        self.assertEqual(window._instrument_rows["bio"].selected_key(), "222")
        # Saving is allowed -- a room may have only one instrument -- but the
        # consequence is spelled out first, because an absent camera here is
        # far more likely to be one that is merely unplugged than one that
        # does not exist. This used to be a hard block; the protection is now
        # the warning, not the disabled button.
        self.assertTrue(window.save_button.isEnabled())
        self.assertFalse(window.omission_label.isHidden())
        self.assertIn("Slit Lamp", window.omission_label.text())
        self.assertIn("Rescan", window.omission_label.text())

    def test_malformed_existing_config_shows_warning_without_crashing(self):
        self.config_path.write_text("{not valid json", encoding="utf-8")

        window = self._make_window()

        self.assertFalse(window.warning_label.isHidden())
        for row in window._all_rows():
            self.assertIsNone(row.selected_key())

    def test_save_gating_requires_all_three_rows_valid(self):
        window = self._make_window(
            ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE]
        )
        self.assertFalse(window.save_button.isEnabled())

        _select(window._instrument_rows["slit_lamp"], "111")
        window._instrument_rows["slit_lamp"].label_edit.setText("Slit Lamp")
        self.assertFalse(window.save_button.isEnabled())

        _select(window._instrument_rows["bio"], "222")
        window._instrument_rows["bio"].label_edit.setText("BIO")
        self.assertFalse(window.save_button.isEnabled())  # third-person still unset

        _select(window._third_person_row, "32E4:9310")
        self.assertTrue(window.save_button.isEnabled())

    def test_one_instrument_plus_third_person_is_enough_to_save(self):
        # A room may have only one instrument, and a dev machine may have
        # only the camera being worked on. What has to be true is that
        # something records, not that every role is filled.
        window = self._make_window(ids_devices=[BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE])
        _select(window._instrument_rows["bio"], "222")
        window._instrument_rows["bio"].label_edit.setText("BIO")
        _select(window._third_person_row, "32E4:9310")

        self.assertTrue(window.save_button.isEnabled())
        window._on_save_clicked()

        written = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(list(written["instruments"]), ["bio"])
        # And the result has to be loadable -- a config settings.py will
        # write but config.py refuses is worse than a blocked Save button.
        self.config_path.write_text(json.dumps(written), encoding="utf-8")
        cfg = load_config(self.config_path)
        self.assertEqual(list(cfg.instruments), ["bio"])

    def test_no_instrument_at_all_still_blocks_save(self):
        window = self._make_window(uvc_devices=[THIRD_PERSON_DEVICE])
        _select(window._third_person_row, "32E4:9310")

        self.assertFalse(window.save_button.isEnabled())

    def test_saving_drops_a_role_whose_camera_is_no_longer_there(self):
        self.config_path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
        window = self._make_window(ids_devices=[BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE])

        window._on_save_clicked()

        written = json.loads(self.config_path.read_text(encoding="utf-8"))
        # Not carried forward from the old file: keeping it would leave
        # students an instrument the technician could not verify.
        self.assertNotIn("slit_lamp", written["instruments"])
        self.assertIn("bio", written["instruments"])

    def test_same_camera_picked_for_two_roles_blocks_save(self):
        window = self._make_window(
            ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE]
        )
        _select(window._instrument_rows["slit_lamp"], "111")
        window._instrument_rows["slit_lamp"].label_edit.setText("Slit Lamp")
        _select(window._instrument_rows["bio"], "111")  # same serial as slit_lamp
        window._instrument_rows["bio"].label_edit.setText("BIO")
        _select(window._third_person_row, "32E4:9310")

        self.assertFalse(window.save_button.isEnabled())
        self.assertFalse(window.conflict_label.isHidden())
        self.assertIn("111", window.conflict_label.text())

        window._on_save_clicked()  # defense in depth: must no-op even if called directly
        self.assertFalse(self.config_path.exists())

        _select(window._instrument_rows["bio"], "222")  # resolve the conflict

        self.assertTrue(window.conflict_label.isHidden())
        self.assertTrue(window.save_button.isEnabled())

    def test_a_matched_profile_names_the_instrument_for_you(self):
        """The label is tied to the supported device, so selecting a known
        camera leaves nothing to type."""
        window = self._make_window(ids_devices=[SLIT_LAMP_DEVICE])
        row = window._instrument_rows["slit_lamp"]
        _select(row, "111")

        self.assertEqual(row.profile_id(), "haag_streit_bi900_slit_lamp")
        self.assertEqual(row.label_text(), "Slit Lamp")
        self.assertTrue(row.is_valid())

    def test_custom_requires_a_typed_name(self):
        """The escape hatch still has to produce something students can read
        on the picker."""
        window = self._make_window(ids_devices=[SLIT_LAMP_DEVICE])
        row = window._instrument_rows["slit_lamp"]
        _select(row, "111")
        row.profile_combo.setCurrentIndex(row.profile_combo.findData(CUSTOM_PROFILE_ID))

        self.assertEqual(row.label_text(), "")  # only a technician can name an unlisted camera
        self.assertFalse(row.is_valid())

        row.label_edit.setText("Borrowed slit lamp")
        self.assertTrue(row.is_valid())

    def test_save_writes_expected_json_shape(self):
        window = self._make_window(
            ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE]
        )
        _select(window._instrument_rows["slit_lamp"], "111")
        window._instrument_rows["slit_lamp"].label_edit.setText("Slit Lamp")
        _select(window._instrument_rows["bio"], "222")
        window._instrument_rows["bio"].label_edit.setText("BIO")
        _select(window._third_person_row, "32E4:9310")

        window._on_save_clicked()

        written = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(written, VALID_CONFIG)

    def test_a_row_with_no_device_claims_nothing(self):
        """A row holding no camera has no business naming an instrument, so
        both fields stay inert until one is chosen."""
        window = self._make_window(ids_devices=[SLIT_LAMP_DEVICE])
        row = window._instrument_rows["slit_lamp"]

        self.assertIsNone(row.selected_key())
        self.assertFalse(row.profile_combo.isEnabled())
        self.assertFalse(row.label_edit.isEnabled())
        self.assertIn("Choose the camera", row.status_label.text())

    def test_choosing_a_device_enables_the_other_two_fields(self):
        window = self._make_window(ids_devices=[SLIT_LAMP_DEVICE])
        row = window._instrument_rows["slit_lamp"]
        _select(row, "111")

        self.assertTrue(row.profile_combo.isEnabled())
        self.assertTrue(row.label_edit.isEnabled())
        self.assertIn("No auto-exposure", row.status_label.text())

    def test_an_unlisted_camera_falls_to_custom_and_asks_for_a_name(self):
        unlisted = _FakeIdsDevice(serial="999", model_name="Unlisted-Cam-1")
        window = self._make_window(ids_devices=[unlisted])
        row = window._instrument_rows["slit_lamp"]
        _select(row, "999")

        self.assertEqual(row.profile_id(), CUSTOM_PROFILE_ID)
        self.assertEqual(row.label_text(), "")
        self.assertFalse(row.is_valid())
        self.assertIn("Type a name", row.status_label.text())

    def test_the_typed_name_is_what_students_see_and_round_trips(self):
        """The profile offers a name; the technician's edit wins and is what
        lands in label, which is the only name app.py reads."""
        window = self._make_window(
            ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE]
        )
        row = window._instrument_rows["slit_lamp"]
        _select(row, "111")
        self.assertEqual(row.label_text(), "Slit Lamp")  # offered, not imposed

        row.label_edit.setText("Big Slit Lamp")
        row.label_edit.textEdited.emit("Big Slit Lamp")  # as a person typing would
        _select(window._instrument_rows["bio"], "222")
        _select(window._third_person_row, "32E4:9310")
        window._on_save_clicked()

        written = json.loads(self.config_path.read_text(encoding="utf-8"))["instruments"]["slit_lamp"]
        self.assertEqual(written["label"], "Big Slit Lamp")
        self.assertEqual(written["profile"], "haag_streit_bi900_slit_lamp")
        self.assertNotIn("nickname", written)

        reopened = self._make_window(ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE])
        row = reopened._instrument_rows["slit_lamp"]
        self.assertEqual(row.profile_id(), "haag_streit_bi900_slit_lamp")
        self.assertEqual(row.label_text(), "Big Slit Lamp")

    def test_a_config_written_before_profiles_reopens_as_custom(self):
        """A typed label and no profile is exactly what Custom means, so it
        has to survive being reopened and resaved."""
        data = json.loads(json.dumps(VALID_CONFIG))
        for entry in data["instruments"].values():
            entry.pop("profile", None)
        data["instruments"]["slit_lamp"]["label"] = "Old Typed Name"
        self.config_path.write_text(json.dumps(data), encoding="utf-8")

        window = self._make_window(ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE])
        row = window._instrument_rows["slit_lamp"]

        self.assertEqual(row.profile_id(), CUSTOM_PROFILE_ID)
        self.assertEqual(row.label_text(), "Old Typed Name")

    def test_a_profile_id_this_build_does_not_know_lands_on_custom(self):
        """Written by a newer build. app.py falls back the same way, so
        Settings must not silently show it as something it isn't."""
        data = json.loads(json.dumps(VALID_CONFIG))
        data["instruments"]["slit_lamp"]["profile"] = "written_by_a_newer_build"
        self.config_path.write_text(json.dumps(data), encoding="utf-8")

        window = self._make_window(ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE])

        self.assertEqual(window._instrument_rows["slit_lamp"].profile_id(), CUSTOM_PROFILE_ID)

    def test_a_profile_on_the_wrong_camera_warns_without_blocking(self):
        """A technician may know better than the table, and Preview shows
        the result either way -- so this is a note, not a refusal."""
        window = self._make_window(ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE])
        row = window._instrument_rows["bio"]
        _select(row, "222")
        row.profile_combo.setCurrentIndex(
            row.profile_combo.findData("keeler_vantage_plus_legacy")
        )

        self.assertTrue(row.is_valid())

    def test_save_preserves_config_the_ui_does_not_model(self):
        """recording fps, a per-instrument orientation / pixel_clock_hz
        override, and a third instrument role have no field in this
        two-row UI. Editing something unrelated and saving must not drop
        them. See DECISIONS.md's 2026-09-09 "Config that would only fail
        at Start" entry.
        """
        from config import (
    PanoptoConfig,
    load_config,
    panopto_secret_path,
)

        data = json.loads(json.dumps(VALID_CONFIG))
        data["recording"] = {"fps": 24}
        data["instruments"]["slit_lamp"]["orientation"] = "rotate_180"
        data["instruments"]["slit_lamp"]["pixel_clock_hz"] = 60_000_000
        data["instruments"]["indirect_scope"] = {"kind": "ids", "serial": "333", "label": "Indirect Scope"}
        self.config_path.write_text(json.dumps(data), encoding="utf-8")

        window = self._make_window(
            ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE]
        )
        window._on_save_clicked()

        written = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(written["recording"], {"fps": 24})
        self.assertEqual(written["instruments"]["slit_lamp"]["orientation"], "rotate_180")
        self.assertEqual(written["instruments"]["slit_lamp"]["pixel_clock_hz"], 60_000_000)
        self.assertEqual(
            written["instruments"]["indirect_scope"],
            {"kind": "ids", "serial": "333", "label": "Indirect Scope"},
        )
        load_config(self.config_path)  # the merged result still parses

    def test_save_drops_an_orientation_override_when_the_role_becomes_net2860(self):
        """The carry-forward must not paste an ids-only key onto a net2860
        entry, which config.py rejects."""
        from config import (
    PanoptoConfig,
    load_config,
    panopto_secret_path,
)

        data = json.loads(json.dumps(VALID_CONFIG))
        data["instruments"]["bio"]["orientation"] = "flip_vertical"
        self.config_path.write_text(json.dumps(data), encoding="utf-8")

        window = self._make_window(
            ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE],
            winusb_devices=[ONE_WINUSB_DEVICE],
        )
        _select(window._instrument_rows["bio"], "net2860_winusb")
        window._on_save_clicked()

        written = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(written["instruments"]["bio"], {"kind": "net2860_winusb", "label": "BIO", "profile": "keeler_vantage_plus_legacy"})
        load_config(self.config_path)

    def _fill_valid_selections(self, window: SettingsWindow) -> None:
        _select(window._instrument_rows["slit_lamp"], "111")
        window._instrument_rows["slit_lamp"].label_edit.setText("Slit Lamp")
        _select(window._instrument_rows["bio"], "222")
        window._instrument_rows["bio"].label_edit.setText("BIO")
        _select(window._third_person_row, "32E4:9310")

    def test_winusb_candidate_is_offered_only_when_one_is_detected(self):
        # An absent camera (or an
        # uninstalled driver package) must show up as nothing offered rather
        # than as an entry that fails when selected.
        absent = self._make_window()
        self.assertNotIn(
            "net2860_winusb", {c.key for c in absent._instrument_rows["bio"]._candidates}
        )

        present = self._make_window(winusb_devices=[ONE_WINUSB_DEVICE])
        bio_keys = {c.key for c in present._instrument_rows["bio"]._candidates}
        slit_lamp_keys = {c.key for c in present._instrument_rows["slit_lamp"]._candidates}

        self.assertIn("net2860_winusb", bio_keys)
        self.assertNotIn("net2860_winusb", slit_lamp_keys)  # BIO-specific

    def test_two_detected_winusb_devices_still_offer_one_candidate(self):
        # There is exactly one of this camera, and the candidate carries no
        # identity to tell two apart -- so a duplicated enum entry must not
        # produce two indistinguishable dropdown rows.
        window = self._make_window(winusb_devices=[("a", "pa"), ("b", "pb")])

        keys = [c.key for c in window._instrument_rows["bio"]._candidates]
        self.assertEqual(keys.count("net2860_winusb"), 1)

    def test_selecting_winusb_and_saving_writes_expected_json_shape(self):
        window = self._make_window(
            ids_devices=[SLIT_LAMP_DEVICE],
            uvc_devices=[THIRD_PERSON_DEVICE],
            winusb_devices=[ONE_WINUSB_DEVICE],
        )
        _select(window._instrument_rows["slit_lamp"], "111")
        window._instrument_rows["slit_lamp"].label_edit.setText("Slit Lamp")
        _select(window._instrument_rows["bio"], "net2860_winusb")
        window._instrument_rows["bio"].label_edit.setText("BIO")
        _select(window._third_person_row, "32E4:9310")

        window._on_save_clicked()

        written = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(written["instruments"]["bio"], {"kind": "net2860_winusb", "label": "BIO", "profile": "keeler_vantage_plus_legacy"})

    def test_existing_winusb_config_preselects_it_on_load(self):
        data = json.loads(json.dumps(VALID_CONFIG))
        data["instruments"]["bio"] = {"kind": "net2860_winusb", "label": "BIO", "profile": "keeler_vantage_plus_legacy"}
        self.config_path.write_text(json.dumps(data), encoding="utf-8")

        window = self._make_window(
            ids_devices=[SLIT_LAMP_DEVICE],
            uvc_devices=[THIRD_PERSON_DEVICE],
            winusb_devices=[ONE_WINUSB_DEVICE],
        )

        self.assertEqual(window._instrument_rows["bio"].selected_key(), "net2860_winusb")
        self.assertEqual(window._instrument_rows["bio"].label_text(), "BIO")

    def test_calibration_round_trips_through_device_row_into_saved_config(self):
        window = self._make_window(
            ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE]
        )
        row = window._instrument_rows["slit_lamp"]
        _select(row, "111")
        row.label_edit.setText("Slit Lamp")

        with patch("settings.PreviewDialog") as mock_dialog_cls:
            mock_dialog = mock_dialog_cls.return_value
            mock_dialog.calibration_supported = True
            mock_dialog.final_exposure_time_us = 12345.0
            mock_dialog.final_gain = 3.5
            row._on_preview_clicked()

        self.assertEqual(row.calibration(), (12345.0, 3.5))

        _select(window._instrument_rows["bio"], "222")
        window._instrument_rows["bio"].label_edit.setText("BIO")
        _select(window._third_person_row, "32E4:9310")
        window._on_save_clicked()

        written = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(written["instruments"]["slit_lamp"]["exposure_time_us"], 12345.0)
        self.assertEqual(written["instruments"]["slit_lamp"]["gain"], 3.5)

    def test_calibration_not_persisted_when_preview_camera_lacks_it(self):
        window = self._make_window(ids_devices=[SLIT_LAMP_DEVICE])
        row = window._instrument_rows["slit_lamp"]
        _select(row, "111")

        with patch("settings.PreviewDialog") as mock_dialog_cls:
            mock_dialog_cls.return_value.calibration_supported = False
            row._on_preview_clicked()

        self.assertEqual(row.calibration(), (None, None))

    def test_changing_a_rows_camera_discards_calibration_made_on_the_old_one(self):
        # The field failure: the slit lamp row once pointed at the BIO,
        # Auto-Calibrate reached the BIO's 25.4x gain, the dropdown was
        # corrected, and 25.4x was saved against the slit lamp (4.0x max).
        window = self._make_window(ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE])
        row = window._instrument_rows["slit_lamp"]
        _select(row, "222")

        with patch("settings.PreviewDialog") as mock_dialog_cls:
            mock_dialog = mock_dialog_cls.return_value
            mock_dialog.calibration_supported = True
            mock_dialog.final_exposure_time_us = 30000.0
            mock_dialog.final_gain = 25.41
            row._on_preview_clicked()
        self.assertEqual(row.calibration(), (30000.0, 25.41))

        _select(row, "111")

        self.assertEqual(row.calibration(), (None, None))

    def test_calibration_loaded_from_config_survives_loading_and_rescan(self):
        config = json.loads(json.dumps(VALID_CONFIG))
        config["instruments"]["slit_lamp"].update(exposure_time_us=6700.0, gain=1.0)
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        window = self._make_window(ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE])
        row = window._instrument_rows["slit_lamp"]

        window.rescan()

        self.assertEqual(row.selected_key(), "111")
        self.assertEqual(row.calibration(), (6700.0, 1.0))

    def test_webcam_enumeration_failure_leaves_settings_usable(self):
        # __init__ calls rescan(), so a raising enumerator used to stop the
        # window opening at all -- leaving a technician no way in to fix the
        # very configuration that would work around the bad device.
        def boom():
            raise RuntimeError("DirectShow device is wedged")

        window = SettingsWindow(
            config_path=self.config_path,
            list_ids_devices_fn=lambda: [SLIT_LAMP_DEVICE],
            list_uvc_devices_fn=boom,
            list_net2860_winusb_fn=lambda: [],
            instrument_preview_camera_factory=lambda candidate: SyntheticCamera(160, 120, fps=30),
            uvc_preview_camera_factory=lambda candidate: SyntheticCamera(160, 120, fps=30),
        )

        self.assertIn("DirectShow device is wedged", window._third_person_row.status_label.text())
        self.assertIsNone(window._third_person_row.selected_key())
        # Contained: the instrument rows still enumerate and select normally.
        _select(window._instrument_rows["slit_lamp"], "111")
        self.assertEqual(window._instrument_rows["slit_lamp"].selected_key(), "111")
        # Save stays disabled -- there is no third-person camera to record with.
        self.assertFalse(window.save_button.isEnabled())

    def test_rescan_preserves_a_still_present_selection(self):
        window = self._make_window(
            ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE]
        )
        _select(window._instrument_rows["slit_lamp"], "111")

        window.rescan()

        self.assertEqual(window._instrument_rows["slit_lamp"].selected_key(), "111")

    def test_rescan_drops_a_selection_that_disappeared(self):
        window = self._make_window(ids_devices=[SLIT_LAMP_DEVICE])
        _select(window._instrument_rows["slit_lamp"], "111")

        window._list_ids_devices_fn = lambda: []
        window.rescan()

        self.assertIsNone(window._instrument_rows["slit_lamp"].selected_key())

    def test_ids_enumeration_failure_is_surfaced_not_crashed(self):
        def _raise():
            raise RuntimeError("ids_peak not installed")

        window = self._make_window()
        window._list_ids_devices_fn = _raise
        window.rescan()

        self.assertIn("ids_peak not installed", window._instrument_rows["slit_lamp"].status_label.text())

    def test_preview_button_uses_the_injected_factory(self):
        calls = []

        def factory(candidate):
            calls.append(candidate.preview_target)
            return SyntheticCamera(160, 120, fps=30)

        window = self._make_window(ids_devices=[SLIT_LAMP_DEVICE], instrument_preview_factory=factory)
        _select(window._instrument_rows["slit_lamp"], "111")

        with patch("settings.PreviewDialog.exec", return_value=None):
            window._instrument_rows["slit_lamp"]._on_preview_clicked()

        self.assertEqual(calls, ["111"])

    def test_preview_button_routes_the_legacy_bio_through_the_instrument_factory(self):
        calls = []

        def factory(candidate):
            calls.append(candidate.kind)
            return SyntheticCamera(160, 120, fps=30)

        window = self._make_window(instrument_preview_factory=factory,
                                   winusb_devices=[ONE_WINUSB_DEVICE])
        _select(window._instrument_rows["bio"], "net2860_winusb")

        with patch("settings.PreviewDialog.exec", return_value=None):
            window._instrument_rows["bio"]._on_preview_clicked()

        self.assertEqual(calls, ["net2860_winusb"])


class PanoptoSectionTest(unittest.TestCase):
    """The technician-facing half of the Panopto integration.

    The optional client secret is the thing worth testing hard: if given,
    it must reach disk encrypted, must never be readable back out of the
    window, and must not survive turning the integration off.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.config_path = Path(self._tmpdir.name) / "config.json"
        self.secret_path = panopto_secret_path(self.config_path)
        self.section = PanoptoSection(self.config_path)

    def _fill(self, secret: str = "") -> None:
        self.section.setChecked(True)
        self.section.host_edit.setText("neco.hosted.panopto.com")
        self.section.client_id_edit.setText("client-1")
        self.section.folder_edit.setText("assign-1")
        self.section.client_secret_edit.setText(secret)

    def test_off_by_default_and_saveable(self):
        self.assertFalse(self.section.isChecked())
        self.assertEqual(self.section.problem(), "")

    def test_an_incomplete_section_names_what_is_missing(self):
        self.section.setChecked(True)
        problem = self.section.problem()
        self.assertIn("site host", problem)
        self.assertIn("client ID", problem)
        self.assertIn("assignment folder", problem)

    def test_the_secret_is_optional(self):
        # The loader doesn't insist on one -- the student's sign-in is the
        # credential. A blank field must not block saving.
        self._fill(secret="")
        self.assertEqual(self.section.problem(), "")
        data: dict = {}
        self.assertEqual(self.section.apply_to(data), "")
        self.assertFalse(self.secret_path.exists())
        self.assertEqual(
            data["panopto"],
            {"host": "neco.hosted.panopto.com", "client_id": "client-1", "assignment_folder_id": "assign-1"},
        )

    def test_a_given_secret_is_encrypted_and_kept_out_of_the_json(self):
        self._fill(secret="do-not-leak-me")
        data: dict = {}
        note = self.section.apply_to(data)

        self.assertNotIn("client_secret", data["panopto"])
        self.assertNotIn("do-not-leak-me", json.dumps(data))
        self.assertIn("encrypted", note)
        self.assertEqual(read_secret(self.secret_path), "do-not-leak-me")

    def test_the_field_is_cleared_after_saving_and_never_refilled(self):
        # A settings window left open on a kiosk must not be able to show
        # anyone the credential it just stored.
        self._fill(secret="s3cret")
        self.section.apply_to({})
        self.assertEqual(self.section.client_secret_edit.text(), "")

        reopened = PanoptoSection(self.config_path)
        reopened.load_from(
            PanoptoConfig(
                host="neco.hosted.panopto.com",
                client_id="client-1",
                assignment_folder_id="assign-1",
                client_secret="s3cret",
            )
        )
        self.assertEqual(reopened.client_secret_edit.text(), "")
        self.assertIn("stored", reopened.client_secret_edit.placeholderText())

    def test_the_secret_field_is_masked(self):
        self.assertEqual(
            self.section.client_secret_edit.echoMode(), QLineEdit.EchoMode.Password
        )

    def test_a_blank_secret_keeps_the_stored_one(self):
        self._fill(secret="s3cret")
        self.section.apply_to({})

        self.section.client_secret_edit.setText("")
        note = self.section.apply_to({})
        self.assertEqual(note, "")
        self.assertEqual(read_secret(self.secret_path), "s3cret")

    def test_turning_the_integration_off_removes_the_credential(self):
        # A machine that no longer uploads has no business still holding
        # anything about the integration.
        self._fill(secret="s3cret")
        self.section.apply_to({})
        self.assertTrue(self.secret_path.exists())

        self.section.setChecked(False)
        data = {"panopto": {"host": "x"}}
        note = self.section.apply_to(data)

        self.assertNotIn("panopto", data)
        self.assertFalse(self.secret_path.exists())
        self.assertIn("removed", note)

    def test_a_hand_set_redirect_port_survives_a_save(self):
        self._fill()
        data = {"panopto": {"host": "old", "redirect_port": 50000}}
        self.section.apply_to(data)
        self.assertEqual(data["panopto"]["redirect_port"], 50000)

    def test_loading_an_existing_config_fills_everything_but_the_secret(self):
        self.section.load_from(
            PanoptoConfig(
                host="neco.hosted.panopto.com",
                client_id="client-1",
                assignment_folder_id="assign-1",
                client_secret="s3cret",
            )
        )
        self.assertTrue(self.section.isChecked())
        self.assertEqual(self.section.host_edit.text(), "neco.hosted.panopto.com")
        self.assertEqual(self.section.folder_edit.text(), "assign-1")
        self.assertEqual(self.section.client_secret_edit.text(), "")


class StreamingSectionTest(unittest.TestCase):
    def setUp(self):
        self.section = StreamingSection()

    def test_off_by_default_and_writes_nothing_to_an_untouched_config(self):
        self.assertFalse(self.section.isChecked())
        data: dict = {}
        self.section.apply_to(data)
        self.assertNotIn("streaming", data)

    def test_turning_it_on_writes_enabled_and_the_layout(self):
        self.section.setChecked(True)
        self.section.layout_combo.setCurrentIndex(self.section.layout_combo.findData("instrument"))
        data: dict = {}
        self.section.apply_to(data)
        self.assertEqual(data["streaming"], {"enabled": True, "layout": "instrument"})

    def test_turning_it_off_again_keeps_the_section_and_hand_set_values(self):
        # A room that set a custom size keeps it across an off/on cycle.
        data = {"streaming": {"enabled": True, "layout": "side_by_side", "width": 1280, "height": 720}}
        self.section.setChecked(False)
        self.section.apply_to(data)
        self.assertEqual(
            data["streaming"], {"enabled": False, "layout": "side_by_side", "width": 1280, "height": 720}
        )

    def test_loads_from_config(self):
        self.section.load_from(StreamingConfig(enabled=True, layout="picture_in_picture"))
        self.assertTrue(self.section.isChecked())
        self.assertEqual(self.section.layout_combo.currentData(), "picture_in_picture")


class AudioSectionTest(unittest.TestCase):
    def _section(self, devices=(), capture_factory=None) -> AudioSection:
        return AudioSection(list_devices_fn=lambda: list(devices), capture_factory=capture_factory)

    def test_off_by_default_and_writes_nothing(self):
        section = self._section([(1, "Mic A")])
        self.assertFalse(section.isChecked())
        data: dict = {}
        section.apply_to(data)
        self.assertNotIn("audio", data)

    def test_lists_devices_by_name_after_the_default(self):
        section = self._section([(1, "Mic A"), (5, "Mic A"), (2, "Mic B")])
        names = [section.device_combo.itemText(i) for i in range(section.device_combo.count())]
        self.assertEqual(names, ["System default microphone", "Mic A", "Mic B"])

    def test_the_default_device_is_stored_as_no_device(self):
        section = self._section([(1, "Mic A")])
        section.setChecked(True)
        data: dict = {}
        section.apply_to(data)
        self.assertEqual(data["audio"], {})

    def test_a_chosen_device_is_stored_by_name_and_carries_hand_set_values(self):
        section = self._section([(1, "Mic A")])
        section.setChecked(True)
        section.device_combo.setCurrentIndex(section.device_combo.findData("Mic A"))
        data = {"audio": {"device": "old", "channels": 2}}
        section.apply_to(data)
        self.assertEqual(data["audio"], {"device": "Mic A", "channels": 2})

    def test_a_configured_device_that_is_unplugged_is_still_offered(self):
        section = self._section([(1, "Mic A")])
        section.load_from(AudioConfig(device="Mic Gone"))
        self.assertTrue(section.isChecked())
        self.assertEqual(section.device_combo.currentData(), "Mic Gone")
        self.assertIn("not connected", section.device_combo.currentText())

    def test_turning_it_off_removes_the_section(self):
        section = self._section([(1, "Mic A")])
        data = {"audio": {"device": "Mic A"}}
        section.setChecked(False)
        section.apply_to(data)
        self.assertNotIn("audio", data)

    def test_test_microphone_reports_a_level(self):
        class LoudCapture:
            def __init__(self):
                self.started = False
            def start(self): self.started = True
            def stop(self): self.started = False
            def level(self): return 0.4
            def get_latest(self): return object()

        made = []
        section = self._section([(1, "Mic A")], capture_factory=lambda device: made.append(LoudCapture()) or made[-1])
        with patch("settings.QApplication.processEvents"):
            section._on_test_clicked()
        self.assertIn("Working", section.test_status.text())
        self.assertFalse(made[0].started, "the test capture must be stopped afterwards")
        self.assertTrue(section.test_button.isEnabled())

    def test_test_microphone_reports_silence_and_failure_distinctly(self):
        class SilentCapture(object):
            def start(self): ...
            def stop(self): ...
            def level(self): return 0.0
            def get_latest(self): return object()

        section = self._section([(1, "Mic A")], capture_factory=lambda device: SilentCapture())
        with patch("settings.QApplication.processEvents"):
            section._on_test_clicked()
        self.assertIn("silent", section.test_status.text())

        def broken(device):
            raise RuntimeError("PortAudio said no")

        section = self._section([(1, "Mic A")], capture_factory=broken)
        with patch("settings.QApplication.processEvents"):
            section._on_test_clicked()
        self.assertIn("PortAudio said no", section.test_status.text())


class PanoptoSaveGateTest(SettingsWindowTest):
    """A half-entered section must not be saveable: config.py refuses to
    load a panopto section it can't complete, which would leave the kiosk
    unable to start at all."""

    def _ready_window(self) -> SettingsWindow:
        window = self._make_window(
            ids_devices=[SLIT_LAMP_DEVICE, BIO_DEVICE], uvc_devices=[THIRD_PERSON_DEVICE]
        )
        for key, device in (("slit_lamp", SLIT_LAMP_DEVICE), ("bio", BIO_DEVICE)):
            window._instrument_rows[key].set_pending_selection(device.serial)
        window._third_person_row.set_pending_selection(THIRD_PERSON_DEVICE.vid_pid)
        window.rescan()
        return window

    def test_save_is_allowed_with_the_integration_off(self):
        window = self._ready_window()
        self.assertTrue(window.save_button.isEnabled())

    def test_a_half_entered_section_blocks_save(self):
        window = self._ready_window()
        window.panopto_section.setChecked(True)
        window.panopto_section.host_edit.setText("neco.hosted.panopto.com")
        self.assertFalse(window.save_button.isEnabled())

    def test_a_complete_section_saves_and_reloads(self):
        window = self._ready_window()
        window.panopto_section.setChecked(True)
        window.panopto_section.host_edit.setText("https://neco.hosted.panopto.com/Panopto/")
        window.panopto_section.client_id_edit.setText("client-1")
        window.panopto_section.folder_edit.setText("assign-1")
        self.assertTrue(window.save_button.isEnabled())

        window._on_save_clicked()

        cfg = load_config(self.config_path)
        self.assertEqual(cfg.panopto.host, "neco.hosted.panopto.com")  # URL reduced to a host
        self.assertEqual(cfg.panopto.assignment_folder_id, "assign-1")
        self.assertIsNone(cfg.panopto.client_secret)


class PreviewDialogTest(unittest.TestCase):
    def test_starts_and_stops_a_real_synthetic_camera(self):
        from settings import PreviewDialog

        camera = SyntheticCamera(160, 120, fps=30)
        dialog = PreviewDialog(camera, "Test")
        try:
            self.assertIsNotNone(camera._thread)  # started
        finally:
            dialog.close()
        self.assertIsNone(camera._thread)  # stopped by closeEvent

    def test_reject_stops_the_camera(self):
        # Esc / reject() calls QDialog.done() without a QCloseEvent, so a
        # closeEvent-only teardown would leak the open camera (and, for a
        # real IdsCamera, hold the device -> GC_ERR_RESOURCE_IN_USE on the
        # next Preview). The finished signal must cover this path.
        from settings import PreviewDialog

        camera = SyntheticCamera(160, 120, fps=30)
        dialog = PreviewDialog(camera, "Test")
        self.assertIsNotNone(camera._thread)  # started
        dialog.reject()
        self.assertIsNone(camera._thread)  # stopped via the finished signal

    def test_camera_stops_when_building_calibration_controls_raises(self):
        # start() succeeds, then a control builder raises before the dialog
        # is ever shown/closed -- the camera must still be released.
        from settings import PreviewDialog

        camera = SyntheticCamera(160, 120, fps=30)
        camera.supports_manual_calibration = lambda: True
        camera.exposure_time_range_us = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

        with self.assertRaises(RuntimeError):
            PreviewDialog(camera, "Test")
        self.assertIsNone(camera._thread)  # released despite the raise

    def test_the_whole_frame_is_shown_scaled_to_the_window(self):
        """A QLabel never scales its pixmap, so a native 1600x1200 frame
        once showed as a cut-out of itself -- useless for calibrating."""
        import numpy as np

        from camera import Frame
        from settings import PreviewDialog

        image = np.full((1200, 1600, 3), 255, dtype=np.uint8)
        image[:200, :200] = (0, 0, 255)  # red top-left corner, BGR
        camera = SyntheticCamera(160, 120, fps=30)
        dialog = PreviewDialog(camera, "Test")
        try:
            camera.get_latest = lambda: Frame(image=image, timestamp=0.0, index=0)
            dialog.video_label.resize(480, 360)
            dialog._update()

            pixmap = dialog.video_label.pixmap()
            self.assertEqual((pixmap.width(), pixmap.height()), (480, 360))
            corner = pixmap.toImage().pixelColor(5, 5)
            self.assertEqual((corner.red(), corner.green(), corner.blue()), (255, 0, 0))
            self.assertEqual(dialog.status_label.text(), "1600x1200")  # still the native size
        finally:
            dialog.close()

    def test_camera_without_supports_manual_calibration_shows_no_calibration_controls(self):
        from settings import PreviewDialog

        camera = SyntheticCamera(160, 120, fps=30)  # no supports_manual_calibration() at all
        dialog = PreviewDialog(camera, "Test")
        try:
            self.assertFalse(dialog.calibration_supported)
            self.assertFalse(hasattr(dialog, "exposure_slider"))
        finally:
            dialog.close()


class _FakeCalibratableCamera(SyntheticCamera):
    """Stands in for an IdsCamera whose ExposureTime/Gain a technician can
    write, and which also lacks BalanceWhiteAuto (the slit lamp) --
    exercises PreviewDialog's exposure/gain branch
    headlessly, without the IDS peak SDK. Extended in place (rather than a
    second fake) specifically so the "both blocks visible at once" case is
    directly testable against one camera.
    """

    def __init__(self):
        super().__init__(160, 120, fps=30)
        self._exposure_time_us = 1000.0
        self._gain = 2.0

    def supports_manual_calibration(self) -> bool:
        return True

    def get_exposure_time_us(self) -> float:
        return self._exposure_time_us

    def set_exposure_time_us(self, value: float) -> None:
        self._exposure_time_us = value

    def exposure_time_range_us(self) -> tuple[float, float]:
        return (100.0, 10_000.0)

    def get_gain(self) -> float:
        return self._gain

    def set_gain(self, value: float) -> None:
        self._gain = value

    def gain_range(self) -> tuple[float, float]:
        return (1.0, 8.0)

    def auto_calibrate(self, **_kwargs) -> bool:
        self._exposure_time_us = 4000.0
        self._gain = 3.0
        return True

class CalibrationCostReportingTest(unittest.TestCase):
    """A calibration must report what it *cost*, not just that it worked.

    "87208.816" tells a technician nothing; "11fps, gain 1.0 of 4.0" tells
    them it is wrong. That missing visibility -- not a missing setting --
    is why the slit lamp sat at an 11fps exposure. See CLAUDE.md's
    "Camera configuration: who decides what".
    """

    def test_cost_line_reports_exposure_gain_and_achievable_fps(self):
        from settings import PreviewDialog

        camera = _FakeCalibratableCamera()
        dialog = PreviewDialog(camera, "Slit Lamp", target_fps=30)
        try:
            camera.set_exposure_time_us(20_000.0)  # 20ms -> 50fps
            camera.set_gain(2.0)
            text = dialog._calibration_cost()
        finally:
            dialog._shutdown()

        self.assertIn("20.0ms", text)
        self.assertIn("gain 2.0x", text)
        self.assertIn("50fps", text)
        self.assertNotIn("BELOW", text)  # 50fps clears a 30fps target

    def test_cost_line_calls_out_an_exposure_below_the_recording_target(self):
        from settings import PreviewDialog

        camera = _FakeCalibratableCamera()
        dialog = PreviewDialog(camera, "Slit Lamp", target_fps=30)
        try:
            camera.set_exposure_time_us(87_208.816)  # the real slit lamp value
            text = dialog._calibration_cost()
        finally:
            dialog._shutdown()

        self.assertIn("87.2ms", text)
        self.assertIn("11fps", text)
        self.assertIn("BELOW", text)
        self.assertIn("30fps", text)

    def test_no_target_fps_means_no_verdict(self):
        """settings.py always passes one, but the dialog must not invent a
        judgement when it has nothing to judge against."""
        from settings import PreviewDialog

        camera = _FakeCalibratableCamera()
        dialog = PreviewDialog(camera, "Slit Lamp")
        try:
            camera.set_exposure_time_us(87_208.816)
            text = dialog._calibration_cost()
        finally:
            dialog._shutdown()

        self.assertIn("87.2ms", text)
        self.assertNotIn("BELOW", text)


class PreviewDialogCalibrationTest(unittest.TestCase):
    def test_the_exposure_slider_cannot_exceed_the_frame_rate_budget(self):
        """A slider that cannot reach a bad value beats a warning about one
        that was saved. A 124ms calibration reached the field and capped that
        camera at 8fps -- see DECISIONS.md 2026-09-13."""
        from settings import PreviewDialog

        class _WideRangeCamera(_FakeCalibratableCamera):
            """Like the real Keeler, whose sensor allows 2 seconds."""

            def exposure_time_range_us(self):
                return (24.0, 2_000_000.0)

        camera = _WideRangeCamera()
        dialog = PreviewDialog(camera, "BIO", target_fps=30)
        try:
            self.assertGreater(camera.exposure_time_range_us()[1], 30_000)
            self.assertLessEqual(dialog.exposure_slider.maximum(), 30_000)
        finally:
            dialog._shutdown()

    def test_calibratable_camera_shows_sliders_seeded_from_its_current_values(self):
        from settings import PreviewDialog

        camera = _FakeCalibratableCamera()
        dialog = PreviewDialog(camera, "Slit Lamp")
        try:
            self.assertTrue(dialog.calibration_supported)
            self.assertEqual(dialog.exposure_slider.value(), 1000)
            self.assertEqual(dialog.final_exposure_time_us, 1000.0)
            self.assertEqual(dialog.final_gain, 2.0)
        finally:
            dialog.close()

    def test_initial_calibration_is_applied_before_reading_slider_seed(self):
        from settings import PreviewDialog

        camera = _FakeCalibratableCamera()
        dialog = PreviewDialog(camera, "Slit Lamp", initial_exposure_time_us=5000.0, initial_gain=4.0)
        try:
            self.assertEqual(camera.get_exposure_time_us(), 5000.0)
            self.assertEqual(dialog.final_gain, 4.0)
        finally:
            dialog.close()

    def test_auto_calibrate_button_updates_sliders_and_final_values(self):
        from settings import PreviewDialog

        camera = _FakeCalibratableCamera()
        dialog = PreviewDialog(camera, "Slit Lamp")
        try:
            dialog._on_calibrate_clicked()
            self.assertEqual(dialog.final_exposure_time_us, 4000.0)
            self.assertEqual(dialog.final_gain, 3.0)
            self.assertEqual(dialog.exposure_slider.value(), 4000)
            self.assertTrue(dialog.calibration_status_label.text().startswith("Calibrated."))
        finally:
            dialog.close()

if __name__ == "__main__":
    unittest.main()
