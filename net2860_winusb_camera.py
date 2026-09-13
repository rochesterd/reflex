"""BaseCamera for the older Vantage Plus BIO's camera over WinUSB.

Replaced the vendor-driver route, removed 2026-09-10, which reached the
same hardware through Keeler's driver and a 32-bit COM filter in a helper
subprocess. This talks to the eMPIA EM2860 bridge directly through
Microsoft's inbox winusb.sys, in-process, with nothing of Keeler's or NET
GmbH's involved. See DECISIONS.md's 2026-09-09 WinUSB entries for why -- briefly:
the vendor driver is unobtainable, not on Windows Update, and loads only
through legacy signing allowances that Microsoft can withdraw.

What the host actually has to do is small, because the camera images
itself: a Sony CXD3172AR CCD signal processor does the CCD timing, the
A/D, the colour processing and the AE/AWB detection, with a Silicon Labs
C8051F321 closing the loop, and it feeds ITU-R BT.656 straight into the
bridge. There is no analog decoder and no sensor to configure. A USB
capture of the vendor driver confirmed this on the wire: 62 register
writes, zero I2C transactions, in Linux em28xx's documented protocol.

Consequences worth knowing:

- No exposure/gain/white-balance control, unlike IdsCamera. That is not a
  gap here -- the AE/AWB loop lives in C8051F321 firmware the host cannot
  address, so no driver for this camera can offer it. Keeler's own filter
  offered none either (audited 2026-09-11).
- What the host *can* set is the bridge's own video processing: EM2860
  registers 0x20-0x25 are contrast, brightness, saturation, blue/red
  balance and sharpness (Linux em28xx-reg.h), and START_WRITES replays
  Keeler's values for them. Post-processing on the digitised stream, not
  sensor control -- and unconfirmed against this hardware. See
  DECISIONS.md's 2026-09-11 entry and ROADMAP.md before changing one.
- Frame.index comes from the *hardware*: each field header carries a
  counter that wraps at 128, unwrapped here into a monotonic frame number.
  Unlike UvcCamera -- which self-counts and therefore looks drop-free by
  construction -- gaps in this index are real source-side drops, which is
  exactly what camera.py's _grab() contract asks for and what recorder.py's
  drop accounting needs.

Video arrives as isochronous transfers only (there is no bulk endpoint).
Each packet carries a 4-byte header: "22 5a <seq> 88" starts a field,
"88 88 88 88" continues one. A field is 720x288 YUYV; two interleave into
one 720x576 frame at 25fps.
"""

from __future__ import annotations

import logging
import time

import cv2
import numpy as np

from camera import ORIENTATION_NONE, BaseCamera
from net2860_init import PICTURE_DEFAULTS, PICTURE_REGISTERS, START_WRITES
from winusb import IsochReader, WinUsbDevice, WinUsbError

logger = logging.getLogger(__name__)

