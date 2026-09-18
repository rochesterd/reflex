"""Loads config.json: which physical camera fills each role (slit lamp,
BIO, third-person). See DECISIONS.md's 2026-08-18 "config.json + loader"
entry for why this exists and what it deviates from the eventual
(Phase 3+) schema.

Deliberately has no import of camera.py/ids_camera.py/uvc_camera.py -- it
only parses JSON into dataclasses, so it stays usable on a dev machine with
no IDS SDK installed, the same assumption app.py's lazy IdsCamera import
already makes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_VID_PID_RE = re.compile(r"^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}$")

_FIX_HINT = "Copy config.example.json to config.json and edit it for this machine."


def is_frozen() -> bool:
    """True under a PyInstaller-frozen app.exe/settings.exe, false for a
    normal `python app.py` dev/test run. `sys.frozen` is set by
    PyInstaller's bootloader before any of the app's own code runs, so
    this is reliable to check at any point, including at import time.
    """
    return bool(getattr(sys, "frozen", False))


def resolve_default_config_path() -> Path:
    """A frozen install has no repo checkout to be relative to, so
    config.json lives in %ProgramData% instead -- see DECISIONS.md's
    "Frozen-exe installer built" entry. Dev/test behavior
    (relative to CWD) is unchanged.
    """
    if is_frozen():
        return Path(os.environ["ProgramData"]) / "Reflex" / "config.json"
    return Path("config.json")


DEFAULT_CONFIG_PATH = resolve_default_config_path()


class ConfigError(RuntimeError):
    """config.json is missing, malformed, or missing/wrong-typed required keys."""


@dataclass
class InstrumentConfig:
    kind: str
    # None only for kind="net2860_winusb" -- that camera has no serial
    # (there's exactly one of it, no identification scheme; see
    # DECISIONS.md). Required (non-None) for kind="ids".
    serial: str | None
    label: str
    # What a technician picked in settings.py, and what produced `label`.
    #
    # `profile` is a device_presets.DeviceProfile id -- the supported camera
    # this is, carrying its orientation and pixel clock. None means custom:
    # a config.json written before profiles existed is exactly that, which
    # is why every one of them stays valid. An id this build doesn't know
    # (written by a newer one) is kept as-is and degrades to custom rather
    # than failing the load -- a kiosk must start. Deliberately not resolved
    # here: config.py stays dependency-free, and app.py/settings.py look it
    # up in device_presets.
    #
    # `label` is what students read on the picker: pre-filled from the
    # profile in settings.py, then whatever the technician made it.
    profile: str | None = None
    # Overrides the profile's black level for this room. Rarely needed: the
    # right value is a property of the sensor, not the room. Present because
    # a camera swapped for a different revision is the case that would need
    # it, and a technician cannot wait for a release.
    black_level: float | None = None
    # A technician's one-time calibration for this instrument, written by
    # settings.py's Preview dialog -- see ids_camera.py's
    # supports_manual_calibration() and DECISIONS.md's 2026-08-25
    # calibration entry. Offered for any camera whose ExposureTime/Gain can
    # be written, which since 2026-09-10 includes the ones that *do* have
    # ExposureAuto/GainAuto: those converge at camera open, which is the
    # moment a student taps the picker, not a moment the scene is real.
    # None still means "let _converge_auto_nodes() handle this axis," the
    # same as before these fields existed -- every config.json written
    # before this is still valid.
    exposure_time_us: float | None = None
    gain: float | None = None
    # Optional escape hatch overriding device_presets.py's per-model default
    # (e.g. the Keeler BIO camera delivers a vertically-flipped image). One
    # of camera.VALID_ORIENTATIONS ("none"/"rotate_180"/"flip_horizontal"/
    # "flip_vertical"); None means "use the model preset." There's no
    # settings.py UI for this yet; it's hand-set for a non-standard
    # mounting. See DECISIONS.md's "Device-model rotation presets" entry
    # and its orientation follow-up.
    orientation: str | None = None
    # Optional override of device_presets.py's per-model pixel clock. The
    # preset is right for the hardware, but the *safe* ceiling depends on
    # the host USB controller, which is per-install -- so unlike most
    # presets this one is deliberately technician-overridable. None means
    # "use the model preset"; see DECISIONS.md.
    pixel_clock_hz: int | None = None


@dataclass
class ThirdPersonConfig:
    kind: str
    vid_pid: str  # "XXXX:YYYY", uppercase hex -- see uvc_enumeration.py
    friendly_name: str


DEFAULT_RECORDING_FPS = 30


@dataclass
class RecordingConfig:
    fps: int


# The microphone. Absent means a silent kiosk, which every config written
# before audio existed is. The device goes by *name*, not index: an index
# changes when a USB device is re-plugged, the same reason cameras go by
# serial. None for the name means the system default input.
DEFAULT_AUDIO_SAMPLERATE = 48000
DEFAULT_AUDIO_CHANNELS = 1


@dataclass
class AudioConfig:
    device: str | None = None
    samplerate: int = DEFAULT_AUDIO_SAMPLERATE
    channels: int = DEFAULT_AUDIO_CHANNELS


# Stream mode: instead of recording, compose both feeds into a virtual
# webcam for Panopto Capture to record (DECISIONS.md 2026-09-18). Layouts
# mirror compositor.LAYOUT_MODES, restated so config.py imports nothing;
# a test holds the two together.
STREAM_LAYOUTS = ("side_by_side", "picture_in_picture", "instrument", "third_person")
DEFAULT_STREAM_LAYOUT = "side_by_side"
DEFAULT_STREAM_FPS = 30
DEFAULT_STREAM_WIDTH = 1920
DEFAULT_STREAM_HEIGHT = 1080


@dataclass
class StreamingConfig:
    """Off means record mode -- the default, and every existing config.
    On means the kiosk records nothing: it publishes the composed feed as
    a virtual camera and Panopto Capture, signed in as the student, does
    the recording."""

    enabled: bool = False
    layout: str = DEFAULT_STREAM_LAYOUT
    fps: int = DEFAULT_STREAM_FPS
    width: int = DEFAULT_STREAM_WIDTH
    height: int = DEFAULT_STREAM_HEIGHT

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


# The client secret, if IT issued one, is never in config.json. It lives
# beside it, encrypted to this machine (secret_store.py). It is low-value
# -- an authorization-code client's secret cannot mint a token without a student
# signing in -- but a secret in a JSON file is still the wrong habit, and
# the store was cheap. settings.py writes both; neither is hand-edited.
PANOPTO_SECRET_NAME = "panopto.secret"

# Matches panopto_api.DEFAULT_REDIRECT_PORT; restated so config.py stays
# free of that import. A test holds the two together.
DEFAULT_PANOPTO_REDIRECT_PORT = 48219


def panopto_secret_path(config_path: Path | str = None) -> Path:
    """Where this machine's Panopto secret lives: beside its config.json."""
    config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    return config_path.parent / PANOPTO_SECRET_NAME


