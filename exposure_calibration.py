"""Pure math behind IdsCamera.auto_calibrate() --
brightness measurement and the exposure/gain correction steps -- split
out from ids_camera.py so it's
unit-testable without the IDS peak SDK (this dev machine has no ids_peak
installed; see CLAUDE.md's Environment section).

See DECISIONS.md's 2026-08-25 calibration entry, and its 2026-09-11
"Retired ROADMAP entries" entry for the calibration UX, for the design: a
one-shot software auto-exposure for cameras with no ExposureAuto/GainAuto
(the slit lamp camera). Raises ExposureTime before Gain when more
brightness is needed -- Gain amplifies sensor noise, ExposureTime doesn't,
and this footage gets reviewed by students studying their own technique.

See DECISIONS.md's 2026-08-26 entry for center_crop() (vignette/center-
weighted metering), added for the same reason as the exposure/gain
algorithm above: no ExposureAuto/GainAuto on the slit lamp means no
device-side auto-convergence to fall back on.
"""

from __future__ import annotations

import numpy as np

DEFAULT_TARGET_MEDIAN = 128.0
DEFAULT_TOLERANCE = 10.0
DEFAULT_MAX_ITERATIONS = 8

# Slit-lamp/BIO video coupled through an eyepiece/beam-splitter commonly
# shows a circular illuminated field surrounded by true black -- unconfirmed
# against real footage from either camera, but if true, a whole-frame median
# is skewed dark by that surround and auto_calibrate() would over-correct
# to compensate. A fixed centered crop is also just
# ordinary center-weighted metering practice regardless of whether a vignette
# is actually present, so it's a safe default either way. 0.5 is a starting
# guess, not a measurement -- revisit once real footage is available.
DEFAULT_METERING_FRACTION = 0.5

# A sensor cannot expose for the whole frame interval -- readout and
# transfer need part of it -- so the usable budget is a fraction of the
# nominal period rather than all of it.
DEFAULT_EXPOSURE_BUDGET_FRACTION = 0.9

# --- Metering modes -------------------------------------------------------
#
# How a frame's brightness is measured for calibration. The two instrument
# cameras look through eyepiece optics at a bright region on an otherwise
# black field -- a slit beam, or a fundus reflex. Such a frame is ~98%
# black *by design*, so its median can never reach a mid-grey target no
# matter the exposure: measured on the real slit lamp, the centre-crop
# median was 0.0 at the sensor's maximum exposure. Steering on it drives
# exposure and gain to their ceilings and destroys the very content the
# recording exists to show. Metering the highlight instead asks the
# question that actually matters: is the beam bright but not clipped?
METERING_MEDIAN = "median"
METERING_HIGHLIGHT = "highlight"
# The BIO's view is the opposite shape from the slit lamp's: a lit disc
# covering roughly half the frame, whose brightest 0.1% is specular
# sparkle (the corneal reflex on an eye) far brighter than the field
# itself. Metering that sparkle at 210 left the field at a median of 27
# with 23% of the frame pure black and called it calibrated -- measured
# 2026-09-14, see DECISIONS.md. The field rule meters the bright end of
# the field instead: a percentile a speck cannot reach.
METERING_FIELD = "field"
VALID_METERING = (METERING_MEDIAN, METERING_HIGHLIGHT, METERING_FIELD)

# Which percentile counts as "the highlight", and where to put it. Chosen
# from a real exposure sweep of the slit lamp beam on a black focus rod
# (see DECISIONS.md): p99.9 tracked the beam's bright core across the whole
# usable range, while clipping stayed under 0.02% up to ~35ms. A target of
# 210 leaves clear headroom under 255 so the beam keeps its internal
# structure; the tolerance is wide enough that the sweep's 30ms/gain-1.0
# point (p99.9 = 191) already counts as converged.
DEFAULT_HIGHLIGHT_PERCENTILE = 99.9
DEFAULT_HIGHLIGHT_TARGET = 210.0
DEFAULT_HIGHLIGHT_TOLERANCE = 20.0

# The field rule's numbers, from a gain sweep of the Keeler at the 30fps
# budget exposure (2026-09-14): p95 tracked the field's brightness across
# the whole range (52 -> 248) while p99 and p99.9 were pinned at 255 by
# gain 8-12; by eye the field was right at gain 14 (p95 184) and blowing
# out by 20 (p95 248). PROVISIONAL: measured on a glossy box, not a
# fundus. Confirm against a model eye and update DECISIONS.md.
DEFAULT_FIELD_PERCENTILE = 95.0
DEFAULT_FIELD_TARGET = 185.0
DEFAULT_FIELD_TOLERANCE = 25.0

# At or above this level the metric is *censored*: a clipped highlight says
# we are over, but not by how much, so a proportional correction crawls
# (measured on the real BIO: eight proportional steps from a 6.6%-clipped
# frame still had not converged). Halving instead re-measures somewhere
# informative within a couple of steps.
DEFAULT_SATURATION_LEVEL = 250.0
DEFAULT_SATURATED_STEP = 0.5



def center_crop(image: np.ndarray, fraction: float = DEFAULT_METERING_FRACTION) -> np.ndarray:
    """Crop to a centered region covering `fraction` of both width and
    height. `fraction=1.0` returns the image unchanged (shape-wise) -- an
    explicit "no crop" case, not a special-cased no-op.
    """
    if not 0 < fraction <= 1.0:
        raise ValueError(f"fraction must be in (0, 1], got {fraction!r}")
    height, width = image.shape[:2]
    crop_h = max(1, round(height * fraction))
    crop_w = max(1, round(width * fraction))
    top = (height - crop_h) // 2
    left = (width - crop_w) // 2
    return image[top : top + crop_h, left : left + crop_w]


