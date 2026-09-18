"""Tests for audio_capture: the block/queue/level contract against
SyntheticAudio and hand-pushed blocks. No microphone is opened; the one
test that touches PortAudio only lists devices, and skips if that fails.
"""

from __future__ import annotations

import time
import unittest

import numpy as np

from audio_capture import (
    AudioCapture,
    AudioUnavailable,
    SyntheticAudio,
    list_input_devices,
    resolve_device,
)


def _tone(frames: int, amplitude: float = 0.5, channels: int = 1) -> np.ndarray:
    t = np.arange(frames) / 48000
    mono = (np.sin(2 * np.pi * 440 * t) * amplitude * 32767).astype(np.int16)
    return np.repeat(mono[:, None], channels, axis=1)


class PushedBlocksTest(unittest.TestCase):
    """The capture's own bookkeeping, driven by hand so timestamps and
    counts are exact."""

    def setUp(self):
        self.capture = AudioCapture(device=None)

    def test_blocks_come_out_in_order_with_their_timestamps(self):
        self.capture.push(_tone(960), timestamp=10.0)
        self.capture.push(_tone(960), timestamp=10.02)
        first, second = self.capture.read(), self.capture.read()
        self.assertEqual((first.index, first.timestamp), (0, 10.0))
        self.assertEqual((second.index, second.timestamp), (1, 10.02))
        self.assertEqual(first.frames, 960)
        self.assertIsNone(self.capture.read())

    def test_get_latest_peeks_without_popping(self):
        self.capture.push(_tone(960), timestamp=1.0)
        self.assertEqual(self.capture.get_latest().index, 0)
        self.assertEqual(self.capture.get_latest().index, 0)
        self.assertEqual(self.capture.read().index, 0)

    def test_level_is_zero_for_silence_and_rises_with_amplitude(self):
        self.capture.push(np.zeros((960, 1), dtype=np.int16))
        self.assertEqual(self.capture.level(), 0.0)
        self.capture.push(_tone(960, amplitude=0.1))
        quiet = self.capture.level()
        self.capture.push(_tone(960, amplitude=0.9))
        loud = self.capture.level()
        self.assertGreater(quiet, 0.0)
        self.assertGreater(loud, quiet)
        self.assertLessEqual(loud, 1.0)

    def test_a_full_queue_drops_rather_than_blocks(self):
        from audio_capture import QUEUE_DEPTH

        for _ in range(QUEUE_DEPTH + 5):
            self.capture.push(_tone(960))
        self.assertEqual(self.capture.dropped, 5)
        # The latest slot still moves, like a camera's: a meter keeps working.
        self.assertEqual(self.capture.get_latest().index, QUEUE_DEPTH + 4)

    def test_samples_are_copied_not_aliased(self):
        # PortAudio reuses its buffer between callbacks; a block that
        # aliases it would change under the recorder's feet.
        buffer = _tone(960)
        self.capture.push(buffer)
        buffer[:] = 0
        self.assertGreater(int(np.abs(self.capture.read().samples).max()), 0)


class SyntheticAudioTest(unittest.TestCase):
    def test_delivers_blocks_at_roughly_real_time(self):
        source = SyntheticAudio()
        source.start()
        self.assertTrue(source.running)
        time.sleep(0.5)
        source.stop()
        self.assertFalse(source.running)
        blocks = []
        while (block := source.read()) is not None:
            blocks.append(block)
        # 20 ms blocks for half a second: ~25, with slack for a busy machine.
        self.assertGreater(len(blocks), 12)
        self.assertLess(len(blocks), 40)
        self.assertGreater(source.level(), 0.0)

    def test_timestamps_advance_by_about_a_block(self):
        source = SyntheticAudio()
        source.start()
        time.sleep(0.3)
        source.stop()
        stamps = []
        while (block := source.read()) is not None:
            stamps.append(block.timestamp)
        deltas = np.diff(stamps)
        self.assertGreater(len(deltas), 5)
        self.assertLess(abs(float(np.median(deltas)) - 0.02), 0.01)

    def test_start_and_stop_are_idempotent(self):
        source = SyntheticAudio()
        source.start()
        source.start()
        source.stop()
        source.stop()
        self.assertFalse(source.running)


class DeviceTest(unittest.TestCase):
    def test_lists_inputs_or_skips(self):
        try:
            devices = list_input_devices()
        except Exception as exc:  # noqa: BLE001 -- no PortAudio here
            self.skipTest(f"no audio backend: {exc}")
        for index, name in devices:
            self.assertIsInstance(index, int)
            self.assertTrue(name)

    def test_an_unknown_device_name_is_a_clear_error(self):
        try:
            list_input_devices()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"no audio backend: {exc}")
        with self.assertRaises(AudioUnavailable):
            resolve_device("No Such Microphone 9000")

    def test_none_means_the_default_device(self):
        self.assertIsNone(resolve_device(None))
        self.assertIsNone(resolve_device(""))


if __name__ == "__main__":
    unittest.main()
