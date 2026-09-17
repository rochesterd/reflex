# Supported Hardware

What's actually been run against this codebase, what's expected to work
but hasn't been tried, and what's explicitly out of scope. CLAUDE.md's
Hardware table describes *this kiosk's* current fixed cameras; this file
is broader — it's the reference for "will camera X work here" at a new
institution. See DECISIONS.md's 2026-08-18 "config.json + loader" entry
for the project this supports.

## Confirmed tested

| Camera | Interface | Tested | Notes |
|---|---|---|---|
| IDS UI-3250CP-C-HQ Rev. 2 (serial 4103484089), on a Haag-Streit BI 900 slit lamp | uEye Transport Layer | 2026-08-12 — DECISIONS.md "Slit lamp camera smoke test" | No auto-exposure: a technician calibrates it in `settings.py`'s Preview (`CALIBRATION.md`). Gain ceiling 4.0x. `device_presets.py` raises its 24 MHz power-on pixel clock to 80 MHz (30 fps, 0 dropped over 150 s) and applies `rotate_180`, visually verified 2026-09-09. See DECISIONS.md's 2026-09-08 pixel-clock entry. |
| IDS U3-327xCP-C (serial 4110050487), on a Keeler Vantage Plus Digital BIO | Native USB3 Vision | 2026-08-12 — DECISIONS.md "Hardware smoke test found two real IdsCamera bugs; both fixed" | Calibrated by a technician like the slit lamp; its own auto-exposure at open is only the fallback, since it runs before the instrument is in use (DECISIONS.md "Converge-at-open was the wrong thing to trust"). That auto-exposure is bounded to the frame-rate budget: 30.0 fps measured. Pixel clock fixed at 197 MHz. `device_presets.py` applies `flip_vertical`. |
| "HD USB Camera" (VID 32E4:PID 9310), presumed ELP-USB100W03M-L21 | UVC (`cv2.VideoCapture`, `CAP_DSHOW`) | 2026-08-17 — DECISIONS.md "UvcCamera hardware-verified against the real ELP camera" | Model inferred from VID/PID and inspection, not a product string. First frame in 0.9-1.6 s. **Autofocus/auto-exposure lock not yet verified on this device** — see DECISIONS.md "Lock UVC autofocus/auto-exposure after a warmup window". |
| NET GmbH "KS722OUP" (eMPIA EM2860 bridge, VID 0x20F1:PID 0x0004), on an older Keeler Vantage Plus Digital BIO, `kind: "net2860_winusb"` | Inbox `winusb.sys`, driven in-process by ctypes | 2026-09-09/10 — DECISIONS.md's WinUSB entries | 720x576 interlaced PAL, 25 fps; 20.74 MB/s sustained with zero packet errors. A real session recorded 301 frames at 25.11 fps with 0 dropped, counted by the camera's own field counter. No exposure/gain/white balance to offer: the camera board runs its own AE/AWB. Needs the signed driver package from `packaging/net2860_winusb/`. |

**Bandwidth, measured 2026-09-08.** Only **one** instrument camera
streams at a time (`select_instrument()` — see CLAUDE.md's Architecture
section), so the worst case is one instrument plus the third-person
webcam: at 30fps roughly 94 MB/s for the Keeler (2048x1536 Bayer8) or
115 MB/s for the slit lamp (1600x1200 Bayer12), plus a 640x480 webcam — well inside
the 350-400 MB/s a single USB 3.0 controller delivers. Measured with both
streaming, counting device-side `Frame.index` gaps:

| run | result |
|---|---|
| slit lamp @ 80MHz + webcam, 150s | 30.01 fps, **0 dropped** |
| slit lamp @ 60MHz + webcam, 150s | 28.57 fps, **0 dropped** |
| BIO + webcam, 60s | 1807 + 1688 frames, **0 dropped** |
| several 10s `KioskController` sessions, both instruments | **0 dropped** |

Caveat worth keeping: the third-person camera in these runs was a laptop
integrated webcam, not the ELP that ships. Its bandwidth share is small,
but the numbers above are not a substitute for one run on the real clinic
machine with the real camera.

## Expected to work, untested

- **Any IDS peak / GenICam camera via native USB3 Vision** — same class as
  the Keeler above. `ids_camera.py`'s `IdsCamera` is built generically
  against GenICam's standard node/feature model rather than per-model
  (see DECISIONS.md's "One IdsCamera class for both real cameras" entry),
  specifically so this would be cheap.
- **Any IDS peak / GenICam camera via the legacy uEye Transport Layer
  bridge** — same class as the slit lamp above. Requires the uEye
  Transport Layer installed separately (SETUP.md Section 3); that
  transport layer only exposes a basic GenICam feature set (freerun/
  triggered acquisition, exposure, pixel clock) — a camera needing
  something beyond that may hit a gap `IdsCamera` doesn't handle.
- **Any UVC-compliant USB webcam**, for the third-person role.
  `uvc_camera.py` has no ELP-specific code — UVC is itself the
  generalization, and resolution is queried at runtime rather than
  assumed. `uvc_enumeration.py`'s VID/PID identification works the same
  way regardless of make/model.

## Explicitly excluded

- **Cameras that only work through the old, pre-GenICam uEye SDK.** A
  different SDK entirely, not a config difference — the uEye Transport
  Layer above is what makes an *older uEye-family sensor* reachable
  through modern IDS peak/GenICam. A camera with no GenICam path at all
  needs the legacy SDK, which this codebase doesn't implement. "Buy a
  currently-supported camera" is the answer here, not "support two SDKs."
- **GigE-connected IDS cameras.** Untested Transport Layer, not expected
  to be encountered in a single-host-PC setup like this one.

Both exclusions are revisitable if a real setup turns up that needs them.

## Reporting a newly-tested device

1. Confirm identity: for an IDS camera, the serial + `ModelName()` from
   `ids_camera.list_ids_devices()` (or `settings.py`'s dropdown). For a
   UVC camera, the friendly name + VID:PID from
   `uvc_enumeration.list_uvc_devices()` (or `settings.py`'s dropdown).
2. Add a dated entry to `DECISIONS.md` describing what was tested and
   found — what worked, what didn't, any limitation (like the slit lamp's
   fixed exposure or the ELP's warm-up latency above). This file stays a
   thin index into those entries, not a duplicate of them.
3. Add a row to "Confirmed tested" above, cross-referencing that entry —
   this is how every row above got there.