@dataclass
class PanoptoConfig:
    """Where a signed-in student's session is uploaded.

    There is no service account and no credential that acts on its own
    (DECISIONS.md 2026-09-18): the student signs in through the browser and
    uploads as themselves into `assignment_folder_id`, a Panopto
    Assignment Folder where each student sees only their own submissions
    and faculty see all. `client_secret` is None unless IT issued one for
    the API client (Panopto's "Server-side Web Application" type, which
    is its authorization-code client), and then it is the decrypted value, in
    memory only.
    """

    host: str  # site hostname only, e.g. "neco.hosted.panopto.com"
    client_id: str
    assignment_folder_id: str
    client_secret: str | None = None
    redirect_port: int = DEFAULT_PANOPTO_REDIRECT_PORT


@dataclass
class AppConfig:
    instruments: dict[str, InstrumentConfig]
    third_person: ThirdPersonConfig
    recording: RecordingConfig
    # None means "no Panopto on this machine" -- the valid, and currently
    # normal, state. Every dev machine is in it, and so is any clinic PC
    # until credentials exist, so it must never be an error: the drive
    # export is still there. viewer.py offers an upload only when this is set.
    panopto: PanoptoConfig | None = None
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    # None means no microphone: record mode is silent, and stream mode's
    # audio is the browser's business either way.
    audio: AudioConfig | None = None


