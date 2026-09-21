"""Tests for uvc_enumeration.list_uvc_devices(). _parse_vid_pid is pure
string parsing, tested directly; list_uvc_devices() itself talks to real
DirectShow COM objects, so it's tested only for well-formedness -- 0
devices is the expected, correct result on a dev machine with none
attached (see SETUP.md's identical precedent for ids_peak), so this can't
assert a specific device is present.
"""

from __future__ import annotations

import unittest

from uvc_enumeration import UvcDeviceInfo, UvcDeviceResolutionError, _parse_vid_pid, list_uvc_devices, resolve_device


class ParseVidPidTest(unittest.TestCase):
    def test_parses_real_device_path(self):
        path = r"\\?\usb#vid_32e4&pid_9310&mi_00#6&3c4e0f5&0&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
        self.assertEqual(_parse_vid_pid(path), "32E4:9310")

    def test_uppercase_input_normalizes_the_same(self):
        path = r"\\?\usb#VID_32E4&PID_9310&MI_00#..."
        self.assertEqual(_parse_vid_pid(path), "32E4:9310")

    def test_none_input_returns_none(self):
        self.assertIsNone(_parse_vid_pid(None))

    def test_path_without_vid_pid_returns_none(self):
        self.assertIsNone(_parse_vid_pid(r"@device:pnp:\\?\some-non-usb-path"))


class ListUvcDevicesTest(unittest.TestCase):
    def test_returns_a_list_of_well_formed_entries(self):
        devices = list_uvc_devices()

        self.assertIsInstance(devices, list)
        for i, device in enumerate(devices):
            self.assertIsInstance(device, UvcDeviceInfo)
            self.assertEqual(device.index, i)
            self.assertIsInstance(device.name, str)
            self.assertTrue(device.vid_pid is None or isinstance(device.vid_pid, str))

    def test_indices_match_cv2_cap_dshow_open_order(self):
        # Regression guard for the whole point of this module: index N here
        # must be openable as index N via cv2.VideoCapture(N, cv2.CAP_DSHOW).
        # Skipped with no devices attached rather than failing, since "0
        # found" is the expected dev-machine state (see module docstring).
        devices = list_uvc_devices()
        if not devices:
            self.skipTest("no UVC devices attached to this machine")

        import cv2

        # A registered virtual camera (stream mode's Unity Capture filter,
        # or OBS's) is a DirectShow device with nothing behind it until
        # its producer runs, so it lists but will not open. It still holds
        # an index in the open order, which is what this test is about.
        virtual = ("unity video capture", "obs virtual camera", "obs-camera")
        for device in devices:
            cap = cv2.VideoCapture(device.index, cv2.CAP_DSHOW)
            try:
                if not cap.isOpened() and device.name.lower() in virtual:
                    continue
                self.assertTrue(cap.isOpened(), f"index {device.index} ({device.name!r}) did not open")
            finally:
                cap.release()


class ResolveDeviceTest(unittest.TestCase):
    def test_no_devices_raises(self):
        with self.assertRaises(UvcDeviceResolutionError):
            resolve_device(None, devices=[])

    def test_single_device_wins_regardless_of_configured_vid_pid(self):
        only = UvcDeviceInfo(index=0, name="Solo Cam", vid_pid="1111:2222")

        self.assertIs(resolve_device(None, devices=[only]), only)
        self.assertIs(resolve_device("9999:9999", devices=[only]), only)

    def test_multiple_devices_with_no_configured_vid_pid_raises(self):
        devices = [
            UvcDeviceInfo(index=0, name="Cam A", vid_pid="1111:2222"),
            UvcDeviceInfo(index=1, name="Cam B", vid_pid="3333:4444"),
        ]
        with self.assertRaises(UvcDeviceResolutionError):
            resolve_device(None, devices=devices)

    def test_multiple_devices_with_matching_vid_pid_resolves(self):
        cam_a = UvcDeviceInfo(index=0, name="Cam A", vid_pid="1111:2222")
        cam_b = UvcDeviceInfo(index=1, name="Cam B", vid_pid="3333:4444")

        self.assertIs(resolve_device("3333:4444", devices=[cam_a, cam_b]), cam_b)

    def test_multiple_devices_with_non_matching_vid_pid_raises(self):
        devices = [
            UvcDeviceInfo(index=0, name="Cam A", vid_pid="1111:2222"),
            UvcDeviceInfo(index=1, name="Cam B", vid_pid="3333:4444"),
        ]
        with self.assertRaises(UvcDeviceResolutionError):
            resolve_device("9999:9999", devices=devices)


if __name__ == "__main__":
    unittest.main()
