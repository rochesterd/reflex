# CLAUDE.md

Standing context for this project. Read this before making changes.

## What this is

Reflex records two cameras simultaneously so optometry students at NECO
can watch themselves from a third-person view alongside the view through
the instrument they're using, played back together.

The purpose is self-directed practice for the NBEO CSE (Clinical Skills
Examination). Students book time, record themselves practising a skill on a
peer, and watch it back. The coordination between what their hands are doing
and what appears through the optics is the entire point of the recording.

## Who uses it

Students, unsupervised, with no technical background and no instructor
present. They will not read documentation, will not open a terminal, and
will not debug anything.

Design consequences:

- Intuitive to Apple's standard, not just "usable." A tech handles
  installation, configuration, and calibration; within a session, the
  student is guided entirely by the app, never by documentation.
- Protecting the student from mistakes is the goal, not minimizing what's
  clickable for its own sake. During recording, nothing that could
  interrupt an irreplaceable capture is interactive — the picker and the
  viewer buttons are disabled. Brightness is the one exception: it writes a
  camera setting, never touches the stream, and the views students need it
  for differ within one session (DECISIONS.md 2026-09-13).
- Before recording, the same goal applies with less rigidity. Today it's
  satisfied by a minimal instrument picker + Start (see `app.py`) — not
  because fewer buttons is inherently better. A more guided flow is fine
  there, provided nothing in it can bypass the readiness gates below or
  misconfigure a session.
- Failures must be **loud and early**. A black pane discovered a week later
  is the worst outcome. Prefer refusing to start over recording something
  broken.
- The start button stays disabled until the selected instrument camera and
  the third-person camera are both confirmed live, and there is enough disk
  space. No instrument selected counts as not ready — Start stays disabled
  and the status line prompts for a selection, the same way it prompts for
  a missing camera. Readiness gates cover what a student *can't* see — a
  camera that isn't running, a frozen picture, a full disk. A visibly wrong
  picture is not a gate: the live preview already shows it, and stranding
  an unsupervised student with a disabled Start is its own failure. See
  DECISIONS.md's 2026-09-10 entry.
- Never make the irreplaceable data depend on a step that can fail. Frames
  get written as they arrive; anything derived comes afterward.

## Hardware

The software is built around these components; `SUPPORTED_HARDWARE.md`
covers tested alternatives, what should work, and what's excluded.