def achievable_fps(exposure_time_us: float) -> float:
    """The fastest a camera can run at this exposure. A sensor exposing
    for E microseconds cannot deliver frames faster than 1/E."""
    return 1_000_000.0 / max(exposure_time_us, 1e-6)


def exposure_fps_warnings(config: "AppConfig") -> list[str]:
    """One message per instrument whose configured exposure makes the
    recording frame rate unreachable.

    Not a ConfigError: a slow camera still records usable video, and
    refusing to start would be worse than the problem. But it must be
    *visible* -- this is exactly the failure that went unnoticed for
    weeks, an 87ms exposure silently capping the slit lamp at ~11fps
    against a 30fps target. Missing visibility, not a missing setting,
    was the gap. See CLAUDE.md's "Camera configuration: who decides
    what".
    """
    messages: list[str] = []
    target = config.recording.fps
    for key, instrument in config.instruments.items():
        if instrument.exposure_time_us is None:
            continue
        possible = achievable_fps(instrument.exposure_time_us)
        if possible < target:
            messages.append(
                f"{instrument.label or key}: exposure {instrument.exposure_time_us / 1000:.1f}ms "
                f"limits this camera to about {possible:.0f}fps, below the {target:g}fps "
                f"recording target - and adds that much motion blur to every frame. "
                f"Re-run Calibrate in settings, or add light at the instrument."
            )
    return messages


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found. {_FIX_HINT}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}. {_FIX_HINT}") from exc

    instruments_raw = raw.get("instruments")
    if not isinstance(instruments_raw, dict) or not instruments_raw:
        raise ConfigError(f"{path}: 'instruments' must be a non-empty object. {_FIX_HINT}")

    instruments: dict[str, InstrumentConfig] = {}
    for key, entry in instruments_raw.items():
        instruments[key] = _parse_instrument(path, key, entry)

    third_person_raw = raw.get("third_person")
    if not isinstance(third_person_raw, dict):
        raise ConfigError(f"{path}: 'third_person' must be an object. {_FIX_HINT}")
    third_person = _parse_third_person(path, third_person_raw)

    recording = _parse_recording(path, raw.get("recording"))
    panopto = _parse_panopto(path, raw.get("panopto"))
    streaming = _parse_streaming(path, raw.get("streaming"))
    audio = _parse_audio(path, raw.get("audio"))

    config = AppConfig(
        instruments=instruments,
        third_person=third_person,
        recording=recording,
        panopto=panopto,
        streaming=streaming,
        audio=audio,
    )
    for message in exposure_fps_warnings(config):
        logger.warning("%s: %s", path, message)
    return config


