"""Rasterize the Reflex mark into the app's .ico files.

    .venv\\Scripts\\python.exe branding\\build_icons.py [--preview DIR]

A developer tool, run by hand whenever the mark or the artwork changes. The
.ico files it writes into assets/ are what gets committed and packaged (see
PACKAGING.md). Needs only PySide6's QtSvg, already in requirements.txt; the
.ico container itself is written with struct. Sizes below 256 are stored as
32bpp DIBs and 256 as PNG -- the layout the earlier icons used, which the
PyInstaller exe resource, Inno Setup's SetupIconFile, and setup_wizard.py's
tkinter iconbitmap() all read.

Supplied artwork wins over the generated placeholder. In
branding/icon-sources/, for an icon named e.g. `reflex`:

- reflex.svg or reflex.png -- a square master (1024px for a PNG), used for
  every size;
- reflex-16.png, reflex-32.png, ... -- exactly that size, replacing just
  that one. For hand-tuning the small sizes, where a downscaled master
  goes soft.

--preview DIR also writes every size as a PNG, for eyeballing.
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

BRANDING_DIR = Path(__file__).resolve().parent
ROOT = BRANDING_DIR.parent
# A script in a subfolder; the project modules it reads live in the root.
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

import neco_reflex_theme as theme  # noqa: E402
from app_icon import ICON_APP, ICON_SETTINGS  # noqa: E402

SIZES = (16, 32, 48, 256)
# Entries this size and up are stored PNG-compressed; smaller ones as DIBs.
PNG_MIN_SIZE = 256
IDLE_MARK = BRANDING_DIR / "mark-idle.svg"
SOURCES_DIR = BRANDING_DIR / "icon-sources"
ASSETS_DIR = ROOT / "assets"
# The tile behind the mark: an .ico has no surface of its own, and a bare
# ring disappears on either a light or a dark taskbar.
TILE_CORNER = 0.18  # corner radius, as a fraction of the icon's width


@dataclass(frozen=True)
class IconSpec:
    filename: str
    tile: str
    mark: str
    edge: str | None = None  # a hairline, so a light tile holds its shape on a light window

    @property
    def stem(self) -> str:
        return Path(self.filename).stem


# One per program, not one for all: see DECISIONS.md's "Three icons, not
# one", and 2026-09-14's "The standalone viewer is gone" for the third.
ICONS = (
    IconSpec(ICON_APP, tile=theme.CHARCOAL, mark=theme.OFF_WHITE),
    IconSpec(ICON_SETTINGS, tile=theme.CHARCOAL, mark=theme.TAUPE),
)


def _canvas(size: int) -> tuple[QImage, QPainter]:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    return image, painter


def _svg(data: bytes, source: Path) -> QSvgRenderer:
    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        raise SystemExit(f"{source}: not a valid SVG")
    return renderer


def _generated(spec: IconSpec, size: int) -> QImage:
    image, painter = _canvas(size)
    tile = QRectF(0, 0, size, size)
    if spec.edge is not None:
        width = max(1.0, size / 128)
        painter.setPen(QPen(QColor(spec.edge), width))
        tile = tile.adjusted(width / 2, width / 2, -width / 2, -width / 2)
    else:
        painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(spec.tile))
    painter.drawRoundedRect(tile, size * TILE_CORNER, size * TILE_CORNER)
    mark = IDLE_MARK.read_text(encoding="utf-8").replace("currentColor", spec.mark)
    _svg(mark.encode("utf-8"), IDLE_MARK).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


def _supplied(spec: IconSpec, size: int) -> QImage | None:
    exact = SOURCES_DIR / f"{spec.stem}-{size}.png"
    if exact.is_file():
        image = QImage(str(exact))
        if image.isNull() or (image.width(), image.height()) != (size, size):
            raise SystemExit(f"{exact}: must be a {size}x{size} PNG")
        return image

    svg = SOURCES_DIR / f"{spec.stem}.svg"
    if svg.is_file():
        image, painter = _canvas(size)
        _svg(svg.read_bytes(), svg).render(painter, QRectF(0, 0, size, size))
        painter.end()
        return image

    png = SOURCES_DIR / f"{spec.stem}.png"
    if png.is_file():
        master = QImage(str(png))
        if master.isNull() or master.width() != master.height():
            raise SystemExit(f"{png}: must be a square PNG")
        return master.scaled(
            size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
    return None


def _dib(image: QImage) -> bytes:
    """A 32bpp DIB, bottom-up, plus the 1bpp AND mask an .ico entry needs.

    Alpha carries the transparency; the mask repeats it for readers that
    ignore alpha.
    """
    # Straight (not premultiplied) alpha, which is BGRA in memory on
    # little-endian -- exactly a DIB's byte order.
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = image.width(), image.height()
    bits = bytes(image.constBits())
    stride = image.bytesPerLine()
    mask_stride = (w + 31) // 32 * 4
    pixels = bytearray()
    mask = bytearray()
    for y in range(h - 1, -1, -1):
        row = bits[y * stride : y * stride + w * 4]
        pixels += row
        mask_row = bytearray(mask_stride)
        for x in range(w):
            if row[x * 4 + 3] == 0:
                mask_row[x >> 3] |= 0x80 >> (x & 7)
        mask += mask_row
    # BITMAPINFOHEADER: the height covers pixels and mask together.
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0)
    return header + bytes(pixels) + bytes(mask)


def _png(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def ico_bytes(images: dict[int, QImage]) -> bytes:
    payloads = [_png(images[size]) if size >= PNG_MIN_SIZE else _dib(images[size]) for size in SIZES]
    offset = 6 + 16 * len(SIZES)
    directory = bytearray()
    for size, payload in zip(SIZES, payloads):
        dim = 0 if size >= 256 else size  # an ICONDIRENTRY writes 256 as 0
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(payload), offset)
        offset += len(payload)
    return struct.pack("<HHH", 0, 1, len(SIZES)) + bytes(directory) + b"".join(payloads)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preview", type=Path, help="also write every size as a PNG into this folder")
    args = parser.parse_args()

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841 - QtSvg needs one

    for spec in ICONS:
        images: dict[int, QImage] = {}
        origins = []
        for size in SIZES:
            supplied = _supplied(spec, size)
            images[size] = supplied if supplied is not None else _generated(spec, size)
            origins.append(f"{size}px {'supplied' if supplied is not None else 'generated'}")
        target = ASSETS_DIR / spec.filename
        target.write_bytes(ico_bytes(images))
        print(f"{target.relative_to(ROOT)}: {', '.join(origins)}")

        if args.preview is not None:
            args.preview.mkdir(parents=True, exist_ok=True)
            for size, image in images.items():
                image.save(str(args.preview / f"{spec.stem}-{size}.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
