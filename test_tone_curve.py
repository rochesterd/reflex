"""Tests for tone_curve on synthetic ramps and noise. What matters is the
property the toe exists for: noise sitting on the floor grows by no more
than the floor slope asked for, while signal above it gets the lift --
and the curve is smooth, monotonic, and lands black on 0 and full scale
on 255, per channel.
"""

from __future__ import annotations

import unittest

import numpy as np

from tone_curve import (
    MAX_FLOOR_SLOPE,
    ToneCurve,
    apply_bayer,
    build_luts,
    curve_values,
    demosaic,
    floor_slope_for_noise,
    max_gain_for_noise,
    offset_for_slope,
    raw_to_bgr8,
)

BLACK = (100.0, 90.0, 110.0)


def _curve(gamma=1.8, floor_slope=2.0) -> ToneCurve:
    return ToneCurve(gamma=gamma, black=BLACK, floor_slope=floor_slope)


class CurveShapeTest(unittest.TestCase):
    def test_the_slope_at_black_is_the_one_asked_for(self):
        for gamma, wanted in ((1.8, 2.0), (1.8, 1.5), (2.2, 3.0), (1.5, 8.0)):
            with self.subTest(gamma=gamma, slope=wanted):
                x = np.array([0.0, 1e-5])
                y = curve_values(x, gamma, wanted)
                measured = (y[1] - y[0]) / 1e-5
                self.assertAlmostEqual(measured, wanted, delta=wanted * 0.02)

    def test_ends_are_pinned_and_the_curve_is_monotonic(self):
        x = np.linspace(0, 1, 4096)
        y = curve_values(x, 1.8, 2.0)
        self.assertAlmostEqual(y[0], 0.0)
        self.assertAlmostEqual(y[-1], 1.0)
        self.assertTrue(np.all(np.diff(y) >= 0))

    def test_gamma_one_or_slope_one_is_a_straight_line(self):
        x = np.linspace(0, 1, 100)
        self.assertTrue(np.allclose(curve_values(x, 1.0, 5.0), x))
        self.assertTrue(np.allclose(curve_values(x, 1.8, 1.0), x))
        self.assertEqual(offset_for_slope(1.8, 1.0), float("inf"))

    def test_a_huge_slope_is_the_bare_gamma(self):
        x = np.linspace(0.05, 1, 50)
        near_bare = curve_values(x, 1.8, MAX_FLOOR_SLOPE)
        bare = x ** (1 / 1.8)
        self.assertTrue(np.allclose(near_bare, bare, atol=0.02))

    def test_shadows_are_still_lifted_at_a_known_cost(self):
        # A quarter of the way up the range: straight gives 0.25, the bare
        # gamma 0.46, and a slope-2 toe 0.36 -- most of the lift, for a
        # floor that no longer amplifies. The number is the trade, so it
        # is pinned rather than hand-waved.
        x = np.array([0.25])
        toed = curve_values(x, 1.8, 2.0)[0]
        self.assertAlmostEqual(toed, 0.361, places=2)
        self.assertGreater(toed, 0.25 + 0.10)


class LutTest(unittest.TestCase):
    def test_black_maps_to_zero_and_full_scale_to_255_per_channel(self):
        for lut, black in zip(build_luts(_curve()), BLACK):
            self.assertEqual(lut[int(black)], 0)
            self.assertEqual(lut[: int(black)].max(), 0)
            self.assertEqual(lut[4095], 255)

    def test_monotonic_non_decreasing(self):
        for lut in build_luts(_curve()):
            self.assertTrue(np.all(np.diff(lut.astype(int)) >= 0))

    def test_no_jumps_anywhere(self):
        # Smooth: no two neighbouring codes differ by more than one level
        # once past the very first few codes, where 12->8 bit quantises.
        for lut in build_luts(_curve()):
            self.assertLessEqual(int(np.diff(lut.astype(int))[200:].max()), 1)


