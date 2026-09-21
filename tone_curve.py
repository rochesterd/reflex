"""A tone curve with a soft toe, applied to raw Bayer data as a lookup table.

The maths behind the slit lamp's host curve (DECISIONS.md 2026-09-21).
Pure numpy: no camera, no IDS SDK, so it is testable on a synthetic ramp
and usable by tools/ without hardware.

**Why a toe.** A bare power curve has infinite slope at zero: it stretches
the darkest levels the most, and the darkest levels of a starved sensor
are read noise, not picture. The curve shipped on 2026-09-17 was a bare
gamma 1.8 on 12-bit data with the floor subtracted first, so its steepest
stretch landed on the noise floor -- which showed as flickering coloured
lines on any dark background.

**The shape.** Per channel, subtract that channel's black (the floor is
not neutral; one master value is what left the residue purple), then map
normalised signal x in [0, 1] through the *offset* gamma

    y = ((x + c)^(1/g) - c^(1/g)) / ((1 + c)^(1/g) - c^(1/g))

This is sRGB's construction. It is smooth, lands 0 on 0 and 1 on 1, and
its slope at black is finite and set by c. `floor_slope` is that slope:
the factor by which noise sitting on the floor is allowed to grow. c is
solved for it. A bare gamma is the limit c -> 0 (infinite slope); a
straight line is c -> infinity (slope 1). A simple linear toe joined to
the bare curve was tried first and rejected: to meet the curve's height
at the join its slope is toe^(1/g - 1), 5x at these numbers -- it caps
the infinity and still amplifies.

**Applied as a LUT.** 12-bit in, 8-bit out, one 4096-entry table per
Bayer channel, indexed by pixel position (GenICam BayerRG: R at (0,0),
G at (0,1)/(1,0), B at (1,1)). A lookup per pixel and a demosaic is a
few milliseconds a frame at 1600x1200.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# OpenCV names its Bayer codes by the *second* row/column, so a GenICam
# BayerRG sensor demosaics with the BG code. Confirmed against IDS's own
# BGR8 conversion on the slit lamp, 2026-09-21: channel means within one
# level; the RG code swaps red and blue.
BAYER_RG_TO_BGR = cv2.COLOR_BayerBG2BGR

# The largest floor slope worth asking for: beyond it the offset is tiny
# and the curve is the bare gamma for all practical purposes.
MAX_FLOOR_SLOPE = 64.0


@dataclass(frozen=True)
class ToneCurve:
    """The curve's parameters, in the raw data's own units."""

    gamma: float  # >1 lifts shadows; 1.0 is straight
    black: tuple[float, float, float]  # per channel R, G, B, raw units
    floor_slope: float = 2.0  # slope at black; 1.0 is no lift there at all
    bits: int = 12

    @property
    def full_scale(self) -> int:
        return (1 << self.bits) - 1


def offset_for_slope(gamma: float, floor_slope: float) -> float:
    """The c that gives the offset gamma the wanted slope at zero.

    slope(c) = (1/g) c^(1/g - 1) / ((1 + c)^(1/g) - c^(1/g)) is monotonic
    decreasing in c, from infinity at c = 0 to 1 as c grows, so a
    bisection finds it. Slopes at or below 1 mean "straight line".
    """
    if gamma <= 1.0 or floor_slope <= 1.0:
        return float("inf")
    floor_slope = min(floor_slope, MAX_FLOOR_SLOPE)
    lo, hi = 1e-9, 1e6
    for _ in range(200):
        mid = np.sqrt(lo * hi)  # geometric: c spans many decades
        if _slope_at_zero(gamma, mid) > floor_slope:
            lo = mid
        else:
            hi = mid
    return float(np.sqrt(lo * hi))


def _slope_at_zero(gamma: float, c: float) -> float:
    inv = 1.0 / gamma
    return inv * c ** (inv - 1.0) / ((1.0 + c) ** inv - c**inv)


def curve_values(x: np.ndarray, gamma: float, floor_slope: float) -> np.ndarray:
    """The normalised curve on x in [0, 1]."""
    if gamma <= 1.0 or floor_slope <= 1.0:
        return x.astype(np.float64)
    c = offset_for_slope(gamma, floor_slope)
    if not np.isfinite(c):
        return x.astype(np.float64)
    inv = 1.0 / gamma
    base = c**inv
    return ((x + c) ** inv - base) / ((1.0 + c) ** inv - base)


def build_luts(curve: ToneCurve) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One uint8 lookup table per channel (R, G, B), raw code in, 8-bit out."""
    return tuple(_lut_for(curve, black) for black in curve.black)  # type: ignore[return-value]


