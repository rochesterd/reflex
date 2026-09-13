# IMAGING.md

What each camera's stack exposes, what each option costs, and where the
ceilings are. Facts only, as measured — *why* a thing was decided is in
`DECISIONS.md`, what we intend to do about it is in `ROADMAP.md`, and the
per-device tested list is `SUPPORTED_HARDWARE.md`.

Re-measure rather than extend by arithmetic: USB3 Vision drops frames
silently, and every figure here came off one development laptop.

## What each stack exposes

Queried on the attached cameras, 2026-09-11. "—" is absent from the
feature set, not merely unused.

| | Slit lamp (uEye TL) | Keeler BIO (USB3 Vision) | Older BIO (EM2860) | Hands camera (UVC) |
|---|---|---|---|---|
| Exposure | 20–33,321 µs | yes | — (sensor's own) | driver, best-effort |
| Gain | 1.00–4.00x | 1.00–25.41x | — | driver, best-effort |
| Auto exposure / gain | — | yes, bounded by `BrightnessAutoExposureTimeMax` | on the camera board | driver, switched off after warmup |
| White balance | — (no auto, no `BalanceRatio`) | auto only | on the camera board | driver auto, left on |
| Black level | `BlackLevel` 0–255, **default 90** | `BlackLevelAuto` = continuous | — (bridge brightness offset instead) | — |
| Gamma / LUT | — | both present, both unused | — | driver gamma present |
| Picture registers | — | — | bridge `R20`–`R25` | brightness/contrast/saturation/sharpness |
| Pixel formats | Bayer8/10/12, BGR8, BGRa8 | Mono8, Bayer8/10/12 + packed 10p/12p, RGB8, BGR8 | YUYV only | YUY2, usually MJPEG |
| Pixel clock | `DeviceClockFrequency`, set to 80 MHz | 197 MHz (untouched) | — | — |
| Frame-rate cap | `AcquisitionFrameRate` | `AcquisitionFrameRate` | fixed 25 fps | requested, verified |
| Orientation | in software (`rotate_180`) | in software (`flip_vertical`) | none needed | none needed |

The asymmetry that matters: the slit lamp's transport layer publishes no
auto-anything and no tone curve, so its exposure is whatever
`auto_calibrate()` measured, and any curve would cost host CPU. The Keeler
can do the same work on the camera for free.

## What each option costs

At 30 fps, computed from the measured resolutions. Today's configuration
is the first row of each camera.

| Camera | Format | Bandwidth | Note |
|---|---|---|---|
| Slit lamp 1600x1200 | Bayer8 | 57.6 MB/s | today |
| | Bayer10/12 | 115.2 MB/s | no packed format on this camera, so 12 bits cost 16 |
| | BGR8 | 172.8 MB/s | camera-side debayer; 3x the data to save host CPU |
| Keeler 2048x1536 | Bayer8 | 94.4 MB/s | today |
| | Bayer12p | 141.6 MB/s | packed, so 12 bits cost 12 |
| | Bayer12 | 188.7 MB/s | unpacked |
| | Mono8 | 94.4 MB/s | saves **no** bandwidth — 8 bits either way |
| Older BIO 720x576 | YUYV, 25 fps | 20.7 MB/s | fixed; alternate setting 6 allocates 23.1 MB/s |
| Hands camera 640x480 | YUY2 | 18.4 MB/s | today |
| | YUY2 1280x720 | 55.3 MB/s | over USB 2.0's practical ceiling |
| | MJPEG 1280x720 | ~5.5 MB/s | the only route to 720p; decode cost, artefacts before H.264 |

## Ceilings

- **USB 3.0 bus.** One instrument streams at a time, so the worst case is
  one instrument plus the hands camera: **113 MB/s of a measured 350–400**.
  Even unpacked 12-bit on the Keeler (207) is about half capacity.
  Bandwidth is not what limits picture quality here.
- **USB 2.0, hands camera.** The tightest bus: 18 MB/s of a practical
  35–40. It rules out uncompressed 720p outright.
- **Disk.** A full 15-minute session is ~0.75 GB at the hardcoded `crf 23`;
  ~1.2 GB at `crf 20`. `kiosk.py`'s preflight reserves double. Disk is the
  cheapest resource in the system.
- **The 8-bit encode.** Every path ends in 8-bit H.264. Detail survives
  only if something moved it into a range 256 levels can carry — which is
  what a tone curve does and what more capture bits alone do not.

## Measured, 2026-09-13

Answers to what this section used to list as unknown. See DECISIONS.md's
2026-09-13 entries for how each was measured.

- **Everything is writable at open.** Region of interest, binning,
  decimation, mirroring, pixel format and the throughput cap read as
  read-only only while streaming: that is `TLParamsLocked`, not a missing
  capability.
- **Region of interest:** slit lamp 16-1600 wide with the offset fixed at
  0; Keeler 256-2056 with a small offset range.
- **Binning:** 2x2 on the slit lamp, 8x8 on the Keeler, which also offers
  2x decimation. It works and roughly doubles the light per pixel, but it
  brightens highlight and shadow alike, so it buys nothing for dynamic
  range -- and costs three quarters of the pixels.
- **Throughput cap:** absent on the slit lamp; 400 MB/s and unthrottled on
  the Keeler.
- **Mirroring:** `ReverseX`/`ReverseY` writable on both, which would retire
  the per-frame rotation in `camera.py` -- but `PixelFormat` does not change
  when mirrored, so the colour-filter phase is an open question.
- **The uEye transport layer resets pixel clock, binning and mirroring on
  every open.** All three have to be applied in `IdsCamera._open()`.
- **Host CPU:** 54% of one core for the slit lamp at 38fps free-running,
  28% for the Keeler at 20fps, capture only.

- **Bit depth costs no frame rate.** Both cameras hold 30fps with zero
  dropped frames at every depth they offer, up to the Keeler's unpacked
  12-bit at 190 MB/s. The cost is host CPU: roughly 45% of one core at
  8-bit, 60% at 12-bit, for the debayer.
- **But switching pixel format alone changes nothing downstream.**
  `_grab()` converts each buffer to BGR8 immediately, so a 12-bit capture
  arrives with the same 248 distinct levels as an 8-bit one. The extra bits
  are only worth anything if a tone curve is applied *during* that
  conversion -- see ROADMAP's Phase 2.
