"""Tests for the part of ids_camera.py that needs no camera: _to_bgr8(),
the conversion every IDS frame goes through, with and without the host
tone curve. Runs against the real ids_peak_ipl -- an image library, not a
device -- and is skipped on a machine without the IDS bindings, which is
every machine that never talks to an IDS camera.

Everything else in ids_camera.py is verified on hardware (tools/, and the
measurements in DECISIONS.md); a mocked GenICam node map would test the
mock.
"""

from __future__ import annotations

import unittest

import numpy as np

try:
    from ids_peak_ipl import ids_peak_ipl

    from ids_camera import IdsCamera, IdsCameraNotFoundError, _to_bgr8
    from tone_curve import ToneCurve, build_luts
except ImportError:  # no IDS bindings here
    ids_peak_ipl = None


@unittest.skipIf(ids_peak_ipl is None, "IDS peak bindings not installed")
class OpenPathTest(unittest.TestCase):
    def test_a_missing_serial_fails_as_not_found_and_can_be_retried(self):
        """Walks the real _open() as far as device lookup, and its cleanup.
        A module-level function once landed in the middle of the class and
        turned every method after it into dead code nested inside it --
        valid Python, and invisible to a suite that never opens a camera.
        This saw it as an AttributeError where NotFound belongs."""
        camera = IdsCamera(serial="no-such-camera")
        for _ in range(2):  # the second proves the first cleaned up
            with self.assertRaises(IdsCameraNotFoundError):
                camera.start()

W, H = 320, 240


def _bayer12(values: np.ndarray):
    image = ids_peak_ipl.Image.CreateFromSize(ids_peak_ipl.PixelFormatName_BayerRG12, W, H)
    image.get_numpy_2D_16()[:] = values
    return image


def _luts(gamma: float, black=(0.0, 0.0, 0.0), floor_slope: float = 64.0):
    return build_luts(ToneCurve(gamma=gamma, black=black, floor_slope=floor_slope))


@unittest.skipIf(ids_peak_ipl is None, "IDS peak bindings not installed")
class ToBgr8Test(unittest.TestCase):
    """_to_bgr8 with the host curve: the raw 12-bit path through
    tone_curve's tables, against IDS's own conversion without one."""

    def setUp(self):
        # The bottom 10% of a 12-bit range, left to right: a background lit
        # only by the room, which lands in the first ~25 of 255 levels.
        self.shadows = np.tile(np.linspace(0, 409, W).astype(np.uint16), (H, 1))

    @staticmethod
    def _row(bgr: np.ndarray) -> np.ndarray:
        return bgr[H // 2, :, 1].astype(int)  # green, across the ramp

    def test_without_a_curve_it_is_ids_conversion(self):
        out = _to_bgr8(_bayer12(self.shadows))
        self.assertEqual(out.shape, (H, W, 3))
        self.assertEqual(out.dtype, np.uint8)
        self.assertLessEqual(int(out.max()), 26)

    def test_the_curve_lifts_the_shadows(self):
        plain = self._row(_to_bgr8(_bayer12(self.shadows)))
        lifted = self._row(_to_bgr8(_bayer12(self.shadows), _luts(2.0)))
        self.assertGreater(int(lifted[W // 2]), int(plain[W // 2]) + 30)

    def test_the_curve_is_applied_at_twelve_bits_not_eight(self):
        # 40 distinct levels survive an 8-bit-first curve; a 12-bit-first
        # one keeps many more of the ramp's steps.
        curved = self._row(_to_bgr8(_bayer12(self.shadows), _luts(2.0)))
        self.assertGreater(len(np.unique(curved)), 60)

    def test_per_channel_black_keeps_the_floor_neutral(self):
        # A floor that is higher in R and B than in G, as the slit lamp's
        # is: subtracting per channel lands on grey, not purple.
        raw = np.empty((H, W), dtype=np.uint16)
        raw[0::2, 0::2] = 120 + 300  # R
        raw[0::2, 1::2] = 90 + 300  # G
        raw[1::2, 0::2] = 90 + 300  # G
        raw[1::2, 1::2] = 125 + 300  # B
        out = _to_bgr8(_bayer12(raw), _luts(1.8, black=(120.0, 90.0, 125.0), floor_slope=2.0))
        centre = out[H // 4 : 3 * H // 4, W // 4 : 3 * W // 4].reshape(-1, 3).mean(axis=0)
        self.assertLess(float(centre.max() - centre.min()), 2.0)

    def test_an_eight_bit_capture_ignores_the_luts(self):
        eight_bit = ids_peak_ipl.Image.CreateFromSize(ids_peak_ipl.PixelFormatName_BayerRG8, W, H)
        eight_bit.get_numpy_2D()[:] = (self.shadows >> 4).astype(np.uint8)
        with_luts = _to_bgr8(eight_bit, _luts(2.0))
        without = _to_bgr8(eight_bit)
        self.assertTrue(np.array_equal(with_luts, without))

    def test_the_result_outlives_the_image(self):
        """The numpy views do not keep their Image alive; the returned
        array must be a copy, readable after the Image is gone."""
        out = _to_bgr8(_bayer12(self.shadows), _luts(2.0))
        import gc

        gc.collect()
        self.assertEqual(out.shape, (H, W, 3))
        self.assertGreater(int(out.sum()), 0)


if __name__ == "__main__":
    unittest.main()
