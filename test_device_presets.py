"""Tests for device_presets.orientation_for_model -- pure lookup logic, no
hardware or SDK.
"""

from __future__ import annotations

import unittest

from camera import ORIENTATION_FLIP_VERTICAL, ORIENTATION_NONE, ORIENTATION_ROTATE_180
from exposure_calibration import METERING_FIELD, METERING_HIGHLIGHT, VALID_METERING
from device_presets import (
    CUSTOM_PROFILE_ID,
    PROFILES,
    black_level_for_model,
    floor_model_for_model,
    gamma_for_model,
    metering_for_model,
    pixel_format_for_model,
    orientation_for_model,
    pixel_clock_hz_for_model,
    profile_for_id,
    profile_for_model,
    profiles_for_role,
)


class OrientationForModelTest(unittest.TestCase):
    def test_keeler_bio_camera_image_is_vertically_flipped(self):
        # Exact string this camera reports (confirmed on hardware 2026-09-01).
        self.assertEqual(orientation_for_model("U3-327xCP-C"), ORIENTATION_FLIP_VERTICAL)

    def test_match_is_case_insensitive_and_substring(self):
        self.assertEqual(orientation_for_model("u3-327xcp-c rev.2"), ORIENTATION_FLIP_VERTICAL)

    def test_slit_lamp_camera_image_is_rotated_180(self):
        """Reported from the instrument 2026-09-08: mirrored on both axes,
        which is a 180-degree rotation rather than two separate flips."""
        self.assertEqual(orientation_for_model("UI325xCP-C"), ORIENTATION_ROTATE_180)

    def test_the_two_instrument_presets_do_not_collide(self):
        """"UI325" and "U3-327" are both substring tokens; each must match
        only its own camera."""
        self.assertEqual(orientation_for_model("UI325xCP-C"), ORIENTATION_ROTATE_180)
        self.assertEqual(orientation_for_model("U3-327xCP-C"), ORIENTATION_FLIP_VERTICAL)

    def test_unknown_model_defaults_to_none(self):
        self.assertEqual(orientation_for_model("SomeOtherCamera"), ORIENTATION_NONE)

    def test_none_and_empty_default_to_none(self):
        self.assertEqual(orientation_for_model(None), ORIENTATION_NONE)
        self.assertEqual(orientation_for_model(""), ORIENTATION_NONE)


class PixelClockForModelTest(unittest.TestCase):
    """The legacy uEye slit lamp camera powers up at 24MHz of a 10-128MHz
    range on every open, which is an ~87ms frame period at 1600x1200 --
    the whole of its long-assumed "11fps sensor limit"."""

    def test_slit_lamp_gets_the_measured_clock(self):
        self.assertEqual(pixel_clock_hz_for_model("UI325xCP-C"), 80_000_000)

    def test_match_is_case_insensitive(self):
        self.assertEqual(pixel_clock_hz_for_model("ui325xcp-c rev.2"), 80_000_000)

    def test_bio_has_no_entry_because_its_clock_is_fixed(self):
        # The USB3 Vision camera reports a fixed, unwritable 197MHz.
        self.assertIsNone(pixel_clock_hz_for_model("U3-327xCP-C"))

    def test_unknown_model_is_left_alone(self):
        self.assertIsNone(pixel_clock_hz_for_model("SomeOtherCamera"))
        self.assertIsNone(pixel_clock_hz_for_model(None))
        self.assertIsNone(pixel_clock_hz_for_model(""))