def median_brightness(image: np.ndarray) -> float:
    """Median pixel value across all channels, 0-255 -- robust against a
    bright reflection or dark surround skewing a plain mean, unlike a mean.
    """
    return float(np.median(image))


def is_converged(
    median: float, target: float = DEFAULT_TARGET_MEDIAN, tolerance: float = DEFAULT_TOLERANCE
) -> bool:
    return abs(median - target) <= tolerance


def exposure_budget_us(
    target_fps: float, fraction: float = DEFAULT_EXPOSURE_BUDGET_FRACTION
) -> float:
    """The longest exposure that still leaves room for `target_fps`.

    Exposure time is a frame-rate budget, not a free parameter: a sensor
    exposing for E microseconds cannot deliver frames faster than 1/E.
    Anything above this ceiling costs frame rate *and* smears motion --
    the exact motion this app exists to record. That makes it a
    constraint rather than a preference, which is why it lives here and
    not in a settings dialog. See CLAUDE.md's "Camera configuration: who
    decides what".
    """
    if target_fps <= 0:
        raise ValueError(f"target_fps must be positive, got {target_fps!r}")
    return (1_000_000.0 / target_fps) * fraction


def highlight_brightness(image, percentile: float = DEFAULT_HIGHLIGHT_PERCENTILE) -> float:
    """Brightness of the image's brightest content -- the slit beam, the
    fundus reflex -- rather than of the frame as a whole."""
    return float(np.percentile(image, percentile))


def metering_brightness(
    image,
    mode: str = METERING_MEDIAN,
    fraction: float = DEFAULT_METERING_FRACTION,
    percentile: float | None = None,
) -> float:
    """The number a calibration steers on, for the given metering mode.

    METERING_MEDIAN crops to the centre first, because a vignetted rim
    would drag a whole-frame average down (see center_crop).
    METERING_HIGHLIGHT deliberately does *not*: the examiner moves the
    beam around the field, and metering a centre crop that happens to miss
    it would read the black background and then drive exposure up until
    the off-centre beam was destroyed. A stray highlight at the rim only
    costs a little underexposure, which is recoverable; blowing out the
    beam is not.
    METERING_FIELD is the highlight rule at a percentile deep enough into
    the lit field that a specular point cannot pin it. `percentile`
    overrides either mode's default.
    """
    if mode == METERING_HIGHLIGHT:
        return highlight_brightness(image, DEFAULT_HIGHLIGHT_PERCENTILE if percentile is None else percentile)
    if mode == METERING_FIELD:
        return highlight_brightness(image, DEFAULT_FIELD_PERCENTILE if percentile is None else percentile)
    return median_brightness(center_crop(image, fraction))


def metering_target(mode: str = METERING_MEDIAN) -> tuple[float, float]:
    """(target, tolerance) that go with a metering mode."""
    if mode == METERING_HIGHLIGHT:
        return DEFAULT_HIGHLIGHT_TARGET, DEFAULT_HIGHLIGHT_TOLERANCE
    if mode == METERING_FIELD:
        return DEFAULT_FIELD_TARGET, DEFAULT_FIELD_TOLERANCE
    return DEFAULT_TARGET_MEDIAN, DEFAULT_TOLERANCE


def next_exposure_gain(
    measured: float,
    exposure_time_us: float,
    exposure_range_us: tuple[float, float],
    gain: float,
    gain_range: tuple[float, float],
    target: float = DEFAULT_TARGET_MEDIAN,
    max_exposure_us: float | None = None,
    saturated_level: float = DEFAULT_SATURATION_LEVEL,
    saturated_step: float = DEFAULT_SATURATED_STEP,
) -> tuple[float, float]:
    """One correction step toward `target`, from a `measured` brightness
    (whatever metering_brightness() returned).

    Works in total light -- exposure x gain -- then redistributes it with
    one fixed preference: use as much exposure as the frame budget allows,
    and only then gain. Gain is the noise source, so the least of it that
    will do is the right amount; and because `max_exposure_us` already
    caps exposure at roughly one frame interval, preferring exposure
    cannot run away into motion blur.

    `max_exposure_us` (from exposure_budget_us()) tightens that clamp to
    the frame-rate budget rather than the sensor's own maximum. Without
    it this function will happily spend the whole frame interval to avoid
    a little gain, which is correct for stills and wrong for video: it
    produced an 87ms exposure on the slit lamp, capping it at ~11fps
    against a 30fps target and putting 87ms of motion blur on every
    frame, while 4x of gain headroom sat unused. See DECISIONS.md's
    "Camera configuration: which layer owns what" entry.
    """
    exposure_min, exposure_max = exposure_range_us
    gain_min, gain_max = gain_range

    if max_exposure_us is not None:
        # Never below what the sensor can physically do: a frame-rate
        # target this camera cannot meet is a reason to warn (see
        # config.exposure_fps_warnings), not a reason to ask it for an
        # impossible exposure.
        exposure_max = max(exposure_min, min(exposure_max, max_exposure_us))

    if measured >= saturated_level:
        ratio = saturated_step
    else:
        ratio = target / max(measured, 1.0)

    # Solve in total light, then redistribute under the constraints. Doing
    # it this way is what makes the preference below hold in *both*
    # directions -- the earlier per-axis version only reduced gain once
    # exposure had bottomed out, so cutting brightness left gain (and its
    # noise) high. Measured on the real BIO: it walked exposure down from
    # 49.9ms to 7.7ms while pushing gain *up* from 15.5x to 21.3x.
    desired_light = exposure_time_us * gain * ratio
    new_exposure = min(exposure_max, max(exposure_min, desired_light / gain_min))
    new_gain = min(gain_max, max(gain_min, desired_light / new_exposure))
    return new_exposure, new_gain
