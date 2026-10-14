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



class CurveForTest(unittest.TestCase):
    """The pure decision behind the host curve: what the technician's
    overrides do to it, without a camera."""

    def setUp(self):
        from device_presets import floor_model_for_model

        self.floor = floor_model_for_model("UI325xCP-C")

    def test_the_preset_tolerance_flattens_the_curve_at_gain_three(self):
        from ids_camera import curve_for

        curve = curve_for(1.8, 3.0, self.floor, 4095, tolerance=2.0, subtract_black=True)
        self.assertLess(curve.floor_slope, 1.2)
        self.assertGreater(curve.black[0], 500)

    def test_a_looser_tolerance_lifts_more(self):
        from ids_camera import curve_for

        tight = curve_for(1.8, 3.0, self.floor, 4095, tolerance=2.0)
        loose = curve_for(1.8, 3.0, self.floor, 4095, tolerance=6.0)
        self.assertGreater(loose.floor_slope, 2.5 * tight.floor_slope)

    def test_no_black_subtraction_keeps_the_pedestal_but_neutral(self):
        """The level stays (the three offsets sum to zero) and the tint
        goes (each channel lands on the mean black)."""
        from ids_camera import curve_for

        curve = curve_for(1.8, 3.0, self.floor, 4095, tolerance=2.0, subtract_black=False)
        measured = self.floor.black(3.0, 4095)
        self.assertAlmostEqual(sum(curve.black), 0.0, places=6)
        for offset, black in zip(curve.black, measured):
            self.assertAlmostEqual(black - offset, sum(measured) / 3.0, places=6)
        full = curve_for(1.8, 3.0, self.floor, 4095, tolerance=2.0, subtract_black=True)
        self.assertEqual(full.black, measured)

    def test_low_gain_lifts_even_at_the_preset_tolerance(self):
        from ids_camera import curve_for

        curve = curve_for(1.8, 1.0, self.floor, 4095, tolerance=2.0)
        self.assertGreater(curve.floor_slope, 2.5)



class DenoiseHeadroomTest(unittest.TestCase):
    def test_frame_averaging_lets_the_curve_lift_at_gain_three(self):
        from device_presets import floor_model_for_model
        from ids_camera import curve_for

        floor = floor_model_for_model("UI325xCP-C")
        plain = curve_for(1.8, 3.0, floor, 4095, tolerance=2.0)
        averaged = curve_for(1.8, 3.0, floor, 4095, tolerance=2.0, sigma_scale=0.5)
        self.assertLess(plain.floor_slope, 1.2)
        self.assertGreater(averaged.floor_slope, 1.9)



@unittest.skipIf(ids_peak_ipl is None, "IDS peak bindings not installed")
class GainLatencyHoldTest(unittest.TestCase):
    """A table built at a gain write waits GAIN_LATENCY_FRAMES grabs before
    _grab() uses it, so frames still at the old gain keep the old table;
    an immediate rebuild cancels the wait."""

    def _camera(self):
        camera = IdsCamera(serial="unused")
        camera._host_luts, camera._denoiser = "old", None
        return camera

    def test_held_for_the_measured_latency_then_applied(self):
        from ids_camera import GAIN_LATENCY_FRAMES

        camera = self._camera()
        camera._pending_curve = (("new", None), GAIN_LATENCY_FRAMES)
        seen = [camera._current_host_curve()[0] for _ in range(GAIN_LATENCY_FRAMES + 2)]
        self.assertEqual(seen, ["old"] * GAIN_LATENCY_FRAMES + ["new", "new"])
        self.assertIsNone(camera._pending_curve)

    def test_an_immediate_rebuild_cancels_the_pending_table(self):
        camera = self._camera()
        camera._pending_curve = (("new", None), 2)
        camera._host_processing = False
        camera._set_host_gamma(1.8)  # processing off: disarms now
        self.assertIsNone(camera._pending_curve)
        self.assertEqual(camera._current_host_curve(), (None, None))


class CurveForWhiteBalanceTest(unittest.TestCase):
    """curve_for() with white balance gains: the pedestal stays neutral."""

    def setUp(self):
        from device_presets import FloorModel

        self.floor = FloorModel((0.0497, 0.0375, 0.0527), (-0.0219, -0.0162, -0.0233),
                                (0.00186, 0.00141, 0.00197), (0.00050, 0.00035, 0.00053))

    def test_the_neutral_pedestal_survives_a_balance(self):
        from ids_camera import curve_for

        gains = (1.6, 1.0, 0.7)
        curve = curve_for(1.8, 2.0, self.floor, 4095, tolerance=2.0, subtract_black=False, gains=gains)
        measured = self.floor.black(2.0, 4095)
        levels = [(b - off) * g for b, off, g in zip(measured, curve.black, gains)]
        for level in levels:
            self.assertAlmostEqual(level, sum(measured) / 3.0, places=6)
        self.assertEqual(curve.gains, gains)


class _FakeEnum:
    def __init__(self, value):
        self.value = value
        self.converge_to = None

    def SetCurrentEntry(self, value):
        self.value = value
        if value == "Once" and self.converge_to is not None:
            self.converge_to()
            self.value = "Off"

    def CurrentEntry(self):
        node = self

        class _Entry:
            def SymbolicValue(self):
                return node.value
        return _Entry()


class _FakeRatio:
    def __init__(self, selector, ratios):
        self.selector, self.ratios = selector, ratios

    def IsAvailable(self):
        return True

    def Minimum(self):
        return 0.5

    def Maximum(self):
        return 3.0

    def Value(self):
        return self.ratios[self.selector.value]

    def SetValue(self, value):
        assert 0.5 <= value <= 3.0
        self.ratios[self.selector.value] = value