VID, PID = 0x20F1, 0x0004
WIDTH, HEIGHT = 720, 576
FIELD_BYTES = WIDTH * (HEIGHT // 2) * 2  # 414,720 -- one field of YUYV

VIDEO_PIPE = 0x82
# Alt 6 gives 2892 bytes per microframe (23.1 MB/s). 720x576 YUYV at 25fps
# needs 20.7 MB/s, so alt 5 (20.6 MB/s) is fractionally short -- measured,
# not guessed; see DECISIONS.md's descriptor table.
DEFAULT_ALT = 6

FIELD_START = b"\x22\x5a"
FIELD_CONT = b"\x88\x88\x88\x88"
SEQ_MASK = 0x7F  # the header's field counter wraps at 128

# A transfer normally completes every few ms; this only has to be long
# enough that an ordinary scheduling hiccup isn't mistaken for a dead
# device.
_TRANSFER_TIMEOUT_MS = 500
# Consecutive timeouts before treating the stream as broken rather than
# slow, and restarting it. Same shape as uvc_camera.py's reconnect: a
# transient glitch should heal itself rather than leave a permanently dead
# pane discovered a week later.
_RESTART_AFTER_TIMEOUTS = 10
_RESTART_COOLDOWN_S = 1.0


class Net2860WinUsbError(RuntimeError):
    """The camera could not be opened or its stream could not be started."""


def _replay(dev: WinUsbDevice, writes: list[tuple[float, int, int]]) -> None:
    """Replay a captured register sequence, honouring its recorded gaps.

    The value goes in BOTH wValue and a one-byte payload -- the captured
    SETUP stage is 9 bytes, not 8, and sending only one of them is not
    enough.
    """
    prev = writes[0][0]
    for delay, reg, val in writes:
        gap = delay - prev
        if gap > 0.005:
            time.sleep(gap)
        prev = delay
        dev.control(0x40, 0x01, val, reg, data=bytes([val]))


class Net2860WinUsbCamera(BaseCamera):
    def __init__(
        self,
        label: str = "bio-legacy",
        queue_size: int = 2,
        orientation: str | None = ORIENTATION_NONE,
        picture: dict[str, int] | None = None,
        alt: int = DEFAULT_ALT,
        packets_per_transfer: int = 64,
        transfer_depth: int = 8,
    ):
        # No transform, verified against a scene with horizontal text: the
        # raw sensor output already reads upright and correctly, and each
        # of flip_vertical / flip_horizontal / rotate_180 mirrors or
        # inverts it. See DECISIONS.md's 2026-09-10 orientation entries --
        # this default was wrong twice before being checked against text,
        # so don't change it on a precedent from the newer BIO or from the
        # removed vendor helper. Both were misleading.
        #
        # Still overridable per-instance, and still applied through
        # BaseCamera's orientation mechanic rather than a hardcoded flip,
        # so if a differently-mounted unit ever does need one, every
        # consumer sees it identically.
        super().__init__(queue_size=queue_size, label=label, orientation=orientation)
        self._alt = alt
        # The bridge's video processing. Defaults are Keeler's own values,
        # adopted deliberately -- see net2860_init.PICTURE_DEFAULTS.
        self._picture = dict(PICTURE_DEFAULTS)
        if picture:
            self._picture.update(picture)
        self._packets = packets_per_transfer
        self._depth = transfer_depth
        self._dev: WinUsbDevice | None = None
        self._reader: IsochReader | None = None

        self._cur = bytearray()
        self._cur_seq: int | None = None
        self._cur_started: float = 0.0
        self._pending: tuple[int, bytes, float] | None = None
        self._last_raw: int | None = None
        self._unwrapped = 0
        self._timeouts = 0
        self._last_restart = 0.0
        self.short_fields = 0

    @property
    def resolution(self) -> tuple[int, int]:
        return (WIDTH, HEIGHT)

    # ---------------------------------------------------------------- open

    def _open(self) -> None:
        try:
            self._dev = WinUsbDevice.open_one(VID, PID)
        except WinUsbError as e:
            raise Net2860WinUsbError(str(e)) from e

        try:
            _replay(self._dev, START_WRITES)
            self._apply_picture()
            self._dev.set_alt(self._alt)
            self._start_stream()
        except Exception:
            self._close()
            raise
        logger.info("%s: streaming on alt %d", self.label, self._alt)

    def _start_stream(self) -> None:
        try:
            self._reader = IsochReader(self._dev, VIDEO_PIPE, self._alt,
                                       self._packets, self._depth)
            self._reader.start()
        except WinUsbError as e:
            raise Net2860WinUsbError("could not start the video stream: %s" % e) from e
        self._reset_assembly()

    def _reset_assembly(self) -> None:
        self._cur = bytearray()
        self._cur_seq = None
        self._pending = None
        self._timeouts = 0

    def _close(self) -> None:
        if self._reader is not None:
            try:
                if self._dev is not None:
                    self._dev.abort_pipe(VIDEO_PIPE)
            except Exception:
                pass
            self._reader.close()
            self._reader = None
        if self._dev is not None:
            try:
                self._dev.set_alt(0)  # release the isochronous bandwidth
            except Exception:
                # Best-effort: we are closing anyway, and a device that has
                # already been unplugged cannot be told to stop.
                pass
            self._dev.close()
            self._dev = None

    # The bridge has no exposure to give, but it does have a brightness
    # offset in R21 -- measured 2026-09-13 raising the frame mean from 77
    # to 151 across its range. From the vendor's own value upwards. The
    # register is quantized in hardware, so this travel is 48 steps rather
    # than continuous -- far finer than a thumb resolves.
    BRIGHTNESS_ADJUSTABLE = True
    _BRIGHTNESS_RANGE = (0x08, 0x38)

    def set_brightness(self, amount: float) -> None:
        amount = max(0.0, min(1.0, float(amount)))
        low, high = self._BRIGHTNESS_RANGE
        self._picture["brightness"] = int(round(low + amount * (high - low)))
        if self._dev is not None:
            self._apply_picture()
        logger.info("%s: brightness %.0f%% (offset 0x%02x)",
                    self.label, amount * 100, self._picture["brightness"])

    def _apply_picture(self) -> None:
        """Set the bridge's picture registers after the captured bring-up.

        START_WRITES touches these too, mid-sequence; writing them again
        here is what makes the values *ours* rather than whatever the
        capture happened to leave. Each is one control transfer, verified by
        reading it straight back -- the same protocol the vendor driver
        used, and the readback is how a masked field (contrast and
        saturation ignore bits above 0x1f) shows up instead of silently
        misbehaving.
        """
        for name, value in self._picture.items():
            register = PICTURE_REGISTERS[name]
            self._dev.control(0x40, 0x01, value, register, data=bytes([value]))
            got = self._dev.control(0xC0, 0x00, 0, register, length=1)[0]
            if got != value:
                logger.warning(
                    "%s: %s (register 0x%02x) set to 0x%02x but reads 0x%02x",
                    self.label, name, register, value, got,
                )

    # ---------------------------------------------------------------- grab

    def _grab(self) -> tuple[np.ndarray, float, int] | None:
        if self._reader is None:
            return None
        packets = self._reader.next_packets(_TRANSFER_TIMEOUT_MS)
        if packets is None:
            self._timeouts += 1
            if self._timeouts >= _RESTART_AFTER_TIMEOUTS:
                self._restart_stream()
            return None
        self._timeouts = 0

        frame = None
        for pkt in packets:
            done = self._consume(pkt)
            if done is not None:
                frame = done  # keep the newest if a transfer spans two
        return frame

    def _consume(self, pkt: bytes) -> tuple[np.ndarray, float, int] | None:
        if len(pkt) >= 4 and pkt[:2] == FIELD_START:
            finished = self._finish_field()
            self._cur = bytearray(pkt[4:])
            self._cur_seq = pkt[2] & SEQ_MASK
            self._cur_started = time.monotonic()
            return finished
        if self._cur_seq is not None:
            self._cur += pkt[4:] if pkt[:4] == FIELD_CONT else pkt
        return None

    def _finish_field(self) -> tuple[np.ndarray, float, int] | None:
        if self._cur_seq is None:
            return None
        if len(self._cur) < FIELD_BYTES:
            # A short field means we joined mid-field or lost packets. Drop
            # it rather than padding: a half-filled field would render as a
            # torn frame, which looks like a camera fault rather than a
            # dropped one.
            self.short_fields += 1
            self._cur_seq = None
            return None

        seq = self._unwrap(self._cur_seq)
        field = bytes(self._cur[:FIELD_BYTES])
        started = self._cur_started
        prev, self._pending = self._pending, (seq, field, started)
        self._cur_seq = None

        if prev is None:
            return None
        pseq, pfield, pstarted = prev
        # Only pair a clean consecutive even->odd run. Anything else means
        # a field went missing, and the gap it leaves in Frame.index is the
        # point -- see this module's docstring.
        if seq - pseq != 1 or pseq % 2 != 0:
            return None
        self._pending = None

        # Even sequence numbers carry the top field. Verified only against
        # a static scene so far: getting this backwards swaps the two field
        # rows, which is invisible without motion in the frame.
        interleaved = np.empty((HEIGHT, WIDTH, 2), np.uint8)
        interleaved[0::2] = np.frombuffer(pfield, np.uint8).reshape(HEIGHT // 2, WIDTH, 2)
        interleaved[1::2] = np.frombuffer(field, np.uint8).reshape(HEIGHT // 2, WIDTH, 2)
        bgr = cv2.cvtColor(interleaved, cv2.COLOR_YUV2BGR_YUY2)
        return bgr, pstarted, pseq // 2

    def _unwrap(self, raw: int) -> int:
        """Extend the header's 7-bit field counter to a monotonic one.

        Advancing by the masked delta rather than by one keeps real gaps
        visible (a lost field advances the counter by two) while surviving
        the wrap. Parity is preserved against the raw counter by seeding
        from the first value seen, which is what decides which field is
        the top one.
        """
        if self._last_raw is None:
            self._unwrapped = raw
        else:
            self._unwrapped += (raw - self._last_raw) & SEQ_MASK
        self._last_raw = raw
        return self._unwrapped

    def _restart_stream(self) -> None:
        now = time.monotonic()
        if now - self._last_restart < _RESTART_COOLDOWN_S:
            return
        self._last_restart = now
        logger.warning("%s: no isochronous data for %d transfers; restarting the stream",
                       self.label, self._timeouts)
        try:
            if self._dev is not None:
                self._dev.abort_pipe(VIDEO_PIPE)
            if self._reader is not None:
                self._reader.close()
                self._reader = None
            self._start_stream()
        except Exception:
            # Swallowed deliberately: _grab() is called in a loop, so a
            # failed restart just means we try again after the cooldown
            # rather than killing the capture thread.
            logger.exception("%s: stream restart failed", self.label)
