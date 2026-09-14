"""Tests for exposure_calibration.py's pure median-brightness/exposure-gain
math -- split out from ids_camera.py specifically so this logic is
testable without the IDS peak SDK. See DECISIONS.md's 2026-08-25
calibration entry.
"""

from __future__ import annotations

import unittest

import numpy as np

from exposure_calibration import (
    DEFAULT_SATURATION_LEVEL,
    METERING_FIELD,
    METERING_HIGHLIGHT,
    exposure_budget_us,
    center_crop,
    is_converged,
    median_brightness,
    metering_brightness,
    metering_target,
    next_exposure_gain,
)


class MeteringModeTest(unittest.TestCase):
    """The BIO failure of 2026-09-14: a lit field covering half the frame
    at a dim level, plus a specular speck. The highlight rule reads the
    speck and calls the frame over-exposed; the field rule reads the
    field."""

    def _bio_like_frame(self) -> np.ndarray:
        image = np.zeros((200, 200, 3), dtype=np.uint8)
        image[:, :100] = 40  # the lit field: half the frame, too dark
        # 0.16% sparkle: above p99.9's reach (the real one measured 0.08%
        # saturated plus a bright halo), far below the field rule's 5%.
        image[0:8, 0:8] = 255
        return image

    def test_highlight_metering_is_pinned_by_a_speck(self):
        measured = metering_brightness(self._bio_like_frame(), METERING_HIGHLIGHT)
        self.assertGreaterEqual(measured, DEFAULT_SATURATION_LEVEL)  # "over-exposed"

    def test_field_metering_reads_the_field(self):
        measured = metering_brightness(self._bio_like_frame(), METERING_FIELD)
        self.assertEqual(measured, 40.0)
        target, tolerance = metering_target(METERING_FIELD)
        self.assertFalse(is_converged(measured, target, tolerance))  # wants more light

    def test_field_rule_asks_for_more_light_where_highlight_would_halve(self):
        frame = self._bio_like_frame()
        for mode, expect_brighter in ((METERING_HIGHLIGHT, False), (METERING_FIELD, True)):
            with self.subTest(mode=mode):
                measured = metering_brightness(frame, mode)
                target, _ = metering_target(mode)
                exposure, gain = next_exposure_gain(measured, 30_000.0, (24.0, 30_000.0), 4.0, (1.0, 25.4), target=target)
                self.assertEqual(exposure, 30_000.0)
                self.assertEqual(gain > 4.0, expect_brighter)


class MedianBrightnessTest(unittest.TestCase):
    def test_uniform_image(self):
        image = np.full((10, 10, 3), 64, dtype=np.uint8)
        self.assertEqual(median_brightness(image), 64.0)

    def test_robust_to_a_bright_outlier_patch(self):
        image = np.full((10, 10, 3), 50, dtype=np.uint8)
        image[0:1, 0:1, :] = 255  # a small hot reflection shouldn't move the median
        self.assertEqual(median_brightness(image), 50.0)


class IsConvergedTest(unittest.TestCase):
    def test_within_tolerance_is_converged(self):
        self.assertTrue(is_converged(120.0, target=128.0, tolerance=10.0))
        self.assertTrue(is_converged(138.0, target=128.0, tolerance=10.0))

    def test_outside_tolerance_is_not_converged(self):
        self.assertFalse(is_converged(100.0, target=128.0, tolerance=10.0))