class _FakeNodeMap:
    def __init__(self, nodes):
        self.nodes = nodes

    def TryFindNode(self, name):
        return self.nodes.get(name)

    def FindNode(self, name):
        return self.nodes[name]


@unittest.skipIf(ids_peak_ipl is None, "IDS peak bindings not installed")
class WhiteBalanceControlsTest(unittest.TestCase):
    """The white balance levers, on a node map shaped like the Keeler's
    (writable ratios beside BalanceWhiteAuto) and like the slit lamp's
    (no white balance nodes, raw 12-bit frames)."""

    def _keeler(self, controls=None):
        camera = IdsCamera(serial="unused", controls=controls)
        selector = _FakeEnum("Red")
        ratios = {"Red": 1.0, "Blue": 1.0}
        auto = _FakeEnum("Continuous")
        auto.converge_to = lambda: ratios.update(Red=1.8, Blue=1.3)
        camera._node_map = _FakeNodeMap({
            "BalanceWhiteAuto": auto, "BalanceRatioSelector": selector,
            "BalanceRatio": _FakeRatio(selector, ratios),
        })
        camera._wb_mode = camera._detect_white_balance()
        return camera, ratios, auto

    def test_the_keeler_writes_its_own_ratios_and_turns_its_auto_off(self):
        camera, ratios, auto = self._keeler()
        self.assertEqual(camera._wb_mode, "device")
        specs = {s.key: s for s in camera.controls()}
        self.assertEqual(set(specs), {"balance_red", "balance_blue"})
        self.assertEqual((specs["balance_red"].minimum, specs["balance_red"].maximum), (0.5, 3.0))
        self.assertEqual(camera.set_control("balance_blue", 9.0), 3.0)  # clamped to the node
        self.assertEqual(ratios["Blue"], 3.0)
        self.assertEqual(auto.value, "Off")

    def test_the_keelers_auto_is_its_own_once_then_read_back(self):
        camera, ratios, _ = self._keeler()
        self.assertEqual(camera.auto_controls("White balance"), {"balance_red": 1.8, "balance_blue": 1.3})
        self.assertEqual(camera._controls, {"balance_red": 1.8, "balance_blue": 1.3})

    def test_saved_ratios_are_what_a_reopened_keeler_holds(self):
        camera, ratios, auto = self._keeler(controls={"balance_red": 2.2, "balance_blue": 0.9})
        camera._write_device_balance(*camera._stored_balance())
        self.assertEqual(ratios, {"Red": 2.2, "Blue": 0.9})
        self.assertEqual(auto.value, "Off")

    def _slit_lamp(self, controls=None):
        camera = IdsCamera(serial="unused", controls=controls)
        camera._node_map = _FakeNodeMap({})
        camera._pixel_format = "BayerRG12"
        camera._model_name = "UI325xCP-C"
        camera._wb_mode = camera._detect_white_balance()
        return camera

    def test_the_slit_lamp_balances_in_the_host_tables(self):
        camera = self._slit_lamp(controls={"balance_red": 1.5})
        self.assertEqual(camera._wb_mode, "host")
        self.assertEqual(camera._host_gains(), (1.5, 1.0, 1.0))
        specs = camera.controls()
        self.assertTrue(all(s.host_processing for s in specs))
        self.assertEqual(camera.set_control("balance_blue", 10.0), 4.0)
        self.assertEqual(camera._host_gains(), (1.5, 1.0, 4.0))

    def test_the_slit_lamps_auto_judges_a_raw_frame(self):
        from device_presets import floor_model_for_model

        camera = self._slit_lamp()
        camera.get_gain = lambda: 1.0
        floor = floor_model_for_model("UI325xCP-C")
        black = floor.black(1.0, 4095)
        raw = np.zeros((80, 80), dtype=np.uint16)
        raw[0::2, 0::2] = int(black[0] + 500)
        raw[0::2, 1::2] = int(black[1] + 1000)
        raw[1::2, 0::2] = int(black[1] + 1000)
        raw[1::2, 1::2] = int(black[2] + 800)

        def deliver():
            camera._raw_snapshot = raw  # what _grab() does on the capture thread
        import threading
        original = camera._judge_host_balance

        def judge():
            # It clears the snapshot and asks for one; deliver after that.
            threading.Timer(0.05, deliver).start()
            return original()
        camera._judge_host_balance = judge
        values = camera.auto_controls("White balance")
        self.assertAlmostEqual(values["balance_red"], 2.0, places=1)
        self.assertAlmostEqual(values["balance_blue"], 1.25, places=1)

    def test_default_puts_the_slit_lamp_back_to_unity(self):
        camera = self._slit_lamp(controls={"balance_red": 1.5, "balance_blue": 0.8})
        camera.reset_controls("White balance")
        self.assertEqual(camera._host_gains(), (1.0, 1.0, 1.0))

    def test_a_camera_with_only_its_own_auto_offers_nothing(self):
        camera = IdsCamera(serial="unused")
        camera._node_map = _FakeNodeMap({"BalanceWhiteAuto": _FakeEnum("Off")})
        camera._pixel_format = "BayerRG8"
        camera._wb_mode = camera._detect_white_balance()
        self.assertIsNone(camera._wb_mode)
        self.assertEqual(camera.controls(), [])

    def test_config_knows_the_same_names(self):
        from config import CONTROL_KEYS

        camera, _, _ = self._keeler()
        self.assertEqual({s.key for s in camera.controls()}, set(CONTROL_KEYS["ids"]))

if __name__ == "__main__":
    unittest.main()
