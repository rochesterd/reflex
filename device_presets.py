"""Per-model camera facts that can't be read from the device itself and
don't vary between installs -- so the program just knows them, rather than
a technician having to discover and configure them.

A `DeviceProfile` is one supported instrument camera: how to recognise it,
what to call it, and the corrections it needs. `PROFILES` is the whole
supported list, and `SUPPORTED_HARDWARE.md` is its prose counterpart -- add
to both together.

Two ways in, for two different questions:

  profiles_for_role()/profile_for_id()  what settings.py offers a
                                        technician, and what a saved
                                        `config.json` profile id resolves to
  orientation_for_model()/              what IdsCamera._open() applies when
  pixel_clock_hz_for_model()            nothing in config.json says otherwise

Both read the same table, so a preset and the profile a technician picked
cannot disagree. A `config.json` `orientation`/`pixel_clock_hz` value still
overrides either (see config.py / app.py), as the escape hatch for a
non-standard mounting or a host that can't take the full clock.

CUSTOM_PROFILE_ID is deliberately not in PROFILES: "custom" means the
technician supplies what a profile would have, so an unlisted but working
camera is never locked out and a new instrument never waits on a code
change. A config.json written before profiles existed -- a typed label and
no profile id -- *is* a custom entry, which is why that shape stays valid.

Lightweight on purpose -- only imports camera.py and exposure_calibration.py
(stdlib + numpy) for their constants, no Qt / IDS SDK, so it stays
importable anywhere ids_camera.py is. See DECISIONS.md's "Device-model rotation presets" entry
and its orientation-correction follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass

from camera import ORIENTATION_FLIP_VERTICAL, ORIENTATION_NONE, ORIENTATION_ROTATE_180
from exposure_calibration import METERING_FIELD, METERING_HIGHLIGHT

CUSTOM_PROFILE_ID = "custom"


@dataclass(frozen=True)
class DeviceProfile:
    """One supported instrument camera.

    `id` is written to config.json and must never change once shipped --
    it is what a saved configuration resolves through. `name` is free to be
    reworded; it is display text, and it is the label students see on the
    picker unless a technician sets a nickname.
    """

    id: str
    name: str
    # What the kiosk's picker button says by default. The full `name` is for
    # a technician choosing from a list; a student needs the short word they
    # already use for the instrument in front of them. A nickname set in
    # settings.py replaces it.
    picker_label: str
    kind: str  # "ids" / "net2860_winusb" -- which BaseCamera subclass this becomes
    roles: tuple[str, ...]  # instrument roles this camera can fill
    # Matched case-insensitively as substrings of ids_peak's ModelName().
    # Empty for a camera with no model string to match on (the legacy BIO),
    # which is identified by being present at all.
    model_tokens: tuple[str, ...] = ()
    orientation: str = ORIENTATION_NONE
    pixel_clock_hz: int | None = None
    # The sensor's black floor. None leaves the camera's own value alone,
    # which is right for a camera that manages it (the Keeler self-adjusts
    # continuously) and wrong for one whose factory value clips -- see the
    # slit lamp below and DECISIONS.md's 2026-09-13 black-level entry.
    black_level: float | None = None
    # What Auto-Calibrate steers on (an exposure_calibration.VALID_METERING
    # member). Which part of the frame *is* the picture differs per
    # instrument: a slit beam is the brightest 0.1% of an otherwise black
    # frame, while the BIO's field is a lit disc whose brightest 0.1% is a
    # specular point far above it. See DECISIONS.md 2026-09-14.
    metering: str = METERING_HIGHLIGHT
    # The tone curve this camera rests at; above 1.0 lifts shadows and
    # compresses highlights rather than clipping them. None means 1.0, a
    # straight line. Where it is applied is the camera's business, not the
    # profile's: on board when the camera has a Gamma node (the Keeler),
    # otherwise on the host during the 8-bit conversion (the slit lamp).
    gamma: float | None = None
    # The capture format, or None for the camera's own (BayerRG8 on both).
    # Only worth raising together with `gamma` on a camera that curves on
    # the host: the extra bits exist solely to give the curve real shadow
    # levels to stretch -- measured 77 steps against 40 -- and are thrown
    # away by the 8-bit conversion otherwise. See DECISIONS.md 2026-09-17.
    pixel_format: str | None = None
    # For a host-curved camera: how much of full scale to subtract before
    # the curve, per unit of gain. A sensor's black floor is not zero, and
    # a curve lifts it like anything else -- empty space turns to haze.
    # Per gain because the floor scales with it. Set a little *under* the
    # measured floor: haze is recoverable, crushed blacks are not.
    digital_black_per_gain: float | None = None
    # One line for the technician, shown under the profile in settings.py.
    note: str = ""


PROFILES: tuple[DeviceProfile, ...] = (
    DeviceProfile(
        id="haag_streit_bi900_slit_lamp",
        name="Haag-Streit BI 900 slit lamp",
        picker_label="Slit Lamp",
        kind="ids",
        roles=("slit_lamp",),
        # Confirmed on real hardware (2026-09-01, again 2026-09-11): this
        # camera reports "UI325xCP-C". "UI-325" is the same model as spelled
        # in IDS's own documentation and SUPPORTED_HARDWARE.md ("UI-3250CP-C-HQ"),
        # carried so a differently-spelled report still matches. Neither
        # token can hit the Keeler's "U3-327xCP-C".
        model_tokens=("UI325", "UI-325"),
        # The image arrives mirrored on both axes, which is a 180-degree
        # rotation, not two separate flips. Reported from the instrument
        # 2026-09-08 and verified against horizontal text.
        orientation=ORIENTATION_ROTATE_180,
        # This legacy uEye camera comes up at 24MHz of a 10-128MHz range *on
        # every open* (it does not persist), and at 24MHz a 1600x1200 frame
        # takes ~87ms -- simultaneously an 11.5fps ceiling and the reason its
        # ExposureTime maximum reads 87208us. Neither is a sensor limit; both
        # are this one unset value. 80MHz is the lowest clock that reliably
        # delivers the full 30fps target (60MHz tops out at 28.6fps),
        # measured with zero device-side drops over 150s. Raising it shortens
        # the maximum exposure in proportion: light traded for frame rate,
        # not a free win. See DECISIONS.md's 2026-09-08 pixel-clock entry.
        pixel_clock_hz=80_000_000,
        # The factory value of 90 sits exactly on this sensor's clipping
        # point: at a scene-appropriate exposure it pins 12-60% of the frame
        # to zero, destroying the shadow end before any software sees it.
        # Measured 2026-09-13 against a beam on a black focus rod: the crush
        # stops between 90 and 95, and at 110 nothing clips while the floor
        # sits at 6-7 of 255 -- indistinguishable from 90 by eye, with the
        # beam unchanged. Gain only ever helps (the offset scales with it),
        # so a value chosen at gain 1.0, the worst case, is safe above it.
        black_level=110.0,
        # Measured 2026-09-17 on a backlit focus rod: to this sensor the
        # beam is 20x brighter than the backlit surface beside it, so with
        # the beam exposed correctly the rest sits ~10 levels above black.
        # 1.8 makes the rod's surface readable with the beam still
        # unclipped (p99.9 208 -> 226); at 2.2 the beam's own texture
        # starts to flatten, and that texture is the clinical content.
        # PROVISIONAL until seen on an eye -- a matte-black rod is the
        # hardest case, not the typical one.
        gamma=1.8,
        pixel_format="BayerRG12",
        # The floor measures 7.0 of 255 at gain 1.0 (0.0275) and scales
        # with gain. 0.030 already crushed 1% of the frame; 0.025 none.
        digital_black_per_gain=0.025,
        note="No auto-exposure of its own: calibrate it in Preview before first use.",
    ),
    DeviceProfile(
        id="keeler_vantage_plus_digital",
        name="Keeler Vantage Plus Digital BIO",
        picker_label="BIO",
        kind="ids",
        roles=("bio",),
        model_tokens=("U3-327",),
        # The instrument's optical path mirrors the image vertically, on
        # every unit -- a property of the product, not a per-clinic variation.
        orientation=ORIENTATION_FLIP_VERTICAL,
        # Reports a fixed, unwritable 197MHz: nothing to set.
        pixel_clock_hz=None,
        # The highlight rule left this camera's field at a median of 27
        # with a quarter of the frame pure black, pinned by sparkle at
        # p99.9 -- measured 2026-09-14. The field rule's numbers are
        # provisional until confirmed against a model eye.
        metering=METERING_FIELD,
        # Applied by the camera's own Gamma node; Brightness works upward
        # from here. Swept 2026-09-17 on skin in the lit field, one open,
        # exposure fixed: by 2.2 the field has gone flat and pale, while
        # 1.3 barely differs from none. Gentler than the slit lamp's 1.8
        # because here the lit field *is* the picture -- the curve is for
        # its dim end, not for the room outside the disc. PROVISIONAL
        # until seen on a fundus, and for noise at the gain it runs at.
        gamma=1.5,
        note="Calibrate in Preview: its own auto-exposure runs before the instrument is in use.",
    ),
    DeviceProfile(
        id="keeler_vantage_plus_legacy",
        name="Keeler Vantage Plus BIO (older, KS722OUP)",
        picker_label="BIO",
        kind="net2860_winusb",
        roles=("bio",),
        # No IDS model string to match on; it is recognised by being present
        # and WinUSB-bound at all. Needs no orientation correction --
        # verified against horizontal text, see DECISIONS.md 2026-09-10.
        orientation=ORIENTATION_NONE,
        note="Exposure and colour are the camera's own; there is nothing to calibrate.",
    ),
)


def profiles_for_role(role: str) -> tuple[DeviceProfile, ...]:
    """Supported cameras a given instrument role can use, in listed order."""
    return tuple(p for p in PROFILES if role in p.roles)


def profile_for_id(profile_id: str | None) -> DeviceProfile | None:
    """The profile a saved config.json id names, or None -- including for
    CUSTOM_PROFILE_ID and for an id written by a newer build. Callers treat
    None as "custom": unknown must degrade to the technician's own values,
    never to a failed load on a kiosk."""
    return next((p for p in PROFILES if p.id == profile_id), None)


def profile_for_model(model_name: str | None, role: str | None = None) -> DeviceProfile | None:
    """The profile matching a camera reporting `model_name`, for offering a
    technician a sensible default. Restricted to `role` when given, so the
    same model on the wrong row doesn't auto-select."""
    normalized = (model_name or "").upper()
    if not normalized:
        return None
    for profile in PROFILES:
        if role is not None and role not in profile.roles:
            continue
        if any(token.upper() in normalized for token in profile.model_tokens):
            return profile
    return None


