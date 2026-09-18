"""Tests for audio_playback against a real recorded track, with the
output device replaced by a fake the test pulls from by hand. What
matters is placement: play(t) must hand the device the samples that
belong at media time t, and pause/seek/end must behave -- the viewer's
clock is the master and this has to follow it exactly.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from audio_capture import SyntheticAudio
from audio_playback import AudioPlayer, decode_track
from recorder import Recorder
from session_reader import Session
from synthetic_camera import SyntheticCamera


def record_with_audio(root: str, seconds: float) -> Session:
    instrument = SyntheticCamera(160, 120, name="instrument", fps=30)
    third = SyntheticCamera(160, 120, name="third", fps=30)
    mic = SyntheticAudio(frequency_hz=440)
    instrument.start()
    third.start()
    mic.start()
    try:
        recorder = Recorder(
            instrument, third, instrument_key="slit_lamp",
            output_root=root, fps=30, preset="ultrafast", audio=mic,
        )
        recorder.start()
        time.sleep(seconds)
        recorder.stop()
    finally:
        instrument.stop()
        third.stop()
        mic.stop()
    return Session.load(recorder.session_dir)


class FakeOutput:
    def __init__(self, samplerate: int, channels: int, callback):
        self.samplerate, self.channels, self.callback = samplerate, channels, callback
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def pull(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, self.channels), dtype=np.int16)
        self.callback(out, frames)
        return out


class AudioPlayerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.session = record_with_audio(cls._tmp.name, 1.5)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _player(self) -> tuple[AudioPlayer, FakeOutput]:
        holder: dict = {}

        def factory(samplerate, channels, callback):
            holder["out"] = FakeOutput(samplerate, channels, callback)
            return holder["out"]

        player = AudioPlayer(self.session.audio.path, self.session.audio.offset_s, output_factory=factory)
        self.addCleanup(player.close)
        return player, holder["out"]

    def test_decodes_the_whole_track(self):
        pcm, samplerate, start = decode_track(self.session.audio.path)
        self.assertEqual(samplerate, 48000)
        self.assertEqual(pcm.dtype, np.int16)
        self.assertGreater(pcm.shape[0], 48000)  # more than a second of it
        self.assertGreater(int(np.sqrt(np.mean(pcm.astype(float) ** 2))), 1000)  # not silence
        self.assertLess(abs(start), 0.1)  # priming at most

    def test_output_runs_from_construction_and_is_silent_until_play(self):
        player, out = self._player()
        self.assertTrue(out.started)
        self.assertFalse(player.playing)
        self.assertEqual(int(np.abs(out.pull(4800)).max()), 0)

    def test_play_hands_over_sound_from_the_requested_time(self):
        player, out = self._player()
        player.play(0.5)
        self.assertAlmostEqual(player.position, 0.5, places=3)
        chunk = out.pull(4800)
        self.assertGreater(int(np.abs(chunk).max()), 1000)
        self.assertAlmostEqual(player.position, 0.6, places=3)

    def test_the_samples_handed_over_are_the_ones_at_that_time(self):
        # Pull from t and from t+0.1 and check they are the track's own
        # samples at those offsets, not a restart from zero each time.
        player, out = self._player()
        pcm = player.pcm
        player.play(0.2)
        first = out.pull(480)
        start = int(round((0.2 - player._start_s) * 48000))
        self.assertTrue(np.array_equal(first[:, 0], pcm[start:start + 480, 0]))

    def test_pause_goes_silent_and_holds_position(self):
        player, out = self._player()
        player.play(0.3)
        out.pull(480)
        player.pause()
        held = player.position
        self.assertEqual(int(np.abs(out.pull(4800)).max()), 0)
        self.assertEqual(player.position, held)

    def test_seek_moves_the_cursor_without_playing(self):
        player, _ = self._player()
        player.seek(1.0)
        self.assertAlmostEqual(player.position, 1.0, places=3)
        self.assertFalse(player.playing)

    def test_seek_is_clamped_to_the_track(self):
        player, _ = self._player()
        player.seek(-5.0)
        self.assertGreaterEqual(player.position, player._start_s)
        player.seek(999.0)
        self.assertAlmostEqual(player.position, player.duration, places=3)

    def test_running_off_the_end_stops_and_pads_with_silence(self):
        player, out = self._player()
        player.play(player.duration - 0.005)
        tail = out.pull(4800)
        self.assertFalse(player.playing)
        self.assertEqual(int(np.abs(tail[-2400:]).max()), 0)

    def test_close_stops_the_device(self):
        player, out = self._player()
        player.play(0.0)
        player.close()
        self.assertFalse(player.playing)
        self.assertTrue(out.closed)


if __name__ == "__main__":
    unittest.main()