def _lut_for(curve: ToneCurve, black: float) -> np.ndarray:
    codes = np.arange(curve.full_scale + 1, dtype=np.float64)
    span = max(1.0, curve.full_scale - black)
    x = np.clip((codes - black) / span, 0.0, 1.0)
    y = curve_values(x, curve.gamma, curve.floor_slope)
    return np.clip(np.round(y * 255.0), 0, 255).astype(np.uint8)


def apply_bayer(raw: np.ndarray, luts: tuple[np.ndarray, np.ndarray, np.ndarray]) -> np.ndarray:
    """Map a raw BayerRG frame (uint16, HxW) to 8-bit Bayer through the
    per-channel tables. The output is still Bayer -- demosaic() follows."""
    r_lut, g_lut, b_lut = luts
    out = np.empty(raw.shape, dtype=np.uint8)
    out[0::2, 0::2] = r_lut[raw[0::2, 0::2]]
    out[0::2, 1::2] = g_lut[raw[0::2, 1::2]]
    out[1::2, 0::2] = g_lut[raw[1::2, 0::2]]
    out[1::2, 1::2] = b_lut[raw[1::2, 1::2]]
    return out


def demosaic(bayer8: np.ndarray) -> np.ndarray:
    """8-bit BayerRG to BGR8, matching IDS's conversion."""
    return cv2.cvtColor(bayer8, BAYER_RG_TO_BGR)


def raw_to_bgr8(raw: np.ndarray, luts: tuple[np.ndarray, np.ndarray, np.ndarray]) -> np.ndarray:
    return demosaic(apply_bayer(raw, luts))


def max_gain_for_noise(
    sigma_slope: float,
    sigma_intercept: float,
    black_slope: float,
    black_intercept: float,
    max_output_sigma: float,
    gain_limit: float,
) -> float:
    """The gain above which even a straight line shows more than
    `max_output_sigma` levels of floor noise -- Auto-Calibrate's ceiling.

    Both sigma and black are lines in gain as fractions of full scale, so
    output sigma through a straight line is
        255 * (ks g + cs) / (1 - (kb g + cb))
    and the ceiling solves that equal to the limit. No solution below
    `gain_limit` means the sensor is quiet enough everywhere.
    """
    m = max_output_sigma / 255.0
    denominator = sigma_slope + m * black_slope
    if denominator <= 0.0:
        return gain_limit
    gain = (m * (1.0 - black_intercept) - sigma_intercept) / denominator
    return float(max(1.0, min(gain_limit, gain)))


def floor_slope_for_noise(sigma_raw: float, black: float, full_scale: int, max_output_sigma: float) -> float:
    """The floor slope that keeps the floor's noise at or under
    `max_output_sigma` 8-bit levels: how much lift the floor can take
    before its noise shows. Never below 1.0 -- a curve cannot usefully
    compress the floor; if even a straight line is too noisy, the answer
    is less gain, which is Auto-Calibrate's ceiling to enforce."""
    straight = sigma_raw / max(1.0, full_scale - black) * 255.0
    if straight <= 0.0:
        return MAX_FLOOR_SLOPE
    return float(max(1.0, min(MAX_FLOOR_SLOPE, max_output_sigma / straight)))