class ProfileRegistryTest(unittest.TestCase):
    """The supported-camera list settings.py offers and config.json resolves
    through. Pure data plus lookups -- no hardware, no Qt."""

    def test_ids_are_unique(self):
        ids = [p.id for p in PROFILES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_ids_are_config_safe(self):
        """They are written into config.json and must never need quoting or
        case-folding -- and never change once shipped."""
        for profile in PROFILES:
            with self.subTest(profile=profile.id):
                self.assertRegex(profile.id, r"^[a-z0-9_]+$")

    def test_every_profile_names_at_least_one_role(self):
        for profile in PROFILES:
            with self.subTest(profile=profile.id):
                self.assertTrue(profile.roles)

    def test_roles_offer_only_their_own_cameras(self):
        slit_lamp = {p.id for p in profiles_for_role("slit_lamp")}
        bio = {p.id for p in profiles_for_role("bio")}
        self.assertIn("haag_streit_bi900_slit_lamp", slit_lamp)
        self.assertIn("keeler_vantage_plus_digital", bio)
        self.assertIn("keeler_vantage_plus_legacy", bio)
        self.assertEqual(slit_lamp & bio, set())

    def test_third_person_has_no_profiles(self):
        """Deliberate: any UVC webcam works, so that row stays a raw device
        list rather than a supported-model list."""
        self.assertEqual(profiles_for_role("third_person"), ())

    def test_model_match_is_scoped_to_the_role(self):
        self.assertEqual(profile_for_model("U3-327xCP-C", "bio").id, "keeler_vantage_plus_digital")
        self.assertIsNone(profile_for_model("U3-327xCP-C", "slit_lamp"))

    def test_model_match_without_a_role_still_resolves(self):
        self.assertEqual(profile_for_model("UI325xCP-C").id, "haag_streit_bi900_slit_lamp")

    def test_unknown_and_empty_models_match_nothing(self):
        for model in ("SomeOtherCamera", "", None):
            with self.subTest(model=model):
                self.assertIsNone(profile_for_model(model))

    def test_the_legacy_bio_never_auto_matches(self):
        """It has no IDS model string; it is recognised by being present at
        all, which is settings.py's job rather than this table's."""
        legacy = profile_for_id("keeler_vantage_plus_legacy")
        self.assertEqual(legacy.model_tokens, ())

    def test_custom_and_unknown_ids_resolve_to_none(self):
        """Unknown must degrade to the technician's own values. A config from
        a newer build has to load on an older kiosk, not fail it."""
        self.assertIsNone(profile_for_id(CUSTOM_PROFILE_ID))
        self.assertIsNone(profile_for_id("written_by_a_newer_build"))
        self.assertIsNone(profile_for_id(None))

    def test_presets_and_profiles_cannot_disagree(self):
        """The camera-open presets read the same table the technician picks
        from, which is the point of merging them."""
        for profile in PROFILES:
            for token in profile.model_tokens:
                with self.subTest(token=token):
                    self.assertEqual(orientation_for_model(token), profile.orientation)
                    self.assertEqual(pixel_clock_hz_for_model(token), profile.pixel_clock_hz)
                    self.assertEqual(metering_for_model(token), profile.metering)
                    self.assertEqual(gamma_for_model(token), profile.gamma)
                    self.assertEqual(pixel_format_for_model(token), profile.pixel_format)

    def test_an_unknown_model_gets_no_tone_curve_or_format(self):
        self.assertIsNone(gamma_for_model("SomeOtherCamera"))
        self.assertIsNone(pixel_format_for_model(None))

    def test_a_raised_capture_format_always_comes_with_a_curve(self):
        """Extra bits are discarded by the 8-bit conversion unless a curve
        uses them first: a format with no gamma is bandwidth for nothing."""
        for profile in PROFILES:
            if profile.pixel_format is not None:
                self.assertIsNotNone(profile.gamma, profile.id)


class MeteringForModelTest(unittest.TestCase):
    """A slit beam is the brightest 0.1% of the frame; the BIO's field is
    a lit disc whose brightest 0.1% is sparkle. See DECISIONS.md 2026-09-14."""

    def test_the_slit_lamp_meters_the_beam(self):
        self.assertEqual(metering_for_model("UI325xCP-C"), METERING_HIGHLIGHT)

    def test_the_keeler_meters_the_field(self):
        self.assertEqual(metering_for_model("U3-327xCP-C"), METERING_FIELD)

    def test_an_unknown_model_keeps_the_old_rule(self):
        self.assertEqual(metering_for_model("SomeOtherCamera"), METERING_HIGHLIGHT)
        self.assertEqual(metering_for_model(None), METERING_HIGHLIGHT)

    def test_every_profile_names_a_valid_rule(self):
        for profile in PROFILES:
            self.assertIn(profile.metering, VALID_METERING, profile.id)

class BlackLevelForModelTest(unittest.TestCase):
    """The slit lamp's factory black level clips; the Keeler manages its own.
    See DECISIONS.md's 2026-09-13 entry for the measurements."""

    def test_the_slit_lamp_gets_a_corrected_floor(self):
        self.assertEqual(black_level_for_model("UI325xCP-C"), 110.0)

    def test_the_keeler_is_left_alone(self):
        """It self-adjusts continuously and does not clip -- writing a value
        would be taking over a job it does correctly."""
        self.assertIsNone(black_level_for_model("U3-327xCP-C"))

    def test_an_unknown_model_is_left_alone(self):
        self.assertIsNone(black_level_for_model("SomeOtherCamera"))
        self.assertIsNone(black_level_for_model(None))

    def test_it_matches_the_profile_it_came_from(self):
        profile = next(p for p in PROFILES if p.id == "haag_streit_bi900_slit_lamp")
        for token in profile.model_tokens:
            with self.subTest(token=token):
                self.assertEqual(black_level_for_model(token), profile.black_level)



class FloorModelTest(unittest.TestCase):
    """The slit lamp's measured floor (DECISIONS.md 2026-09-21)."""

    def test_reproduces_the_dark_frame_at_gain_three(self):
        floor = floor_model_for_model("UI325xCP-C")
        self.assertIsNotNone(floor)
        black = floor.black(3.0, 4095)
        for got, measured in zip(black, (517.3, 391.2, 548.9)):
            self.assertAlmostEqual(got, measured, delta=6.0)
        sigma = floor.sigma(3.0, 4095)
        for got, measured in zip(sigma, (24.6, 18.4, 26.0)):
            self.assertAlmostEqual(got, measured, delta=1.0)

    def test_the_floor_is_not_neutral(self):
        # R and B above G is the purple the single master value left behind.
        r, g, b = floor_model_for_model("UI325xCP-C").black(1.0, 4095)
        self.assertGreater(r, g + 15)
        self.assertGreater(b, g + 15)

    def test_black_never_goes_negative(self):
        self.assertEqual(min(floor_model_for_model("UI325xCP-C").black(0.0, 4095)), 0.0)

    def test_the_keeler_has_none(self):
        # It curves on board; the floor model is for host curves only.
        self.assertIsNone(floor_model_for_model("U3-327xCP-C"))


if __name__ == "__main__":
    unittest.main()