def _parse_instrument(path: Path, key: str, entry: object) -> InstrumentConfig:
    if not isinstance(entry, dict):
        raise ConfigError(f"{path}: instruments.{key} must be an object. {_FIX_HINT}")

    kind = entry.get("kind")
    if kind == "net2860":
        # Named specifically rather than falling into the generic "unknown
        # kind" message below: this one used to be valid, so a technician
        # meeting it is looking at a config that worked before, and the
        # useful thing to tell them is the one-word replacement.
        raise ConfigError(
            f"{path}: instruments.{key}.kind \"net2860\" reached the legacy BIO through "
            f"Keeler's vendor driver, which has been removed. Change it to "
            f"\"net2860_winusb\" -- the same camera, through WinUSB, with no other "
            f"configuration change needed. {_FIX_HINT}"
        )
    if kind not in ("ids", "net2860_winusb"):
        raise ConfigError(
            f"{path}: instruments.{key}.kind must be \"ids\" or \"net2860_winusb\", "
            f"got {kind!r}. {_FIX_HINT}"
        )

    label = entry.get("label")
    if not isinstance(label, str) or not label:
        raise ConfigError(f"{path}: instruments.{key}.label must be a non-empty string. {_FIX_HINT}")

    profile = _parse_optional_text(path, f"instruments.{key}.profile", entry.get("profile"))

    if kind == "net2860_winusb":
        # The legacy BIO, through Microsoft's inbox winusb.sys in-process.
        #
        # No serial: there's exactly one of this camera and no
        # identification scheme -- see DECISIONS.md's "Net2860Camera"
        # entry. No exposure/gain/white-balance either, and that is not a
        # missing feature: the AE/AWB loop runs on the camera board itself
        # (a Sony CXD3172AR with a C8051F321 closing the loop), where the
        # host cannot reach it. No orientation, because the flip this
        # instrument's optics need is a fixed property of the hardware, not
        # a room-level choice -- it belongs in code, not in a settings
        # dialog. See CLAUDE.md's "who decides what" table.
        #
        # Rejected loudly rather than silently ignored, so copy-pasting an
        # "ids" entry and only changing "kind" fails fast instead of
        # producing a config.json that looks configured but isn't.
        unexpected = {
            "serial", "exposure_time_us", "gain", "black_level", "orientation", "pixel_clock_hz"
        } & entry.keys()
        if unexpected:
            raise ConfigError(
                f"{path}: instruments.{key} is kind \"{kind}\", which doesn't take "
                f"{', '.join(sorted(unexpected))}. {_FIX_HINT}"
            )
        return InstrumentConfig(kind=kind, serial=None, label=label, profile=profile)

    serial = entry.get("serial")
    if not isinstance(serial, str) or not serial:
        raise ConfigError(f"{path}: instruments.{key}.serial must be a non-empty string. {_FIX_HINT}")

    exposure_time_us = _parse_optional_positive_number(path, f"instruments.{key}.exposure_time_us", entry.get("exposure_time_us"))
    gain = _parse_optional_positive_number(path, f"instruments.{key}.gain", entry.get("gain"))

    black_level = _parse_optional_positive_number(path, f"instruments.{key}.black_level", entry.get("black_level"))
    orientation = _parse_optional_orientation(path, f"instruments.{key}.orientation", entry.get("orientation"))
    pixel_clock_hz = entry.get("pixel_clock_hz")
    if pixel_clock_hz is not None:
        pixel_clock_hz = _positive_int(path, f"instruments.{key}.pixel_clock_hz", pixel_clock_hz)

    return InstrumentConfig(
        kind=kind,
        serial=serial,
        label=label,
        profile=profile,
        black_level=black_level,
        exposure_time_us=exposure_time_us,
        gain=gain,
        orientation=orientation,
        pixel_clock_hz=pixel_clock_hz,
    )


