"""Sweep one picture lever on an attached IDS camera and report what it
did to the frame, so a preset is chosen from numbers and pictures rather
than from a guess.

    python tools/measure_picture.py <serial> gain   4 6 8 10 14 20
    python tools/measure_picture.py <serial> gamma  1.0 1.4 1.8 2.2 2.6
    python tools/measure_picture.py <serial> gamma  1.0 1.5 2.0 --pixel-format BayerRG12

Get the serial from tools/check_ids.py. `gamma` goes wherever this camera
takes it -- its own Gamma node (the Keeler) or the host-side curve in
ids_camera._to_bgr8() (the slit lamp) -- through IdsCamera's own open
path, so what is measured is what the app would record. Pass
--pixel-format to measure a host curve at the depth it would ship with.

Starts from config.json's calibration for that serial when there is one,
else from whatever the camera holds. Writes nothing to config.json, and
puts exposure and gain back when it finishes. Close Settings and the
kiosk first: only one process can hold the camera.

Per step: p50/p90/p95/p99/p99.9, the fraction of the frame that is pure
black and the fraction clipped (>=250), plus a PNG under --out (default
./picture_sweep) -- the numbers say whether the shadows came up, only a
person can say whether they came up as picture or as noise.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ConfigError, load_config  # noqa: E402
from ids_camera import IdsCamera, IdsCameraNotFoundError  # noqa: E402

SETTLE_S = 0.4


def fresh_frame(camera: IdsCamera) -> np.ndarray:
    """A frame captured under the current settings, not one queued before
    they changed -- same reasoning as IdsCamera._wait_for_fresh_frame()."""
    time.sleep(SETTLE_S)
    while camera.read(timeout=0) is not None:
        pass
    frame = None
    for _ in range(3):
        frame = camera.read(timeout=3.0) or frame
    if frame is None:
        raise SystemExit("no frame arrived")
    return frame.image


def describe(image: np.ndarray) -> str:
    brightest = image.max(axis=2)
    p = np.percentile(image, [50, 90, 95, 99, 99.9])
    return (
        f"p50 {p[0]:3.0f}  p90 {p[1]:3.0f}  p95 {p[2]:3.0f}  p99 {p[3]:3.0f}  p99.9 {p[4]:3.0f}  "
        f"black {100 * (brightest == 0).mean():5.1f}%  clipped {100 * (brightest >= 250).mean():5.2f}%"
    )


def saved_calibration(serial: str) -> tuple[float | None, float | None]:
    try:
        cfg = load_config()
    except ConfigError:
        return None, None
    for instrument in cfg.instruments.values():
        if getattr(instrument, "serial", None) == serial:
            return instrument.exposure_time_us, instrument.gain
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("serial")
    parser.add_argument("lever", choices=("gain", "gamma"))
    parser.add_argument("values", type=float, nargs="+")
    parser.add_argument("--pixel-format", help="e.g. BayerRG12; default is the profile's, else the camera's own")
    parser.add_argument("--out", type=Path, default=Path("picture_sweep"))
    parser.add_argument(
        "--calibrate", action="store_true",
        help="run Auto-Calibrate once against this scene, with no curve, and hold its result for every step",
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    exposure, gain = saved_calibration(args.serial)
    if args.calibrate:
        # With no curve, deliberately: a curve changes what the metering
        # sees, and the sweep must vary one thing.
        # target_fps=None, as Settings' Preview opens it: on the uEye
        # transport a 30fps cap lengthens ExposureTime's maximum past what
        # the next open (which sets exposure before the cap) can accept, so
        # a capped calibration reached 30.0ms and every step then clamped
        # it to 26.3ms. auto_calibrate() still gets the 30fps budget.
        camera = IdsCamera(serial=args.serial, target_fps=None, converge_auto=False,
                           pixel_format=args.pixel_format, gamma=1.0)
        try:
            camera.start()
        except IdsCameraNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        try:
            converged = camera.auto_calibrate(target_fps=30)
            exposure, gain = camera.get_exposure_time_us(), camera.get_gain()
        finally:
            camera.stop()
        print(f"serial {args.serial}: calibrated ({'converged' if converged else 'NOT converged'}) "
              f"to {exposure / 1000:.1f}ms, {gain:.2f}x")
    else:
        print(f"serial {args.serial}: starting from "
              + (f"config.json ({exposure / 1000:.1f}ms, {gain:.2f}x)" if exposure and gain else "the camera's own values"))

    original: tuple[float, float] | None = None
    for value in args.values:
        # gamma is resolved at open (and the host curve with it), so each
        # gamma step is its own open; gain is a live write on one open.
        camera = IdsCamera(
            serial=args.serial, exposure_time_us=exposure, gain=gain, target_fps=30, converge_auto=False,
            pixel_format=args.pixel_format, gamma=value if args.lever == "gamma" else None,
        )
        try:
            camera.start()
        except IdsCameraNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        try:
            if original is None:
                original = (camera.get_exposure_time_us(), camera.get_gain())
            if args.lever == "gain":
                low, high = camera.gain_range()
                camera.set_gain(min(high, max(low, value)))
            image = fresh_frame(camera)
            print(f"  {args.lever} {value:5.2f}: {describe(image)}")
            height, width = image.shape[:2]
            small = cv2.resize(image, (1024, round(height * 1024 / width)), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(args.out / f"{args.serial}_{args.lever}_{value:05.2f}.png"), small)
            if args.lever == "gain":
                camera.set_gain(original[1])
        finally:
            camera.stop()
    print(f"frames in {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