def pixel_clock_hz_for_model(model_name: str | None) -> int | None:
    """Pixel clock this model should run at, or None to leave it alone."""
    profile = profile_for_model(model_name)
    return profile.pixel_clock_hz if profile is not None else None


def black_level_for_model(model_name: str | None) -> float | None:
    """The black floor this model needs, or None to leave the camera's own
    alone. Same shape as orientation_for_model(): it exists so a camera gets
    its preset from the model string when `config.json` names no profile --
    which is every configuration written before profiles existed."""
    profile = profile_for_model(model_name)
    return profile.black_level if profile is not None else None


def gamma_for_model(model_name: str | None) -> float | None:
    """The tone curve this model rests at, or None for a straight line."""
    profile = profile_for_model(model_name)
    return profile.gamma if profile is not None else None


def digital_black_per_gain_for_model(model_name: str | None) -> float | None:
    """Floor to subtract before a host curve, per unit of gain, or None."""
    profile = profile_for_model(model_name)
    return profile.digital_black_per_gain if profile is not None else None


def pixel_format_for_model(model_name: str | None) -> str | None:
    """The capture format this model should use, or None for its own."""
    profile = profile_for_model(model_name)
    return profile.pixel_format if profile is not None else None


def metering_for_model(model_name: str | None) -> str:
    """How Auto-Calibrate should meter this model. An unknown model gets
    the highlight rule, which is what every camera got before profiles
    carried one."""
    profile = profile_for_model(model_name)
    return profile.metering if profile is not None else METERING_HIGHLIGHT


def orientation_for_model(model_name: str | None) -> str:
    """The orientation fix (a `camera.VALID_ORIENTATIONS` member) for a
    camera reporting `model_name`, or ORIENTATION_NONE if no profile
    matches."""
    profile = profile_for_model(model_name)
    return profile.orientation if profile is not None else ORIENTATION_NONE