def _parse_optional_text(path: Path, field_name: str, value: object) -> str | None:
    """A non-empty string, or None when absent. An empty string means the
    same as absent: a field a technician cleared is not an error to fix,
    it is an unset field."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{path}: {field_name} must be a string. {_FIX_HINT}")
    return value


def _parse_optional_positive_number(path: Path, field_name: str, value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{path}: {field_name} must be a positive number. {_FIX_HINT}")
    return float(value)


_VALID_ORIENTATIONS = ("none", "rotate_180", "flip_horizontal", "flip_vertical")


def _parse_optional_orientation(path: Path, field_name: str, value: object) -> str | None:
    # Mirrors camera.VALID_ORIENTATIONS (kept as a literal here so config.py
    # stays import-light -- it deliberately doesn't import camera.py). All
    # four are dimension-preserving; 90/270 rotation is excluded because it
    # would desync frames from the camera's reported .resolution.
    if value is None:
        return None
    if value not in _VALID_ORIENTATIONS:
        raise ConfigError(
            f"{path}: {field_name} must be one of {', '.join(_VALID_ORIENTATIONS)}. {_FIX_HINT}"
        )
    return value


def _parse_third_person(path: Path, entry: dict) -> ThirdPersonConfig:
    kind = entry.get("kind")
    if kind != "uvc":
        raise ConfigError(f"{path}: third_person.kind must be \"uvc\", got {kind!r}. {_FIX_HINT}")

    vid_pid = entry.get("vid_pid")
    if not isinstance(vid_pid, str) or not _VID_PID_RE.match(vid_pid):
        raise ConfigError(
            f"{path}: third_person.vid_pid must look like \"XXXX:YYYY\" (hex). {_FIX_HINT}"
        )
    # Normalized, not just validated: uvc_enumeration.py always uppercases,
    # and resolve_device() matches by direct string equality -- a
    # hand-typed lowercase value here would otherwise silently never match
    # an attached device.
    vid_pid = vid_pid.upper()

    friendly_name = entry.get("friendly_name")
    if not isinstance(friendly_name, str) or not friendly_name:
        raise ConfigError(f"{path}: third_person.friendly_name must be a non-empty string. {_FIX_HINT}")

    return ThirdPersonConfig(kind=kind, vid_pid=vid_pid, friendly_name=friendly_name)


def _parse_recording(path: Path, entry: object) -> RecordingConfig:
    # Optional section, unlike instruments/third_person: a missing
    # `recording` key means "use the measured default," not a broken
    # config -- see DECISIONS.md's "config-driven recording fps" entry for
    # why this is a measured value (a technician tunes it against real
    # observed throughput) rather than something read off the camera.
    if entry is None:
        return RecordingConfig(fps=DEFAULT_RECORDING_FPS)
    if not isinstance(entry, dict):
        raise ConfigError(f"{path}: 'recording' must be an object. {_FIX_HINT}")

    fps = entry.get("fps", DEFAULT_RECORDING_FPS)
    # Must be a whole number: it becomes the encoder's frame rate and its
    # keyframe interval, and PyAV's add_stream(rate=...) raises on a float.
    # A fractional value here otherwise surfaces only as a crash on the
    # first Start, with nothing shown -- exactly the failure this file's
    # loud-and-early validation exists to prevent. 30.0 is accepted (JSON
    # writes whole numbers as floats) and coerced; 29.97 is rejected.
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps <= 0 or fps != int(fps):
        raise ConfigError(f"{path}: recording.fps must be a positive whole number. {_FIX_HINT}")

    return RecordingConfig(fps=int(fps))


def _parse_audio(path: Path, entry: object) -> "AudioConfig | None":
    """Optional. Absent is silent. Present-and-wrong is an error, so a
    kiosk meant to record sound never quietly records none."""
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise ConfigError(f"{path}: 'audio' must be an object. {_FIX_HINT}")
    device = entry.get("device")
    if device is not None and (not isinstance(device, str) or not device.strip()):
        raise ConfigError(f"{path}: audio.device must be a device name, or omitted for the default. {_FIX_HINT}")
    samplerate = _positive_int(path, "audio.samplerate", entry.get("samplerate", DEFAULT_AUDIO_SAMPLERATE))
    channels = _positive_int(path, "audio.channels", entry.get("channels", DEFAULT_AUDIO_CHANNELS))
    if channels > 2:
        raise ConfigError(f"{path}: audio.channels must be 1 or 2. {_FIX_HINT}")
    return AudioConfig(device=device.strip() if device else None, samplerate=samplerate, channels=channels)


def _parse_streaming(path: Path, entry: object) -> StreamingConfig:
    """Optional; absent is record mode. Present-and-wrong is an error, for
    the usual reason: a kiosk that was meant to stream and silently
    records instead has two students' faces in a buffer nobody will look
    at, and a Panopto session with nothing in it."""
    if entry is None:
        return StreamingConfig()
    if not isinstance(entry, dict):
        raise ConfigError(f"{path}: 'streaming' must be an object. {_FIX_HINT}")

    enabled = entry.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{path}: streaming.enabled must be true or false. {_FIX_HINT}")

    layout = entry.get("layout", DEFAULT_STREAM_LAYOUT)
    if layout not in STREAM_LAYOUTS:
        raise ConfigError(
            f"{path}: streaming.layout must be one of {', '.join(STREAM_LAYOUTS)}. {_FIX_HINT}"
        )

    fps = _positive_int(path, "streaming.fps", entry.get("fps", DEFAULT_STREAM_FPS))
    width = _positive_int(path, "streaming.width", entry.get("width", DEFAULT_STREAM_WIDTH))
    height = _positive_int(path, "streaming.height", entry.get("height", DEFAULT_STREAM_HEIGHT))
    if width % 2 or height % 2:
        # Browsers and encoders want even dimensions; an odd canvas fails
        # somewhere downstream with a message nobody can act on.
        raise ConfigError(f"{path}: streaming.width and height must be even. {_FIX_HINT}")

    return StreamingConfig(enabled=enabled, layout=layout, fps=fps, width=width, height=height)


def _parse_panopto(path: Path, entry: object) -> "PanoptoConfig | None":
    """Optional section; absent means uploads are off, which is valid.

    Deliberately absent from config.example.json too: the example must stay
    loadable as copied, and a placeholder would fail every machine that has
    no Panopto site. The shape is:

        "panopto": {
          "host": "neco.hosted.panopto.com",
          "client_id": "...",
          "assignment_folder_id": "...",
          "redirect_port": 48219          // optional
        }

    A client *secret*, if IT issued one, is not in here and must not be:
    it sits beside this file, encrypted to the machine (secret_store.py),
    written by settings.py. Optional in the loader, though Panopto's
    "Server-side Web Application" clients are issued one and its own sample
    sends it in the code exchange -- expect to enter it.

    Present-but-incomplete is an error rather than a fallback to off. A
    half-filled section means someone was configuring uploads and didn't
    finish, and silently recording to a machine that won't upload is the
    black-pane failure in another costume.
    """
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise ConfigError(f"{path}: 'panopto' must be an object. {_FIX_HINT}")

    if "client_secret" in entry:
        # Refused rather than accepted-with-a-warning: accepting it would
        # make a plaintext secret on a kiosk the normal case, which the
        # encrypted store exists to prevent -- even for a low-value one.
        raise ConfigError(
            f"{path}: panopto.client_secret must not be stored in config.json. "
            f"Enter it in Settings, which encrypts it to this machine."
        )

    values: dict[str, str] = {}
    for field_name in ("host", "client_id", "assignment_folder_id"):
        value = entry.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"{path}: panopto.{field_name} must be a non-empty string. "
                f"Remove the whole 'panopto' section to disable uploads."
            )
        values[field_name] = value.strip()

    # A technician handed a site URL will paste the URL. Taking the
    # hostname out of it is unambiguous, so do that rather than fail.
    host = values["host"]
    host = host.split("://", 1)[-1].split("/", 1)[0]
    if not host:
        raise ConfigError(f"{path}: panopto.host has no hostname in it. {_FIX_HINT}")
    values["host"] = host

    redirect_port = entry.get("redirect_port", DEFAULT_PANOPTO_REDIRECT_PORT)
    if isinstance(redirect_port, bool) or not isinstance(redirect_port, int) or not 1024 <= redirect_port <= 65535:
        raise ConfigError(
            f"{path}: panopto.redirect_port must be a whole number between 1024 and 65535. "
            f"It has to match the redirect URI registered with IT."
        )

    return PanoptoConfig(
        client_secret=_read_panopto_secret(path),
        redirect_port=redirect_port,
        **values,
    )


def _read_panopto_secret(config_path: Path) -> str | None:
    """The decrypted client secret, None if none was stored, or a
    ConfigError naming the fix if one was stored and can't be read.

    Imported here rather than at module scope so config.py stays importable
    where secret_store's Windows-only DPAPI is not -- the same reason this
    module never imports a camera.
    """
    secret_path = panopto_secret_path(config_path)
    if not secret_path.exists():
        return None
    from secret_store import SecretError, read_secret

    try:
        secret = read_secret(secret_path)
    except SecretError as exc:
        raise ConfigError(f"{secret_path}: {exc}") from exc
    return secret.strip() or None


def _positive_int(path: Path, field_name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{path}: {field_name} must be a positive integer. {_FIX_HINT}")
    return value