class NextExposureGainTest(unittest.TestCase):
    """The correction step solves in total light (exposure x gain) and then
    redistributes it with one preference: as much exposure as the budget
    allows, as little gain as will do. Gain is the noise source, so every
    step pulls it back toward its minimum -- which is what lets a camera
    recover from a bad starting point rather than inheriting it."""

    def test_a_reachable_increase_is_taken_on_exposure_and_returns_gain_to_minimum(self):
        new_exposure, new_gain = next_exposure_gain(
            measured=64.0,  # half the target -- needs 2x brightness
            exposure_time_us=1000.0,
            exposure_range_us=(100.0, 10_000.0),
            gain=2.0,
            gain_range=(1.0, 8.0),
            target=128.0,
        )
        self.assertAlmostEqual(new_exposure, 4000.0)
        self.assertAlmostEqual(new_gain, 1.0)
        # Total light doubled, as asked: 1000x2 -> 4000x1.
        self.assertAlmostEqual(new_exposure * new_gain, 1000.0 * 2.0 * 2.0)

    def test_gain_makes_up_the_shortfall_once_exposure_maxes_out(self):
        new_exposure, new_gain = next_exposure_gain(
            measured=16.0,  # needs 8x brightness, exposure can only give 2x
            exposure_time_us=5000.0,
            exposure_range_us=(100.0, 10_000.0),
            gain=2.0,
            gain_range=(1.0, 8.0),
            target=128.0,
        )
        self.assertAlmostEqual(new_exposure, 10_000.0)  # clamped at max
        self.assertAlmostEqual(new_gain, 8.0)  # clamped at max; still short, next step retries

    def test_a_reduction_comes_off_gain_first(self):
        """The case the earlier per-axis version got backwards: it shortened
        exposure and left gain (and its noise) untouched."""
        new_exposure, new_gain = next_exposure_gain(
            measured=192.0,  # 1.5x the target -- needs 2/3 the light
            exposure_time_us=1000.0,
            exposure_range_us=(100.0, 10_000.0),
            gain=4.0,
            gain_range=(1.0, 8.0),
            target=128.0,
        )
        self.assertAlmostEqual(new_gain, 1.0)  # gain emptied
        self.assertAlmostEqual(new_exposure * new_gain, 1000.0 * 4.0 * (128.0 / 192.0))

    def test_a_reduction_past_the_exposure_floor_leaves_both_at_minimum(self):
        new_exposure, new_gain = next_exposure_gain(
            measured=249.0,  # bright, but not treated as censored
            exposure_time_us=150.0,
            exposure_range_us=(100.0, 10_000.0),
            gain=1.0,
            gain_range=(1.0, 8.0),
            target=128.0,
        )
        self.assertAlmostEqual(new_exposure, 100.0)  # exposure floor
        self.assertAlmostEqual(new_gain, 1.0)  # gain floor -- it cannot go dimmer

    def test_a_saturated_measurement_halves_rather_than_stepping_proportionally(self):
        """A clipped highlight is censored: it says we are over, not by how
        much. Measured on the real BIO, proportional steps from a 6.6%
        clipped frame had not converged after eight iterations."""
        new_exposure, new_gain = next_exposure_gain(
            measured=255.0,
            exposure_time_us=1000.0,
            exposure_range_us=(100.0, 10_000.0),
            gain=4.0,
            gain_range=(1.0, 8.0),
            target=210.0,
        )
        # Half the light, not the 210/255 = 0.82 a proportional step implies.
        self.assertAlmostEqual(new_exposure * new_gain, 1000.0 * 4.0 * 0.5)
        self.assertAlmostEqual(new_gain, 1.0)

    def test_the_outcome_does_not_depend_on_the_starting_split(self):
        """Same total light, different exposure/gain split -- the step must
        land in the same place. This is why a camera stuck at high gain
        recovers instead of inheriting it."""
        common = dict(
            measured=64.0, exposure_range_us=(100.0, 10_000.0),
            gain_range=(1.0, 8.0), target=128.0,
        )
        a = next_exposure_gain(exposure_time_us=1000.0, gain=4.0, **common)
        b = next_exposure_gain(exposure_time_us=2000.0, gain=2.0, **common)
        self.assertEqual(a, b)


class CenterCropTest(unittest.TestCase):
    def test_half_fraction_crops_both_dimensions(self):
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        cropped = center_crop(image, fraction=0.5)
        self.assertEqual(cropped.shape, (50, 100, 3))

    def test_full_fraction_is_a_no_op(self):
        image = np.zeros((60, 80, 3), dtype=np.uint8)
        cropped = center_crop(image, fraction=1.0)
        self.assertEqual(cropped.shape, image.shape)

    def test_zero_fraction_raises(self):
        image = np.zeros((10, 10, 3), dtype=np.uint8)
        with self.assertRaises(ValueError):
            center_crop(image, fraction=0.0)

    def test_fraction_above_one_raises(self):
        image = np.zeros((10, 10, 3), dtype=np.uint8)
        with self.assertRaises(ValueError):
            center_crop(image, fraction=1.5)

    def test_demonstrates_the_vignette_skew_scenario(self):
        # A bright center patch surrounded by true black -- the whole-frame
        # median is skewed dark by the surround; a centered crop that stays
        # inside the bright patch reads the real content instead.
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        image[25:75, 25:75, :] = 200
        whole_frame_median = median_brightness(image)
        cropped_median = median_brightness(center_crop(image, fraction=0.4))
        self.assertEqual(whole_frame_median, 0.0)
        self.assertEqual(cropped_median, 200.0)