class NoiseAmplificationTest(unittest.TestCase):
    """The reason for the toe, as a number."""

    def _output_sigma(self, curve: ToneCurve, floor: float, sigma: float) -> float:
        rng = np.random.default_rng(1)
        raw = np.clip(rng.normal(floor, sigma, size=200_000), 0, 4095).astype(np.uint16)
        return float(build_luts(curve)[1][raw].astype(np.float64).std())

    def test_floor_noise_grows_by_about_the_floor_slope_not_more(self):
        black, sigma = 90.0, 30.0  # the slit lamp at gain 3, roughly
        floor = black + 3 * sigma  # noise sitting just above black
        straight = self._output_sigma(ToneCurve(1.0, BLACK, 1.0), floor, sigma)
        bare = self._output_sigma(ToneCurve(1.8, BLACK, MAX_FLOOR_SLOPE), floor, sigma)
        toed = self._output_sigma(ToneCurve(1.8, BLACK, 2.0), floor, sigma)
        self.assertGreater(bare, 3.0 * straight, "the bare gamma should show the defect")
        self.assertLess(toed, 2.3 * straight, "a slope-2 toe should hold noise near 2x")
        self.assertGreater(toed, 1.3 * straight)

    def test_floor_slope_for_noise_targets_an_output_sigma(self):
        # sigma 30 raw on a 4095 scale is ~1.9 levels straight; allowing 3
        # levels of output noise permits a slope of ~1.6.
        slope = floor_slope_for_noise(30.0, 90.0, 4095, max_output_sigma=3.0)
        self.assertAlmostEqual(slope, 3.0 / (30.0 / 4005.0 * 255.0), places=3)
        # Too noisy for even a straight line: clamp at 1.0, never compress.
        self.assertEqual(floor_slope_for_noise(200.0, 90.0, 4095, max_output_sigma=3.0), 1.0)
        # No noise at all: the bare curve is fine.
        self.assertEqual(floor_slope_for_noise(0.0, 90.0, 4095, max_output_sigma=3.0), MAX_FLOOR_SLOPE)


    def test_max_gain_is_where_a_straight_line_gets_too_noisy(self):
        # The slit lamp's red channel: sigma 0.00186 g + 0.0005, black
        # 0.0497 g - 0.0219, limit 2 levels -> about gain 3.3.
        gain = max_gain_for_noise(0.00186, 0.00050, 0.0497, -0.0219, 2.0, 4.0)
        self.assertAlmostEqual(gain, 3.3, delta=0.15)
        # A quiet sensor never hits the limit inside its range.
        self.assertEqual(max_gain_for_noise(0.0001, 0.0, 0.01, 0.0, 2.0, 4.0), 4.0)
        # A hopeless one is clamped to unity, never below.
        self.assertEqual(max_gain_for_noise(0.5, 0.5, 0.01, 0.0, 2.0, 4.0), 1.0)


class BayerTest(unittest.TestCase):
    def test_apply_bayer_routes_each_position_through_its_channel(self):
        raw = np.full((4, 4), 2000, dtype=np.uint16)
        r = np.full(4096, 10, dtype=np.uint8)
        g = np.full(4096, 20, dtype=np.uint8)
        b = np.full(4096, 30, dtype=np.uint8)
        out = apply_bayer(raw, (r, g, b))
        self.assertEqual((out[0, 0], out[0, 1], out[1, 0], out[1, 1]), (10, 20, 20, 30))

    def test_demosaic_produces_bgr_of_the_same_size(self):
        raw = np.full((8, 8), 1000, dtype=np.uint16)
        out = raw_to_bgr8(raw, build_luts(ToneCurve(1.0, (0.0, 0.0, 0.0), 1.0)))
        self.assertEqual(out.shape, (8, 8, 3))
        self.assertEqual(out.dtype, np.uint8)

    def test_a_grey_scene_stays_grey_when_blacks_are_per_channel(self):
        # Raw floors differ per channel; after subtraction a flat scene is
        # neutral, which is what the purple cast lacked.
        raw = np.empty((16, 16), dtype=np.uint16)
        raw[0::2, 0::2] = 100 + 500
        raw[0::2, 1::2] = 90 + 500
        raw[1::2, 0::2] = 90 + 500
        raw[1::2, 1::2] = 110 + 500
        bgr = raw_to_bgr8(raw, build_luts(_curve()))
        centre = bgr[4:12, 4:12].reshape(-1, 3).mean(axis=0)
        self.assertLess(centre.max() - centre.min(), 2.0)

    def test_demosaic_uses_the_code_that_matches_ids(self):
        # A pure-red raw frame (R sites high) must come out red in BGR --
        # channel 2, not channel 0.
        raw = np.zeros((16, 16), dtype=np.uint16)
        raw[0::2, 0::2] = 4000
        bgr = demosaic(apply_bayer(raw, build_luts(ToneCurve(1.0, (0.0, 0.0, 0.0), 1.0))))
        centre = bgr[4:12, 4:12].reshape(-1, 3).mean(axis=0)
        self.assertGreater(centre[2], centre[0] + 100)


if __name__ == "__main__":
    unittest.main()
