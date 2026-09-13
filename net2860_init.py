"""EM2860 bridge initialisation for the legacy BIO camera.

(delay_s, register, value) triples, replayed by net2860_winusb_camera.py.
Not hand-written: captured from Keeler's vendor driver on the wire, then
verified by reading every register back (53 of 53 matched). The delays are the vendor
driver's own -- the two ~100ms gaps around register 0x12 look like
settling time for the enable sequence, so they are reproduced rather than
trimmed.

Split into two sequences because the capture brackets the session: the
same four writes (0x21/0x20/0x22/0x25) appear at both ends, and replaying
the trailing pair as part of startup switches the bridge straight back
off. START_WRITES is what brings the camera up; STOP_WRITES is what the
vendor driver sends on the way down.

That reason was wrong, tested on hardware 2026-09-13: replaying
START + STOP together streams perfectly, and so does a normal start
afterwards. Those four are the bridge's picture registers, which cannot
switch a bridge off. STOP_WRITES is kept only as the record of where the
vendor's session ended; nothing replays it now -- PICTURE_DEFAULTS below is
where those values live, chosen rather than inherited. See DECISIONS.md's
2026-09-13 entry.

To re-derive it: the capture is vendor/net2860_driver/capture/bio_all.pcap,
which is gitignored -- not in the repo, and the vendor driver that produced
it is no longer obtainable, so it can't be re-taken. The parser that turned
it into (delay, register, value) triples went with the WinUSB spike;
recover it with
    git show bf61492:spike_net2860_winusb/gen_init.py
It emits the 62 writes as one flat list; the 58/4 START/STOP split below was
made by hand at the bracketing writes.
"""

START_WRITES = [
    ( 0.001, 0x21, 0x08),
    ( 0.002, 0x20, 0x10),
    ( 0.002, 0x22, 0x0f),
    ( 0.002, 0x25, 0x02),
    ( 0.138, 0x0b, 0x00),
    ( 0.138, 0x0d, 0x42),
    ( 0.138, 0x0f, 0x07),
    ( 0.138, 0x13, 0x08),
    ( 0.139, 0x14, 0x32),
    ( 0.139, 0x15, 0x20),
    ( 0.139, 0x16, 0x20),
    ( 0.139, 0x17, 0x20),
    ( 0.140, 0x18, 0x00),
    ( 0.140, 0x19, 0x00),
    ( 0.140, 0x1a, 0x00),
    ( 0.140, 0x1b, 0x00),
    ( 0.140, 0x1c, 0x00),
    ( 0.140, 0x1d, 0x00),
    ( 0.140, 0x1e, 0xb4),
    ( 0.141, 0x1f, 0x48),
    ( 0.141, 0x20, 0x10),
    ( 0.141, 0x21, 0x08),
    ( 0.141, 0x22, 0x10),
    ( 0.141, 0x23, 0x00),
    ( 0.141, 0x24, 0x00),
    ( 0.142, 0x25, 0x02),
    ( 0.142, 0x26, 0x00),
    ( 0.142, 0x28, 0x01),
    ( 0.142, 0x29, 0xb3),
    ( 0.142, 0x2a, 0x01),
    ( 0.142, 0x2b, 0x47),
    ( 0.142, 0x2c, 0x93),
    ( 0.142, 0x2d, 0x93),
    ( 0.142, 0x2e, 0x93),
    ( 0.143, 0x2f, 0x93),
    ( 0.143, 0x30, 0x99),
    ( 0.143, 0x31, 0x01),
    ( 0.143, 0x32, 0x18),
    ( 0.143, 0x33, 0x10),
    ( 0.143, 0x34, 0x02),
    ( 0.143, 0x35, 0x00),
    ( 0.143, 0x36, 0x43),
    ( 0.144, 0x37, 0x10),
    ( 0.144, 0x38, 0x00),
    ( 0.144, 0x39, 0x00),
    ( 0.144, 0x3a, 0x00),
    ( 0.144, 0x3b, 0x00),
    ( 0.144, 0x3c, 0x00),
    ( 0.144, 0x3d, 0x00),
    ( 0.144, 0x3e, 0x28),
    ( 0.144, 0x3f, 0x00),
    ( 0.145, 0x12, 0x27),
    ( 0.145, 0x27, 0x34),
    ( 0.145, 0x10, 0x00),
    ( 0.245, 0x12, 0x67),
    ( 0.346, 0x11, 0x11),
    ( 0.346, 0x0c, 0x10),
    ( 0.347, 0x06, 0x4c),
]

STOP_WRITES = [
    ( 0.954, 0x21, 0x08),
    ( 0.955, 0x20, 0x10),
    ( 0.955, 0x22, 0x0f),
    ( 0.955, 0x25, 0x02),
]


# The EM2860's own video processing, confirmed on hardware 2026-09-13 by
# sweeping each one on a live stream (DECISIONS.md). Names are Linux
# em28xx-reg.h's; the behaviour is ours, measured:
#
#   0x20 contrast    5-bit gain, 0x10 = unity. 0x08 halved the frame mean.
#                    Bits above the field are masked: 0x20 reads back 0x00.
#   0x21 brightness  signed offset. 0x00/0x08/0x20/0x40 gave means of
#                    77/86/114/151.
#   0x22 saturation  5-bit gain, same masking. 0x00 is monochrome.
#   0x23 blue, 0x24 red  signed colour-balance offsets.
#   0x25 sharpness   edge enhancement; subtle, moves p95 without the mean.
PICTURE_REGISTERS = {
    "contrast": 0x20,
    "brightness": 0x21,
    "saturation": 0x22,
    "blue_balance": 0x23,
    "red_balance": 0x24,
    "sharpness": 0x25,
}

# Keeler's own values, which is where the vendor's session *ended* -- the
# trailing writes this file used to treat as a shutdown sequence. Adopted
# deliberately as the starting point (someone chose them with the
# instrument in front of them), not inherited by accident.
PICTURE_DEFAULTS = {
    "contrast": 0x10,
    "brightness": 0x08,
    "saturation": 0x0F,
    "blue_balance": 0x00,
    "red_balance": 0x00,
    "sharpness": 0x02,
}