class ExposureBudgetTest(unittest.TestCase):
    """Exposure time is a frame-rate budget: a sensor exposing for E
    microseconds cannot deliver faster than 1/E."""

    def test_budget_is_the_frame_interval_less_readout_headroom(self):
        # 30fps -> 33.33ms interval, times the 0.9 headroom fraction.
        self.assertAlmostEqual(exposure_budget_us(30), 30_000.0, delta=1.0)
        self.assertAlmostEqual(exposure_budget_us(60), 15_000.0, delta=1.0)

    def test_budget_rejects_a_non_positive_rate(self):
        for bad in (0, -30):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    exposure_budget_us(bad)


class NextExposureGainBudgetTest(unittest.TestCase):
    def test_without_a_budget_exposure_absorbs_everything(self):
        """The pre-existing behaviour, kept as the contrast for the test
        below -- this is what produced 87ms on the slit lamp."""
        exposure, gain = next_exposure_gain(
            measured=48.0,
            exposure_time_us=15_000.0,
            exposure_range_us=(10.0, 200_000.0),
            gain=1.0,
            gain_range=(1.0, 4.0),
            target=128.0,
        )
        self.assertAlmostEqual(exposure, 40_000.0, delta=1.0)
        self.assertEqual(gain, 1.0)

    def test_a_budget_caps_exposure_and_spills_the_remainder_onto_gain(self):
        exposure, gain = next_exposure_gain(
            measured=48.0,
            exposure_time_us=15_000.0,
            exposure_range_us=(10.0, 200_000.0),
            gain=1.0,
            gain_range=(1.0, 4.0),
            target=128.0,
            max_exposure_us=exposure_budget_us(30),
        )
        self.assertAlmostEqual(exposure, 30_000.0, delta=1.0)
        self.assertAlmostEqual(gain, 40_000.0 / 30_000.0, places=3)

    def test_the_budget_never_asks_for_less_than_the_sensor_can_do(self):
        exposure, _gain = next_exposure_gain(
            measured=48.0,
            exposure_time_us=15_000.0,
            exposure_range_us=(20_000.0, 200_000.0),
            gain=1.0,
            gain_range=(1.0, 4.0),
            target=128.0,
            max_exposure_us=5_000.0,
        )
        self.assertEqual(exposure, 20_000.0)

    def test_a_budget_that_is_not_binding_changes_nothing(self):
        args = dict(
            measured=100.0, exposure_time_us=10_000.0, exposure_range_us=(10.0, 200_000.0),
            gain=1.0, gain_range=(1.0, 4.0), target=128.0,
        )
        self.assertEqual(
            next_exposure_gain(**args),
            next_exposure_gain(**args, max_exposure_us=exposure_budget_us(30)),
        )

    def test_the_slit_lamp_regression_converges_inside_the_frame_budget(self):
        """Run the loop the way auto_calibrate() does, from a near-black
        frame, and confirm it lands inside a 30fps budget by using gain
        rather than spending the whole frame interval."""
        exposure, gain = 15_000.0, 1.0
        budget = exposure_budget_us(30)
        median = 20.0
        for _ in range(8):
            if is_converged(median, 128.0, 10.0):
                break
            exposure, gain = next_exposure_gain(
                median, exposure, (10.0, 200_000.0), gain, (1.0, 4.0),
                target=128.0, max_exposure_us=budget,
            )
            median = min(255.0, 20.0 * (exposure / 15_000.0) * gain)

        self.assertLessEqual(exposure, budget)
        self.assertGreater(gain, 1.0)
        self.assertGreaterEqual(1_000_000.0 / exposure, 30.0)


if __name__ == "__main__":
    unittest.main()