| Instrument | Camera | Interface | Notes |
|---|---|---|---|
| Haag-Streit BI 900 slit lamp | IDS UI-3250CP-C-HQ Rev. 2 | USB 3.0 | 1600x1200, legacy uEye family — needs the uEye Transport Layer. Its pixel clock resets to 24 MHz on every open (an ~87 ms frame period); `device_presets.py` sets 80 MHz → 30 fps and applies `rotate_180`. |
| Keeler Vantage Plus Digital | IDS U3-327xCP-C | USB 3.0 | 2056x1542, USB3 Vision, native to IDS peak. `device_presets.py` applies `flip_vertical`. The *older* BIO needs **no** correction (`net2860_winusb_camera.py`) — don't assume the two BIOs share a transform. |
| Third-person (student's hands) | ELP-USB100W03M-L21 | USB 2.0, UVC | Plain UVC webcam; resolution queried at runtime. Identified by VID/PID via `settings.py`, with a single-device fallback (DECISIONS.md). Any UVC-compliant webcam should work. |

The instrument cameras are machine vision cameras, not webcams — no RTSP,
ONVIF or DirectShow; raw frames through the IDS peak SDK on a host PC. The
third-person webcam goes through `cv2.VideoCapture` (`uvc_camera.py`).

**Bandwidth:** one instrument plus the webcam, measured clean at 30fps;
per-format costs and ceilings are in `IMAGING.md`. USB3 Vision drops frames
silently rather than erroring: trust measured throughput over datasheets,
and re-measure if a second concurrent camera is ever added.

## Camera configuration: who decides what

Neither instrument camera is a Keeler or Haag-Streit camera: both are
generic IDS machine-vision cameras, and Reflex talks to IDS peak directly,
**not** through Keeler's Kinexis. So there is no instrument-maker sensor
configuration to inherit, and IDS's power-on defaults were measured
unusable here (a near-black frame pointed straight at a lamp). See
DECISIONS.md's "Camera configuration: which layer owns what" entry.

| Layer | Owns | Where it lands |
|---|---|---|
| Keeler / Haag-Streit | the **optics** — light path, image orientation, how much light reaches the sensor at all | can't be configured; compensate in `device_presets.py`, or improve the instrument's own illumination |
| IDS | sensor capability, generic defaults, and **which features exist at all** — the transport layer decides: the uEye one (slit lamp) publishes no auto exposure/gain/white balance, the USB3 Vision one (Keeler) publishes all three | defaults are a starting point, not an answer. IDS peak Cockpit stays the expert tool for sensor-level work (black level, pixel format) — don't reimplement it |
| Developer | the answers, once known | `device_presets.py` for per-model facts; hard constraints belong in the algorithm, not a settings dialog |
| Technician | *this room's* light | `settings.py` → `config.json`, one action per decision |
| Student | nothing | already true — keep it that way |

Rules that follow from this:

- **Push decisions up the table; don't add an "Advanced" tab.** A knob a
  technician can set wrong is worse than a value the app derives. An
  advanced panel is usually a sign a preset hasn't been decided yet.
- **The app holds the values, the camera doesn't.** GenICam cameras
  persist `ExposureTime`/`Gain` across power cycles, so "set it once in
  Cockpit" appears to work — but that is invisible, unversioned state that
  walks off the moment a camera is swapped or reset. `config.json` values
  have provenance and get reapplied in `IdsCamera._open()`.
- **Report what a calibration cost, not just that it succeeded.** A
  technician with no imaging background can judge "30fps, gain 2.6× of
  4.0 max"; nobody can judge "87208.816".
- **Exposure time is a frame-rate budget.** Anything above
  `1/recording.fps` costs frame rate *and* adds motion blur to exactly
  the motion this app exists to record. That is a constraint, not a
  preference — it belongs in code.
- **Check what actually gates a limit before designing around it.** A
  limit that appears in two places at once is worth one query to the
  device before it becomes an assumption — the slit lamp's "11 fps limit"
  and "87.2 ms maximum exposure" were one unset pixel clock.

## Architecture

`BaseCamera` in `camera.py` is the boundary. It owns the capture thread, the
bounded queue, and the latest-frame slot. Subclasses implement only `_open`,
`_close`, `_grab`, and `resolution`.

**Nothing outside a camera module may import or reference the IDS SDK.**
Everything downstream — compositor, encoder, GUI — works against
`BaseCamera` and `Frame`. This is what allows development against
`SyntheticCamera` without hardware attached, and what will make a third
instrument cheap to add.

Frames carry a monotonic timestamp taken at grab time. Never assume frames
arrive at the nominal rate; always use the timestamps.

Cameras are identified by **serial number**, never by device index. Index
order changes across reboots and USB port changes and is the most common way
setups like this silently break. The third-person UVC camera is the one
documented exception — see the Hardware table.

Only one instrument camera runs at a time. The student selects the
instrument before starting; the other one's camera stays stopped, and
switching stops the running instrument camera and starts the new one. The
third-person camera runs for the app's whole lifetime. `kiosk.py`'s
`KioskController` owns this — `select_instrument()` is the only thing that
starts or stops an instrument camera.

`BaseCamera` exposes frames two ways, and consumers must pick the one that
matches what they're doing:

- **`get_latest()`** — peeks the most recent frame without touching the
  queue. For display: `preview.py` polls this every UI tick and is allowed
  to skip frames it never asked for.
- **`read(timeout)`** — pops the next queued frame, so a consumer that
  drains it in order can detect gaps in `Frame.index` and count them as
  drops. For recording: `recorder.py` drains both queues this way precisely
  so `session.json` can report real dropped-frame counts.

Getting these backwards is the likely failure mode if a third consumer gets
added: `get_latest()` in a recorder undercounts drops silently, `read()` in
a UI poll loop can stall the display waiting on a queue.

## Modules

Each module's docstring has the detail; this table is the map and the one
constraint per file a reader would otherwise break. Every `test_<name>.py`
tests `<name>.py` and says how in its own docstring.

| File | Role |
|---|---|
| `camera.py` | `Frame` and abstract `BaseCamera` (capture thread, bounded queue, latest-frame slot). The per-frame `orientation` (`VALID_ORIENTATIONS`, `apply_orientation()`) is applied in `_run()` before queueing, so every consumer sees the same image. |
| `exposure_calibration.py` | The pure maths (numpy only) behind `IdsCamera.auto_calibrate()`: metering modes (`METERING_HIGHLIGHT` for bright-beam-on-black frames), `exposure_budget_us()`, and `next_exposure_gain()`, which prefers exposure up to the budget and the least gain that will do. See DECISIONS.md's 2026-09-08 entries. |
| `device_presets.py` | The supported-camera registry: `DeviceProfile`/`PROFILES` (what settings.py offers and `config.json`'s `profile` resolves through), plus `orientation_for_model()`/`pixel_clock_hz_for_model()`/`black_level_for_model()` reading that same table, so a preset and a picked profile can't disagree. `CUSTOM_PROFILE_ID` is deliberately absent from `PROFILES`: unknown degrades to custom, never to a failed load. Consulted by `IdsCamera._open()`; a `config.json` `orientation` overrides it. The older BIO's (no-op) orientation is defaulted in `net2860_winusb_camera.py` instead, since it has no IDS model string. Where this is headed: ROADMAP.md's device-profiles entry. |
| `synthetic_camera.py` | `SyntheticCamera` — generated frames with a burned-in counter; `latency`/`drop_rate` knobs exercise failure paths without hardware. |
| `uvc_camera.py` | `UvcCamera` — the third-person webcam via `cv2.VideoCapture`: `device` (a DirectShow index, for Preview) or `vid_pid` (resolved at `start()`, the runtime path). `Frame.index` is self-counted. Reopens itself after ~0.5 s of failed reads, since `cv2.VideoCapture` never recovers on its own. |
| `uvc_enumeration.py` | `list_uvc_devices()` (index/name/VID:PID in `CAP_DSHOW`'s open order, via `pygrabber`) and `resolve_device()` (the single-device fallback and ambiguity rules). |
| `ids_camera.py` | `IdsCamera` — both IDS GenICam cameras, opened by serial; resolves an unset `orientation` from `device_presets` by `ModelName()`. `list_ids_devices()` feeds `settings.py`'s dropdowns. |
| `winusb.py` | ctypes over inbox `winusb.dll`/`setupapi.dll` — no vendor SDK. Discovery by VID/PID, control transfers, alternate settings, `IsochReader`. Knows USB, not frames: `net2860_winusb_camera.py` is its only importer, the same boundary drawn around the IDS SDK. |
| `net2860_winusb_camera.py` | `Net2860WinUsbCamera` — the older BIO's KS722OUP (EM2860 bridge), `kind: "net2860_winusb"`. The camera board runs its own AE/AWB, so there is no exposure/gain to offer; the bridge's picture registers are ours and applied at open from `net2860_init.PICTURE_DEFAULTS` (Keeler's values, chosen and hardware-confirmed 2026-09-13), overridable per camera via `picture=`. `Frame.index` is the camera's own field counter, so its gaps are **real** drops. |
| `net2860_init.py` | `START_WRITES`/`STOP_WRITES` — the EM2860 init sequence captured from Keeler's driver and verified by register readback. `STOP_WRITES` is kept only as the record of where the vendor's session ended — nothing replays it, and replaying it at startup was tested harmless (2026-09-13), so the old reason for the split is retired. Picture values live in `PICTURE_DEFAULTS`. Its docstring says how to re-derive it; the capture is gitignored and irreplaceable. |
| `config.py` | `load_config()` reads `config.json` (template: `config.example.json`), raising `ConfigError` before `QApplication` exists. Default paths split on `is_frozen()`: CWD in dev, `%ProgramData%`/`%PUBLIC%\Documents` when frozen (DECISIONS.md's "Frozen-exe installer built"). `exposure_fps_warnings()` reports, never raises. |
| `compositor.py` | Aspect-preserving, letterboxed layouts, and `compose_layout()` — the one place deciding which stream goes where. `viewer.py` and `session_export.py` both render through it, so what a student sees is what they export. Called at watch time, never at record time. |
| `session_format.py` | The on-disk vocabulary of a session (`SESSION_FORMAT_VERSION`, role names, `MANIFEST_NAME`). No imports, so readers don't pull in the writer — which keeps the encoder out of the viewer-only build. |
| `session_export.py` | `export_session()` — one constant-frame-rate MP4 in a chosen layout. Writes a `.partial.mp4` and moves it into place, so a cancelled or failed export leaves nothing that looks finished; never overwrites a stream file. No Qt. |
| `session_reader.py` | `Session.load()` (`format_version: 2` only), `list_sessions()` (newest first, skipping unreadable ones), and `SessionPlayer` — per stream, the last frame at or before media time *t*; alignment is by PTS alone. No Qt. |
| `viewer.py` | `ViewerDialog` (play, scrub, live layout picker, cancellable Export) and `SessionPickerDialog` (with "Open a recording folder…", which a review machine needs). `QDialog`s so `app.py` opens them modally and `main()` runs standalone as `viewer.exe`. Never modifies a recording. Tears down on `finished`, not `closeEvent`: it holds open PyAV decoders. |
| `app_icon.py` | `icon_path()` — `assets/` in a checkout, `sys._MEIPASS/assets` when frozen. Stdlib only, since the tkinter `setup_wizard.py` needs it too; a missing file gives a null `QIcon`, never an exception. |
| `qt_image.py` | `bgr_to_pixmap()` — the one BGR-to-`QPixmap` conversion, shared by every live-feed window. |
| `neco_reflex_theme.py` | Brand palette and fonts — constants only, no imports (a test enforces it); every brand color or font reads from here. The brand spec is the Notion page "NECO Reflex — Branding Decisions", deliberately not restated in this public repo. Banner reds/ambers and the black behind video panes are not brand colors. |
| `reflex_style.py` | `apply()` — Fusion style, palette, font and a stylesheet keyed on object names. Called by `app.main()`/`viewer.main()` only; `settings.py` and `preview.py` stay native. |
| `reflex_mark.py` | `ReflexMark` — the cat-eye mark, the kiosk's recording indicator. The pupil opens from slit to round, shape and color together, so the state survives grayscale. `set_recording()` is idempotent (called every poll tick). No focus, no clicks. |
| `branding/` | The mark's SVG geometry (`test_reflex_mark.py` holds the widget to it) and `build_icons.py`, which writes `assets/*.ico`. Not packaged. |
| `preview.py` | Dev tool: two live cameras, layout dropdown, frame-index/skew readout, via `get_latest()`. Has no Start/Stop discipline. |
| `recorder.py` | Two separate VFR files on one shared clock, no compositing. A `_StreamWriter` per camera drains with `read()`, stamps ms PTS from the session origin, enforces the `recording.fps` ceiling, then remuxes MKV→MP4, verifies, and deletes the MKV. Writes `session.json` v2. See DECISIONS.md's "Recorder/Viewer split" entries. |
| `retention.py` | Opt-in cleanup of old sessions (`config.json`'s `retention`, off by default), once at `app.py` startup: an age sweep plus a low-disk pass. Never touches a non-session folder, a session without `session.json`, or the newest session. |
| `kiosk.py` | `KioskController` — the state machine (idle/ready/recording/error): the `select_instrument()` lifecycle, preflight (liveness, **freshness**, disk space), stall and freeze detection, and the session time limit (`MAX_SESSION_MINUTES` — reaching it is a normal stop, and it's the session length the disk preflight budgets for). Freshness (`_frame_signature()`) catches a camera that delivers frames but has stopped *seeing*. No Qt. |
| `app.py` | The kiosk: a thin PySide6 shell over `KioskController` — the Reflex mark, instrument picker, **Start Recording**, **Stop Recording** (the status line counts up against the session limit), a three-step **Brightness** control that stays live while recording, **Watch Last Recording** (modal; no browsing of other sessions — DECISIONS.md 2026-09-13; `_with_preview_paused()` stops the preview around them while the cameras keep running). Picker and viewer buttons disable while recording. Owns no decisions. |
| `settings.py` | Technician tool: per role a device dropdown, a profile dropdown (instrument rows only; auto-selected from the model, Custom always available), the student-facing name (pre-filled from the profile, technician's to change), and Preview (with Auto-Calibrate), then Rescan, a recordings folder, opt-in retention, and Save to `config.json`. No hot reload. A separate program from `app.py`. |
| `setup.ps1`, `setup_wizard.py` | Developer-machine bootstrap (venv, requirements, IDS runtime check), and its tkinter GUI — tkinter because it runs before PySide6 is installed. Not part of any clinic machine's path; see `SETUP.md`. |
| `packaging/*.spec` | PyInstaller specs for the three exes. `viewer.spec`'s `excludes` (`ids_peak`, `ids_peak_ipl`, `pygrabber`, `comtypes`) is an assertion: if the viewer ever reaches camera code, the build fails loudly. See `PACKAGING.md`. |
| `packaging/reflex.iss` | Clinic installer: `app.exe` (Desktop and Start menu), `settings.exe` (Start menu only), the bundled IDS peak installed silently, and the legacy BIO's driver package. No `viewer.exe` — `app.exe` contains the viewer. `AppId` is pinned to `reflex`; changing it orphans existing installs. |
| `packaging/reflex-viewer.iss` | Viewer-only installer (~90 MB): `viewer.exe`, per-user, no admin, Desktop shortcut. Its own `AppId`, so it coexists with a clinic install. See DECISIONS.md's "Recorder/Viewer split, phase 4: two installers". |
| `packaging/net2860_winusb/` | `build_driver_package.ps1` builds and signs the legacy BIO's WinUSB driver package. Re-run it after any change to the INF — a stale catalogue fails only at install time (`PACKAGING.md` step 2b). |
| `tools/` | Standalone hardware diagnostics (IDS and legacy-BIO smoke tests, two-camera bandwidth, third-person stall watch, legacy-BIO identity dump). Each documents itself; nothing imports them. |

Who runs what: students run the frozen `app.exe` (the clinic installer's
Desktop shortcut) and `viewer.exe` (the kiosk's Watch buttons, or the
viewer-only installer) — never point a student at anything else.
`settings.py` is the technician's, following `CALIBRATION.md`; `preview.py`
and `setup.ps1`/`setup_wizard.py` are developer tooling (`SETUP.md`).

## Recording output

Each recording writes to `<sessions_dir>/<YYYY-MM-DD_HHMM>/`
(minute-collision gets a `_2`, `_3`, ... suffix rather than overwriting).
`sessions_dir` defaults to a relative `sessions/` folder in dev/test, or
`%PUBLIC%\Documents\Reflex\sessions` in a frozen install unless a
technician picked somewhere else via `settings.py`'s Browse field (see
`config.py`'s `resolve_default_sessions_dir()`):

- `instrument.mp4`, `third_person.mp4` — one file per camera, at that
  camera's **native resolution**, **variable frame rate**. Nothing is
  composited at record time. Fixed filenames by *role*, so the Viewer and
  Export never have to consult the manifest to find them.
- `session.json` — `format_version: 2`, the manifest the Viewer reads: the
  instrument, the shared clock origin, `duration_s` (so a recordings list
  needn't open a video), and per stream its resolution, frame count,
  dropped frames (gaps in `Frame.index`, not estimated), offsets and
  `verified`. `session_format.py` and `recorder.py` have the full schema.
- `<layout>.mp4` — only if a student used the Viewer's Export. A rendered
  single-file composite, not part of the recording; safe to delete.
- `<role>.mkv` — written live during capture, interruption-safe; exists
  only as the crash-safe copy. Each stream's `stop()` remuxes it (stream
  copy, no re-encode) to `<role>.mp4`, verifies that (first frames must
  actually decode — the dropped-keyframe failure in DECISIONS.md's
  packet-filter entry — and packet count must match), then deletes the
  MKV. A stream that fails verification keeps **both** files and is
  flagged `verified: false`. Never both deleted.

**Synchronization is by timestamp, not by frame pairing.** Every frame's
PTS in every stream is `Frame.timestamp - clock.origin_monotonic`, on a
1/1000 time base. Two frames with equal PTS in different files were
grabbed at the same instant — that is the whole sync story. A slower
camera (the older BIO at 25fps beside a 30fps third-person) simply has
fewer frames spanning the same interval; the Viewer holds its last frame
rather than anything interpolating or duplicating.

Everything under `sessions_dir` (gitignored as `sessions/` in dev) is the
deliverable handed to a student — data, never something to regenerate.

## Environment

- Windows, Python 3.13, venv at `.venv` (activate before running anything;
  `setup.ps1`/`setup_wizard.py` script this — see SETUP.md)
- Dependencies, pinned in `requirements.txt`: numpy, opencv-python, av
  (PyAV), PySide6, pygrabber + comtypes (UVC enumeration via DirectShow).
- IDS peak (drivers and transport layers) is installed separately per
  machine — it includes kernel drivers. The `ids_peak`/`ids_peak_ipl`
  bindings come from PyPI, pinned in a separate `requirements-ids.txt` so
  they match the installed runtime without being required elsewhere.
- Development happens on a machine without cameras attached; use
  `SyntheticCamera` and don't hard-code anything that must be measured.
- The `ids_peak`/`ids_peak_ipl` bindings are compiled, with no readable
  source. `vendor/ids_peak_api.txt` (gitignored, regenerate via
  `inspect`/`dir()`) is the authoritative API reference. Don't invent IDS
  method names or signatures — if something isn't in that dump, say so.

## Conventions

- Values that depend on measurement belong in config, not source — unless
  the device can report them when needed, which beats config. Role
  assignment (serials, labels, the webcam's VID/PID) lives in `config.json`
  (gitignored; `config.example.json` is the template), written by
  `settings.py`, never hand-edited. The recording canvas is device-derived
  (`width`/`height` default to `None`: the sum of both cameras'
  `.resolution`). `recording.fps` stays config, deliberately *not* read
  from the device — datasheet fps is the number not to trust. See
  DECISIONS.md's "Config-driven recording fps, device-derived canvas size".
- Record to MKV during capture, remux to MP4 afterward, then verify the
  MP4 and delete the MKV — never delete both (a failed verification keeps
  the MKV). An interrupted MKV is still playable; an interrupted MP4 is
  lost. When remuxing, filter packets on `packet.size == 0`, not
  `packet.dts is None` — see `DECISIONS.md`.
- For variable-frame-rate output, set **both** `stream.time_base` and
  `stream.codec_context.time_base`. Setting only the stream leaves the
  encoder at 1/fps, silently rescaling ms PTS into 1/fps ticks; DTS goes
  non-monotonic and the failure surfaces only at the MP4 remux. See
  `DECISIONS.md`.
- Prefer dropping frames over blocking a capture thread. A blocked capture
  thread stalls the device.
- "The camera is delivering frames" is not "the camera is working." A
  blocked or switched-off camera can keep a stream alive on a replayed
  image while `Frame.index` climbs. `kiosk.py` compares consecutive
  frames' pixels (`_frame_signature()`) to tell the difference; any new
  liveness check should ask what the camera *sees*.
- When a decision has a non-obvious reason behind it, add an entry to
  `DECISIONS.md` rather than a comment.
- Tests use stdlib `unittest`, not pytest — pytest isn't a project
  dependency. Prefer integration-style tests that spin up real
  `SyntheticCamera` instances and check real decoded output over mocking
  internals; that's what caught the remux bug above.

## Documentation

| File | For | Budget | Holds |
|---|---|---|---|
| `CLAUDE.md` | Claude Code, contributors | ≤310 lines, ≤26 KB | Rules and pointers, not stories. A Modules row is a role plus the one constraint a reader would break; tests, `tools/` and spikes get none. |
| `DECISIONS.md` | contributors | ≤60 lines per entry | Why, dated, append-only — including a spike's findings and where its irreplaceable inputs are backed up. |
| `ROADMAP.md` | the developer | ≤150 lines | Open plans only. The commit that adds a plan's DECISIONS entry deletes it and repoints anything citing it. |
| `SETUP.md`, `PACKAGING.md` | contributors | ≤190 / ≤340 lines | Steps and warnings; no dated status paragraphs. |
| `CALIBRATION.md` | technician | ≤185 lines | A self-contained procedure — its reader has no repo. |
| `SUPPORTED_HARDWARE.md` | other institutions | ≤90 lines, ≤6 KB | One row per tested device, and the measured bandwidth. |
| `IMAGING.md` | contributors | ≤120 lines | What each camera's stack exposes and what each option costs. Facts as measured; no plans, no rationale. |

- Over budget means cut before adding. One fact, one home; everything else
  points at it.
- Spike code is deleted when its production module lands. No per-folder
  READMEs, and no new `.md` without a row here.
- Audit by hand now and then: sizes against these budgets, backticked names
  that no longer exist, and facts stated twice.
