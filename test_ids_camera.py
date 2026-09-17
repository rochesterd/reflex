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


def _corrector(gamma: float):
    corrector = ids_peak_ipl.GammaCorrector()
    corrector.SetGammaCorrectionValue(gamma)
    return corrector


@unittest.skipIf(ids_peak_ipl is None, "IDS peak bindings not installed")
class ToBgr8Test(unittest.TestCase):
    def setUp(self):
        # The bottom 10% of a 12-bit range, left to right: a background lit
        # only by the room, which lands in the first ~25 of 255 levels.
        self.shadows = np.tile(np.linspace(0, 409, W).astype(np.uint16), (H, 1))

    def _row(self, array: np.ndarray) -> np.ndarray:
        return array[H // 2, 8:-8, 1]  # clear of the debayer's edge handling

    def test_without_a_curve_the_frame_is_a_plain_8_bit_conversion(self):
        out = _to_bgr8(_bayer12(self.shadows))
        self.assertEqual(out.shape, (H, W, 3))
        self.assertEqual(out.dtype, np.uint8)
        self.assertLessEqual(int(self._row(out).max()), 26)

    def test_a_curve_above_one_lifts_the_shadows(self):
        plain = self._row(_to_bgr8(_bayer12(self.shadows)))
        lifted = self._row(_to_bgr8(_bayer12(self.shadows), _corrector(2.0)))
        self.assertGreater(int(lifted.max()), 2 * int(plain.max()))

    def test_the_highlight_is_compressed_not_clipped(self):
        bright = np.full((H, W), 3300, dtype=np.uint16)  # ~205 of 255
        value = int(_to_bgr8(_bayer12(bright), _corrector(2.0))[H // 2, W // 2, 1])
        self.assertGreater(value, 205)
        self.assertLess(value, 250)

    def test_the_curve_runs_before_the_conversion_so_12_bits_survive(self):
        """The reason the capture format is raised at all: curved after an
        8-bit conversion, the same shadows come out in about half the
        steps -- visible banding in exactly the region being lifted."""
        curved_first = self._row(_to_bgr8(_bayer12(self.shadows), _corrector(2.0)))

        eight_bit = ids_peak_ipl.Image.CreateFromSize(ids_peak_ipl.PixelFormatName_BayerRG8, W, H)
        eight_bit.get_numpy_2D()[:] = (self.shadows >> 4).astype(np.uint8)
        curved_after = self._row(_to_bgr8(eight_bit, _corrector(2.0)))

        self.assertGreater(len(np.unique(curved_first)), 1.5 * len(np.unique(curved_after)))

    def test_digital_black_keeps_empty_space_black_while_the_picture_lifts(self):
        """A sensor's floor is not zero, and a curve alone lifts it into
        haze. Measured on the slit lamp: floor 7 of 255 at gain 1.0."""
        floor, surface = 112, 272  # 12-bit: the floor, and a backlit surface 10 levels above it
        scene = np.full((H, W), floor, dtype=np.uint16)
        scene[:, W // 2 :] = surface

        hazy = _to_bgr8(_bayer12(scene), _corrector(1.8))
        corrector = _corrector(1.8)
        corrector.SetDigitalBlack(0.025)
        clean = _to_bgr8(_bayer12(scene), corrector)

        empty, lit = (slice(None), slice(8, W // 2 - 8), 1), (slice(None), slice(W // 2 + 8, -8), 1)
        self.assertGreater(float(hazy[empty].mean()), 25)  # the floor, lifted into haze
        self.assertLess(float(clean[empty].mean()), 12)  # ...and put back
        self.assertGreater(float(clean[lit].mean()), 3 * float(clean[empty].mean()) + 10)  # picture survives

    def test_the_result_outlives_every_intermediate_image(self):
        """get_numpy_3D() is a view that does not keep its Image alive;
        reading it late is an access violation, not an exception."""
        import gc

        out = _to_bgr8(_bayer12(self.shadows), _corrector(2.0))
        gc.collect()
        self.assertTrue(out.flags.owndata)
        self.assertGreater(int(out.sum()), 0)


if __name__ == "__main__":
    unittest.main()
