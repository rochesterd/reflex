"""The committed .ico files in assets/: the entry layout
branding/build_icons.py writes, that every entry decodes, and that they
stay distinguishable from each other (DECISIONS.md's "Three icons, not
one"). Checks the files themselves rather than the script, so it holds for
supplied artwork too.
"""

from __future__ import annotations

import struct
import unittest

from PySide6.QtGui import QImage

from app_icon import ICON_APP, ICON_SETTINGS, icon_path

ICONS = (ICON_APP, ICON_SETTINGS)
EXPECTED_SIZES = (16, 32, 48, 256)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _entries(name: str) -> dict[int, tuple[int, bytes]]:
    """{size: (bits per pixel, payload)} for each image in the .ico."""
    data = icon_path(name).read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if (reserved, kind) != (0, 1):
        raise AssertionError(f"{name}: not an .ico header")
    entries = {}
    for i in range(count):
        w, h, _colors, _reserved, _planes, bpp, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        if (w or 256) != (h or 256):
            raise AssertionError(f"{name}: entry {i} is not square")
        entries[w or 256] = (bpp, data[offset : offset + size])
    return entries


def _dib_pixels(payload: bytes, size: int) -> bytes:
    return payload[40 : 40 + size * size * 4]


class TestIcons(unittest.TestCase):
    def test_each_icon_has_the_expected_sizes_at_32bpp(self):
        for name in ICONS:
            with self.subTest(name):
                entries = _entries(name)
                self.assertEqual(tuple(sorted(entries)), EXPECTED_SIZES)
                self.assertTrue(all(bpp == 32 for bpp, _ in entries.values()))

    def test_256_is_png_and_the_rest_are_dibs(self):
        for name in ICONS:
            for size, (_bpp, payload) in _entries(name).items():
                with self.subTest(name=name, size=size):
                    if size == 256:
                        self.assertTrue(payload.startswith(PNG_SIGNATURE))
                        image = QImage.fromData(payload, "PNG")
                        self.assertEqual((image.width(), image.height()), (256, 256))
                    else:
                        header_size, width, height, _planes, bpp = struct.unpack_from("<IiiHH", payload, 0)
                        self.assertEqual((header_size, width, height, bpp), (40, size, size * 2, 32))
                        mask = (size + 31) // 32 * 4 * size
                        self.assertEqual(len(payload), 40 + size * size * 4 + mask)

    def test_small_sizes_are_not_blank(self):
        for name in ICONS:
            for size in (16, 32, 48):
                with self.subTest(name=name, size=size):
                    pixels = _dib_pixels(_entries(name)[size][1], size)
                    alphas = pixels[3::4]
                    self.assertGreater(sum(a == 255 for a in alphas), len(alphas) // 2)

    def test_the_icons_differ(self):
        for size in (16, 32):
            pixels = {name: _dib_pixels(_entries(name)[size][1], size) for name in ICONS}
            for i, a in enumerate(ICONS):
                for b in ICONS[i + 1 :]:
                    with self.subTest(size=size, pair=(a, b)):
                        self.assertNotEqual(pixels[a], pixels[b])


if __name__ == "__main__":
    unittest.main()
