"""Tests for config.load_config() -- the config.json schema and its error
messages, in isolation from app.py (which only wires the result into
camera construction; see DECISIONS.md's 2026-08-18 "config.json + loader"
entry for why this file exists).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import (
    ConfigError,
    achievable_fps,
    exposure_fps_warnings,
    load_config,
    resolve_default_config_path,
)

VALID = {
    "instruments": {
        "slit_lamp": {"kind": "ids", "serial": "111", "label": "Slit Lamp"},
        "bio": {"kind": "ids", "serial": "222", "label": "BIO"},
    },
    "third_person": {"kind": "uvc", "vid_pid": "32E4:9310", "friendly_name": "HD USB Camera"},
}


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "config.json"

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_valid_config_loads(self):
        self._write(VALID)
        cfg = load_config(self.path)

        self.assertEqual(set(cfg.instruments), {"slit_lamp", "bio"})
        self.assertEqual(cfg.instruments["slit_lamp"].serial, "111")
        self.assertEqual(cfg.instruments["slit_lamp"].label, "Slit Lamp")
        self.assertEqual(cfg.third_person.vid_pid, "32E4:9310")
        self.assertEqual(cfg.third_person.friendly_name, "HD USB Camera")

    def test_more_than_two_instruments_is_not_a_fixed_pair(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["indirect_scope"] = {"kind": "ids", "serial": "333", "label": "Indirect Scope"}
        self._write(data)

        cfg = load_config(self.path)

        self.assertEqual(set(cfg.instruments), {"slit_lamp", "bio", "indirect_scope"})

    def test_missing_file_raises(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.path)
        self.assertIn("config.example.json", str(ctx.exception))

    def test_malformed_json_raises(self):
        self.path.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_empty_instruments_raises(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"] = {}
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_missing_instrument_key_raises(self):
        data = json.loads(json.dumps(VALID))
        del data["instruments"]["slit_lamp"]["serial"]
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_wrong_typed_instrument_key_raises(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["serial"] = 111
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_unsupported_instrument_kind_raises(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["kind"] = "uvc"
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_the_removed_vendor_driver_kind_names_its_replacement(self):
        # "net2860" was valid until the vendor-driver route was removed, so
        # anyone meeting this error has a config that used to work. Telling
        # them the replacement is the whole point of not letting it fall
        # into the generic unknown-kind message.
        data = json.loads(json.dumps(VALID))
        data["instruments"]["bio"] = {"kind": "net2860", "label": "BIO"}
        self._write(data)

        with self.assertRaises(ConfigError) as ctx:
            load_config(self.path)
        self.assertIn("net2860_winusb", str(ctx.exception))

    def test_net2860_winusb_instrument_loads_with_no_serial(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["bio"] = {"kind": "net2860_winusb", "label": "BIO"}
        self._write(data)

        cfg = load_config(self.path)

        self.assertEqual(cfg.instruments["bio"].kind, "net2860_winusb")
        self.assertIsNone(cfg.instruments["bio"].serial)
        self.assertEqual(cfg.instruments["bio"].label, "BIO")

    def test_profile_is_parsed_when_present(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["profile"] = "haag_streit_bi900_slit_lamp"
        self._write(data)

        self.assertEqual(
            load_config(self.path).instruments["slit_lamp"].profile, "haag_streit_bi900_slit_lamp"
        )

    def test_a_config_without_them_is_a_custom_entry(self):
        """Every config.json written before profiles existed: a typed label
        and no profile. That shape has to keep working untouched."""
        self._write(json.loads(json.dumps(VALID)))

        inst = load_config(self.path).instruments["slit_lamp"]

        self.assertIsNone(inst.profile)
        self.assertTrue(inst.label)

    def test_an_unknown_profile_id_still_loads(self):
        """Written by a newer build. Resolving it is app.py's problem, and
        its answer is to fall back -- not to refuse to start a kiosk."""
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["profile"] = "written_by_a_newer_build"
        self._write(data)

        self.assertEqual(
            load_config(self.path).instruments["slit_lamp"].profile, "written_by_a_newer_build"
        )

    def test_a_non_string_profile_raises(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["profile"] = 7
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_the_legacy_bio_can_carry_a_profile(self):
        """It has a profile like any other supported camera -- what it lacks
        is a serial and anything to calibrate."""
        data = json.loads(json.dumps(VALID))
        data["instruments"]["bio"] = {
            "kind": "net2860_winusb",
            "label": "BIO",
            "profile": "keeler_vantage_plus_legacy",
        }
        self._write(data)

        inst = load_config(self.path).instruments["bio"]

        self.assertEqual(inst.profile, "keeler_vantage_plus_legacy")
        self.assertIsNone(inst.serial)


    def test_net2860_winusb_instrument_rejects_the_same_fields_as_net2860(self):
        # Same camera, different driver route -- so the same empty config
        # shape. The AE/AWB loop runs on the camera board either way, and
        # the orientation is a fixed property of the instrument's optics.
        for field, value in [
            ("serial", "222"),
            ("exposure_time_us", 1000.0),
            ("gain", 2.0),
            ("orientation", "flip_vertical"),
            ("pixel_clock_hz", 60_000_000),
        ]:
            with self.subTest(field=field):
                data = json.loads(json.dumps(VALID))
                data["instruments"]["bio"] = {"kind": "net2860_winusb", "label": "BIO", field: value}
                self._write(data)

                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_an_unknown_kind_names_the_valid_ones(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["bio"] = {"kind": "net2860_usb", "label": "BIO"}
        self._write(data)

        with self.assertRaises(ConfigError) as ctx:
            load_config(self.path)
        message = str(ctx.exception)
        for kind in ("ids", "net2860_winusb"):
            self.assertIn(kind, message)

    def test_net2860_winusb_instrument_still_requires_label(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["bio"] = {"kind": "net2860_winusb"}
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_missing_third_person_raises(self):
        data = json.loads(json.dumps(VALID))
        del data["third_person"]
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_wrong_typed_third_person_vid_pid_raises(self):
        data = json.loads(json.dumps(VALID))
        data["third_person"]["vid_pid"] = 12345
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_empty_third_person_vid_pid_raises(self):
        data = json.loads(json.dumps(VALID))
        data["third_person"]["vid_pid"] = ""
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_badly_shaped_third_person_vid_pid_raises(self):
        data = json.loads(json.dumps(VALID))
        data["third_person"]["vid_pid"] = "not-a-vid-pid"
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_lowercase_third_person_vid_pid_is_normalized_uppercase(self):
        data = json.loads(json.dumps(VALID))
        data["third_person"]["vid_pid"] = "32e4:9310"
        self._write(data)

        cfg = load_config(self.path)

        self.assertEqual(cfg.third_person.vid_pid, "32E4:9310")

    def test_missing_third_person_friendly_name_raises(self):
        data = json.loads(json.dumps(VALID))
        del data["third_person"]["friendly_name"]
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_wrong_typed_third_person_friendly_name_raises(self):
        data = json.loads(json.dumps(VALID))
        data["third_person"]["friendly_name"] = 123
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_unsupported_third_person_kind_raises(self):
        data = json.loads(json.dumps(VALID))
        data["third_person"]["kind"] = "ids"
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_unknown_extra_keys_are_ignored(self):
        data = json.loads(json.dumps(VALID))
        data["notes"] = "hand-added field, not part of the schema"
        data["instruments"]["slit_lamp"]["vid_pid"] = "32E4:9310"
        self._write(data)

        cfg = load_config(self.path)

        self.assertEqual(cfg.instruments["slit_lamp"].serial, "111")

    def test_missing_exposure_gain_default_to_none(self):
        self._write(VALID)
        cfg = load_config(self.path)

        self.assertIsNone(cfg.instruments["slit_lamp"].exposure_time_us)
        self.assertIsNone(cfg.instruments["slit_lamp"].gain)

    def test_exposure_gain_are_parsed_when_present(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["exposure_time_us"] = 15000
        data["instruments"]["slit_lamp"]["gain"] = 2.5
        self._write(data)

        cfg = load_config(self.path)

        self.assertEqual(cfg.instruments["slit_lamp"].exposure_time_us, 15000.0)
        self.assertEqual(cfg.instruments["slit_lamp"].gain, 2.5)

    def test_zero_exposure_time_us_raises(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["exposure_time_us"] = 0
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_negative_gain_raises(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["gain"] = -1.0
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_wrong_typed_exposure_time_us_raises(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["exposure_time_us"] = "fast"
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_missing_orientation_defaults_to_none(self):
        self._write(VALID)
        cfg = load_config(self.path)

        self.assertIsNone(cfg.instruments["bio"].orientation)

    def test_orientation_is_parsed_when_present(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["bio"]["orientation"] = "flip_vertical"
        self._write(data)

        cfg = load_config(self.path)

        self.assertEqual(cfg.instruments["bio"].orientation, "flip_vertical")

    def test_orientation_none_string_is_allowed_and_kept(self):
        # "none" is meaningful: it overrides a device-model preset that
        # would otherwise transform (device_presets.py), so it must survive
        # as "none", not collapse to Python None.
        data = json.loads(json.dumps(VALID))
        data["instruments"]["bio"]["orientation"] = "none"
        self._write(data)

        cfg = load_config(self.path)

        self.assertEqual(cfg.instruments["bio"].orientation, "none")

    def test_unsupported_orientation_raises(self):
        for bad in ("upside_down", "rotate_90", 180, "", True):
            with self.subTest(bad=bad):
                data = json.loads(json.dumps(VALID))
                data["instruments"]["bio"]["orientation"] = bad
                self._write(data)

                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_net2860_winusb_instrument_rejects_orientation(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["bio"] = {"kind": "net2860_winusb", "label": "BIO", "orientation": "flip_vertical"}
        self._write(data)

        with self.assertRaises(ConfigError):
            load_config(self.path)

class DefaultPathsTest(unittest.TestCase):
    """resolve_default_config_path() splits on sys.frozen -- a frozen
    install (see DECISIONS.md's "Frozen-exe installer built" entry) has no
    repo checkout to be relative to.
    """

    def test_dev_mode_config_path_is_relative_to_cwd(self):
        with patch("sys.frozen", False, create=True):
            self.assertEqual(resolve_default_config_path(), Path("config.json"))

    def test_frozen_config_path_is_under_programdata(self):
        with patch("sys.frozen", True, create=True), patch.dict(
            "os.environ", {"ProgramData": "C:\\ProgramData"}
        ):
            self.assertEqual(
                resolve_default_config_path(), Path("C:\\ProgramData") / "Reflex" / "config.json"
            )

class ExposureFpsWarningTest(unittest.TestCase):
    """An exposure that cannot meet the recording frame rate is a warning,
    not a ConfigError: a slow camera still records usable video, and
    refusing to start would be worse than the problem. But it must be
    visible -- this went unnoticed for weeks. See CLAUDE.md's "Camera
    configuration: who decides what".
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "config.json"

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_achievable_fps_is_the_reciprocal_of_exposure(self):
        self.assertAlmostEqual(achievable_fps(87_208.816), 11.47, places=1)
        self.assertAlmostEqual(achievable_fps(33_333.0), 30.0, places=1)

    def test_the_real_slit_lamp_value_warns_against_a_30fps_target(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["exposure_time_us"] = 87208.816
        data["instruments"]["slit_lamp"]["gain"] = 1.0
        self._write(data)

        warnings = exposure_fps_warnings(load_config(self.path))

        self.assertEqual(len(warnings), 1)
        self.assertIn("87.2ms", warnings[0])
        self.assertIn("11fps", warnings[0])
        self.assertIn("30fps", warnings[0])

    def test_an_exposure_that_meets_the_target_does_not_warn(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["exposure_time_us"] = 20_000.0
        self._write(data)
        self.assertEqual(exposure_fps_warnings(load_config(self.path)), [])

    def test_no_configured_exposure_means_nothing_to_check(self):
        self._write(VALID)
        self.assertEqual(exposure_fps_warnings(load_config(self.path)), [])

    def test_the_warning_follows_the_configured_recording_fps(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["exposure_time_us"] = 87208.816
        data["recording"] = {"fps": 10}
        self._write(data)
        self.assertEqual(exposure_fps_warnings(load_config(self.path)), [])

    def test_a_too_long_exposure_is_not_a_hard_error(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["exposure_time_us"] = 87208.816
        self._write(data)
        load_config(self.path)


class PixelClockConfigTest(unittest.TestCase):
    """Unlike most device presets this one is deliberately overridable: the
    preset suits the hardware, but the *safe* clock depends on the host USB
    controller, which is per-install."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "config.json"

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_absent_means_use_the_model_preset(self):
        self._write(VALID)
        self.assertIsNone(load_config(self.path).instruments["slit_lamp"].pixel_clock_hz)

    def test_an_override_is_parsed(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["slit_lamp"]["pixel_clock_hz"] = 60_000_000
        self._write(data)
        self.assertEqual(load_config(self.path).instruments["slit_lamp"].pixel_clock_hz, 60_000_000)

    def test_a_non_positive_or_non_integer_override_is_rejected(self):
        for bad in (0, -1, 60.5, "60000000", True):
            with self.subTest(bad=bad):
                data = json.loads(json.dumps(VALID))
                data["instruments"]["slit_lamp"]["pixel_clock_hz"] = bad
                self._write(data)
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_net2860_winusb_rejects_it(self):
        data = json.loads(json.dumps(VALID))
        data["instruments"]["bio"] = {"kind": "net2860_winusb", "label": "BIO", "pixel_clock_hz": 60_000_000}
        self._write(data)
        with self.assertRaises(ConfigError):
            load_config(self.path)


class RecordingFpsTest(unittest.TestCase):
    """recording.fps becomes the encoder's frame rate; PyAV's
    add_stream(rate=...) raises on a float, so a fractional value here would
    surface only as a crash on the first Start. It must be a whole number,
    validated loudly at load."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "config.json"

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_absent_section_uses_the_default(self):
        self._write(VALID)
        self.assertEqual(load_config(self.path).recording.fps, 30)

    def test_a_whole_float_is_accepted_and_coerced_to_int(self):
        data = json.loads(json.dumps(VALID))
        data["recording"] = {"fps": 30.0}
        self._write(data)
        fps = load_config(self.path).recording.fps
        self.assertEqual(fps, 30)
        self.assertIsInstance(fps, int)

    def test_a_fractional_or_non_numeric_fps_is_rejected(self):
        for bad in (29.97, 0, -5, "30", True, None, [30]):
            with self.subTest(bad=bad):
                data = json.loads(json.dumps(VALID))
                data["recording"] = {"fps": bad}
                self._write(data)
                with self.assertRaises(ConfigError):
                    load_config(self.path)

if __name__ == "__main__":
    unittest.main()
