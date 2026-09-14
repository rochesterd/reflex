# Decisions

Append-only. Newest at the bottom. One entry per decision that has a reason
someone might otherwise undo by accident.

Format: what was decided, why, and what was rejected.

---

## 2026-08-11 — Purpose-built application rather than OBS

**Decided:** Build a dedicated app instead of configuring OBS Studio as a
hidden recording engine.

**Why:** OBS is designed around having an operator. It works as a kiosk
engine, but its failure modes all require one — a replugged camera shows
black and needs re-selecting, a full disk stops recording silently, a
Windows update can reorder device enumeration and swap the panes. None of
these produce a useful error message; they produce "the video was black,"
discovered later. With unsupervised students the requirement isn't just that
a novice can use it, but that a novice can rely on it. A purpose-built app
can check both cameras before enabling the button, verify disk space, and
fail loudly mid-session.

**Rejected:** OBS + Source Record plugin + foot pedal + Lua post-stop
script. Faster to build, and still a reasonable prototype, but not
deployable unsupervised.

---

## 2026-08-11 — Composite live, not in post-production

**Decided:** Stack the two frames in memory during capture and encode one
output stream, rather than recording separately and combining afterward.

**Why:** The composite is the deliverable — students want to see hand
movement against the optical view, and they want it immediately while the
muscle memory is fresh. Live compositing makes the two streams synchronised
by construction: no offset calculation, no drift, no render wait, no post
step that can fail. A two-minute render is two minutes of a student losing
interest.

**Consequence:** The individual raw files drop from "deliverable" to
"debugging artifact." Keep writing them if disk allows, but nothing depends
on them.

**Rejected:** Separate capture with timestamp sidecars and an FFmpeg
`hstack` pass afterward. Necessary only if live compositing can't keep up,
which it should at 30fps.

---

## 2026-08-11 — Wide canvas, not a 1080p split

**Decided:** Default output 2560x1080 for side-by-side, giving each pane a
full 4:3 frame. Picture-in-picture (1920x1080, optical view large, room view
inset) offered as an alternative.

**Why:** Splitting a 1080p canvas evenly gives each pane 960px of width. The
slit lamp camera's 1600x1200 or 2056x1542 gets crushed, and the fine optical
detail is exactly what makes the recording worth watching. The room view is
about gross hand and body position and survives being small; the optical
view does not.

**Open:** Which layout students actually prefer. Worth asking two of them
before the UI is finalised — cheap to answer now, expensive later.

---

## 2026-08-11 — Camera abstraction with a synthetic implementation

**Decided:** `BaseCamera` defines the interface; `SyntheticCamera`
implements it with generated frames (burned-in counter, timestamp, sweeping
bar, optional artificial latency and drop rate).

**Why:** Development happens on machines without the cameras attached. More
importantly, a synthetic source with a visible frame counter makes dropped
frames and sync errors *observable* in a way real cameras never do, and
lets failure paths (camera disappears, queue backs up, frames stop) be
triggered on demand rather than by unplugging things.

**Consequence:** The IDS-specific code stays isolated in one module instead
of spreading through the app, which is what will make adding a third
instrument cheap.

---

## 2026-08-11 — Python 3.13, not 3.11

**Decided:** Python 3.13 (3.13.15).

**Why:** 3.11 was the initial choice on the theory that it maximised
compatibility with older IDS wheels. That was wrong. 3.11 is in
security-fixes-only mode with no binary installers since 3.11.9 (April
2024), so installing it today means a Python missing two years of patches —
which a campus IT security scan will flag. The IDS wheels are tagged
`cp311-abi3`, meaning forward-compatible with any Python 3.11+, so 3.13
works fine. 3.13 has binary installers and regular bugfix releases.

**Rejected:** 3.14 — current and fine, but 3.13 has had longer for the
ecosystem to settle, and IDS builds against 3.11+.

---

## 2026-08-11 — Correct for inter-camera latency, don't ignore it

**Decided:** Measure the fixed pipeline latency difference between the two
sources once, then delay the faster stream through a small ring buffer
before compositing. Store the offset in config, re-verify after any camera
or driver change.

**Why:** Different capture paths have different buffering. If the newest
frame from each is naively stacked, one view lags the other by several
frames — and the relationship between the two views is precisely what the
student is trying to study. An uncorrected offset doesn't look like a bug,
it looks like bad technique.

**How to measure:** Wave a hand across both fields of view and count the
frame offset between the two streams.

---

## 2026-08-11 — Licensing is a non-issue at NECO, but stays worth watching

**Decided:** Use x264 via PyAV, and PySide6 or PyQt6 as convenient.

**Why:** GPL obligations trigger on distribution, not use. This runs on
college machines and doesn't leave the institution. Even if it were later
shared with another school, compliance means publishing source, which a
non-profit educational tool has no reason to object to.

**Watch for:** If this ever becomes something sold or distributed
commercially, the encoder choice (hardware NVENC/QuickSync instead of x264)
and the Qt binding both need revisiting. Cheaper to change before building
around them.

---

## 2026-08-11 — recorder.py ships without latency correction

**Decided:** `recorder.py` composites whatever each camera's queue has most
recently delivered, with no ring buffer or measured offset applied.

**Why:** The "Correct for inter-camera latency" decision above is still
open — it needs a measurement step and a config slot that don't exist yet.
Building it speculatively risked guessing the wrong shape. Recorder does
track per-camera dropped-frame counts (gaps in each `Frame.index` sequence)
so a real skew will at least be visible in `session.json`, not silent.

**Consequence:** Until the offset correction lands, recordings may show a
few-frame lag between panes. Do not let students start relying on frame-tight
sync before this is closed out.

---

## 2026-08-11 — PyAV remux filters on empty packets, not missing dts

**Decided:** When remuxing MKV to MP4, drop only packets where
`packet.size == 0`, not packets where `packet.dts is None`.

**Why:** The MKV demux stream's leading keyframe sometimes reports
`dts is None` even though it holds valid frame data. Filtering on that
condition silently drops the keyframe, and the resulting MP4 has valid
headers but decodes zero frames — no error, just an empty-looking file.
Found by decoding the remuxed output and getting 0 frames back instead of
the expected count.

**Rejected:** Filtering on `packet.dts is None` — looked like the obvious
"skip flush packets" check and is the kind of thing a future cleanup could
reintroduce.

---

## 2026-08-11 — Disk-space preflight: 2x a 10-minute estimate, not a flat number

**Decided:** `app.py`/`kiosk.py` block Start until free space under
`sessions/` is at least 2x the estimated size of a 10-minute recording at
Recorder's default canvas (2560x1080, 30fps), using ~0.08 bits/pixel/frame
as the libx264 crf=23 quality estimate (~498 MB for 10 minutes at those
settings, so ~1 GB required).

**Why:** A flat "500 MB free" style check doesn't scale if the canvas
resolution changes later, and undercounts by half in practice: `Recorder`
keeps both `composite.mkv` and `composite.mp4` after `stop()` (see the
MKV/MP4 decision above), so a completed session occupies roughly two
copies of the single-file estimate, not one. The 2x isn't a safety margin
bolted on top — it's what one real session needs on disk. 10 minutes was
picked as "a full CSE practice attempt," not a hard limit; recordings can
run longer or shorter, this is only a preflight tripwire.

**Rejected:** Checking free space only once at launch. Disk fills up over a
day of back-to-back student sessions; the check re-runs on a timer (same
poll as the camera-liveness check) so Start disables live if space runs
low mid-day, not just at boot.

---

## 2026-08-11 — 2-second stall timeout, and ERROR is a transient state

**Decided:** During recording, if a camera's `Frame.index` hasn't advanced
for 2 seconds, `kiosk.KioskController` stops the recording and enters
`State.ERROR` immediately. The next `poll_preflight()` call (same timer,
next tick) re-evaluates conditions and moves `state` on to READY/IDLE —
`State.ERROR` isn't a stuck state requiring a dismiss action. What
persists until the next session starts is `error_message` and
`last_session_info`, which the UI displays in a dedicated banner/summary
label independent of `state`.

**Why:** 2 seconds is short enough that "loud and early" actually means
early — at 30fps that's ~60 missed frames, well past normal jitter but
fast enough that a student doesn't practice for two more minutes against a
frozen pane before finding out. The transient-ERROR design exists because
CLAUDE.md's kiosk constraint is strictly two buttons, Start and Stop —
adding a third "acknowledge error" button to clear a stuck ERROR state
would violate that. Auto-resolving `state` while keeping the banner text
sticky gets both: the state machine never wedges, and the failure stays
visible until a student (or staff) starts a new session.

**Rejected:** A dismiss/acknowledge button. Simpler state machine, but
breaks the one-Start-one-Stop kiosk rule for a corner case that doesn't
need a third control.

---

## 2026-08-11 — `_drain_remaining` is one bounded pass, not a loop to empty

**Decided:** `Recorder._drain_remaining()` pulls whatever's currently
queued from each camera exactly once and encodes a final pair if both are
present, instead of looping until both queues are simultaneously empty.
`Recorder.stop()` also now checks `thread.is_alive()` after `join(timeout=
10.0)` and raises rather than proceeding if the capture thread is still
running.

**Why:** Found via a real crash: `app.py`'s cameras keep running (for the
live preview) well past a recording session ending, continuously refilling
their queues. The old `_drain_remaining` looped until a poll found both
queues empty at once - a condition that's only reliably reached if encoding
keeps pace with the camera's frame rate. When it doesn't (observed on a dev
machine at the full 2560x1080 canvas), the drain loop chases fresh frames
indefinitely and never returns. `stop()`'s `thread.join(timeout=10.0)`
would then time out, and - since it never checked whether the join actually
succeeded - proceed to call `stream.encode(None)` to flush from the calling
thread while the capture thread was still mid-`encode()` on the same
stream. Two threads touching one PyAV encoder produced
`av.error.EOFError: avcodec_send_frame()`.

**Consequence:** A single bounded pass can't silently loop forever, so the
join can no longer race the flush this way. If the capture thread somehow
still doesn't finish in 10s, `stop()` now raises instead of quietly
producing a corrupt or truncated `composite.mkv`.

**Rejected:** Raising the join timeout, or stopping the cameras before
calling `recorder.stop()`. Both mask the symptom without fixing the actual
mismatch between "drain until empty" and "cameras never stop."

---

## 2026-08-11 — `compositor.py` canvas fills were the frame-drop bottleneck, not the encoder

**Decided:** `_fit_into_pane` now writes resize output directly into a
`dst` view of the caller's canvas (via `cv2.resize(..., dst=...)`) instead
of building a separate array and copying it in, and only calls a new
`_fast_fill` helper when there's actual letterbox padding to cover.
`side_by_side`/`picture_in_picture` allocate their canvas with `np.empty`
instead of `np.full`, since between them the `_fit_into_pane` calls always
write every pixel.

**Why:** Recordings at the real target resolutions (1600x1200 + 2056x1542
composited into 2560x1080 @ 30fps) were dropping frames at a rate that
scaled linearly with duration - a sustained per-tick deficit, not a
one-time cost. Instrumenting every pipeline stage (`side_by_side` →
`draw_timer` → `VideoFrame.from_ndarray` → `.reformat(yuv420p)` →
`stream.encode()` → `container.mux()`) found the cause: `np.full(shape,
(0,0,0))` - a *tuple* fill value - forces numpy into a slow element-wise
broadcast loop instead of a memset. Three of these ran per frame (the
outer canvas plus one per pane), costing ~23ms of a ~36-39ms
`side_by_side` call, out of a ~50-53ms total tick against a 33.3ms budget
at 30fps. The encoder itself was cheap (~3ms even at "ultrafast") and was
never the bottleneck - which is why the earlier `veryfast` → `ultrafast`
preset swap "reduced but didn't eliminate" drops: it optimized a small
piece of the problem. The outer canvas fill in `side_by_side` was also
pure waste on top of being slow: it's unconditionally fully overwritten by
the two pane writes that follow it, so nothing ever read the color it
just spent 11.5ms painting.

**Measured effect** (30s recording, real `Recorder`, full production
settings): dropped frames per camera went from a sustained ~37% of frames
(336/899 in a matched benchmark run) to **2 out of 901** - not a tuning
change, a bug fix. `side_by_side` cost dropped from ~36ms to ~13ms mean.
Pixel output is unchanged (verified byte-identical against the prior
implementation across several letterbox/background-color cases before
replacing it).

**Rejected:** Increasing `BaseCamera`'s queue size to buffer more frames.
That only delays when drops start - it doesn't fix the underlying
per-tick deficit, so drops would still accumulate linearly, just from a
later starting point. Shrinking the recording canvas was also rejected -
it would help the same way (less work per frame) but directly undoes the
"wide canvas, not a 1080p split" decision above, and wasn't needed once
the actual bug was fixed.

---

## 2026-08-12 — IDS Python bindings install from PyPI, pinned — supersedes the local-wheel instruction

**Decided:** Install `ids_peak` and `ids_peak_ipl` from PyPI, pinned to
exact versions in `requirements-ids.txt`, instead of installing wheels out
of the local IDS peak installation directory. This supersedes the
local-wheel instructions that previously lived in `SETUP.md`'s IDS peak
section — a future reader should not reinstate them.

**Why:** As of IDS peak 2.10, IDS stopped bundling the Python wheels in
the Windows setup and publishes them on PyPI instead. Confirmed on this
machine running IDS peak 26.06.1-943: `ids_peak\generic_sdk\api` contains
only `cmake_finder`, `doc`, `include`, `lib` — no `binding` folder — and a
recursive search of the whole install tree for `*.whl`, `*.pyd`, and
`ids_peak*.py` returns nothing. The Custom installer's component tree
offers no Python binding option either. The original "install from the
local install, not PyPI" reasoning was sound when written against IDS
peak 2.x, where the wheels genuinely lived there and PyPI had nothing
IDS-published to install against. It describes a layout that no longer
exists in 26.06, not a wrong idea at the time.

**What still holds:** The underlying concern — binding version must match
the installed SDK runtime — is real and unchanged. It's now enforced by
pinning `ids_peak`/`ids_peak_ipl` versions in `requirements-ids.txt`
rather than by installing from a runtime-local path. Drivers and
transport layers (`ids_ueyegentl` for the UI-3250, `ids_u3vgentl` for the
U3-327x) still come from the IDS peak Windows installer, per machine, same
as before — only the Python bindings moved. Bindings and runtime must be
upgraded together: a peak update on a lab machine without a matching
bindings pin fails at device enumeration, not at import time.

**Rejected:** Leaving the local-wheel instructions in place and noting
PyPI as an alternative. IDS peak 26.06 doesn't ship a local wheel option
at all, so "install from local, or PyPI" isn't actually a choice — the
local path is gone, and presenting it as one path among two would send
the next reader on the same dead-end search this entry documents.

---

## 2026-08-12 — One IdsCamera class for both real cameras, not one per model

**Decided:** `ids_camera.py` has a single `IdsCamera(BaseCamera)`,
parameterized by serial number, used for both the Haag-Streit slit lamp
(UI-3250CP-C-HQ) and the Keeler (U3-327xCP-C).

**Why:** `vendor/ids_peak_api.txt` shows no camera-family-specific
classes in the bindings — `Device`, `DataStream`, `NodeMap`, `Buffer` are
all generic GenICam/GenTL types. Once the uEye Transport Layer is
installed (SETUP.md Section 3), both cameras enumerate through the same
`DeviceManager` and the same buffer/acquisition lifecycle applies to
both. The only real difference between them (resolution, Bayer pattern)
is read from each device's own node map and each captured buffer at
runtime, not hard-coded per model — so a second class would duplicate the
entire acquisition path for no behavioral difference.

**Consequence:** Frames are converted from whatever Bayer pattern the
buffer reports to BGR8 per-frame via `ids_peak_ipl.Image.ConvertTo`,
rather than assuming a fixed pattern for either camera.

**Rejected:** `ImageConverter` with `PreAllocateConversion` for the BGR8
conversion, which would avoid a per-frame allocation. Its exact argument
signature isn't recoverable from `vendor/ids_peak_api.txt` (SWIG strips
argument info from `inspect`), and this path can't be exercised without
hardware attached — a wrong guess there fails silently or crashes deep in
a background thread instead of at an obvious call site. `Image.ConvertTo`
takes one argument and is unambiguous. Revisit once real hardware is
available to measure whether the per-frame allocation actually matters at
30fps, per this project's existing "measured throughput over datasheet
numbers" stance.

---

## 2026-08-12 — Hardware smoke test found two real IdsCamera bugs; both fixed

**Decided:** Ran `tools/smoke_test_camera.py` against a real Keeler
(U3-327xCP-C, serial 4110050487) for the first time. Two bugs surfaced
and were fixed in `ids_camera.py`, both now confirmed working end to end
(final capture: full 0-255 dynamic range, recognizable image).

**Bug 1 — child GenTL handles invalidated by Python GC.** `_open()` held
`device` as a local variable; `self._node_map` and the `DataStream` are
derived from it. The moment `_open()` returned, `device` had no
remaining Python reference and was garbage collected, which invalidated
the handles derived from it — the very next `WaitForFinishedBuffer()`
call from the capture thread raised
`InvalidInstanceException: dataStreamHandle is invalid!`. Fixed by
storing `self._device` and `self._remote_device` as instance attributes
for the camera's whole open lifetime, not just locals in `_open()`.

**Bug 2 — sensor power-on defaults are unusable.** With the SDK/buffer
path working, captured frames were still near-black (raw Bayer max
value 3-4 out of 255) even pointed directly at a lamp — confirmed via
IDS peak Cockpit that this wasn't a physical/mounting issue, and via a
raw-buffer-before-conversion capture that it wasn't our BGR8 conversion
either. Root cause: the device's power-on defaults were `ExposureTime
~15ms, Gain 1.0` — nowhere near enough for the room. Cockpit's live view
had converged to `ExposureTime ~47.5ms, Gain ~25.4` for the same scene.
Fixed by adding `_converge_auto_exposure()`: sets `ExposureAuto`/
`GainAuto` to `Once` right after `StartAcquisition()`, drains buffers
until both read back `Off` (converged), then leaves them locked for the
rest of the session. Measured convergence time on this hardware: ~10
frames, ~0.5s.

**Why `Once` and not `Continuous`, and why converge at all instead of a
fixed value:** A hardcoded exposure/gain number would be exactly the
kind of measurement-dependent magic constant CLAUDE.md's Conventions
section warns against — this session's numbers are already known wrong
for at least one room. `Continuous` was rejected because it would keep
adjusting during a recording, and visible exposure "pumping" mid-session
is a distracting artifact in a video students review for technique.
Converge once at open, lock for the session.

**Consequence:** `_converge_auto_exposure()` skips whichever axis
(`ExposureAuto`/`GainAuto`) isn't available rather than failing camera
open over it — SETUP.md Section 3 already documents that the slit lamp
camera's uEye Transport Layer only exposes a basic feature set and may
lack these nodes, untested since only the Keeler was available for this
smoke test.

**Open:** Only the Keeler has been hardware-tested. The slit lamp camera
(UI-3250CP-C-HQ via uEye Transport Layer) still needs its own smoke
test before anyone trusts `IdsCamera` against it — the "one class for
both" decision above is sound on API-surface grounds, but is now
verified for one of the two cameras, not both.

---

## 2026-08-12 — Slit lamp camera smoke test: one more bug fixed, one real hardware limitation confirmed and accepted

**Decided:** Ran `tools/smoke_test_camera.py` against the real slit lamp
camera (UI325xCP-C, uEye Transport Layer, serial 4103484089) for the
first time, closing the "Open" item above. One more bug found and fixed;
one limitation SETUP.md Section 3 already warned about turned out to be
real, and is accepted rather than worked around.

**Bug — `DataStream.PayloadSize()` isn't implemented on the uEye
Transport Layer.** Raises `InternalErrorException` wrapping GenTL error
`GC_ERR_NOT_IMPLEMENTED` (`STREAM_INFO_PAYLOAD_SIZE is not
implemented!`) — note the exception class, not `NotImplementedException`
despite the underlying GenTL code, and this may differ by transport
layer. Fixed with `_payload_size()`: try `DataStream.PayloadSize()`
first (still the primary path, unchanged and still verified on the
Keeler), catch both `NotImplementedException` and
`InternalErrorException`, and fall back to reading the standard
`PayloadSize` GenICam node from the remote device's node map instead —
confirmed on this camera to report the same value
(1600×1200×1 byte/px = 1920000 for `BayerRG8`) that the DataStream
method would have.

**Limitation — no `ExposureAuto`/`GainAuto` on this camera.** Confirmed
via `IsAvailable()`: both `False`, before and after
`StartAcquisition()`. `_converge_auto_exposure()`'s existing
`IsAvailable()`/`IsWriteable()` guard (written before any hardware could
verify it — see the "One IdsCamera class" entry above) skips both
correctly, so this did not require a code change, only confirms the
defensive path was right. Captured frames without hand-tuned
exposure/gain were near-black (raw max ~20/255) — manually maxing
`ExposureTime` (87.2ms, its ceiling) and `Gain` (4.0, its ceiling — note
this camera's gain ceiling is far below the Keeler's ~25x) brought raw
mean from ~10 to ~76/255, confirming the capture/conversion pipeline
itself is correct for this camera too; the near-black default is purely
an exposure problem with no software fix available.

**Why accepted rather than worked around:** This camera's real operating
context is imaging through the slit lamp's own illumination path, not
ambient room light (same reasoning as the Keeler needing to be worn on
the BIO headset, see the exposure-convergence entry above) — a bench
test with no instrument light on isn't representative, and there's no
GenICam-level auto-exposure to fall back on. A hardcoded exposure/gain
value would be exactly the kind of measurement-dependent magic constant
CLAUDE.md's Conventions section warns against, and unlike the Keeler
there's no way to measure it here without the physical instrument.

**Open:** This camera needs a one-time manual exposure/gain calibration
via IDS peak Cockpit once mounted on the actual slit lamp with its
illuminator on, analogous to focusing an optical instrument — not
something `IdsCamera` can determine on its own. Until that happens,
whatever `ExposureTime`/`Gain` the device last had persists across
sessions (GenICam devices retain these across power cycles unless
explicitly changed) — fine once correctly tuned once, but means a stale
or accidentally-changed value would silently carry into the next kiosk
session with no code-level check catching it.

---

## 2026-08-12 — Frame.index was never actually gap-detectable; fixed, and a dual-camera bandwidth test confirms no real drops

**Decided:** Ran `tools/dual_camera_smoke_test.py` (new) with both real
cameras plugged in and streaming simultaneously, to test CLAUDE.md's
single most emphasized hardware risk directly: "USB3 Vision degrades by
silently dropping frames rather than raising an error." That test
surfaced a real, previously-invisible bug in `camera.py` itself —
unrelated to IDS hardware specifically — which is now fixed.

**Bug — `BaseCamera._run()` assigned `Frame.index` as its own gapless
counter, incrementing only on delivered frames.** This meant
`Frame.index` could *never* show a gap from any frame a camera's
`_grab()` failed to deliver — not `SyntheticCamera`'s injected
`drop_rate`, not a real camera's `WaitForFinishedBuffer` timeout, not a
device silently skipping frames on the wire. `recorder.py`'s
`_CameraTrack.absorb()` already had correct gap-detection logic
(comparing consecutive `frame.index` values), so it looked like drop
tracking worked — it just never had a real gap to detect, other than
this class's own bounded queue evicting a frame a slow *consumer*
hadn't read yet (which does still create a gap, since eviction happens
after `Frame.index` is assigned). CLAUDE.md's own module table
description ("dropped frames ... computed from gaps in Frame.index, not
estimated") was accurate about the *mechanism* but the mechanism had a
hole: it only ever caught consumer-side backpressure, never source-side
loss — exactly the category CLAUDE.md's Hardware section warns about.

**Fix:** `BaseCamera._grab()`'s contract changed from returning
`(image, timestamp)` to `(image, timestamp, index)` — every camera
implementation now reports its *own* frame sequence number rather than
having `BaseCamera` renumber deliveries itself. `SyntheticCamera` uses
its existing internal counter (which already incremented even for
`drop_rate`-skipped frames, so this was almost free). `IdsCamera` uses
`Buffer.FrameID()`, confirmed via hardware smoke test to start at 0 and
increment per frame on both cameras. `recorder.py`'s
`_CameraTrack.frame_count` — previously derived as `last_index + 1`,
which only worked because indices used to be gapless and zero-based —
now tracks a separate `received` counter instead, since neither
assumption holds once gaps are real. Added a regression test
(`test_recorder.py`) proving `SyntheticCamera(drop_rate=0.3)` now
produces `dropped_frames > 0` in `session.json` — before this fix, that
exact assertion would have failed regardless of `drop_rate`.

**Bandwidth result, now trustworthy:** with the fix in place,
`tools/dual_camera_smoke_test.py` showed the Keeler losing ~5% of
frames (23/425) over a 20s simultaneous run while the slit lamp lost
none — but a follow-up raw-GenTL check (bypassing `BaseCamera`'s queue
entirely, reading `DataStream.NumUnderruns()` and `Buffer.FrameID()`
gaps directly) showed **0 underruns and 0 FrameID gaps on both cameras**
running simultaneously. So the 23 losses are this process's own
`BaseCamera` queue (`queue_size=2` default) being evicted by a consumer
that didn't drain fast enough — not USB bandwidth, not a device-level
drop. At current settings (Keeler ~20fps native, slit lamp ~11fps
native/exposure-limited, well under either camera's target), CLAUDE.md's
core bandwidth concern is not observed.

**Open:** This doesn't yet test the actual worst case CLAUDE.md
describes (both cameras at full resolution and 30fps) — the slit lamp
is currently capped well under 30fps by its uncalibrated ~87ms exposure
(see the entry above), so real target-rate bandwidth testing is blocked
on that calibration happening first. Separately: whether `queue_size=2`
needs to grow is still an open question — this test's simple
read-and-count consumer isn't `Recorder`, which does real per-frame
encoding work and could behave differently under load. Worth a real
`Recorder` run against both real cameras once the slit lamp's exposure
is calibrated, rather than tuning the queue size on guesswork now.

---

## 2026-08-12 — Fullscreen by default; closing the window during a
recording is refused, not confirmed or auto-stopped

**Decided:** `app.py` now launches with `showFullScreen()` (no title bar,
no border drag-to-resize, no minimize button) unless started with
`--windowed`, which keeps the old resizable-window behavior for
development. Separately, `KioskWindow.closeEvent()` now calls
`event.ignore()` and leaves the session running if `state == RECORDING`,
rather than either popping a confirm dialog or silently stopping the
recording and exiting (the previous behavior).

**Why:** CLAUDE.md's "Who uses it" is explicit that nothing besides Start
and Stop should be clickable during a session, for students with no
technical background and no instructor present. A plain resizable window
leaves several such surfaces live — the title bar's X button, Alt+F4,
drag-to-resize, minimize — none of which are "Stop" but all of which can
end or disrupt a session. A confirm dialog was considered and rejected:
it's itself a new clickable thing mid-session, and a student startled by
an accidental Alt+F4 is exactly who will reflexively click through a
dialog without reading it. Refusing outright means the only way to end a
session is the one button that says Stop.

**Consequence:** There is now no way to quit the app from the GUI while
RECORDING — not even for a developer or proctor — short of killing the
process. That's intentional for the student-facing build. A stalled
camera still exits RECORDING on its own via `poll_recording()`'s stall
detection, which calls `_fail()` (not `closeEvent`) and is unaffected by
this change.

**Not addressed:** this only closes off what Qt itself can intercept.
Alt+Tab, the Windows key, and the taskbar remain available to switch away
from the app — actually locking those down is an OS-level kiosk
configuration, out of scope for this codebase.

**Rejected:** a confirmation dialog on close ("Stop recording and exit?").
Rejected for the reason above — it reintroduces a clickable surface
during the one state where CLAUDE.md says there shouldn't be one.

---

## 2026-08-12 — Reverted fullscreen; close during recording confirms
instead of refusing outright

**Decided:** Walked back the entry immediately above, per direct
feedback. `app.py` goes back to launching as a normal resizable window
(no fullscreen, no `--windowed` flag — there's only one mode again).
`closeEvent()` no longer refuses to close during `RECORDING`; instead it
calls `_confirm_stop_and_exit()`, which shows a `QMessageBox` ("A
recording is in progress. Stop it and exit?", defaulted to **No**). Only
on explicit confirmation does it call `controller.stop_recording()` — a
clean stop that finalizes the MP4 remux and writes `session.json`, not an
abrupt kill — before proceeding to close.

**Why:** the fullscreen window was a worse experience than the plain
window it replaced, and refusing to close at all left no way to
force-quit even deliberately (e.g. to abandon a broken session, or for a
developer testing the app). A defaulted-to-No confirmation keeps a single
accidental click or Alt+F4 from silently ending a session — the failure
mode the previous entry was actually trying to prevent — while still
leaving a deliberate two-click exit available.

**Consequence:** `_confirm_stop_and_exit()` is a separate method
specifically so tests can monkeypatch it instead of driving a real modal
dialog headlessly (see `test_app.py`'s `TestCloseLockdown`).

**Rejected (again, different reason than last time):** refusing close
outright, i.e. keeping the previous entry's behavior. Still true that a
confirm dialog is a clickable surface CLAUDE.md's "nothing else
clickable" line argues against, but no escape hatch at all turned out to
be the worse tradeoff in practice.

---

## 2026-08-12 — Durable logging to `logs/app.log`, configured only in `app.py`

**Decided:** Every module that makes a decision or can fail
(`camera.py`, `recorder.py`, `kiosk.py`, `app.py`) now has a module-level
`logger = logging.getLogger(__name__)` and logs at the meaningful points
— capture thread start/stop, an exception inside `_grab()`, state
transitions in `KioskController`, recording start/stop with frame and
drop counts, `_fail()`'s error message, camera-start failure and
recovery, the force-close confirm decision. Handlers (a `RotatingFileHandler`
writing `logs/app.log`, capped at 4 × 2MB, plus a console handler) are
configured exactly once, in `app._configure_logging()`, called at the top
of `main()`. Every other module only calls `getLogger` and never adds a
handler itself — standard practice so importing e.g. `recorder.py` from a
test doesn't also wire up file logging as a side effect. `app.py` also
installs a `sys.excepthook` that logs uncaught exceptions before
delegating to the default hook.

**Why:** these are unsupervised, unattended machines — CLAUDE.md's "Who
uses it" already establishes that failures must be loud and early in the
UI, but the UI's error banner and `session.json` both disappear the
moment the window closes or a new session starts. When something breaks
on a specific student's machine and nobody was there to see the banner,
`logs/app.log` is the only thing left afterward. The `_grab()` try/except
in `camera.py` closes a real gap: previously an unhandled exception in
the capture thread died silently, and the *only* symptom downstream was
`kiosk.py`'s stall detector firing a couple seconds later with no
indication of the actual cause.

**Consequence:** state-transition logging in `poll_preflight()` only logs
on an actual `State` change, not every poll tick (it runs every 250ms via
`app.py`'s timer) — logging every tick would flood the file with
"IDLE -> IDLE" noise. Camera-start-failure logging in `app.py` similarly
logs the first failure and the eventual recovery, not every 2s retry.

**Rejected:** a per-session log file under each `sessions/<...>/`
directory, mirroring `session.json`. Rejected because the failures this
is most needed for — a camera never starting, a crash before any session
begins — happen *before* a session directory exists. A single rotating
app-level log covers those; `session.json` remains the authoritative
per-session record for anything that did complete.

---

## 2026-08-17 — Third-person UVC camera: index-identified, self-counted
`Frame.index`, and only one instrument camera runs at a time

**Decided:** Added an ELP-USB100W03M-L21 third-person camera via a new
`UvcCamera(BaseCamera)` backed by `cv2.VideoCapture`. It deviates from two
rules elsewhere in this codebase, both accepted deliberately, plus a
change to camera lifecycle: only one of the two instrument cameras (slit
lamp, BIO/Keeler) ever runs at a time, chosen by the student via
`KioskController.select_instrument()`, rather than both running for the
app's whole lifetime the way they used to.

**Why identified by device index/name, not serial:** CLAUDE.md's
Architecture section requires serial-number identification specifically
because index order changes across reboots and USB port changes — real
past failure mode for the two IDS cameras. OpenCV's DirectShow backend
(`cv2.VideoCapture`) has no equivalent stable identifier to key off of.
Accepted because there's exactly one UVC camera in this setup; revisit if
a second UVC camera is ever added, since two indistinguishable-by-name
UVC devices would reintroduce the exact enumeration-order risk the
serial-number rule exists to prevent.

**Why `Frame.index` is self-counted here, unlike `IdsCamera`:** The
"Frame.index was never actually gap-detectable" entry above fixed
`BaseCamera` subclasses to report the *source's own* sequence number
specifically so `recorder.py` could detect real source-side drops, not
just consumer-side queue evictions. `cv2.VideoCapture`/UVC exposes no such
counter, so `UvcCamera._grab()` assigns its own gapless counter —
reintroducing, for this one camera, the exact blind spot that entry
fixed. Accepted because UVC isn't subject to CLAUDE.md's core worry
(USB3 Vision silently dropping frames under bandwidth pressure); a
webcam-class device failing looks like a stall (caught by `kiosk.py`'s
stall timeout) or a disconnect (caught by `_grab()` raising), not a
silent partial drop. `dropped_frames` in `session.json` for this camera
will therefore only ever reflect consumer-side queue evictions, same
caveat as before the source-index fix for the other two cameras.

**Why only one instrument camera runs at a time:** Only one instrument is
ever physically in use in a real session — a student is at the slit lamp
or the BIO, never both. Running both continuously (as the two fixed
cameras used to) would mean the unused one sits open for no reason and
adds a picker-independent camera-liveness check with no session it's ever
part of. `select_instrument()` now owns starting the newly chosen
instrument camera and stopping whichever one was previously running; the
third-person camera keeps the old always-on lifecycle since it's used in
every session regardless of instrument.

**Consequence:** This is also why CLAUDE.md's "Who uses it" section no
longer says "one Start button, one Stop button, nothing else clickable" —
the app now needs an instrument picker. It's scoped as tightly as
possible to preserve the original intent: two large always-visible
choices (not a menu), enabled only outside `RECORDING`, so it can never
interrupt or be part of an active session.

**Rejected:** Keeping both instrument cameras running continuously and
just choosing which feed to composite. Rejected as pointless resource use
once "only one instrument in use" was confirmed — the camera that isn't
in the composite this session never needs to be open at all.

---

## 2026-08-17 — UvcCamera hardware-verified against the real ELP camera

**Decided:** Ran a standalone smoke test (`UvcCamera(0, ...).start()`,
polling `get_latest()`) against real hardware — a "HD USB Camera"
(VID_32E4&PID_9310), the only UVC camera-class device attached to this
machine and almost certainly the ELP-USB100W03M-L21. Confirmed working:
640x480 reported resolution (queried, not hardcoded — see the entry
above), real image content (mean pixel ~128, full 0-255 range, not a
black/broken frame), and `Frame.index` incrementing correctly over a 5s
run.

**Finding:** Time to first frame was variable across three separate runs
— roughly 0.9s to 1.6s from `start()` to the first non-`None`
`get_latest()` — noticeably slower and less consistent than the IDS
cameras' ~0.5s auto-exposure convergence (see the "Hardware smoke test"
entries above). No code change was needed: `KioskController._cameras_ready()`
already treats "not live yet" as not-ready regardless of camera type, so
Start simply stays disabled a little longer after picking this instrument,
the same mechanism that already absorbs the IDS cameras' convergence
delay. Worth knowing if the picker ever feels sluggish to respond in
practice — this is why, not a bug.

**Also confirmed:** `_open()` running in the calling thread (via
`BaseCamera.start()`) while `_grab()` reads from the spawned capture
thread — the pattern every `BaseCamera` subclass uses — does not corrupt
`cv2.VideoCapture`'s DirectShow backend across threads. Checked directly
because DirectShow's underlying COM apartment-threading rules can in
principle make this unsafe; a targeted cross-thread `read()` test showed
no failures under `CAP_DSHOW`, `CAP_MSMF`, or `CAP_ANY`. The original
"no frame after 1s" result while investigating this turned out to be the
warm-up latency above, not a threading bug.

---

## 2026-08-18 — config.json + loader (ROADMAP.md Phase 1)

**Decided:** Added `config.py`/`config.json` so which physical camera
fills each role (slit lamp, BIO, third-person) is no longer a source
constant in `app.py`. `config.json` is gitignored, like `sessions/` and
`logs/` — it's install-specific data, not source. `config.example.json`
is committed in its place, seeded with this machine's real (already
publicly documented in CLAUDE.md's Hardware table) serials, so a fresh
clone has something to copy rather than a silent fallback to someone
else's hardware. `kiosk.KioskController` needed no changes — it was
already generic over an `instruments: dict[str, BaseCamera]`; this phase
is purely about where `app.py` sources that dict's contents from.

**Why `third_person` still carries a `device` index, not `vid_pid`:**
ROADMAP.md's end-state schema sketch uses `vid_pid`/`friendly_name` for
the third-person role, since no reliable per-unit serial can be assumed
across arbitrary consumer webcams. That resolution logic (plus the
single-device-attached fallback) is explicitly scoped as Phase 3 —
"Runtime resolution hardening" — not this phase. Phase 1 keeps today's
identification method (a UVC device index) so this stays the mechanical,
low-risk move ROADMAP.md describes it as, rather than pulling forward
undecided work (the UVC-enumeration spike ROADMAP.md flags as needed
before `settings.py`).

**Why the config-load failure is checked before `QApplication` exists:**
It's a technician-facing setup error (wrong/missing `config.json` on this
specific machine), not the student-facing camera-liveness preflight
`kiosk.py` already owns — a student never reaches a state where
`config.json` could be the problem, since the picker only ever shows
instruments the config already defines. So it doesn't need Qt at all:
`logger.error(...)` (already reaching both `logs/app.log` and stderr via
the existing `StreamHandler`) plus a nonzero exit code is the whole
response, checked in `main()` right after `_configure_logging()` and
before `QApplication(sys.argv)` is constructed.

**Rejected:** A `--config` CLI flag for the file path. Nothing in
ROADMAP.md's Phase 1 description calls for one, `settings.py` (Phase 2)
will always write the canonical `config.json` path, and no test drives
`app.main()` directly, so there's no plumbing reason either — the
CWD-relative default matches existing conventions (`LOG_DIR`, `kiosk.py`'s
`output_root`).

**Also rejected:** Rejecting unknown/extra keys in `config.json` (strict
schema validation). Chose to validate only the presence and type of keys
Phase 1 actually reads, and ignore anything else — leaves room for a
stray hand-added field, or a transitional `vid_pid` key sitting next to
`device` once Phase 3 lands, without the loader needing to change in
lockstep.

---

## 2026-08-18 — UVC device enumeration: pygrabber's DirectShow internals,
not Get-PnpDevice/WMI

**Decided:** `uvc_enumeration.list_uvc_devices()` resolves the open
question ROADMAP.md's "Device compatibility & camera setup system" entry
left for the start of the `settings.py` work — how to get a UVC device's
friendly name and VID/PID *and* have it correlate reliably to the index
`cv2.VideoCapture(i, cv2.CAP_DSHOW)` opens by. It calls into `pygrabber`'s
DirectShow COM wrappers (`dshow_core.ICreateDevEnum`,
`dshow_ids.DeviceCategories`/`clsids`) directly — not `pygrabber`'s public
`FilterGraph.get_input_devices()`, which only exposes `FriendlyName` — and
additionally reads each moniker's `DevicePath` property bag entry, which
carries the device's VID/PID (confirmed against the real ELP camera
currently attached to this dev machine: `DevicePath` came back
`\\?\usb#vid_32e4&pid_9310&mi_00#...`, matching `Get-PnpDevice`'s
`InstanceId` for the same device exactly). Index, name, and VID/PID all
come from one walk of DirectShow's own device enumerator, so there's no
separate correlation step — verified end to end by opening
`cv2.VideoCapture(N, cv2.CAP_DSHOW)` for every index this function
reports and confirming each one actually opens (see
`test_uvc_enumeration.py`).

**Why not `Get-PnpDevice`/WMI (the other candidate ROADMAP.md named):**
It gives friendly name + VID/PID cleanly (confirmed working against the
same real hardware), but not a DirectShow enumeration index — matching
its result back to a `cv2.CAP_DSHOW` index would still need a second pass
(trial-opening indices in order and matching by friendly name), which is
both slower and, with two identically-named devices attached, genuinely
ambiguous. Shelling out to PowerShell from Python is also just more
moving parts than a direct COM call for a Windows-only project that
already accepts a `comtypes`-based dependency's weight (`pygrabber`
itself depends on it).

**New dependencies:** `pygrabber==0.2`, `comtypes==1.4.16` (pinned in
`requirements.txt`). Both Windows-only (`comtypes` wraps `ctypes.windll`),
consistent with this project already being Windows-only.

**Caveat, matching this project's existing `vendor/ids_peak_api.txt`
precedent for undocumented/compiled surfaces:** `DevicePath` and the
`ICreateDevEnum`/`dshow_ids` internals this relies on are not part of
`pygrabber`'s advertised public API (only `FilterGraph`'s methods are
documented) — verify against the installed `pygrabber` version if a
future upgrade ever makes `list_uvc_devices()` silently stop returning
`vid_pid`, rather than assuming today's behavior still holds.

---

## 2026-08-18 — settings.py + third-person identity moves to VID/PID now

**Decided:** Built `settings.py` (ROADMAP.md's Phase 2) and, alongside it,
moved the third-person role's identity in `config.json` from a plain UVC
device index to `vid_pid`/`friendly_name` — originally planned as Phase 3
("runtime resolution hardening"), pulled forward because `uvc_enumeration.py`
already provided everything the resolution logic needed and ROADMAP.md
already fully specced the algorithm; no design ambiguity remained to defer.
Confirmed with the user before implementing (an index is exactly the
fragile-across-reboots identity this whole effort exists to move away
from — shipping `settings.py` writing one would have meant an
almost-immediate follow-up rewrite). This **supersedes** the 2026-08-18
"config.json + loader" entry's "Why `third_person` still carries a
`device` index, not `vid_pid`" paragraph — that reasoning was correct for
Phase 1 in isolation, but the phase boundary moved once Phase 2 started.
IDS instrument roles are unaffected (already serial-based, already
correct).

**`config.json`'s `third_person` block:**
```json
"third_person": { "kind": "uvc", "vid_pid": "32E4:9310", "friendly_name": "HD USB Camera" }
```
`uvc_enumeration.py` gained `resolve_device(vid_pid, devices=None)`,
implementing exactly the strategy CLAUDE.md's Hardware table and
ROADMAP.md's "Third-person (UVC) role" section describe: exactly one UVC
device attached → use it regardless of configured `vid_pid` (zero
configuration friction for the common case, self-heals if that single
camera is swapped for a different model); more than one attached →
require a `vid_pid` match, refuse to guess otherwise. `devices` is
injectable so this is unit-tested directly with canned lists — no
hardware needed for the logic itself, though the underlying enumeration
was separately verified against the real ELP camera (see the prior
entry).

**Resolution happens inside `UvcCamera._open()`, not at construction
time — this is load-bearing, not a style choice.** Verified directly in
`camera.py`: `BaseCamera.start()` (lines 81-88) calls `self._open()`
*before* creating the capture thread, and only sets `self._thread` after
`_open()` succeeds. A raise inside `_open()` therefore leaves
`self._thread` as `None`, so the *next* `start()` call retries `_open()`
from scratch — this is exactly the mechanism `app.py`'s existing
`camera_retry_timer` already depends on for a busy/wrong-serial IDS
camera, and a third-person camera that isn't physically plugged in yet
when `app.py` launches needs to fall into that same loop rather than
raising once, synchronously, out of `main()` before any UI exists (which
would crash the whole process). `UvcCamera.__init__` therefore gained a
second identification mode alongside the existing `device` index —
`UvcCamera(device=..., ...)` XOR `UvcCamera(vid_pid=..., ...)`
(constructor raises `ValueError` if both or neither are given). Both
modes stay: `vid_pid` resolves dynamically at `start()` time (used by
`app.py`'s real runtime path); `device` opens a literal index with no
resolution (used by `settings.py`'s Preview, which wants to open exactly
the highlighted dropdown entry, not whatever the fallback logic would
pick). A resulting `UvcDeviceResolutionError` propagates out of `_open()`
unwrapped rather than being caught and re-raised as
`UvcCameraNotFoundError` — they're genuinely different failure classes
("couldn't decide which device to open" vs. "knew which device, `cv2`
couldn't open it"), and every caller already just stringifies whichever
exception it gets, so no unification was needed.

**`vid_pid` is normalized in `config.py`, not just validated.**
`uvc_enumeration._parse_vid_pid` always uppercases; `resolve_device`
matches by direct string equality. A hand-typed lowercase value in
`config.json` (e.g. `"32e4:9310"`) would otherwise pass shape validation
(`^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}$`) but then silently and permanently fail
to match any attached device — a real, easy-to-hit bug, not a
hypothetical, so `config.py`'s `_parse_third_person` stores
`vid_pid.upper()`, not the raw string.

**A UVC device with no discoverable `DevicePath`/VID-PID** (a real,
documented case — see `uvc_enumeration.py`'s `UvcDeviceInfo.vid_pid: str
| None`) still appears in `settings.py`'s third-person dropdown rather
than being silently filtered out, but can't be saved: `DeviceRow`
represents it as a `RowCandidate` with `key=None`, which `is_valid()`
already treats the same as "nothing selected," plus a status-line note
when it's the highlighted selection ("This device has no discoverable
VID/PID and can't be saved") so the reason is visible, not just a
disabled Save button with no explanation.

**`ids_camera.py` gained `list_ids_devices()`**, a standalone addition
(not a refactor of the existing `_open_device()`, which needs the raw
descriptor objects to call `.OpenDevice()` on and wasn't worth risking).
Required by CLAUDE.md's architecture rule that nothing outside a camera
module may import the IDS SDK — `settings.py` cannot enumerate IDS
devices any other way. Brackets `ids_peak.Library.Initialize()`/`Close()`
itself, matching `SETUP.md`'s reference script, since — unlike
`_open_device()` — it isn't tied to a camera object's `_open()`/`_close()`
lifecycle. `settings.py` imports it lazily (inside a function body, not
at module level), mirroring `app.py`'s existing `_make_camera` pattern:
this dev machine has no `ids_peak` installed at all (`import ids_camera`
raises `ModuleNotFoundError` immediately, confirmed), so a top-level
import would break `python settings.py` here. `uvc_enumeration`/
`uvc_camera`, by contrast, are safe as normal top-level imports —
`pygrabber`/`comtypes` are unconditional `requirements.txt` entries, not
a separate per-machine install like `ids_peak`. When enumeration fails
this way, `settings.py` shows a distinct "Could not enumerate IDS
devices: ..." status on the instrument rows rather than the generic
"‹not connected›" a row shows with 0 *devices* — "SDK missing" and
"camera unplugged" are different problems with different fixes, worth
distinguishing even though both currently happen to gate Save the same way.

**`bgr_to_pixmap` extracted to `qt_image.py`.** It was duplicated
verbatim in `app.py` and `preview.py` already; `settings.py`'s
`PreviewDialog` needed it a third time, past the rule-of-three threshold
this codebase otherwise respects (see `compositor.py`/`uvc_enumeration.py`
as precedent for small, focused modules). `app.py` and `preview.py` now
both import it instead of keeping their own copies.

**`PreviewDialog` is opened modally (`.exec()`, not `.show()`).** Two
reasons: it sidesteps an open question this project has not answered —
whether `ids_peak.Library.Initialize()`/`Close()` is safely reentrant
across a Preview holding an `IdsCamera` open while a concurrent Rescan
tries to enumerate (not covered by `vendor/ids_peak_api.txt`'s scope, not
worth risking rather than just serializing the two) — and it prevents
Rescan from mutating a row's candidate list out from under a preview
that's currently showing "the highlighted entry." **This reentrancy
question is explicitly unresolved, not answered** — flagging it here
rather than asserting a behavior that hasn't actually been tested.

---

## 2026-08-18 — Config-driven recording fps, device-derived canvas size

**Decided:** `recorder.py`'s `width`/`height`/`fps` constructor defaults
(2560x1080x30, previously hardcoded and unreachable from `app.py`) split
into two different fixes, not one: `fps` became genuinely config-driven
(`recording.fps` in `config.json`, `config.py`'s new `RecordingConfig` /
`DEFAULT_RECORDING_FPS`); `width`/`height` did **not** — they became
device-derived instead. `Recorder.start()` now computes them from
`self._track_a.camera.resolution` / `self._track_b.camera.resolution`
whenever the constructor wasn't given explicit values (sum of widths, max
of heights — the same formula `compositor.side_by_side`'s own `out_size`
default already used, just previously unreached because `Recorder`
always passed an explicit `out_size`). `KioskController` mirrors the same
`None`-means-derive default and threads it through unchanged to
`Recorder`; its disk-space preflight estimate (`_estimated_canvas()`)
prefers the same real resolutions once both cameras are live, falling
back to the old 2560x1080 constant only before then — never a
correctness issue, since Start stays gated on `cameras_ready` regardless
of how accurate the estimate is at that point.

**Why not config for canvas size too:** it would work, but device-query
strictly dominates it for this value — a technician would otherwise have
to remeasure and re-edit `config.json` every time a camera is swapped for
a different-resolution model, exactly the kind of drift-prone manual step
config.json's `label`/`serial`/`vid_pid` fields already avoid for camera
*identity*. Resolution is a `BaseCamera.resolution` property specifically
*because* it's meant to be queried, not assumed (see `uvc_camera.py`'s
"resolution isn't known until it's opened" comment) — there is no
equivalent reason to keep it out of config the way there is for fps
(next paragraph), so there's no tradeoff being made here, just an unused
capability (`side_by_side(out_size=None)`) getting wired up.

**Why fps stays config, not device-queried:** the opposite call, on
purpose. A camera's advertised/nominal fps (GenICam's
`AcquisitionFrameRate` node, or UVC's `CAP_PROP_FPS`) is a datasheet
number, and CLAUDE.md's Hardware section already establishes that this
project treats measured throughput as authoritative over datasheet
numbers for exactly this hardware — the slit lamp is the live
counterexample: it advertises ~60fps but currently sustains far less
because of an uncalibrated long exposure time (see the "Frame.index was
never actually gap-detectable" entry above), a limit no device query
would reveal, since the sensor doesn't know its own effective throughput
under the current exposure settings. `fps` here also isn't really a
camera capability at all — it's `Recorder._run()`'s encode/composite
pacing rate, decoupled from actual per-camera capture rate by design
(gaps are caught via `Frame.index`, not by tying encoding to arrival).
Treating it as a measured, technician-set value (observe real sustained
throughput, set it once) fits `CLAUDE.md`'s existing "values that depend
on measurement belong in a config file" convention exactly, rather than
inventing an auto-throttling feature that isn't otherwise needed.

**Rejected:** auto-measuring real sustained fps at startup (a brief
calibration pass before each recording) instead of a static config value.
Would get closer to "true" throughput than either a datasheet number or a
stale manual setting, but it's a materially bigger feature — a recording
target that can silently change session-to-session isn't obviously
better than a stable value a technician sets once after watching real
performance, and no current need justifies the complexity.

---

## 2026-08-18 — settings.py blocks Save when two instrument roles pick the same camera

**Decided:** Nothing previously stopped a technician from selecting the
same physical IDS camera's serial for both `slit_lamp` and `bio` in
`settings.py` — each row's dropdown is independently populated from the
same enumerated device list, so picking the same entry twice was always
possible and `Save` would have written the duplicate serial into both
roles without complaint. `SettingsWindow._duplicate_serial_roles()` now
detects this (any serial selected by more than one instrument row) and
`_update_save_enabled()` disables `Save` with an explicit message
(`conflict_label`, styled like the existing malformed-config
`warning_label` but recomputed live on every row change, not just at
startup) rather than only catching it in a docstring or leaving it to
`app.py` to fail confusingly later — two roles both trying to open one
physical device isn't a state `app.py`'s per-role `IdsCamera` construction
has any way to reconcile. `_on_save_clicked()` also re-checks directly
(defense in depth against a future caller that bypasses the button's
`isEnabled()` gate, matching this file's existing pattern of checking
`is_valid()` in both places).

**Why block Save rather than just warn:** matches every other Save-gating
condition already in this file (empty label, "‹not connected›", no
VID/PID) — a real, catchable-at-config-time mistake gets caught here,
not deferred to a confusing runtime failure on the actual kiosk machine.

---

## 2026-08-18 — Lock UVC autofocus/auto-exposure after a warmup window

**Decided:** `UvcCamera._open()` now calls
`_lock_autofocus_and_exposure()`: read a bounded number of frames (10, or
2.0s, whichever comes first) with autofocus/auto-exposure left at
whatever `cv2.VideoCapture`'s driver defaults are, then
`cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)` and
`cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)`. Motivation: nothing
previously stopped the third-person webcam from continuously
autofocusing/auto-exposing through an entire recording, which can
visibly hunt (refocus, re-expose) mid-session — exactly the distracting-
artifact problem `ids_camera.py`'s `ExposureAuto`/`GainAuto` = `Once` (not
`Continuous`) convergence already solves for the two instrument cameras
(see the 2026-08-12 hardware smoke test entries above). Never fails
camera open: `cap.set()` returning `False` just means this device doesn't
support that control, the same tolerance `ids_camera.py`'s
`IsAvailable()` guard gives an axis the hardware doesn't expose.

**Not the same shape as `ids_camera.py`'s convergence, and that's a real
platform limitation, not a stylistic choice.** GenICam's
`ExposureAuto`/`GainAuto` nodes report back `"Off"` once `Once` mode
actually finishes, so `_converge_auto_exposure()` polls for that signal
with a timeout that only fires if convergence is genuinely stuck (see
`IdsCameraAutoExposureTimeoutError`). UVC/DirectShow via
`cv2.VideoCapture` exposes no equivalent "has this converged yet" flag —
there is nothing to poll. So this is a fixed warmup window followed by an
unconditional lock attempt, not poll-until-done, and there's no
equivalent timeout-error path: a fixed window can't "get stuck," it just
elapses.

**Explicitly unverified against real hardware as of this entry.** The
`0.25` value for `CAP_PROP_AUTO_EXPOSURE` is a commonly cited convention
for "manual mode" across various OpenCV+webcam combinations, not a
documented standard the way GenICam's node model is — `cv2.VideoCapture`
property behavior via DirectShow is well known to vary per device/driver,
the same category of uncertainty this project already treats
`vendor/ids_peak_api.txt` as the antidote for on the IDS side, except
there's no equivalent authoritative reference for arbitrary UVC webcams
to check against. The real ELP camera happened to be unplugged when this
was implemented (confirmed via `Get-PnpDevice` — no camera-class device
present), so this shipped as best-effort per explicit instruction rather
than blocking on hardware access. **Still needs confirming against the
real camera:** whether `0.25` actually engages manual exposure on this
device (vs. e.g. `0`/`1`/`3` conventions some drivers use instead) and
whether `CAP_PROP_AUTOFOCUS` does anything on a device that may be
fixed-focus to begin with (in which case `cap.set()` returning `False`
here would be the correct, expected outcome, not a bug). Update this
entry once verified.

---

## 2026-08-19 — `setup.ps1`: script the environment bootstrap, not role assignment

**Decided:** Add `setup.ps1` at the repo root, a PowerShell script covering
SETUP.md Section 1 (venv + `requirements.txt`) and optionally Section 2's
`requirements-ids.txt` install, ending with a check for whether the IDS
peak SDK runtime is actually importable and a pointer to `settings.py` as
the next step. It does not touch `config.json` or camera role assignment.

**Why:** Deploying to a new PC (see CLAUDE.md's setup story generally)
turned out to have two different kinds of friction bundled together: (1)
Python/venv/pip — mechanical, identical every time, safe to script — and
(2) the IDS peak SDK — a real third-party MSI/EXE with kernel drivers that
no script of ours can install or wrap, and where the interesting choices
(extended vs. standard setup, whether this machine drives the slit lamp)
are exactly the human judgment calls SETUP.md Sections 2-3 already walk
through. Scripting (1) removes real, repetitive friction; scripting (2)
isn't possible and wasn't attempted — `setup.ps1` just detects the outcome
(bindings installed, runtime importable or not) and tells the operator
which SETUP.md section to go read.

This is a distinct thing from ROADMAP's "Device compatibility & camera
setup system" entry's rule that role assignment is "explicitly *not* a
one-shot install script" — that rule is about `settings.py` staying a
persistent, re-run-any-time tool for *which physical device fills which
role*, not a general objection to automating setup steps. `setup.ps1`
doesn't do role assignment at all; it prepares the environment and then
hands off to `settings.py` unchanged. Re-running `setup.ps1` itself is
also safe (reuses an existing `.venv`, `pip install` is idempotent) — it
follows the same "repeatable tool, not a one-shot installer" spirit for
the step it *does* own, it just owns a different step. Amended a
clarifying parenthetical into ROADMAP's existing line rather than leaving
it to read as a broader rule than it was.

**Rejected (at first, then partially revisited — see below):** a full GUI
install wizard covering the entire process end-to-end, including the IDS
SDK. Still rejected in that full form: it would not actually remove the
SDK's driver-install friction (still a third-party installer requiring
the same human choices), and this project's actual remaining audience for
"set this up on a new machine" is a technician following SETUP.md, not a
student — the kiosk-facing simplicity CLAUDE.md's "Who uses it" demands
is about `app.py`, not the setup path.

---

## 2026-08-19 — `setup_wizard.py`: a GUI front end over setup.ps1, not a rewrite of it

**Decided:** Add `setup_wizard.py`, a paged (Welcome → IDS question → run →
finish) tkinter GUI that shells out to the *same* `setup.ps1`, passing the
IDS-cameras answer as `-DriveIds Yes|No` instead of `setup.ps1`'s own
interactive `Read-Host` prompt, and streams its stdout live into a log box.
`setup.ps1` gained a `-DriveIds` parameter for this; called with no
argument (a plain `.\setup.ps1` from a terminal) it still prompts
interactively, unchanged.

**Why a wizard now:** direct follow-up request after the `setup.ps1`
decision above, once "make it easier" specifically meant "GUI, not a
terminal prompt" rather than "eliminate the SDK-install step" (that step
still can't be automated — see above). Shelling out to `setup.ps1` rather
than reimplementing venv/pip logic in Python keeps exactly one copy of
that logic, consistent with this codebase's existing aversion to
duplicated logic (`qt_image.py`'s `bgr_to_pixmap` extraction is the
precedent).

**Why tkinter, not PySide6:** every other GUI tool here (`app.py`,
`preview.py`, `settings.py`) uses PySide6, but PySide6 is one of the
packages `setup.ps1` installs — a PySide6-based wizard couldn't run on the
truly fresh machine it's meant for, before `requirements.txt` has been
installed. tkinter ships with the standard python.org Windows installer,
so it works at that earlier point. This is a deliberate one-off exception
to "use PySide6 for GUIs" — the ordering constraint here doesn't apply to
`settings.py`/`preview.py`, which only ever run inside an already-set-up
venv.

**Why the subprocess call happens in a background thread, polled via a
queue:** `subprocess.Popen(...).stdout` iteration blocks, and Tkinter
widgets aren't thread-safe to update directly from a worker thread — the
standard pattern is a worker thread pushing lines onto a `queue.Queue`
and the Tk main loop draining it via `after()`, which is what `RunPage`
does. Confirmed working via a headless smoke test (constructed the
wizard, drove it through every page without `mainloop()`, and separately
verified a real `setup.ps1` invocation it kicked off completed and exited
cleanly with no leftover process or repo changes).

**Rejected:** reimplementing the venv/pip/IDS-bindings logic directly in
Python inside the wizard instead of shelling out to `setup.ps1`. Would
avoid a subprocess boundary, but at the cost of two independent
implementations of the same install steps needing to be kept in sync —
worse than the subprocess/streaming-output complexity it would save,
especially since `setup.ps1` is also meant to keep working standalone
for anyone in a terminal.

---

## 2026-08-20 — Removed the "will this machine drive real IDS cameras?" question

**Decided:** `setup.ps1` no longer asks (interactively or via the
`-DriveIds` parameter above); it unconditionally installs
`requirements-ids.txt` and runs the runtime check every time. `setup_wizard.py`
loses `OptionsPage`/`drive_ids` entirely — Welcome goes straight to Run.
**This supersedes the `-DriveIds Yes|No` mechanism described in the
2026-08-19 `setup_wizard.py` entry above**, which is left as-written for
the historical record rather than edited.

**Why:** every machine this installer actually targets is a real
deployment running the prescribed slit lamp/BIO cameras (CLAUDE.md's
Hardware table), not a bare dev checkout — a dev machine like the one
this project is built on doesn't run `setup.ps1` at all, it already has
its environment set up ad hoc. The question was answering a case that
doesn't occur in practice, at the cost of one more decision a technician
has to get right (and one more way to end up in the "answered No by
mistake, `settings.py` shows empty IDS candidate rows" state documented
in the ROADMAP's IDS-installer entry). Removing it doesn't change
behavior for the actual audience, only removes a place they could
misclick.

**Not revisited:** whether a genuinely `SyntheticCamera`-only development
machine should be able to skip `requirements-ids.txt`. It still can —
just by not running `setup.ps1` at all and installing
`requirements.txt` directly, which is exactly what this project's own
dev machine has always done.

---

## 2026-08-20 — Frozen-exe installer built: PyInstaller + Inno Setup, config paths, technician-configurable sessions folder

**Decided:** Executed ROADMAP.md's "Distribute a frozen-exe installer,
not a Python source bootstrap" plan. `app.py`/`settings.py` freeze into
`app.exe`/`settings.exe` via `packaging/app.spec`/`packaging/
settings.spec`; `packaging/reflex.iss` (Inno Setup) packages those
plus a bundled, interactively-launched copy of the IDS peak *extended*
installer into one real installer. `config.py` gained
`resolve_default_config_path()`/`resolve_default_sessions_dir()`,
splitting on `is_frozen()` (PyInstaller's `sys.frozen`): relative to CWD
in dev/test (unchanged), under `%ProgramData%`/`%PUBLIC%\Documents`
under a frozen install. `AppConfig` gained an optional `sessions_dir`
field, technician-set via a new Browse-button field in `settings.py`
(not a per-role `DeviceRow` — recordings aren't a camera). New
`PACKAGING.md` documents the developer-only build procedure;
`SETUP.md`/`CLAUDE.md`'s module table updated to mark `setup.ps1`/
`setup_wizard.py` as developer-only tooling, no longer part of any path
a clinic machine goes through.

**Why sessions_dir needed to become config-driven, not just relocated:**
originally planned as a fixed `%PUBLIC%\Documents\Reflex\sessions`
default (mirroring `config.json`'s `%ProgramData%` placement) — revised
during implementation because recordings are literally "the actual
deliverable handed to a student" (CLAUDE.md), and a technician needs to
be able to point that somewhere else (a network share, an external
drive) without editing `config.json` by hand. `resolve_default_sessions_dir()`
is still what pre-fills the picker and what `app.py` falls back to if
`sessions_dir` is absent — existing/dev-mode `config.json` files and
every existing test fixture needed no changes, confirmed via Explore
before writing any code (`test_config.py`/`test_settings.py` never
depend on `DEFAULT_CONFIG_PATH`'s actual value; the only bare
`load_config()` call anywhere is `app.py:439`).

**The PyInstaller freeze needed no hidden-imports or `--collect-all`
overrides at all** — confirmed empirically, not assumed: `pyinstaller-
hooks-contrib` (installed alongside PyInstaller 6.22.2) already covers
PySide6, `cv2`, `av`, and `comtypes.client` (the `pygrabber`/
`uvc_enumeration.py` dependency), and PyInstaller's own binary-dependency
scan picked up `ids_peak`/`ids_peak_ipl`'s native DLLs without any
vendor-specific hook. Verified by actually running the frozen `app.exe`
three ways, not just checking the build exited 0: `--synthetic` (all-
synthetic path), `--third-person-synthetic` (forces the real `from
ids_camera import IdsCamera` import and `ids_peak`/`ids_peak_ipl` module
load at startup, using this dev machine's real slit-lamp/BIO serials from
`config.example.json`), and confirming both `config.json` and `logs/`
correctly resolved to `%ProgramData%\Reflex\...` once frozen. All
three ran cleanly with no import or DLL-load errors. Real-camera-attached
verification of `IdsCamera.start()` itself is still a follow-up for
whoever has hardware attached, same caveat this project always carries
about dev-machine hardware access.

**`upx=False` in both specs, deliberately:** UPX-compressed executables
are a known source of false-positive antivirus flags, a real risk on a
locked-down clinic machine and not worth the smaller file size.

**Inno Setup script specifics:**
- Bundled installer file expected at a fixed, version-agnostic path,
  `vendor/ids-peak-win-extended-setup-64.exe` (gitignored, same
  convention as `vendor/ids_peak_api.txt`) — deliberately not named with
  today's version number, so the `.iss` script never needs editing just
  because IDS ships a new release; whoever builds the installer
  re-downloads and renames.
- `[Run]`'s `Filename` has no silent switches — `waituntilterminated`
  (Inno's default for a non-`postinstall` entry, made explicit) pauses
  this installer's own wizard while the technician clicks through IDS's
  real one, same as if they'd double-clicked it directly. This is
  bundling for convenience, not a reopening of the "Can setup.ps1 drive
  the IDS peak SDK installer?" entry's silent-vs-interactive decision.
- Added a `Check:` guard (`IdsPeakAlreadyInstalled`, checking
  `DirExists('{pf}\IDS\ids_peak')`) so re-running the installer later
  (e.g. to update the app itself) doesn't force a technician back through
  IDS's wizard every time it's already installed. That install path was
  confirmed directly on this machine during the earlier EULA/licensing
  investigation (see ROADMAP.md's IDS-installer entry) — more version-
  independent than checking a specific product GUID in the Uninstall
  registry key, which changes across IDS peak releases.
- Compiled successfully end to end (`packaging/installer_output/
  reflex-setup.exe`, ~513MB — expected, given it embeds the 356MB
  IDS installer). **Not run** on this machine as part of this work — that
  writes to Program Files/Start Menu and chain-launches a real
  third-party installer, confirmed as a deliberate stop point before
  doing so on a real or disposable machine, per `PACKAGING.md`.

**Rejected:** hand-writing the `.spec`/`.iss` files from a blank template
based on assumed dependency needs. Built empirically instead — a first
bare `pyinstaller app.py`/`pyinstaller settings.py` CLI run, actually
launching the result, then encoding whatever that run actually needed
(nothing extra, as it turned out) into the checked-in spec files. Matches
this project's existing preference for integration-style verification
over assumed-correct configuration.

## 2026-08-25 — Missing-config startup failure was silent in the frozen exe

Found by actually running `reflex-setup.exe` on a machine with no
`config.json` yet (a fresh Windows Sandbox session, standing in for a
never-before-configured clinic machine) — exactly the "not verified" gap
the 2026-08-20 entry above flagged. The installer itself worked correctly
(IDS peak's wizard launched and completed, both shortcuts appeared), but
clicking the `app.exe` Desktop shortcut produced nothing visible at all.

**Root cause:** `app.py`'s `main()` handled a missing/malformed
`config.json` by logging the error and returning 1, on the reasoning
(recorded in the removed comment) that this is "a technician setup
error... it never needs Qt at all." That was true when the only way to
run this was `python app.py` from a terminal, where `logger`'s
`StreamHandler` reaching `stderr` was actually visible. `packaging/
app.spec` builds `app.exe` with `console=False` (deliberately, for a
windowed kiosk app), and Explorer launches a Desktop shortcut with no
console attached at all — so that `stderr` write reaches nobody. The
`RotatingFileHandler` still captured the error durably in `LOG_FILE`
(`%ProgramData%\Reflex\logs\app.log`), so the information wasn't
*lost*, but nothing pointed a technician at it. This directly contradicts
CLAUDE.md's "failures must be loud and early" — a double-click producing
literally nothing is quieter than the "black pane" that principle already
calls out as the worst case.

**Fix:** `QApplication.instance() or QApplication(sys.argv)` moved before
the `load_config()` call (previously constructed only after it succeeded,
further down `main()`), so a `ConfigError` can show `QMessageBox.critical`
before returning 1 — matched by a regression test
(`test_app.TestMissingConfigStartup`) mocking `load_config` to raise and
asserting the dialog fires. `QApplication.instance() or ...` rather than
an unconditional constructor call: makes `main()` safe to call from a
test process that already created one at module import time (this test
file's existing `_qt_app = QApplication.instance() or QApplication([])`
pattern), with no behavior change in real usage where no instance exists
yet when `main()` runs.

Verified by rerunning the frozen `app.exe` directly with no
`%ProgramData%\Reflex\config.json` present: the process now stays
alive holding a blocking dialog open, instead of exiting within
milliseconds as it did before the fix. Installer recompiled with the
fixed `app.exe`; re-verification on a clean machine is the immediate
follow-up, same as the 2026-08-20 entry's original "not yet verified"
item this replaces.

## 2026-08-25 — `IdsPeakAlreadyInstalled()` checks a version, not just a folder

Follow-up investigation into the restart-prompt friction from the
Sandbox re-test above led to comparing how other real optometry/
ophthalmology device vendors handle IDS peak deployment — specifically
Haag-Streit's EyeSuite and Keeler's own Kinexis (Vantage Plus Digital
BIO) installers, both supplied by the user for analysis. Binary/archive
inspection (embedded-string scanning for the InstallShield-vs-Advanced-
Installer-vs-Inno-Setup engine question, then `innounp` — the standard
Inno Setup archive unpacker — to list Kinexis's actual embedded file
table) found:

- **EyeSuite** (Advanced Installer, MSI-backed) bundles and installs a
  full IDS peak SDK + both transport layers (`ids_ueyegentl`,
  `ids_u3vgentl`) via a real named-property silent install (`/qn`,
  `APPDIR=`) — confirming Advanced Installer's silent path is genuinely
  robust, unlike IDS's own InstallScript-based installer.
- **Kinexis** (Inno Setup) bundles `ids_peak_2.9.0.0.exe` plus a real,
  working InstallShield response file and drives it exactly the way this
  project's earlier IDS-installer entry described hypothetically:
  `ids_peak_2.9.0.0.exe /s /f1".\Install_Setup.iss"`, no exit-code check
  afterward beyond a flat `timeout /t 4`. The response file's own
  `[...SetupType2-0] Result=303` / `Result=304` entries are exactly the
  positional, non-semantic dialog-index recording described in that
  entry — real confirmation, not a hypothetical. It does *not* enable the
  uEye Transport Layer, consistent with the Vantage Plus Digital's camera
  being native USB3 Vision (no Transport Layer needed) per CLAUDE.md's
  hardware table.

**The concrete, actionable finding:** Kinexis silently installs IDS peak
**2.9.0.0** — a version far older than `26.06.1`, what
`vendor/ids-peak-win-extended-setup-64.exe` currently bundles — into the
same `{pf}\IDS\ids_peak` location `IdsPeakAlreadyInstalled()` in
`packaging/reflex.iss` was checking with a bare `DirExists()`. A real
clinic machine that's only ever run Kinexis (plausible, since it's
Keeler's own software for the same BIO) would have that check wrongly
report "already installed" and skip the bundled installer entirely,
silently leaving a years-out-of-date SDK in place instead of the current
one `requirements-ids.txt`'s pinned Python bindings actually expect —
this was a real gap, not a hypothetical one, found by actually inspecting
a real vendor installer rather than assumed.

**Fix:** `IdsPeakAlreadyInstalled()` now checks
`{pf}\IDS\ids_peak\program\ids_peak.dll`'s own `FileVersion` via Inno's
`GetVersionNumbers`/`ComparePackedVersion`, against a new
`IdsPeakMinDllVersion` preprocessor constant (`"1.16.0.0"`) — confirmed
empirically to be what the currently-bundled `26.06.1` extended setup
actually installs, via `(Get-Item '...\ids_peak\program\ids_peak.dll').
VersionInfo.FileVersion` on this dev machine (which has run that exact
installer). IDS's installer-package version string (e.g. `"26.06.1"`) and
this DLL's own `FileVersion` (`"1.16.0.0"`) are unrelated numbering
schemes — confirmed by checking both directly, not assumed to match —
so the DLL's own version field is what has to be compared, and
`IdsPeakMinDllVersion` needs updating (the same empirical way) whenever
`vendor/ids-peak-win-extended-setup-64.exe` is bumped to a new IDS
release.

**A syntax pitfall hit while writing this fix, worth remembering:** Inno
Setup's Pascal `[Code]` section comments (`{ ... }`) don't nest, and don't
treat their own contents as inert — a literal `{pf}` constant reference
*inside* a `{ }`-delimited comment closes the comment early at that `}`,
silently turning the rest of the intended comment into parsed code
(`Error on line 96: Column 52: Syntax error.` was the actual symptom).
Fixed by switching that comment block to `(* ... *)` delimiters, which
don't conflict with literal braces, and by avoiding constant-reference
syntax inside comments in general going forward.

**Decision on the restart/silent-install question this whole thread
started from:** unchanged — still keep IDS's own bundled installer
interactive, with "choose Restart Later if prompted" as the
documentation fix (Reflex's own files/shortcuts are already
installed by the time `[Run]` fires `ids-peak-win-extended-setup-64.exe`,
so nothing functionally depends on that restart happening immediately).
Kinexis proves silent driving is *possible* — a real vendor ships it —
but its own implementation (no error-checking beyond a flat sleep, a
now-confirmed-real version-drift case) is a live example of the exact
fragility risk the earlier entry was concerned about, not a
counterargument to it.

## 2026-08-25 — Silent IDS peak install, with verification Kinexis doesn't have

### Reversal

The entry directly above concluded "stays manual," and the 2026-08-19
entry before that reached the same conclusion for `setup.ps1`. Both are
now superseded: `packaging/reflex.iss` drives IDS peak's installer
**silently**. What actually changed the calculus, across this
conversation's back-and-forth:

- **The "student finds a dead camera weeks later" framing was wrong.** A
  technician runs `settings.py` and uses its Preview button right after
  installing, before ever handing the machine to a student -- a bad
  silent install surfaces there, close in time to the actual cause, not
  weeks later disconnected from it.
- **Version-drift risk assumes a careless process; this project doesn't
  have one.** One developer (the user) controls every
  `vendor\ids-peak-win-extended-setup-64.exe` bump and can re-record
  deliberately each time, rather than a team where "someone forgot" is
  realistic.
- **The installer's own success signal was never the only available
  one.** `IdsPeakAlreadyInstalled()`'s real `ids_peak.dll` version check
  (previous entry) already exists and can be run *after* a silent
  install, not just before one -- closing the actual gap in Kinexis's
  own approach (it trusts a silent replay with zero verification) without
  needing a human to watch the wizard to get that verification.

### What was built

`InstallIdsPeakSilently()` in `packaging/reflex.iss`'s `[Code]`
section, called from `CurStepChanged(ssPostInstall)` (replacing the old
declarative interactive Run entry entirely):

1. Skips entirely if `IdsPeakAlreadyInstalled()` already passes (unchanged
   behavior from the previous entry).
2. Otherwise runs `ids-peak-win-extended-setup-64.exe /s /f1"<response
   file>" /f2"<log file>"` via `Exec()`, capturing the real exit code --
   this is the same `/s /f1` mechanism Kinexis uses in production (see
   previous entry), just with the verification it lacks layered on:
   - `Exec()` returning False, or a nonzero `ResultCode`, is a **hard
     failure** (Windows blocked it, the installer crashed) -- shown as an
     explicit error dialog pointing at `SETUP.md`'s manual steps, not
     swallowed.
   - Even `ResultCode = 0` isn't trusted as the whole story: a stale
     response file can replay cleanly against a *different* dialog layout
     in a newer IDS peak version and report success while silently
     picking the wrong components -- exactly the failure mode discussed
     in the previous entry. `IdsPeakAlreadyInstalled()` runs *again*
     afterward as the real verification; if it still fails, that's shown
     as its own explicit error rather than assumed fine because the exit
     code was 0.
3. `NeedRestart(): Boolean` -- Inno Setup's own built-in hook, returning
   True only when this run actually installed and verified IDS peak.
   Needed because the response file's recorded answer to IDS's own
   restart prompt is deliberately "restart later" (below) -- with nobody
   watching a silent install, that answer just fires unprompted if
   recorded as "restart now," so it has to defer, which means *something*
   still needs to surface "you should restart" to the technician. Inno's
   native Finished-page restart prompt is that surface, rather than a
   custom dialog.
4. The IDS installer's own `/f2` log now writes to `{app}\
   ids-peak-install.log` (not `{tmp}`, which Inno wipes once setup
   finishes -- a log path a technician can't actually open after the fact
   isn't useful in an error message).

### Recording the response file

`vendor\ids-peak-response.iss`, recorded via `<installer>.exe /r /f1"..."`
on a **fresh Windows Sandbox session** (not this dev machine, which
already has IDS peak -- recording against an already-installed machine
gets InstallShield's Modify/Repair/Remove flow instead of a fresh-install
one, recording the wrong thing). Full steps in `PACKAGING.md`. Two things
learned doing this for real, not assumed:

- **This IDS peak version (26.6.1) is better-behaved than Kinexis's much
  older bundled 2.9.0.0.** Its `SdComponentTree` dialog records **named**
  component keys (`UEyeSupport\UEyeDrivers`, `USB3VisionProducer`, ...),
  not pure numeric positions the way the older `SetupType2` dialog does
  for the top-level install type. Doesn't remove the positional-replay
  risk in general (the top-level dialogs are still numeric `Result=`
  codes), but it's a real, observed data point that "positional" isn't
  uniformly true across every dialog in every IDS peak version.
- **The recorded restart answer matters more here than in the abandoned
  interactive design.** Told the user explicitly, before recording: since
  the replay is silent, whatever gets recorded at the final restart
  prompt fires automatically on every future clinic machine with nobody
  present to answer it -- recording "restart now" would mean an
  unannounced reboot on a real clinic machine every time this response
  file gets replayed. Recorded "No, I will restart later"
  (`SdFinishReboot-0`: `Result=1, BootOption=0`), consistent with
  `NeedRestart()` above picking up the slack instead.

### A recurring Pascal Script syntax pitfall (three more hits, same bug)

The 2026-08-25 "checks a version, not just a folder" entry above already
noted that a literal `{pf}` inside a `{ }`-delimited Pascal comment closes
the comment early, since Inno's Pascal comments don't nest. Building this
feature hit the *same* bug twice more in one sitting -- `{app}`/`{tmp}`
inside a `{ }` comment, and separately a literal `[Run]` inside a `{ }`
(now `(* *)`) comment, which isn't a Pascal nesting issue at all but a
different mechanism: Inno's own section-tag scanner runs a line-based
pass looking for `[SectionName]`-shaped lines *before* Pascal comment
parsing happens at all, so a bracketed reference to another section's
name inside any comment can break section detection regardless of
comment style. Take away for next time: avoid literal `{constant}` and
`[SectionName]` text inside any `[Code]` comment, in any delimiter style
-- reword around it instead of assuming the comment is inert.

### Status

Built and compiles cleanly (`packaging\installer_output\reflex-setup.exe`).
**Not yet verified end-to-end** on a real clean machine -- the sandbox
session used to record the response file now has IDS peak installed from
that recording, so it can't also be the clean-machine test for the silent
replay; that needs a *separate* fresh sandbox session. Also not yet
verified: the deliberate-failure path (temporarily renaming/breaking
`vendor\ids-peak-response.iss` and confirming the error dialogs actually
fire instead of silently continuing).

## 2026-08-25 — Silent IDS peak install: the restart prompt never showed

First real Sandbox test of the silent-install build (previous entry)
surfaced a bug: after a genuinely fresh, verified-successful silent IDS
peak install, Setup went straight to the plain "Completing the Reflex
Setup Wizard" Finished page -- no restart-choice radio buttons, even
though `NeedRestart()` returned `IdsPeakInstalledThisRun`, which
`InstallIdsPeakSilently()` had just set `True`. No error dialog either,
and `ids_peak.dll` was confirmed present on disk -- so the install itself
genuinely worked; only the restart notification was silently missing.

### Root cause, confirmed empirically, not guessed

Three throwaway `.iss` test scripts (compiled and run with `/VERYSILENT
/LOG=`, entirely outside the sandbox loop, to avoid another 15-minute
round trip per iteration) isolated this precisely:

1. A script returning a **hardcoded** `NeedRestart(): Boolean := True`
   worked correctly (`Need to restart Windows? Yes` in Setup's own log).
2. A script matching the *real* structure -- a global flag set to `True`
   inside `CurStepChanged(ssPostInstall)`, read back by `NeedRestart()`
   -- did **not** work (`Need to restart Windows? No`), reproducing the
   bug exactly.
3. Adding `Log()` calls to every step nailed the exact ordering:
   ```
   CurStepChanged called with CurStep=1     (ssInstall)
   Creating directory: ...                   (Files/Icons written here)
   NeedRestart() called, MyFlag=False        <- queried HERE
   CurStepChanged called with CurStep=2     (ssPostInstall)
   Setting MyFlag to True                    <- too late, already asked
   Need to restart Windows? No
   ```
   Inno queries `NeedRestart()` exactly once, right after Files/Icons
   finish writing but **before** `CurStepChanged` is ever called with
   `ssPostInstall`. Nothing re-queries it afterward. A flag set from
   inside `ssPostInstall` is therefore structurally always one step too
   late, regardless of what the flag's value is -- this wasn't a
   Pascal bug, it was a wrong assumption about Inno's own call order
   (confirmed against Inno's actual source on GitHub,
   `jrsoftware/issrc`, which shows the internal `NeedsRestart` decision
   and its "Need to restart Windows?" log line both happening inside the
   same `Install` procedure that later calls `CurStepChanged(ssPostInstall)`
   -- not after it).

### Why not just move the IDS install earlier (to `ssInstall`)

That would fix the ordering, but reopens the exact risk `ssPostInstall`
was chosen to avoid: `ssInstall` fires *before* `[Files]`/`[Icons]` are
written, so if the silent `Exec()` call ever hung indefinitely (not just
returned a nonzero code, which is already handled either way) rather
than erroring, Reflex's own `app.exe`/`settings.exe`/shortcuts might
never get installed at all. Keeping the IDS install in `ssPostInstall`
preserves "Reflex itself is safe regardless of what happens to the
bundled IDS installer" from the original 2026-08-20 frozen-exe entry.

### Fix

Dropped `NeedRestart()` and the `IdsPeakInstalledThisRun` global
entirely. `InstallIdsPeakSilently()` now shows an explicit
`MsgBox(mbInformation)` directly -- "A restart is recommended..." --
right after a verified-successful install, instead of depending on
Inno's internal call order at all. Simpler code, and a message that's
guaranteed to actually appear rather than a native mechanism with a
timing gotcha this project only found by instrumenting it directly.
Matches CLAUDE.md's general "loud and early" bias better than the
native mechanism did anyway.

Verified against the real `reflex.iss` compiling cleanly; **not yet
re-verified in an actual fresh Sandbox** that the MsgBox appears at the
right moment -- that's the next real test, along with the
still-outstanding deliberate-failure-path check from the previous entry.

## 2026-08-25 — Silent IDS peak install: native restart page

Follow-up to the entry directly above. The MsgBox fix worked, but wasn't
what was actually wanted -- a plain "OK"-only popup, not a real Restart
now/Restart later choice like IDS's own installer. First attempt at that
(`TaskDialogMsgBox` with custom button labels, triggering `shutdown.exe
/r` directly on "Restart now") worked but was still a separate popup, not
integrated into Setup's own Finished page. Getting the *native* Finished-
page radio-button choice back on the table required working out, and
then accepting, what it actually costs -- summarized here since the
reasoning spans several back-and-forths, not one decision.

**Why `ssPostInstall` can't produce the native choice, confirmed, not
assumed:** `NextButtonClick` doesn't fire for the Finished page's Finish
button (confirmed against Inno's own documentation) -- so even manually
toggling `WizardForm.YesRadio`/`NoRadio` visibility from `ssPostInstall`
would show a choice with no supported way to read back which one got
picked when Finish is clicked.

**Why moving the whole install to `ssInstall` isn't just risky, it's
broken outright, as literally described:** a purpose-built empirical test
(a `[Files]` entry logged for both `{tmp}`-file existence and `{app}`-dir
existence at each `CurStepChanged` step) showed both are `0` at
`ssInstall` and `1` by `ssPostInstall` -- Setup's normal file-copy phase,
`{tmp}` extraction included, hasn't run yet when `ssInstall` fires. Naively
moving `Exec()` there would point at a 356MB file that doesn't exist.

**What actually made it work:** `ExtractTemporaryFile()` (confirmed via
Inno's own docs: pulls a specific `[Files]` entry, flagged `dontcopy`, out
of the archive on demand, ahead of Setup's normal bulk-copy timing) plus
`ForceDirectories(ExpandConstant('{app}'))` to manually create the install
directory early for the log path. A combined throwaway test (dontcopy +
ExtractTemporaryFile + ForceDirectories + setting the restart flag, all
from `ssInstall`) confirmed the full chain works: `NeedRestart()` sees the
flag as `True`, and Setup's own log shows `Need to restart Windows? Yes`.

**The real tradeoff, worked through with direct pushback, not glossed
over:** moving the silent IDS install to `ssInstall` means Reflex's
own files/shortcuts are no longer guaranteed installed before it runs --
the entire reason `ssPostInstall` was chosen in the first place. Several
rounds of direct questioning actually about what this protects against in
practice:
- A genuine indefinite hang (not a clean error -- those are already
  caught by the exit-code/log checks either way) is the only scenario
  where ordering matters at all.
- Inno has no timeout/cancel on `Exec(..., ewWaitUntilTerminated, ...)`,
  so a real hang can only ever be resolved by a technician force-killing
  the process by hand -- there is no automatic rollback in either
  ordering once that happens; whatever was already written to disk stays
  written.
- Without a working system-wide IDS peak install, neither `app.exe` (no
  instrument cameras) nor `settings.exe` (no IDS devices to detect) can
  do their real job anyway -- "Reflex is installed" was overstating
  the benefit as "Reflex works," which it doesn't, without IDS peak.
- The odds of an actual indefinite hang (as opposed to a clean, already-
  handled error) are low, and `WizardForm.StatusLabel.Caption` already
  identifies which step is running if a technician comes back to a stuck
  installer either way.

Net conclusion: the `ssPostInstall` ordering's real remaining benefit was
narrower than originally presented (diagnostic clarity + not re-doing the
fast file-copy step on retry, not "a working app"), the failure mode it
protects against doesn't resolve cleanly under either ordering regardless,
and the native restart-page UX was worth that narrower tradeoff. Decision:
switched to `ssInstall`.

**What changed in `packaging/reflex.iss`:**
- `vendor\ids-peak-win-extended-setup-64.exe` and
  `vendor\{#IdsPeakResponseFile}`'s `[Files]` entries: `deleteafterinstall`
  → `dontcopy`, moved to the top of `[Files]` (solid-compression
  decompression cost grows with position in the archive).
- `InstallIdsPeakSilently()`: calls `ExtractTemporaryFile()` for both,
  then `ForceDirectories({app})`, before doing anything else. Ends by
  setting `IdsPeakInstalledThisRun := True` on verified success (the hard-
  failure and verification-failure `MsgBox`es are unchanged from the
  previous entry -- those still apply regardless of timing).
- `NeedRestart()` and the `IdsPeakInstalledThisRun` global are back, this
  time actually working -- the `TaskDialogMsgBox`/`shutdown.exe` block
  from the previous entry is removed entirely.
- `CurStepChanged`: `ssPostInstall` → `ssInstall`.

Verified against the real script compiling cleanly, including the
`vendor\` files still landing in the compressed archive despite the
`dontcopy` flag change (confirmed via the compile log's `Compressing:`
lines). **Not yet re-verified end-to-end in a fresh Sandbox** -- same
outstanding real test as the previous two entries, now including
confirming the native Finished-page choice actually appears and that
choosing "Yes" there genuinely triggers a restart.

---

## 2026-08-25 — Built the exposure/gain calibration feature (not yet hardware-verified)

**Decided:** Implemented ROADMAP.md's "In-app exposure/gain calibration"
design in full: `IdsCamera` gains `needs_manual_calibration()`,
`get_/set_exposure_time_us()`, `exposure_time_range_us()`,
`get_/set_gain()`, `gain_range()`, and `auto_calibrate()`; `InstrumentConfig`
gains optional `exposure_time_us`/`gain` fields that `_open()` applies
per-axis every session; `settings.py`'s `PreviewDialog` shows exposure/gain
sliders plus an Auto-Calibrate button whenever the previewed camera reports
`needs_manual_calibration()`, and `DeviceRow`/`SettingsWindow` round-trip
the resulting values into `config.json`.

**Why split `exposure_calibration.py` out of `ids_camera.py`:** the actual
median-brightness-and-correction-step math (`median_brightness()`,
`is_converged()`, `next_exposure_gain()`) has zero IDS SDK dependency, so
pulling it into its own module makes it unit-testable on this dev machine
(no `ids_peak` installed -- see CLAUDE.md's Environment section) instead of
being untestable dead weight inside a module this dev machine can't even
import. `test_exposure_calibration.py` covers it directly;
`ids_camera.py`'s `auto_calibrate()` is a thin loop calling into it plus
the real node reads/writes, same division of labor `compositor.py`/
`test_compositor.py` already model elsewhere in this codebase.

**Why `_open()` applies exposure_time_us/gain per-axis, not all-or-nothing:**
`InstrumentConfig`'s two fields are independently optional in the schema,
so a camera with e.g. working `ExposureAuto` but a calibrated manual `gain`
(unlikely today, not ruled out) gets exactly what config says for the axis
it specifies and today's auto-converge for the one it doesn't --
`_converge_auto_exposure()` grew `skip_exposure`/`skip_gain` params for
this rather than becoming two copies of near-identical code.

**What's NOT verified, and why this entry says so instead of claiming
otherwise:** this dev machine has no `ids_peak` installed and
`vendor/ids_peak_api.txt` doesn't exist here (it's gitignored, generated on
a machine with the SDK) -- CLAUDE.md is explicit that IDS method names must
come from that dump or hardware, not be invented. Every node-map call this
feature adds beyond what `_converge_auto_exposure()` already used
(`TryFindNode`/`IsAvailable`/`IsWriteable`/`SetCurrentEntry`, all
pre-existing and hardware-confirmed) is the `ExposureTime`/`Gain` float
node's `Value()`/`SetValue()`/`Minimum()`/`Maximum()` -- standard GenApi
`IFloat` accessor names, and the same `Value()`/`SetValue()` pattern this
file already uses for `Width`/`Height` (though those are integer nodes),
but not independently confirmed against real hardware or the dump. Flagged
in `ids_camera.py`'s module docstring and inline above those methods, and
in ROADMAP.md's status line for this entry, rather than presented as
verified.

**Rejected: a runtime brightness preflight check.** Already rejected in the
ROADMAP entry this implements, for the same reason (real false-positive
risk against a legitimately dim scene) -- not revisited.

**Also not done:** re-recording a hardware smoke test
(`tools/smoke_test_camera.py`) against the real slit lamp to confirm the
node accessor names above, and closing the loop on whether writing
ExposureTime/Gain from the GUI thread while the capture thread concurrently
calls `WaitForFinishedBuffer()`/`QueueBuffer()` on the same device is
actually safe -- assumed reasonable (this is how live exposure adjustment
during streaming works in GenICam generally, and how IDS peak Cockpit
itself already does it), not confirmed against this specific SDK's
concurrency guarantees.

---

## 2026-08-26 — Built the vignette/white-balance/fps-cap/backlight-compensation follow-ups

**Decided:** Implemented ROADMAP.md's 2026-08-26 entry in full: a centered
crop before brightness/color measurement, white balance (automatic or
manual-software depending on hardware), an acquisition frame-rate cap on
both cameras, and UVC backlight compensation.

**`exposure_calibration.py`:** added `center_crop()` (used by both
`auto_calibrate()` and the new `auto_white_balance()` -- one crop, reused
for both), `channel_medians()`, `is_white_balanced()`,
`next_balance_ratios()`. All pure and SDK-free, directly unit-tested in
`test_exposure_calibration.py` -- the only part of this work fully
verifiable on this dev machine (no `ids_peak` installed).

**`ids_camera.py`'s `_converge_auto_exposure()` was replaced, not wrapped,
by `_converge_auto_nodes(node_names)`:** exposure, gain, and (now)
white-balance convergence all follow the identical
Once-then-poll-until-Off-then-lock shape, so `_open()` now builds one list
of whichever `*Auto` nodes lack a manually-calibrated config value and
converges all of them in a single pass, rather than a second,
separately-polled loop bolted on for white balance. Rejected keeping the
old method as a thin wrapper: it had no external callers (no
`test_ids_camera.py` exists -- `ids_peak` isn't importable here), so there
was no compatibility cost to removing it, and keeping it alongside a
second loop would have been strictly worse than one combined pass. Renamed
`_AUTO_EXPOSURE_TIMEOUT_S`/`_AUTO_EXPOSURE_MAX_FRAMES` to
`_AUTO_CONVERGE_TIMEOUT_S`/`_AUTO_CONVERGE_MAX_FRAMES` and
`IdsCameraAutoExposureTimeoutError` to `IdsCameraConvergenceTimeoutError`
to match (private/internal-only, no external references, so both renames
are low-risk).

**White balance, two cases, both handled because it's unknown which
applies to either camera:** a camera with `BalanceWhiteAuto` gets it
automatically, folded into the `_converge_auto_nodes()` pass, no config, no
UI. A camera without it (the likely case for the slit lamp, which already
lacks `ExposureAuto`/`GainAuto`) gets `needs_manual_white_balance()`,
`get_/set_red_balance_ratio()`, `get_/set_blue_balance_ratio()`,
`red_/blue_balance_ratio_range()`, and `auto_white_balance()` -- mirroring
the exposure/gain accessor shape exactly, backed by GenICam's standard
`BalanceRatioSelector`+`BalanceRatio` selector-then-value pattern (six
public methods, one private `_select_balance_ratio()` helper, so the
selector-set line isn't duplicated six times).

**Config validation is asymmetric with exposure/gain, deliberately:**
`InstrumentConfig` gained `red_balance_ratio`/`blue_balance_ratio`, but
unlike exposure/gain (independent hardware axes, validated independently),
these two are two facets of one concept -- `BalanceWhiteAuto=Once`
converges both together, there's no "auto blue, manual red" -- so
`config.py` rejects exactly one of the pair being present as a
`ConfigError` rather than silently accepting a half-specified state.

**`settings.py`'s white-balance controls are a separate block from
exposure/gain's, not merged into one abstraction:** a camera could
plausibly lack `ExposureAuto`/`GainAuto` *and* `BalanceWhiteAuto`
simultaneously, so both blocks can be visible on the same dialog at once,
and a single shared status label would have one calibration's result
message clobber the other's -- confirmed as an actual correctness
requirement, not just a style preference, by
`test_exposure_gain_and_white_balance_status_labels_stay_independent` in
`test_settings.py`. The only extraction was the repetitive slider-row
construction (`_add_slider_row()`), used by both blocks.

**Acquisition frame-rate cap:** both `IdsCamera` and `UvcCamera` gained an
independently-optional `target_fps` constructor param (`None` = untouched
free-run -- preserves every existing call site, including
`tools/smoke_test_camera.py`/`tools/dual_camera_smoke_test.py`, which never
pass it). `app.py` threads `cfg.recording.fps` into both instrument and
third-person camera construction. Motivation, confirmed by reading
`recorder.py`'s `_tick()`/`_drain_latest()` rather than assumed: the
recorder already discards any frame a camera captures faster than
`recording.fps` before encoding, so letting cameras free-run at native
~58-60fps produces zero benefit and burns exactly the USB bandwidth margin
CLAUDE.md's Hardware section flags as tight. `settings.py`'s Preview
cameras deliberately never receive `target_fps` -- Preview is single-camera
and technician-supervised, so the bandwidth concern this cap exists for
(two simultaneous instrument streams) never applies there.

**Rejected: capping frame rate by reading it back from the device instead
of config.** Not actually considered as a real alternative -- `target_fps`
is sourced from `recording.fps`, which DECISIONS.md's "config-driven
recording fps" entry already established as config-driven for its own
reasons (encoder pacing, not a camera capability). This entry only adds
that the *camera* should also respect that same number, not that the
number's source should change.

**Backlight compensation (UVC):** `uvc_camera.py`'s
`_lock_autofocus_and_exposure()` renamed to `_configure_capture()` (it now
does more than autofocus/exposure) and gained `cv2.CAP_PROP_BACKLIGHT`,
set *before* the existing warmup loop so the auto-exposure convergence that
warmup lets run happens with backlight compensation already active, not
after.

**Black balance was scoped out entirely, not just deprioritized** -- it
turned out to be the user conflating it with backlight compensation above,
not a separate thing actually wanted, so nothing was built for it. No
`get_/set_black_level()`, no config, no UI.

**What's NOT verified, stated plainly:** every new call into
`ids_camera.py`'s GenICam node map (`BalanceWhiteAuto`,
`BalanceRatioSelector`/`BalanceRatio`, `AcquisitionFrameRateEnable`/
`AcquisitionFrameRate`) is unconfirmed against real hardware or
`vendor/ids_peak_api.txt` -- this dev machine has neither. Also unverified:
whether `AcquisitionFrameRate` can be set *after* `AcquisitionStart` the
way `ExposureAuto`/`GainAuto` already are (confirmed working post-start),
since it more directly reconfigures stream timing than a pure value node;
if hardware testing shows otherwise, the call needs to move earlier in
`_open()`, before `data_stream.StartAcquisition()`. The `0.5` center-crop
fraction is a starting guess, not a measurement against real slit-lamp/BIO
footage. `_converge_auto_nodes()` itself is a refactor of
previously-hardware-verified control flow (exposure/gain), not purely
additive, so re-running `tools/smoke_test_camera.py` against both real
cameras is warranted even though per-axis semantics didn't change.
`cv2.CAP_PROP_BACKLIGHT`/`CAP_PROP_FPS`/`CAP_PROP_AUTO_WB` are, by
contrast, confirmed to exist on this dev machine (cv2 is installed here) --
lower risk, though their real-device *effect* still isn't verified. Full
test suite (134 tests) passes; nothing IDS-node-specific has
`test_ids_camera.py` coverage since `ids_peak` isn't importable here, same
precedent the exposure/gain feature was already shipped under.

---

## 2026-08-26 — `Net2860Camera`: 32-bit helper process for the older Vantage Plus BIO

**Decided:** A second, older-model Keeler Vantage Plus Digital BIO was
connected for testing. Built and hardware-verified `Net2860Camera`, a new
`BaseCamera` implementation for it, plus a 32-bit helper subprocess
(`net2860_helper.py`) and a small framed stdout protocol
(`net2860_protocol.py`) between the two. Not yet wired into
`config.py`/`app.py`/`kiosk.py`/`settings.py` -- this entry covers only the
camera module itself; making it selectable end-to-end in the running kiosk
app is separate follow-up work (`kiosk.py` needs zero changes for that
when it happens -- confirmed it's already generic over `BaseCamera` and
never branches on `InstrumentConfig.kind`).

**Hardware identity:** Not IDS Imaging hardware (VID `0x1409`) at all --
raw USB descriptor shows VID `0x20F1` (NET GmbH), PID `0x0004`,
`bInterfaceClass = 0xFF` (Vendor Specific, i.e. not UVC). Opening the unit
confirmed the actual capture silicon is an eMPIA EM2860 USB video bridge
chip (marking on the board: "eMPIA EM2860 PGNA7-014 1510-01AG"), OEM'd by
NET GmbH under their own VID/PID with their own driver rather than eMPIA's
reference identity (`0xEB1A`). The sensor line is a 1/2" CCD (NET GmbH
"KS722OUP" board), interlaced PAL, negotiating `720x576` in practice
(device-derived at runtime, not hardcoded -- see below).

**Why neither `ids_camera.py` nor `uvc_camera.py` can see it:** No
GenICam/USB3 Vision presence (not IDS hardware). No UVC/DirectShow-
video-capture-source-category presence either -- confirmed via
`uvc_enumeration.list_uvc_devices()`, which only saw the laptop's built-in
webcam and a phone virtual camera. The vendor driver (`net2860_usbx64.sys`
+ `netvecam4.ax`) registers a DirectShow filter as an ordinary COM class
(`{6B83EF35-8FB5-45CB-BFF4-0876FF6F31D5}`, registered name `"KS722OUP"`)
but NOT under `CLSID_VideoInputDeviceCategory`, so nothing that discovers
capture devices by enumeration (OpenCV, `pygrabber`, this project's own
`uvc_enumeration.py`) can find it. It has to be instantiated directly by
CLSID -- confirmed this is exactly the mechanism Keeler's own bundled app
("Kapture") uses internally, by reading `Kapture`'s `Logs\Capture.log`
(`AddFilterbyCLSID Name,pF = USB source,...`) from a prior Kapture install
already present on the dev machine. Kapture itself was not used or needed
for any part of this -- its license only gates its own app UI, not the
driver/filter, which is an ordinary Windows COM component independent of
it.

**Why a 32-bit helper subprocess, not in-process:** Every user-mode piece
of this vendor filter (`netvecam4.ax`, and the `Sample Grabber`/`qedit.dll`
registration used alongside it) is registered only in the WOW6432Node
(32-bit) COM view -- confirmed by reading PE headers directly:
`net2860_usbx64.sys` (kernel driver) is native x64, but `netvecam4.ax`,
`NET_USBIO_EMP1.dll`, and Kapture's own `Kapture.exe`/`VC2860.exe` are all
x86. File dates suggest this is simply a 2011 shipping decision NET GmbH
never revisited (the kernel driver is dated 2009, already 64-bit; the
32-bit-only user-mode filter is dated two years later) rather than a
technical requirement -- the product line appears dead since (NET GmbH's
current site has moved on to unrelated GenICam/USB3/GigE Vision products).
The project's venv Python is 64-bit, so this camera's capture code cannot
run in the same process as the rest of `Reflex`; `net2860_camera.py`
launches a 32-bit Python subprocess (`net2860_helper.py`, under a separate
`.venv32/` -- see `setup_net2860_helper.ps1`) that does the actual
DirectShow work and streams frames back over a pipe. This mirrors
CLAUDE.md's "nothing outside a camera module may reference the vendor SDK"
rule, with the process boundary standing in for the module boundary
`ids_camera.py`/`uvc_camera.py` normally provide on their own --
`net2860_camera.py` itself never imports `comtypes`/`pygrabber`.

**Generic eMPIA driver considered and rejected:** Microsoft's own Update
Catalog (WHQL-signed, downloaded directly from
`catalog.s.download.windowsupdate.com`) has a genuine 64-bit eMPIA EM28xx
driver package (`emPRP64.ax`, `emBDA64.sys`, `emOEM64.sys`, dated 2015),
which would have eliminated the 32-bit constraint entirely. Rejected after
inspecting its `.inf`: all 54 hardware IDs it declares use eMPIA's own
reference VID (`0xEB1A`) only -- none match NET GmbH's OEM VID
(`0x20F1&PID_0004`). Using it would require hand-editing the INF to add
our hardware ID and enabling machine-wide test-signing mode (both kernel
drivers in the package are unsigned once the INF is modified) -- a
security-posture change affecting the whole machine, for an unverified
payoff (no guarantee this differently-designed board even initializes
correctly under a generic reference driver). The proven CLSID/32-bit-helper
path was already working by the time this was evaluated, so it stayed.

Note that this paragraph rejected *one specific driver package*, on
grounds specific to that package. It was later read as a general rule
that the vendor driver can never be replaced and the 32-bit helper is
permanent. It is not such a rule, and the 2026-09-09 entry below
supersedes that reading.

**Wire protocol (`net2860_protocol.py`), why not named pipe/socket/shared
memory:** A subprocess stdout pipe with a small framed protocol (`RDY1`
handshake carrying device-derived resolution, `FRM1` per frame, `ERR1` on
failure) is the simplest correct option for what is always a 1:1
parent/child relationship -- process lifetime and pipe lifetime are
naturally tied together (no separate handle/port/segment to leak or clean
up), and it needed no new project dependency. Frame timestamps are
`time.monotonic()` taken in the helper at the moment its
`ISampleGrabberCB.BufferCB` callback fires, which is valid to compare
against the main process's own `time.monotonic()` since Windows'
monotonic clock (`QueryPerformanceCounter`) is one system-wide clock
domain, not per-process.

**`Frame.index` deviation:** Assigned by `net2860_helper.py`, incrementing
once per `BufferCB` callback -- not a vendor-reported sequence number,
since this filter's frame-numbering (if it has one at all) isn't
documented or observed anywhere, and inventing a read of one would be
guessing at an undocumented API surface the same way CLAUDE.md already
warns against for the IDS SDK. Same accepted deviation `uvc_camera.py`'s
`UvcCamera` already makes from `ids_camera.py`'s contract (a
locally-assigned counter standing in for a real FrameID), but assigned by
the helper rather than by `Net2860Camera` itself, since the helper is the
component actually sitting at the capture callback -- `net2860_camera.py`
is one pipe-read removed from that boundary, and re-counting there would
only count "frames this process happened to read," not "frames the source
produced."

**Verified against real hardware:** `tools/smoke_test_net2860_camera.py`
against the real camera through the actual `Net2860Camera`/`BaseCamera`
capture thread (not a standalone script): resolution negotiated as
`720x576`; 30 frames captured via `camera.read()` at roughly 14fps;
`Frame.index` increments contiguously 0-29 with no gaps. Captured image
content is real sensor noise (confirmed non-corrupted, non-garbage pixel
data) rather than a real picture, because the BIO's own illumination
wasn't on and it wasn't pointed at anything during this test -- a physical
setup issue, not a software one; see the mechanism verification earlier in
this investigation for the equivalent finding via a scratch proof-of-concept
before this module existed. Full test suite (158 tests) passes, including
new `test_net2860_protocol.py` (pure wire-format round-trip/error-case
tests, no mocking needed) and `test_net2860_camera.py` (mocks
`subprocess.Popen`, same boundary-mocking pattern `test_uvc_camera.py`
uses for `cv2.VideoCapture`) -- no `test_ids_camera.py`-style gap here,
since both are fully offline/hardware-independent.

**What's NOT done, stated plainly:** Not wired into
`config.py`/`app.py`/`kiosk.py`/`settings.py` -- this camera is not yet
selectable in the running app. Not packaged -- `net2860_helper.py` is not
frozen into a standalone exe, so a clinic install would currently need a
32-bit Python present, which `PACKAGING.md`'s frozen-exe distribution
model doesn't provide; freezing it is real, separate future work. No
`IAMStreamConfig`-based frame-rate control (mirroring `UvcCamera.
_apply_frame_rate_cap`) -- this vendor filter's stream-config support
hasn't been observed or tested, so it isn't claimed. `SUPPORTED_HARDWARE.md`
lists this under a new "Prototyped, not yet integrated" section rather
than "Confirmed tested" -- it doesn't fit that table's implicit "usable in
the app today" meaning (it isn't wired in or packaged), but calling it
merely "untested" alongside things nobody has ever tried would undersell
the real hardware verification above.

---

## 2026-08-26 — `Net2860Camera` wired into `config.py`/`app.py`/`settings.py`, real recording verified

**Decided:** Wired the previous entry's `Net2860Camera` into the running app
as `kind: "net2860"`, an alternative to `kind: "ids"` for the existing `bio`
instrument role -- not a new role. `kiosk.py` needed zero changes (confirmed:
already fully generic over `BaseCamera`, never branches on `kind`); the
picker still shows exactly two buttons.

**`config.py`:** `InstrumentConfig.serial` becomes `str | None`.
`_parse_instrument()` accepts `kind: "net2860"`, requiring only `label` and
loudly rejecting `serial`/`exposure_time_us`/`gain`/`red_balance_ratio`/
`blue_balance_ratio` if present (catches copy-pasting an `"ids"` entry and
only changing `kind`, rather than silently ignoring the leftover fields).

**`app.py`:** `_make_camera()` had no real `kind` dispatch before this --
it inferred real-vs-synthetic from `serial is None` and always built an
`IdsCamera`. Now takes the whole `InstrumentConfig` plus an explicit
`synthetic: bool`, branching on `inst.kind` (`net2860` -> lazily-imported
`Net2860Camera(label=name)`, same lazy-import-avoids-a-hard-dependency
reasoning as the existing `ids_camera` import).

**`settings.py`:** the BIO row's dropdown needed a net2860 candidate
alongside real IDS devices, but this camera has no device-manager-visible
category to enumerate (see the previous entry) -- so it's a **static**
candidate (`_net2860_candidates()`), always offered, BIO row only, not
device-scanned. `RowCandidate` gained a `kind` field so a row can mix
candidate kinds; the preview factory (`ids_preview_camera_factory` ->
`instrument_preview_camera_factory`) now receives the whole candidate and
branches on `.kind` instead of always building an `IdsCamera`. Save
(`_instrument_data()`) branches the same way, writing `{"kind": "net2860",
"label": ...}` with no `serial`/calibration keys. Load
(`_load_existing_config()`) preselects via `inst.kind` rather than
`inst.serial` when the kind isn't `"ids"` -- the static candidate's
`key="net2860"` doubles as its own sentinel, since there's exactly one of
this camera to select.

**Verified end-to-end with real hardware, headlessly** (no GUI-automation
tool available, so driven through the same production code the GUI calls --
`kiosk.py` is documented as "unit-testable headlessly" for exactly this
reason):
1. `settings.py` with real enumeration/factories: `rescan()` lists the
   net2860 candidate on the BIO row only; selecting it and calling the real
   `_instrument_data()` produces `{"kind": "net2860", "label": "BIO
   (legacy)"}`; the real default preview factory builds a working
   `Net2860Camera` that starts and returns a real frame
   (`shape=(576, 720, 3)`).
2. `app.py`'s real `_make_camera()` with a real `InstrumentConfig(kind=
   "net2860", ...)` and `synthetic=False` returns a working `Net2860Camera`
   producing real frames -- confirms the dispatch itself, not just that the
   class works standalone.
3. A full real recording via a real `KioskController` (real `Net2860Camera`
   for `bio`, the real UVC "Integrated Webcam" for third-person, no slit
   lamp -- none is attached to this dev machine right now, and
   `KioskController` doesn't require exactly two instruments): reached
   `READY`, recorded 3 real seconds, produced a real `composite.mp4`
   (2,739,629 bytes) and `session.json` with sane numbers -- composite
   `1360x576` (720+640 wide, `max(576, 480)` tall, matching
   `side_by_side`'s aspect-preserving letterbox), 31 composite frames at
   the 10fps target over ~3.1s. `bio`'s `dropped_frames: 46` against 31
   delivered is expected, not a bug -- the net2860 camera free-runs faster
   than the 10fps recording target used for this test, and
   `recorder.py`'s `_drain_latest()` deliberately discards the excess
   (CLAUDE.md: "prefer dropping frames over blocking a capture thread"),
   showing up correctly in the drop count exactly the way real hardware
   drops do.

**Tests:** `test_config.py` gained 4 cases (valid net2860 parse; rejects
`serial`; rejects each calibration field; still requires `label`).
`test_settings.py`'s `_make_window()` fixture updated (factory now receives
a candidate, not a bare target) plus 5 new cases (BIO-only candidate
presence, save shape, load preselection, preview routing). Full suite: 166
tests, all passing.

**What's still NOT done:** Packaging -- `net2860_helper.py` isn't frozen
into a standalone exe, so a clinic install built via `PACKAGING.md`'s
current process still needs a 32-bit Python present, which it doesn't
provide. Explicitly the next, separate step (per the user: wire in and
confirm it works first, then decide on packaging).

---

## 2026-09-01 — settings.py Preview leaked the IDS device when closed via Esc

**Decided:** `PreviewDialog` stops its camera from a slot connected to the
`finished` signal (plus an explicit teardown on the `__init__` control-build
failure path), not only from `closeEvent`.

**Why:** `PreviewDialog` runs modally via `.exec()`. `QDialog.reject()` --
which the Esc key triggers directly -- calls `done()`/`hide()` without ever
delivering a `QCloseEvent`, so a `closeEvent`-only teardown never ran
`camera.stop()`. Because `_on_preview_clicked` parents the dialog, the
leaked `IdsCamera` then stayed alive holding the IDS device open for the
rest of the settings session, and every subsequent Preview on that row
failed with `GC_ERR_RESOURCE_IN_USE` ("Module IDS/... is open already").
Only a full restart of `settings.py` recovered it. A second variant of the
same leak: `start()` succeeds, then one of the calibration-control builders
(which touch IDS nodes this codebase flags as unverified) raises inside
`__init__`, propagating out before the dialog is ever shown or closed.

`finished` fires for `accept()`, `reject()`, Esc and the window X alike, so
it covers every way `.exec()` can return. `_shutdown()` is idempotent
(reached from both `finished` and `closeEvent`) and tolerates being called
before `self.timer` exists.

**Rejected:** `WA_DeleteOnClose` -- `_on_preview_clicked` reads
`dialog.final_*` after `.exec()` returns, which would be use-after-free.
Overriding `reject()`/`done()` -- narrower than connecting `finished`, and
easy to miss a path.

**Tests:** `test_settings.py` gained `test_reject_stops_the_camera` and
`test_camera_stops_when_building_calibration_controls_raises`.

---

## 2026-09-01 — slit lamp has no white-balance nodes at all; `needs_manual_white_balance()` must check

**Decided:** `IdsCamera.needs_manual_white_balance()` returns True only when
the camera has no `BalanceWhiteAuto` **and** does expose the manual
`BalanceRatioSelector`/`BalanceRatio` nodes.

**Why:** Hardware-surfaced once the Preview device-leak fix (above) let
execution reach this path on the real slit lamp. The slit lamp via the uEye
Transport Layer's basic feature set exposes *neither* `BalanceWhiteAuto`
*nor* `BalanceRatioSelector` -- it has no white balance to control at all,
the same practical situation as the Keeler (which has working auto). The
old check keyed purely on `BalanceWhiteAuto` being absent, so it returned
True and `settings.py`'s `_build_white_balance_controls()` then crashed
with `NotFoundException` on `FindNode("BalanceRatioSelector")`, taking down
the whole Preview dialog.

This mirrors what `needs_manual_calibration()` already gets right for
exposure/gain by luck -- the slit lamp *does* expose `ExposureTime`/`Gain`,
so that path was fine; white balance was the one axis where the manual
fallback nodes are also missing.

**Still not hardware-verified:** the manual `BalanceRatio` read/write path
itself (`get_/set_red_balance_ratio()` etc.) -- no attached camera exercises
it, since the only one that lacks `BalanceWhiteAuto` also lacks
`BalanceRatio`. If a future camera has that combination, re-check against
`tools/smoke_test_camera.py`.

---

## 2026-09-01 — Device-model rotation presets

**Decided:** The Keeler Vantage Plus Digital BIO's camera mounts inverted,
so frames need a 180° rotation. That rotation is keyed on the IDS **model
name** in `device_presets.py` (`rotation_for_model()`), not set per-install
in `config.json`. `IdsCamera._open()` resolves it once the model is known
(`descriptor.ModelName()`); `BaseCamera._run()` applies it to every frame
before queueing, so recorder, preview and kiosk all agree. A `config.json`
`instruments.<role>.rotation` (0 or 180) overrides the preset as an escape
hatch, but there is **no `settings.py` UI for it** yet.

**Why keyed on model, not config:** every unit of that Keeler product ships
the camera in the same orientation -- it's a property of the hardware
model, not something that varies between clinics or that a technician
should have to discover and type. Confirmed model strings on real hardware:
BIO camera reports `U3-327xCP-C`, slit lamp reports `UI325xCP-C`, so the
`"U3-327"` substring token hits the former only. Verified end to end: the
BIO camera resolves `rotation=180` and delivers contiguous 2048x1536
frames; the slit lamp resolves `0`.

**Why 0/180 only (no 90/270):** a 90/270 rotation makes frames no longer
match the camera's reported `.resolution`, which `recorder.py`/`kiosk.py`
use to size the recording canvas -- supporting it would mean making
`BaseCamera.resolution` rotation-aware across all four subclasses. No
instrument needs it. `camera.ALLOWED_ROTATIONS` and
`config._parse_optional_rotation()` both enforce this.

**Why a config override at all:** cheap (a dataclass field + one parser),
and it's the escape hatch if a preset is ever wrong or a clinic has a
non-standard mounting, without a code change. `rotation: 0` explicitly
survives as 0 (not None) precisely so it can cancel a preset.

**Rejected:** device-side `ReverseX`/`ReverseY` GenICam nodes -- 180° needs
both, which shifts the Bayer phase, and this dev machine has no
`vendor/ids_peak_api.txt` to confirm the sensor compensates. Software
rotation is one contiguous-array copy per frame (~9MB on the BIO camera,
well under a frame interval) and provably correct.

**Not done:** `settings.py` still has the technician pick a camera from a
free dropdown and type a label. The better shape -- a category dropdown of
known-compatible devices, each carrying its own presets -- is a real
redesign, written up in ROADMAP.md.

**Tests:** new `test_device_presets.py` (lookup logic) and `test_camera.py`
(the `BaseCamera` rotation mechanic, via a fixed-frame fake and a real
`SyntheticCamera`); `test_config.py` gained 5 rotation cases.

---

## 2026-09-01 — Device-model presets: generalized "rotation" to "orientation" (BIO image is mirrored, not rotated)

**Supersedes the mechanic in the "Device-model rotation presets" entry
above** (the model-keyed-preset rationale and the 0/90/270 exclusion still
stand). On real hardware the Keeler BIO image, after the 180° rotation that
entry applied, was still wrong: correct top-to-bottom but mirrored
left-to-right. The BIO's instrument optics mirror the image; the camera
isn't physically rotated. Composing the applied 180° with the needed
horizontal mirror, the actual correct fix is a **pure vertical flip**
(`image[::-1, :]`) -- which is exactly what `net2860_helper.py` already
does (`np.flip(axis=0)`) for the older BIO camera. Same instrument, same
fix.

**Decided:** `camera.py` now carries an `orientation` (one of
`VALID_ORIENTATIONS`: `"none"` / `"rotate_180"` / `"flip_horizontal"` /
`"flip_vertical"`) instead of a `rotation` int -- the full Klein-four group
of dimension-preserving symmetries, since a mounting/optics quirk can be
any of them, not just 180°. `apply_orientation()` is the one place the
transform is defined. `device_presets.orientation_for_model()` maps
`U3-327x` → `"flip_vertical"`. `config.json`'s override field is renamed
`rotation` → `orientation` and takes the same four strings.

**Why a full rename, not `rotation` + a new `flip` field:** two fields
would be redundant (`rotate_180` == both flips) and so ambiguous when they
disagree. One enum over the closed group is unambiguous and still just a
string in config. Nothing was deployed on the old `rotation` field (no
clinic config, not in `config.example.json`), so a clean rename costs
nothing.

**Not yet hardware-verified:** the IDS cameras dropped off USB enumeration
during packaging (a force-killed frozen `app.exe` left the transport layer
wedged -- needs a replug) before the `flip_vertical` result could be seen
through the BIO. The transform math is unit-tested
(`test_camera.test_apply_orientation_composition_matches_the_group`) and
the model→orientation mapping is confirmed against the real model string;
the actual through-the-instrument image still needs a look.

**Tests:** `test_camera.py` / `test_device_presets.py` reworked for the
four orientations; `test_config.py`'s rotation cases became orientation
cases.

---

## 2026-09-01 — UVC camera reconnects itself when it drops mid-stream

**Decided:** `UvcCamera._grab()` counts consecutive failed `cv2.VideoCapture.read()`
calls; after `_RECONNECT_AFTER_FAILURES` (~0.5s at 30fps) it calls
`_try_reconnect()`, which releases the capture and reopens the device
(re-resolving the `vid_pid` index if that's the identification mode),
rate-limited to one attempt per `_RECONNECT_COOLDOWN_S`. The reopen uses
`_configure_capture(warmup=False)` -- no autofocus/exposure re-warmup,
since the scene is unchanged and the capture thread can't stall 2s there.
A failed read below the threshold, and a cooldown wait, each `time.sleep`
briefly so the capture thread doesn't spin a core while the device is down.

**Why:** reported on real hardware -- an integrated webcam used as the
third-person camera "randomly cut out" and stayed dead. `cv2.VideoCapture`
does not recover on its own from a USB power-management suspend, another
process grabbing the camera, or a brief unplug: `read()` just returns
`(False, None)` forever. Before this, that left a permanently black
third-person pane with nothing trying to fix it -- exactly the
"a replugged camera shows black and needs re-selecting" failure the
2026-08-11 "purpose-built app rather than OBS" entry says this app exists
to beat. (The 2026-08-17 "Third-person UVC camera" entry claimed a
disconnect was "caught by `_grab()` raising" -- it never was; `read()`
returns a falsy tuple, it doesn't raise. This is that gap actually
closed.)

**Interaction with `kiosk.py`'s stall detection (unchanged):** a drop that
self-heals within `DEFAULT_STALL_TIMEOUT_S` (2.0s) during a recording
leaves `poll_recording()` untouched -- `get_latest().index` resumes
advancing, the recording keeps going with a short gap in the third-person
view. A drop that outlasts the reconnect still trips the stall timeout and
fails the session loudly, which is the right call for a camera that's
genuinely gone. `Frame.index` (self-counted) is deliberately *not* reset on
reconnect, so it stays monotonic and `recorder.py` doesn't miscount the
pause as a burst of drops.

**Scope:** `UvcCamera` only. `IdsCamera` has its own acquisition model
(`WaitForFinishedBuffer` timeouts, the `ids_peak.Library` lifecycle) and no
reported reconnect problem; not touched.

**Tests:** `test_uvc_camera.py` gained `UvcCameraReconnectTest` (5 cases:
sub-threshold failures don't reopen, sustained failure reopens exactly once
within the cooldown, a successful reopen restores frames and resets the
counter, a failed reopen is swallowed and retried, reconnect bails when
stopping) plus a `warmup=False` case.

---

## 2026-09-01 — Delete composite.mkv after the MP4 is verified (not "keep both")

**Supersedes** the "keeps both `composite.mkv` and `composite.mp4` after
`stop()`" behavior noted in the 2026-08-11 disk-preflight entry.

**Decided:** `Recorder.stop()` now deletes `composite.mkv` once it has
remuxed `composite.mp4` **and** verified that MP4 decodes. Verification
(`_mp4_verifies()`): decode the first ~15 frames (must yield >0 -- catches
the dropped-leading-keyframe failure from the "PyAV remux filters on empty
packets" entry: valid headers, zero decodable frames) and demux-count the
video packets (must be within 2 of what was encoded -- catches gross
truncation). Both checks are cheap; no full decode pass a waiting student
would feel. If verification fails, **both files are kept** and
`session.json` records `mp4_verified: false` plus `composite.mkv` in
`output_files`.

**Why:** the MKV exists only as the interruption-safe copy *during*
capture (an interrupted MKV is still playable, an interrupted MP4 is
lost). The remux is a stream copy, so after a clean stop the MP4 holds
byte-identical video and the MKV is pure redundancy -- and it's not small:
keeping it doubled every session's footprint on disk, on a kiosk that
records ~500 MB/session unattended all day. The 2026-08-11 "Composite
live" entry already classes the raw/interim files as "debugging artifact...
nothing depends on them," so this isn't reversing a principle, just
stopping paying 2x storage for a file whose job is done.

**Never deletes both:** the irreplaceable-data rule (CLAUDE.md) means a
failed verification must leave *something* playable. The MKV is the
fallback, and the failure is loud (logged ERROR, surfaced in
`session.json`).

**Disk preflight (`kiosk.py`) unchanged at 2x:** during finalization both
files still briefly coexist at full size (remux writes the MP4 before the
MKV is deleted), so the transient *peak* one session needs is still ~2x
the estimate, even though a completed session now settles to 1x. Comment
updated; `REQUIRED_SPACE_MULTIPLIER` not touched.

**Tests:** `test_recorder.py`'s happy-path test now asserts the MKV is
gone, `mp4_verified` is true, and `output_files` has no `mkv`; a new test
forces `_mp4_verifies()` false and asserts both files survive and
`session.json` says so.

---

## 2026-09-01 — Automatic cleanup of old recordings (opt-in, age sweep + low-disk pass)

**Decided:** New `retention.py`, run once by `app.py` at startup (never on
the recording timer, never against an in-progress session). Driven entirely
by an opt-in `retention` section in `config.json` -- absent means no
cleanup at all, which stays the default. Configured via `settings.py`'s
"Automatically delete old recordings" group (a checkable `QGroupBox`,
unchecked by default).

Two passes:
1. **Age sweep** (`max_age_days`, always runs): delete every *completed*
   session older than N days, regardless of free space.
2. **Capacity pass** (`min_free_gb` + `protect_days`, set together or not
   at all; only runs when free space is actually below `min_free_gb`):
   delete the oldest completed sessions -- oldest first, but never one
   younger than `protect_days` -- until free space recovers. If it can't
   recover without crossing `protect_days`, it stops and
   `RetentionResult.capacity_target_met` is False; `app.py` logs a warning
   and `kiosk.py`'s existing disk preflight takes over (Start disabled,
   loud status). Deleting this week's recordings to keep a kiosk running is
   a human's call, not this module's.

**Never deleted, either pass:** a folder that isn't a well-formed
`YYYY-MM-DD_HHMM[_N]` session dir (something a technician put there); a
session with no `session.json` (in progress, or a *failed* session a
technician should review -- deliberately not auto-cleaned even though
that's the junk you'd most want gone); the single newest completed
session, whatever its age.

**Why opt-in, not a conservative default-on:** recordings are the
irreplaceable deliverable (CLAUDE.md), the retrieval step is manual, and
there's no "retrieved" marker in a session folder. A full disk today is
already a *safe* failure (preflight disables Start, nothing corrupts), so
this trades an occasional "tech has to clear space" for a standing risk of
auto-deleting an un-retrieved recording. That trade is the institution's
to opt into with a policy, not something shipped hot.

**Why "both" passes rather than one:** age-only removes recordings even
when there's plenty of room; capacity-only lets a burst of long sessions
blow past a sensible keep-window before the disk-pressure trigger fires.
Together: a predictable steady-state window, with a pressure valve that can
reach past it when genuinely necessary, floored by `protect_days`.

**Config validation (`config.py`):** `max_age_days` required positive int;
`min_free_gb`/`protect_days` both-or-neither (like `red/blue_balance_ratio`);
`protect_days <= max_age_days`. `settings.py` always writes all three when
the group is checked (an age-only policy needs a hand-edit) -- keeps both
the UI and the validation simple.

**Failure isolation:** `app.py` wraps the whole pass in try/except and
logs -- a cleanup bug must never stop the kiosk starting. `retention.py`
itself swallows a per-session delete/size error and moves on.

**Tests:** new `test_retention.py` (10 cases: age sweep incl.
newest-session and incomplete-session protection, non-session folders
ignored, minute-collision suffixes recognised, capacity pass oldest-first /
protect-days floor / target-not-met / no-op when space is fine / skipped
when unconfigured, missing dir no-op). `test_config.py` +8, `test_settings.py`
+4. Full suite 218.

---

## 2026-09-02 - Recorder/Viewer split, phase 1: two VFR streams instead of a live composite

**Decided:** `recorder.py` no longer composites. A session writes
`instrument.mp4` + `third_person.mp4`, each at its camera's native
resolution and true variable frame rate, plus a `session.json` with
`format_version: 2`. `compositor.py` moves from capture time to watch
time (the Viewer, phase 2). Design and the other three phases are in
ROADMAP.md's "Recorder/Viewer split: design" entry; the four shaping
decisions (two raw streams only, Viewer both in-kiosk and standalone,
true per-camera timestamps, students/self-review) were made in
conversation on 2026-09-02.

**This supersedes the 2026-08-11 "Composite live, not in post-production"
entry.** That decision's guarantees are kept, not dropped:

- *Synchronized by construction* -> every frame's PTS is
  `Frame.timestamp - clock.origin_monotonic` on a 1/1000 time base, so
  equal PTS in two files means the same instant. Stronger than before: it
  survives a slow camera instead of duplicating its frames into a
  fixed-rate composite. Measured on mismatched synthetic cameras (11fps
  instrument, 30fps third-person, 5s): 56 vs 145 frames spanning the same
  0.02-5.03s, cross-stream lag at any point 10-49ms, bounded by the slow
  camera's own frame period. The old model would have stored 145
  composite frames, duplicating the instrument frame ~2.6x.
- *Student watches immediately, no render wait* -> the Viewer's Watch
  button opens the session as-is. Nothing is rendered unless the student
  asks for a single-file Export.
- *No post step that can fail* -> watching needs none; the only post step
  is the MKV->MP4 remux+verify that already existed, now run per stream.

**Why two files rather than keeping the composite too:** three encoders
would have cost noticeably more CPU and ~2x the disk for a file that the
Viewer can produce on demand, and the composite is lossy about what was
captured (it bakes in a layout and duplicates the slower camera).

**`_StreamWriter` per camera, each on its own thread:** a slow encode on
one stream can't starve the other camera's bounded queue. Each drains
with `read()` (not `get_latest()`) so gaps in `Frame.index` are still
real dropped-frame counts, per CLAUDE.md's Architecture section. Writers
drain their camera's queue at `start()` so a frame captured before the
session origin isn't clamped to pts 0.

**Recorder-side fps ceiling, counted separately:** a writer skips a frame
that arrives less than `0.9/fps` after the last one it encoded, counting
it in `rate_limited_frames` (distinct from `dropped_frames`). The
camera-side caps (`IdsCamera._apply_frame_rate_cap`, `UvcCamera`'s
`CAP_PROP_FPS`) are best-effort and a device may ignore them; this is the
guarantee. The 0.9 slack matters: a camera pacing itself at exactly the
recording rate jitters a millisecond either way, and a strict `1/fps`
threshold rejected a random ~half of its frames.

**GOP set to `recording.fps` frames:** libx264's default (~250) would
make a Viewer scrub decode up to ~8s forward from the previous keyframe.
At `fps` frames it's <=1s of media for a full-rate stream and always <=30
frames of decode-forward, for a few percent of file size.

**Both time bases must be pinned to 1/1000** -- `stream.time_base` *and*
`stream.codec_context.time_base`. Found in a feasibility run against the
pinned PyAV 18.0.0 before writing any of this: `add_stream(..., rate=fps)`
leaves the encoder at 1/fps, so ms PTS get rescaled into 1/fps ticks, two
jittered ~33ms-apart frames collapse into one tick, DTS goes
non-monotonic, and the MP4 muxer rejects the remux with EINVAL. The MKV
muxer tolerates it, so this surfaces only at the remux step, and only on
the faster stream -- exactly the kind of thing that would have looked
like a random late-stage bug. Also confirmed there: exact PTS round-trip
through MKV->MP4, and seek landing at or before the target within one GOP.

**No compatibility with the old `composite.mp4` layout.** Decided
2026-09-02: nothing in that format is in circulation, so there's no
migration path to maintain and the Viewer will read `format_version: 2`
only.

**Also changed:** `Recorder`'s `width`/`height` params are gone (no
canvas). `KioskController` gained `instrument_labels`/`third_person_label`
so labels land in the manifest, and its `width`/`height` now only feed the
disk-space estimate -- which is unchanged, since sum-of-widths x
max-height is still the right proxy for total pixels/sec whether the two
streams are stacked or separate. `app.py`'s post-session summary reports
per-stream counts and flags an unverified stream.

**Tests:** `test_recorder.py` rewritten around two decoded outputs (5
cases); `test_kiosk.py`'s two session assertions moved to the v2 shape.
Full suite 219.

---

## 2026-09-02 - Recorder/Viewer split, phase 2: playback engine and Viewer

**Decided:** `session_reader.py` (no Qt) reads a session back;
`viewer.py`'s `ViewerDialog` is a thin PySide6 shell over it, opened
modally from a new Watch button in the kiosk and runnable standalone.
Phases 1 and 2 land together, so the app is never in a state where a
session has no side-by-side view. Phases 3 (Export, Past recordings) and
4 (`viewer.exe` packaging) remain.

**Alignment is by timestamp, and only by timestamp.** Each stream keeps a
decode cursor; presenting media time `t` means "advance each cursor to the
last frame at or before `t`". Because the recorder wrote PTS as
`grab time - clock origin`, that yields frames genuinely captured at the
same instant, with no frame pairing, sidecar or offset search. Confirmed
visually on an 11fps-instrument/30fps-third-person recording: at t=2.0s
the panes show instrument frame 21 (t=1.910s) beside third-person frame 59
(t=1.967s) -- 57ms apart, bounded by the slow camera's own ~91ms frame
period, and the frame *numbers* are unrelated, as they should be.

**Decoding and presenting are separate.** Advancing past several frames
still decodes each one (H.264 P-frames leave no choice) but only the frame
actually shown pays for `to_ndarray()`. So a UI that falls behind skips
presentation, not decoding, and media time never drifts from wall-clock.
`ViewerDialog._tick()` computes `t` from wall-clock elapsed since Play
rather than accumulating per-tick increments, for the same reason.

**A stream shows its first frame for any `t` before it, rather than
black.** Found by a test failing at t=0: the recorder discards frames
captured before the session origin, so stream 0 is a few ms in, and the
two cameras don't deliver their first frame at the same instant. Black
would misrepresent a camera that was running the whole time.

**`any(cursor.advance_to(t) for ...)` was a real bug**, caught by the same
test run: `any()` short-circuits, so every stream after the first one that
moved never advanced -- one pane playing, the other frozen. Now a list
comprehension. Worth recording because the generator form reads as
obviously correct.

**`ViewerDialog` is a `QDialog`, not a `QMainWindow`**, so `app.py` can
`.exec()` it modally (which is what disables the kiosk's own controls
while it's up) and `main()` can still show it standalone. Teardown hangs
off the `finished` signal, not `closeEvent` -- Esc routes through
`QDialog.reject()`, which delivers no `QCloseEvent`, and this dialog holds
open PyAV decoders. Exactly the leak the "settings.py Preview leaked the
IDS device" entry above describes, applied preemptively this time, with a
test driving `reject()` specifically.

**Watch pauses the live preview but not the cameras.** Restarting an
instrument camera costs seconds and re-enters the device-busy class of
failures; there's no comparable cost to skipping preview compositing
behind a modal window. `app.py` restarts the preview timer in a `finally`,
so a viewer that raises can't leave the kiosk with a dead preview.

**Also added:** `compositor.fit_into_canvas()` -- the single-image
counterpart to `side_by_side`/`picture_in_picture`, so the viewer's
"instrument only" / "third-person only" modes get the same
aspect-preserving letterbox rather than letting Qt stretch a raw frame.

**Tests:** new `test_session_reader.py` (12) and `test_viewer.py` (12),
both against real recorded sessions rather than mocked PyAV -- what's
worth testing is whether alignment actually holds across two
independently-encoded files, which a mock can't tell us. `test_app.py`
gained 5 Watch-button cases. Full suite 248.

---

## 2026-09-02 - Recorder/Viewer split, phase 3: Export and the recordings picker

**Decided:** `session_export.py` renders a session's streams into one
constant-frame-rate MP4 in the chosen layout, on demand; `viewer.py` gains
an Export button (worker thread, cancellable progress dialog) and a
`SessionPickerDialog`; `app.py` gains a Past recordings button. This is
the last of the app-side work -- only packaging (phase 4) remains.

**Export is constant-frame-rate, unlike the streams it reads.** The
session files are VFR to preserve each camera's true cadence. An export is
one file for sharing or an LMS, where broad player compatibility matters
more, so it samples media time at a fixed `fps`. Both facts can be true at
once precisely because the session keeps the real timing and the export is
derived.

**`compose_layout()` lives in `compositor.py`, used by both the viewer and
the exporter.** Layout selection was about to exist twice -- once for the
screen, once for the file -- which is exactly how "the export doesn't
match what I was looking at" bugs happen. One function, two callers.

**Export writes `<name>.partial.mp4` and moves it into place.** A
cancelled or crashed export must not leave something that looks like a
finished file; the rename is the commit point. The partial keeps the real
suffix rather than being `<name>.mp4.partial` because PyAV infers the
container format from the extension -- found immediately, as 10 failing
tests.

**Export refuses to write over one of the session's own stream files**
(`ValueError`), since the save dialog defaults into the session folder and
`instrument.mp4` is one keystroke away from a plausible export name. The
recording is irreplaceable; the export is not.

**"No outcome" is reported as unfinished, not as success.** If the export
thread outlives the join timeout, the viewer says so and names the file to
check, rather than claiming either result. A student being told "saved"
when nothing was is worse than being told to look.

**`duration_s` added to the manifest** (per stream and overall). The
picker shows each recording's length, and doing that by opening two video
files per row would make a term's worth of sessions feel slow.
`SessionPlayer` still derives duration from the containers themselves,
which stays authoritative for playback.

**The picker has an explicit "Open a recording folder..." browse**, and
takes being pointed straight at a single session folder as a pick rather
than an empty list. This is a phase-4 prerequisite surfacing early: the
standalone viewer runs on machines with no `config.json`, where recordings
have been copied to a USB stick or Downloads (see ROADMAP.md's "Phase 4:
two installers").

**Also:** `session_format.py` now holds the three shared format constants,
so `session_reader.py`/`session_export.py` no longer import the *writer*
for its vocabulary -- which read oddly and dragged the encoder path into a
viewer-only build. `app.py`'s two viewer buttons share
`_with_preview_paused()`, which restarts the preview in a `finally`.

**Verified end to end** on two mismatched-rate recordings: the picker
lists both newest-first with labels and durations straight from the
manifests, and a side-by-side export of the 5s session produced a real
2240x1200, 151-frame MP4 with the session folder untouched.

**Tests:** new `test_session_export.py` (12); `test_viewer.py` +11
(export wiring, picker); `test_app.py` +2 (Past recordings). Full suite
273.

---

## 2026-09-02 - Recorder/Viewer split, phase 4: two installers

**Decided:** the build produces `reflex-setup.exe` (clinic: app +
settings + bundled IDS peak, admin, Program Files) and
`reflex-viewer-setup.exe` (review: `viewer.exe` only, no IDS peak,
per-user under `%LOCALAPPDATA%`, no admin). New `packaging/viewer.spec`
and `packaging/reflex-viewer.iss`; `PACKAGING.md` gains a "Two
installers" table and a step 6. Completes the Recorder/Viewer split.

**Why a second installer rather than "just run the full one":** the
machine that reviews a recording usually isn't the machine that made it.
A student reviewing at home has no cameras, often no admin rights, and no
reason to install 356MB of machine-vision SDK and kernel drivers.
Measured result: **92 MB vs 513 MB**. That difference is whether
reviewing happens at all.

**The clinic installer needed no change.** `app.py` imports `viewer.py`,
so `app.exe` already contains the viewer and Watch / Past recordings work
from inside the kiosk. Shipping a separate `viewer.exe` there would have
added a second full PySide6+OpenCV+PyAV tree -- hundreds of MB -- for a
capability that machine already has. Its `[Files]`/`[Icons]` are
untouched.

**`viewer.spec`'s `excludes` is an assertion, not a size optimisation.**
Naming `ids_peak`/`ids_peak_ipl`/`pygrabber`/`comtypes` means a future
edit that makes the viewer reach something camera-facing fails the build
loudly, rather than silently gaining a dependency the review machine
cannot satisfy. Verified on this build: the frozen `viewer.exe` contains
none of `recorder`, `camera`, `kiosk`, `ids_camera` or `uvc_camera` --
cleaner than planned, because phase 3's `session_format.py` refactor had
already removed the last reader-imports-writer edge.

**Per-user install, `PrivilegesRequired=lowest`:** nothing in the viewer
writes outside the user's profile and there are no drivers, so requiring
admin would be friction with no purpose -- and students frequently don't
have it on their own machines. It gets a Desktop shortcut, unlike
`settings.exe`: students *are* this program's audience.

**Distinct `AppId` on the viewer installer only.** `reflex.iss`
deliberately leaves `AppId` implicit (Inno derives it from `AppName`);
adding one now would stop existing clinic installs being recognised as
upgradable. The viewer installer sets `AppId=reflex-viewer`
explicitly so the separation is deliberate and documented rather than an
accident of differing `AppName`s.

**Verified on the build machine:** `viewer.exe <session folder>` plays
it; `viewer.exe` with no arguments logs "no usable config.json ... using
the default recordings folder" and shows the picker rather than erroring
-- the correct review-machine path, since there is legitimately no
config there. **Not yet verified:** installing
`reflex-viewer-setup.exe` on a machine that has never had Reflex
on it (PACKAGING.md step 6's checklist).

---

## 2026-09-03 - Frozen-picture detection: a camera can deliver frames without seeing anything

**Decided:** `KioskController` compares consecutive frames' pixels per
camera (`_frame_signature()` + `_update_freshness()`). A camera whose
picture is byte-identical for `DEFAULT_FREEZE_TIMEOUT_S` (5s) is reported
in `PreflightStatus.frozen_cameras`, which blocks Start, and fails a
recording in progress with its own message.

**Why:** found while diagnosing a report that an integrated webcam
"disconnects after a second". Every existing check said the camera was
perfectly healthy -- reads succeeded, 30fps steady, zero reconnects,
standalone and inside a real `KioskWindow`. Comparing consecutive frames
showed 39 of 39 pairs pixel-identical: the camera was blocked at the
OS/driver level and the stream was re-delivering one still image.

Nothing in the app could see that. `cap.read()` returns True, and
`UvcCamera` self-counts `Frame.index` (UVC exposes no source counter --
see the "Third-person UVC camera" entry), so the counter climbs even
when the pixels are dead and `poll_recording()`'s stall check never
fires. A student could have recorded a full session of frozen video and
been told "0 dropped". That is exactly the outcome CLAUDE.md calls the
worst: a black pane discovered a week later.

**Exact equality, not a difference threshold.** A real sensor emits noise
on every frame, so two byte-identical frames mean the pixels are not
coming from a sensor at all. A threshold would have to guess how dark and
how static a legitimate scene may be -- and the scene this app points at
*is* dark and static (a slit lamp beam on a dark field). Equality needs no
such guess. `_frame_signature()` therefore uses strided indexing rather
than any resampling, which would smooth away the one-bit differences that
prove liveness.

**Only ever judges a frame it has not already judged.** The first
implementation compared whatever `get_latest()` returned each poll, and
the tests immediately caught it flagging a perfectly healthy camera: at a
4Hz poll against a slower camera you re-examine the *same* frame and
conclude the picture is frozen. Tracking the last `Frame.index` assessed
fixes it and gives the right decomposition -- index not advancing is the
stall check's business; index advancing while pixels do not is this one's.
That matters concretely: the slit lamp runs at ~11fps.

**Freeze timeout (5s) is deliberately longer than the stall timeout
(2s).** A stalled camera has stopped delivering and is unambiguous; this
check asks a subtler question, so it trades a little lateness for a much
smaller chance of accusing a live camera. ~20 consecutive byte-identical
samples from a real sensor is essentially impossible.

**`frozen_cameras` is separate from `cameras_ready`** so the UI can say
which problem it is. "Waiting for cameras" would send a student to check
a cable; the camera is running. `app.py` says the picture is frozen and
names what to check (covered, switched off, blocked).

**Also fixed here:** the stall and freeze messages now use the
technician-set labels ("BI900", "third-person camera") rather than the
internal role keys, which is what a student actually reads in the error
banner.

**Verified against real hardware, one direction.** The false-positive
direction -- the one that matters more, since wrongly refusing to record
is its own failure -- was confirmed on the real webcam: live at 30fps for
7s, never flagged. The machine's camera recovered before the true
positive could be reproduced on it, so that direction rests on the tests
(`_FreezablePictureCamera` advances `Frame.index` while returning the same
image, which is precisely the observed real behaviour).

**Tests:** `test_kiosk.py` +7 -- `_frame_signature` (equality, a changed
sampled pixel, that it copies rather than views), a live camera never
flagged, a frozen camera blocking Start while `cameras_ready` stays True,
a camera freezing mid-recording stopping it loudly with a real partial
session on disk, and deselecting an instrument dropping its history. Full
suite 280.

---

## 2026-09-03 - Camera configuration: which layer owns what

**Decided:** Recorded the ownership model for camera configuration as a
standing section in CLAUDE.md ("Camera configuration: who decides what"),
because a wrong assumption about it silently produced a bad calibration
that sat in `config.json` for weeks.

**The assumption worth killing:** that Keeler and Haag-Streit configured
these sensors better than we could, so the sensor layer is already
handled. Neither is true. Both instrument cameras are generic **IDS**
machine-vision cameras that the instrument makers merely mount, and
Reflex talks to IDS peak directly rather than through Keeler's
Kinexis -- so there is no instrument-maker configuration to inherit. And
IDS's own defaults were measured unusable here: the 2026-08-12 hardware
smoke test found `ExposureTime ~15ms / Gain 1.0` gave a near-black frame
pointed straight at a lamp (raw Bayer max 3-4 of 255).

**The evidence that our calibration strategy, not the hardware, is the
outlier** -- three settings for comparable scenes:

| | exposure | gain |
|---|---|---|
| IDS factory default | 15 ms | 1.0 (near-black) |
| IDS peak Cockpit's own auto-exposure | 47.5 ms | 25.4 |
| `exposure_calibration.next_exposure_gain()` on the slit lamp | 87.2 ms | 1.0 |

The vendor's own implementation reaches for gain readily and keeps
exposure moderate. Ours is documented as doing the opposite ("only spills
the remainder onto Gain once ExposureTime is clamped"), which is correct
for stills and wrong for video: exposure time is a frame-rate budget.
87.2ms caps the slit lamp at ~11.5fps against a 30fps target and puts
87ms of motion blur on the view of a moving eye. That is where the "slit
lamp runs at ~11fps" figure in this project's notes actually comes from
-- it was read as a hardware property, and it is not.

**What the instrument makers do own: the optics.** Light path, image
orientation (the BIO's flip, handled in `device_presets.py`), and how
much light reaches the sensor at all. None of it is configurable; it is
compensated for, or improved at the instrument. This is why "turn up the
slit lamp's own illuminator" is the real fix rather than any software
setting -- more light buys short exposure *and* low gain together.

**Why `config.json` rather than the camera's own memory.** GenICam
devices persist `ExposureTime`/`Gain` across power cycles, so "set it
once in Cockpit" appears to work. It is invisible, unversioned state with
no provenance that walks off with a swapped or reset camera -- and the
2026-08-11 slit-lamp entry already flagged the hazard: a stale value
"would silently carry into the next kiosk session with no code-level
check catching it." Config values have provenance and are reapplied in
`_open()`. Same reasoning as serial-number identification over device
index.

**Why no "Advanced" tab in settings.py.** Tiering by user skill is the
wrong cut for a project with three humans separated by role rather than
ability. The cut that pays is *where the knowledge lives*, and the useful
move is pushing decisions up that table -- every value the app derives is
one nobody can set wrong. An advanced panel is usually a sign a preset
has not been decided yet, and it is the first place a confused technician
clicks. The 87ms case is the proof: it was not a missing setting (the
setting existed and was set), so no amount of extra configurability would
have prevented it. What was missing was any report of what the
calibration *cost*.

**Rejected:** deferring sensor configuration to IDS peak Cockpit
entirely, the common pattern for this class of app. The 2026-08-18
in-app-calibration entry already settled that for the routine case. This
entry only narrows it: Cockpit stays the right tool for genuine
sensor-level work (black level, pixel format), and once such a value is
known it belongs in `device_presets.py`, not in a dialog.

**Not yet acted on:** bounding exposure by `1/recording.fps` in
`next_exposure_gain()`, warning when a configured exposure cannot meet
the target frame rate, and reporting achieved fps/gain after a
calibration. See the CLAUDE.md section's rules.

---

## 2026-09-03 - Exposure is bounded by the frame-rate budget, and calibration reports what it cost

**Decided:** Acted on the three items the "Camera configuration: which
layer owns what" entry left open.

1. `exposure_calibration.exposure_budget_us(target_fps)` gives the longest
   exposure that still leaves room for a frame rate (the interval times
   `DEFAULT_EXPOSURE_BUDGET_FRACTION`, 0.9, for readout headroom).
   `next_exposure_gain()` takes an optional `max_exposure_us` that
   tightens its clamp to that budget instead of the sensor's own maximum;
   `IdsCamera.auto_calibrate(target_fps=...)` supplies it.
2. `config.exposure_fps_warnings()` reports any instrument whose
   configured exposure cannot meet `recording.fps`, logged at
   `load_config()`.
3. `settings.py`'s Preview reports what a calibration achieved --
   "exposure 30.0ms | gain 2.6x of 4.0 max | allows ~33fps" -- and calls
   it out explicitly when that falls below the recording target.

**Why the clamp needed no restructuring:** the existing algorithm already
spills onto Gain "once ExposureTime is clamped at its range limit". Only
the limit was wrong -- the sensor's maximum rather than the frame budget.
Lowering it makes the existing spill do the right thing, so this is a
one-line behavioural change plus its plumbing, not a new search strategy.

**The budget never asks for less than the sensor can do.** It is clamped
to at least `exposure_range_us[0]`. A frame-rate target this camera cannot
physically meet is a reason to warn, not a reason to request an impossible
exposure and get nothing.

**Warning, not ConfigError.** A slow camera still records usable video;
refusing to start would be a worse failure than the one being reported,
and would brick the current install (`config.json` carries the 87.2ms
value today). Loud and visible, not fatal.

**Reporting is the half that actually prevents a repeat.** The 87ms case
was not a missing setting -- the setting existed and was set. What was
missing was any statement of its cost. "87208.816" is unjudgeable;
"11fps, gain 1.0 of 4.0 max" is obviously wrong to anyone. That is why
the fix is a status line rather than another slider, per CLAUDE.md's
"Camera configuration: who decides what".

**`auto_calibrate()` falls back to the camera's own `target_fps`** when
the caller names none, so `app.py`'s runtime path is bounded too -- but
`settings.py`'s Preview cameras deliberately never receive `target_fps`
(they must not cap acquisition), which is exactly why calibration takes
it as its own argument rather than reading the camera's.

**Verified by calculation, not hardware** -- no IDS camera is attached.
`exposure_fps_warnings()` fires correctly against the real `config.json`
("BI900: exposure 87.2ms limits this camera to about 11fps, below the
30fps recording target"). The regression test runs the real convergence
loop from a near-black frame and confirms it now lands inside a 30fps
budget by using gain rather than spending the frame interval. What the
slit lamp actually settles on has to be re-measured in the room.

**Tests:** `test_exposure_calibration.py` +7 (budget arithmetic, the
clamp-and-spill, the sensor-minimum floor, a non-binding budget being a
no-op, and the slit-lamp convergence regression alongside the unbounded
behaviour it replaces); `test_config.py` +6; `test_settings.py` +3 for
the cost line. Full suite 296.

---

## 2026-09-08 - Highlight metering, total-light redistribution, and what they uncovered

First calibration work done against the real instruments (slit lamp with a
black focus rod and the beam on; BIO looking at a lit object).

**Metering the highlight, not the median.** `auto_calibrate()` steered on
the centre-crop median toward 128. On the slit lamp that signal measured
**0.0** -- the frame is ~98% black by design, a thin beam on a dark field,
so no exposure can move its median. The search therefore pinned exposure
at its ceiling and gave up. `metering_brightness(..., METERING_HIGHLIGHT)`
drives p99.9 to 210 instead, which asks the question that matters: is the
beam bright but not clipped? `IdsCamera.auto_calibrate()` defaults to it,
since every IDS camera in this app is an instrument camera looking through
eyepiece optics.

Percentile and target were chosen from a measured sweep, not guessed:
p99.9 tracked the beam's core across the usable range while clipping
stayed under 0.02% up to ~35ms, and 210 leaves headroom under 255 so the
beam keeps its internal structure.

**Correction steps now solve in total light.** The per-axis version only
reduced gain once exposure had bottomed out, so *cutting* brightness left
gain and its noise untouched -- measured on the real BIO, it walked
exposure down from 49.9ms to 7.7ms while pushing gain *up* from 15.5x to
21.3x, and never converged. `next_exposure_gain()` now computes the
desired exposure x gain, then redistributes with one preference: as much
exposure as the frame budget allows, as little gain as will do. The
outcome no longer depends on the starting split, which is what lets a
camera recover from a bad state instead of inheriting it.

**A clipped measurement is censored,** so a proportional step crawls: it
says we are over, not by how much. At or above `DEFAULT_SATURATION_LEVEL`
the step halves instead. Measured: eight proportional steps from the BIO's
6.6%-clipped frame had not converged; with halving both cameras converge
in under two seconds.

Result on real hardware, both from bad starting points:

| | before | after |
|---|---|---|
| slit lamp | 87.21ms, gain 1.00, 1.54% clipped | 30.00ms, gain 1.09, 0.006% clipped, p99.9 206 |
| BIO | 36.79ms, gain 21.30, 6.73% clipped | 30.00ms, gain 1.31, 0.05% clipped, p99.9 210 |

**Two ordering fixes in `_open()`**, both hardware-motivated: the
frame-rate cap is applied after exposure and gain are settled rather than
before (its limit depends on them), and config's exposure/gain are applied
before `TLParamsLocked` rather than after. Both cameras verified to open,
stream and record with the new order.

**Also answered by hardware:** `_apply_frame_rate_cap()` carried a note
that it was unverified whether `AcquisitionFrameRate` could be set after
`AcquisitionStart`. It can, on both cameras; `AcquisitionFrameRateEnable`
is absent on both.

### What this uncovered, and did NOT fix

The slit lamp still delivers ~11.5fps. The cause is not exposure at all:
`DeviceClockFrequency` (the uEye pixel clock) **defaults to 24MHz out of a
128MHz range, and resets to 24MHz on every open**. At 24MHz a 1600x1200
frame takes ~87ms, which is simultaneously the 11.46fps ceiling *and* the
reason `ExposureTime`'s maximum reads 87208.8us. So this project's
long-standing "the slit lamp runs at ~11fps" and "87.2ms is the sensor
maximum" are the same artifact of an unset pixel clock -- neither is a
sensor limit, and the calibration was only ever filling the frame period
it was handed.

Measured, single 3s trials, counting real device-side `Frame.index` gaps:

| clock | max exposure | delivered | drops |
|---|---|---|---|
| 24MHz (default) | 87.21ms | 12.4fps | 0 |
| 60MHz | 34.99ms | 29.4fps | 0 |
| 80MHz | 26.31ms | 38.7fps | 0 |
| 100MHz | 21.10ms | 48.1fps | 3 |
| 128MHz | 16.56ms | 61.3fps | 0 |

Raising the clock shortens the maximum exposure in exact proportion, so it
trades available light for frame rate -- it is not free, and the safe
ceiling depends on the host USB controller, which is install-specific.
Deliberately not acted on yet: the value belongs in `device_presets.py`
(per-model) or `config.json` (per-install) per CLAUDE.md's camera-
configuration table, and the drop testing behind it is three seconds per
point, which is not enough to bet a clinic on.

---

## 2026-09-08 - The slit lamp's "11fps sensor limit" was an unset pixel clock

**Decided:** `device_presets.pixel_clock_hz_for_model()` sets the legacy
uEye slit lamp camera to **80MHz**, applied by `IdsCamera._apply_pixel_clock()`
early in `_open()` -- before anything reads or writes `ExposureTime`,
because the clock determines that node's range. `config.json`'s
`instruments.<role>.pixel_clock_hz` overrides it.

**What was actually wrong.** `DeviceClockFrequency` on that camera powers
up at **24MHz of a 10-128MHz range, and resets on every open** -- unlike
ExposureTime/Gain, it does not persist. At 24MHz a 1600x1200 frame takes
~87ms, which is simultaneously an 11.46fps ceiling *and* the reason
`ExposureTime`'s maximum reads 87208.8us. So this project's two
long-standing "facts" about that camera -- that it runs at ~11fps, and
that 87.2ms is its sensor maximum -- were the same single unset value.
Neither was a sensor limit. Every previous calibration was merely filling
whatever frame period it had been handed.

Measured end to end, real hardware, one 10s session per row:

| | instrument frames / 10s | delivered |
|---|---|---|
| before (24MHz) | 116 | 11.5fps |
| after (80MHz) | 302 | **30.0fps** |

**Why 80MHz and not 60.** Sustained 150s runs with the third-person camera
also streaming, counting device-side `Frame.index` gaps:

| clock | delivered | dropped |
|---|---|---|
| 60MHz | 28.57fps | 0 |
| 80MHz | **30.01fps** | 0 |

60MHz is clean but its ceiling is 28.56fps, so `_apply_frame_rate_cap()`
can never actually reach the 30fps recording target. 80MHz is the lowest
clock that delivers the full target. Higher was measured too (100MHz gave
48fps but showed drops in a short trial; 128MHz gave 61fps) -- all far
past what `recording.fps` asks for, and each step shortens the maximum
exposure in proportion, so the extra clock would be bought with gain.

**It is a trade, not a free win.** Frame period falls with clock, and
`ExposureTime`'s maximum falls with it: 87.21ms at 24MHz, 34.99ms at
60MHz, 26.31ms at 80MHz. Less available light per frame, made up with
gain. Re-calibrating at 80MHz landed at 26.31ms / gain 1.09 -- still the
minimum end of a 1.0-4.0 gain range, so the trade cost nothing here. On a
dimmer subject it would.

**Why this one is technician-overridable** when `orientation` is not: the
preset is a property of the camera, but the *safe* clock depends on the
host USB controller, which is per-install. That is the one condition
CLAUDE.md's configuration table says earns a knob. There is no
`settings.py` control for it yet -- same standing gap as `orientation`.

**Also fixed here:** `_open()` now clamps a configured exposure into the
node's live range before applying it, since the clock above may have moved
that range and a value written under a different clock would otherwise be
out of bounds.

**The lesson worth keeping** (now in CLAUDE.md's Conventions): a limit
that shows up in two places at once -- here a frame rate *and* an exposure
maximum, both explained by one number -- is worth one query to the device
before it becomes an assumption. This one survived weeks of being designed
around.

**Tests:** `test_device_presets.py` +4, `test_config.py` +4. Full suite 306.

---

## 2026-09-08 - The frame-rate budget applies to the vendor's auto-exposure too

**Decided:** `IdsCamera._apply_auto_exposure_limit()` sets
`BrightnessAutoExposureTimeMax` to `exposure_budget_us(target_fps)` and
turns `BrightnessAutoExposureTimeLimitMode` on, before
`_converge_auto_nodes()` runs.

**Why:** the budget was only ever enforced on `auto_calibrate()` -- the
software path, which exists for cameras that have *no* auto-exposure. A
camera that has one was free to ignore it, and did. The BIO's own
`ExposureAuto` settled on 49.92ms, capping `AcquisitionFrameRate` at 20.00
against a 30fps target. "Exposure time is a frame-rate budget" is true
whoever is choosing the exposure; enforcing it on only one of the two
paths was an oversight, not a design.

Measured on real hardware, 10s sessions through `KioskController`:

| | instrument frames / 10s | delivered |
|---|---|---|
| BIO before | 201 | 20.0fps |
| BIO after | 302 | **30.0fps** |

Node-level: `ExposureAuto` converges to 30.00ms instead of 49.92ms and the
`AcquisitionFrameRate` ceiling rises 20.00 -> 33.24. The light it can no
longer take from time it takes from gain, which is the intended trade and
the same one the pixel-clock entry describes.

**Ceiling before mode.** `BrightnessAutoExposureTimeMax` is set first and
the limit mode switched on second, so the camera never briefly enforces
whatever stale maximum it was holding (it powers up at 2 seconds).

**Best-effort, like every optional node in this file:** the slit lamp's
uEye transport exposes neither node and has no auto-exposure to bound --
verified still opening, streaming and recording at 30fps unaffected.

With this and the pixel clock, **both instrument cameras now deliver
exactly the configured 30fps** with zero dropped frames, from a starting
point of 11.5fps (slit lamp) and 20fps (BIO).

---

## 2026-09-08 - Slit lamp image is rotated 180 degrees

**Decided:** `device_presets.orientation_for_model()` now returns
`rotate_180` for the slit lamp camera (`UI325` token), alongside the BIO's
existing `flip_vertical`.

**Why:** reported from the instrument -- the image arrives mirrored on
*both* axes. Flipping both axes is a 180-degree rotation, which is why
this is one orientation rather than two, and why `VALID_ORIENTATIONS`
covers the whole Klein-four group rather than just the two axis flips.

Note the two instrument cameras need *different* corrections --
`flip_vertical` for the BIO, `rotate_180` for the slit lamp -- which is
the case for keying this per model rather than applying one blanket
"instrument optics" rule. A test now pins that the `UI325` and `U3-327`
substring tokens each match only their own camera.

**NOT visually verified.** The cameras dropped off USB enumeration before
the change could be checked against a live image, so this rests on the
report alone. Two reasons to re-check it against something with an
obvious up and down before trusting it: the earlier BIO correction went
`rotate_180` -> `flip_vertical` after exactly this kind of verbal report,
and the slit lamp's usual subject -- a vertical beam on a black field --
is close enough to symmetric that a wrong orientation would not be
obvious. If it is wrong, `config.json`'s `orientation` overrides the
preset without a code change.

**Verified 2026-09-09.** Checked on a second machine against a subject
with an obvious top and bottom, using the real BI 900 camera -- the image
is correctly oriented. The caveat above is kept for the record; it no
longer blocks trusting the preset. See the 2026-09-09 entry below.

---

## 2026-09-08 - Documentation caught up with the day's hardware work

Housekeeping entry, recorded because most of what changed was *removing*
caveats, and a removed caveat leaves no trace of why it went.

**`ids_camera.py` carried a blanket "none of this is hardware-verified"
paragraph** covering white balance, the frame-rate cap and
`_converge_auto_nodes()`, plus per-method notes on ExposureTime/Gain's
`Minimum()`/`Maximum()` and on `AcquisitionFrameRate`. Today's work
exercised all of those against both real cameras, so the notes were no
longer true -- and a stale "unverified" is worse than none, because it
invites re-doing work that has been done. Replaced with what is actually
verified, and what is not.

**One caveat was kept and sharpened rather than removed:** the manual
white-balance path (`auto_white_balance()`, BalanceRatioSelector/
BalanceRatio) is still unverified and *cannot* be verified on this
hardware -- `needs_manual_white_balance()` is False on both cameras, since
the Keeler has working `BalanceWhiteAuto` and the slit lamp exposes no
white-balance nodes at all. It needs a camera with no BalanceWhiteAuto but
with BalanceRatio, which neither of these is. That is a more useful note
than "unverified" alone, because it says what would be required.

**Two claims had become actively wrong**, not merely stale:

- `_apply_frame_rate_cap()`'s docstring said the slit lamp's long exposure
  was "expected to make this a no-op in practice." The opposite was true:
  at the default 24MHz pixel clock its ceiling was 11.46fps, so the clamp
  was the thing holding the camera down. Now that the clock is raised both
  cameras have headroom above 30fps and it genuinely caps rather than
  throttles.
- `SUPPORTED_HARDWARE.md` said the 30fps-simultaneous worst case was
  unmeasured and "blocked on the slit lamp's exposure/gain calibration."
  Both are now done. It also assumed both IDS cameras stream together,
  which stopped being true when `select_instrument()` made only one
  instrument camera run at a time -- so the real concurrent load is one
  instrument plus the webcam, roughly 94 MB/s at 30fps for the Keeler
  against a 350-400 MB/s controller. Replaced with the measured runs
  (zero device-side drops at 150s, 60s and repeated 10s sessions), and
  the standing caveat that the third-person camera in all of them was a
  laptop integrated webcam rather than the ELP that ships.

**Also added to CLAUDE.md's tables:** `exposure_calibration.py` and
`test_exposure_calibration.py`, which had never been listed despite
`exposure_calibration.py` now holding the metering modes, the frame-rate
budget and the total-light correction step; and `config.py`'s row gained
`exposure_fps_warnings()`.

**An editing lesson worth keeping:** one of these notes was supposed to
have been corrected in the pixel-clock commit and silently was not -- the
replacement did not match and the script had no assertion on that one
edit, so it reported success. Every replacement in this pass asserts its
target exists first. A patch script without an assert is a patch script
that lies.

---

## 2026-09-09 - Clinic installer and slit-lamp rotation verified on a second machine

Verification milestone, not a new decision -- recorded so the caveats it
closes leave a trail, same as the 2026-09-08 doc catch-up entry above.

**The full clinic installer was run end to end on a second machine.**
`reflex-setup.exe` installed, both the Desktop and Start-menu
shortcuts launched their `.exe`s, the bundled IDS peak SDK installed
silently, the Finished page showed the native restart choice, and the
installed `app.exe` recorded a real session with the slit lamp camera
attached. This exercises most of PACKAGING.md step 5's "before handing
this to anyone" checklist. Two sub-checks are still not explicitly done
and stay in that checklist: that re-running the installer on a machine
with a current-enough IDS peak skips reinstalling it
(`IdsPeakAlreadyInstalled`), and that a failed silent install shows the
explicit error dialog rather than continuing (the
rename-`vendor\ids-peak-response.iss` test). The viewer-only installer
(step 6) is still untested on a clean machine.

**The slit lamp's 180-degree rotation preset is now visually verified.**
The 2026-09-08 "Slit lamp image is rotated 180 degrees" entry shipped the
`rotate_180` preset on a verbal report alone and flagged that the usual
beam-on-black subject is too near-symmetric to confirm it by eye.
Checked on the second machine against a subject with an obvious top and
bottom, using the real BI 900 camera: correctly oriented. That entry's
"NOT visually verified" caveat is closed.

**One stale ROADMAP note corrected while here:** the 2026-08-20
frozen-exe entry still described relocating `config.json` to
`%ProgramData%` as "not yet implemented." It has been implemented since
that work -- `config.py`'s `resolve_default_config_path()` returns
`%ProgramData%\Reflex\config.json` under `is_frozen()`, and stays
CWD-relative in dev/test.

---

## 2026-09-09 - Harden the recording path against silent truncation

A pre-ship review found three ways the recording path could fail without
the loud-and-early signal CLAUDE.md requires. All three are now closed.

**A `_StreamWriter` thread that raised mid-recording died silently.**
`_StreamWriter._run()` had no guard around its encode loop -- unlike
`BaseCamera._run()`, which wraps its capture loop for exactly this reason.
A full disk or an encoder fault would kill the thread; `stop()` then found
it already exited (so "not stuck"), `finalize()` ran as if clean, and
`_mp4_verifies()` could even pass on the truncated file. The kiosk's
stall/freeze detection watches the *camera*, not the writer, so nothing
noticed. A 10-minute session could come back as a 3-minute file reported
as complete -- CLAUDE.md's worst outcome.

Now `_run()` catches, logs, and records `self._error`. `finalize()` sees
it, keeps the MKV, never claims the stream verified, and still attempts a
best-effort remux for convenience. `info()` carries an `error` string into
the manifest; `app._format_summary()` turns that into "recording ended
early: ... - .mkv kept". The two writers are independent threads, so one
failing leaves the other's stream intact and verified.

And it stops the session *immediately*, not just at Stop: `Recorder.failed_stream()`
exposes the first writer error and `kiosk.poll_recording()` checks it
alongside the stall and freeze checks, so a dead writer trips the same
loud ERROR path a stalled camera does rather than letting the other
stream run on frozen for minutes.

**A UVC reconnect at a different resolution would corrupt the recording.**
`UvcCamera._try_reconnect()` deliberately keeps the original
`self.resolution` after a mid-stream USB drop, but `cv2.VideoCapture` can
reopen a device at a different frame size. Post the recorder/viewer split
the recorder encodes `frame.image` directly at a fixed size (the compositor
no longer runs there), so a mismatched frame makes `stream.encode()` raise
-- feeding the silent-writer-death path above. The stale "the compositor
letterboxes a mismatched frame anyway" comment was true only before the
split.

Two layers now: `_try_reconnect()` asks for the original width/height back
via `CAP_PROP_FRAME_WIDTH/HEIGHT` after reopening, and `_StreamWriter._encode()`
resizes anything that still doesn't match the encoder (a one-time warning,
then a plain resize -- a rare already-degraded path where a possibly
aspect-distorted pane beats losing the rest of the recording).

**`KioskController.stop_recording()` had no handler for a failing finalize.**
`Recorder.stop()` can raise (a stuck writer thread; a full disk during the
MKV->MP4 remux or the `session.json` write). Neither `stop_recording()`
nor `app._on_stop_clicked()` caught it -- the exception hit the excepthook,
the controller stayed in `RECORDING` with `_recorder` still set, and Stop
stayed live. `_fail()` already handled this for mid-recording failures;
`stop_recording()` now mirrors it: log, land in `ERROR`, name the raw-file
location in `error_message`, keep `last_session_dir`, and return the
`{"error": ...}` shape the summary already understands. `_on_stop_clicked()`
shows the banner when that happens.

Also, `_StreamWriter.info()` now points `file` at whichever of the MP4 /
MKV actually exists, so a session whose remux failed outright still loads
in the Viewer (PyAV reads either container).

Tests: `test_recorder.py` gains a mid-recording writer failure (MKV kept,
`error` flagged, other stream fine, session still loads) and a
frame-size-change-mid-recording case (resized, not fatal, output stays at
the declared resolution). `test_kiosk.py` gains a writer that fails
mid-recording (session stops loudly, error banner names the instrument,
MKV kept, other stream verified) and a `stop_recording()` whose recorder
raises on `stop()` (lands in ERROR, doesn't propagate, names the files).

From the same review: `settings.py` Save dropping config keys it doesn't
model, a disconnected `sessions_dir` drive wedging the preflight poll, and
a fractional `recording.fps` crashing every Start -- all three fixed in
the next entry.

---

## 2026-09-09 - Config that would only fail at Start now fails at load

Three ways a `config.json` a technician could plausibly produce turned a
misconfiguration into a crash with nothing shown, instead of the
loud-and-early message `config.py`/`kiosk.py` validation is meant to give.
All three fixed.

**A fractional `recording.fps` crashed every recording, silently.**
`config._parse_recording()` accepted any positive number, but the value
becomes the encoder's frame rate and PyAV's `add_stream(rate=...)` raises
`AttributeError: 'float' object has no attribute 'numerator'` on a
non-integer. That propagated through `Recorder.start()` ->
`KioskController.start_recording()` -> `app._on_start_clicked()` -- none of
which catch -- into the excepthook. State stayed READY, Start looked
inert, every click re-crashed. Reachable by hand-editing `"fps": 29.97`,
or even `"fps": 30.0` (JSON writes whole numbers as floats). Now
`_parse_recording()` requires a whole number: `30.0` is accepted and
coerced, `29.97` is a `ConfigError`. `RecordingConfig.fps` was already
typed `int`.

**A disconnected `sessions_dir` drive wedged the preflight poll.**
`kiosk.poll_preflight()` called `shutil.disk_usage()` unguarded; if the
configured recordings folder is on a drive that isn't there (a removed USB
disk, an offline network share), that raises every 250ms poll. At startup
`KioskWindow.__init__` calls `poll_preflight()`, so it was a bare crash
with only a log line -- a technician double-clicks `app.exe` and nothing
happens. Now the disk check is wrapped: `PreflightStatus` gains
`disk_error`, `disk_ok` goes False, and `app._idle_reason()` shows "the
recordings folder (E:\...) - is that drive connected?" instead of a bogus
"0 MB free". A mid-recording drive loss is covered separately -- the
writer's disk write fails and trips the `failed_stream()` path from the
previous entry.

**`settings.py` Save silently dropped config it has no field for.**
`_on_save_clicked()` built a fresh dict from only the fields its two-row
UI models, so a hand-set per-instrument `orientation` / `pixel_clock_hz`
override (the documented escape hatches -- the same `orientation` key that
corrects the slit lamp), a `recording` section, or a third instrument role
were all wiped on the next Save. Now Save reads the existing `config.json`
as raw JSON and merges its edits onto it: unknown top-level keys pass
through, every instrument already in the file is kept, and each modelled
instrument carries its `orientation` / `pixel_clock_hz` forward -- unless
its `kind` changed to `net2860`, which rejects those keys. An unchecked
retention group still removes an existing policy.

Tests: `test_config.py` -- fps default / whole-float coercion / fractional
rejection. `test_kiosk.py` -- a `disk_usage_fn` that raises is reported,
not propagated. `test_settings.py` -- Save preserves `recording`, a
per-instrument override and a third role; and drops an `orientation`
override when the role becomes `net2860`.

Still open from the review, lower priority: `settings.py`'s UVC
enumeration isn't failure-wrapped the way IDS is (a bad USB device can
stop `settings.exe` opening at all), `resolve_device()` picks silently
among an ambiguous VID/PID match, and the small items (Watch offered for
a manifest-less session, `MANIFEST_NAME` hardcoded in
`retention.py`/`viewer.py`).

---

## 2026-09-09 - Five more found by probing the record path with real failures

The previous two entries came from reading. These came from *running* the
recorder against cameras and paths that misbehave -- which found things
reading did not. Each was reproduced first, then fixed, then pinned by a
test.

**A camera that captured nothing took the whole session with it.** PyAV
never creates a container until a packet is written, so a camera delivering
no frames for an entire session leaves neither an MKV nor an MP4. `info()`
still named a file, the manifest pointed at a phantom, and `Session.load()`
refused the *whole* session -- including the other camera's perfectly good
recording. One dead camera destroyed access to the surviving stream, which
is the opposite of what "never make the irreplaceable data depend on a step
that can fail" asks for. Now: `finalize()` says "no frames were captured"
instead of reporting a confusing remux error, `info()` writes `file: null`
rather than naming something absent, and `Session.load()` *skips* a stream
whose file is missing (recording it in `Session.missing_streams`, which the
Viewer displays) and raises only when no stream is loadable at all. That
last part also covers a session copied to a USB stick without all its files.

**An odd camera resolution recorded nothing at all.** libx264 with yuv420p
refuses odd dimensions -- `avcodec_open2` fails outright -- so a camera
reporting e.g. 641x481 produced an empty stream. `session_export.py` has
had an `_even()` helper for exactly this since it was written; the *record*
path never did. Now `_StreamWriter.start()` rounds down to even and the
per-frame resize added in the previous entry (for the UVC reconnect case)
handles the mismatch, so the same backstop serves both.

**A sessions_dir that can't be created made Start silently inert.** The
disk preflight measures free space on the nearest *existing* ancestor, so
a `sessions_dir` under a path that is actually a file -- or anywhere
unwritable -- passes preflight as READY. `Recorder.start()` then raised
through `KioskController.start_recording()` and `app._on_start_clicked()`,
neither of which caught it, into the excepthook: the student presses Start,
nothing happens, forever, with no message. Now `start_recording()` catches,
lands in ERROR with "Could not start recording: ...", and `app.py` shows
the banner. Same shape as the `stop_recording()` fix in the previous entry;
the two ends of a session now behave the same way.

**A 0x0 resolution would have produced a "verified" 2x2 recording.** Some
DirectShow drivers don't report a frame size until a frame has been pulled,
and `UvcCamera._open()` never checked. `_cameras_ready()` only asks whether
frames arrive, so preflight passed. Worse, the `_even()` floor of 2 added
above would have turned 0x0 into a 2x2 encoder -- a recording that verifies
and is useless, the exact "discovered a week later" failure CLAUDE.md names
as the worst outcome. Now `UvcCamera._open()` falls back to the first
frame's own shape and raises if that is still unusable (feeding the normal
start()-fails/retry path), and `_StreamWriter.start()` refuses a resolution
below 2x2 outright -- refusing to start beats recording something broken.

**Every Watch left a window behind for the life of the kiosk.**
`open_session()`/`browse_sessions()` parent their modal dialogs to the
kiosk window and never released them, so Qt parent-child ownership kept
each `ViewerDialog` -- and the full-size `QPixmap` rendered into it --
alive until the process exited. Measured: five open/close cycles left five
dialogs attached. The kiosk runs unattended for days, and Watch is the
button this app exists for. `_release()` now unparents and schedules
deletion; `setParent(None)` as well as `deleteLater()`, because
`deleteLater()` alone only fires when control returns to the event loop at
the level it was called from, which is fragile straight after a nested
`exec()`. `open_session()` also calls the (idempotent) `_shutdown()`
explicitly, so releasing the PyAV decoders never depends on a signal having
fired. `settings.py`'s PreviewDialog got the same treatment.

Also closed here, from the earlier review's small list: `Recorder.start()`
now tears down any writer it already started if a later one fails, and
removes the session directory it just created when nothing landed in it --
otherwise a repeated failed Start littered `sessions_dir` with empty
folders that `retention.py` deliberately never cleans up.

**The lesson worth keeping:** three of these five are invisible to code
review and obvious within seconds of running the thing. The record path
deserves a few deliberately hostile cameras -- no frames, odd size, lying
about its resolution -- the same way `test_recorder.py` already keeps a
deliberately-too-fast one.


## 2026-09-09 - The legacy BIO's vendor driver is replaceable after all

### Reversal

The 2026-08-26 `Net2860Camera` entry rejected a generic eMPIA driver and,
read loosely, has since been treated as a standing rule that the NET GmbH
vendor driver is the only option and the CLSID/32-bit-helper path is
permanent. That reading is withdrawn. The rejection was about *one
package*, on grounds specific to it; it never established that a
replacement is impossible.

**Why this matters at all: the vendor driver is a finite-lifetime
dependency.** `net2860_usbx64.sys` carries no embedded signature -- the
package is catalog-signed via `NET_2860_x64.cat`, whose signer is `CN=NET
New Electronic Technology Vertriebs GmbH` through GlobalSign, **not**
Microsoft. It was never WHQL-certified and is therefore **not on Windows
Update**: a fresh Windows install will not find any driver for this device
(interface class `0xFF`, no inbox class driver, no matching Update Catalog
entry). It loads today only through legacy allowances -- a signing cert
issued 2011-08-04 (before the 2015-07-29 cross-signing cutoff),
countersigned 2011-10-17, chaining to a `CN=Microsoft Code Verification
Root` cross-certificate that itself expired 2016-05-23, over a SHA-1
digest. Confirmed working under Secure Boot on Windows 11 build 26200, so
nothing is urgent -- but each of those allowances is Microsoft's to
withdraw, and NET GmbH has left this product line, so the package can
never be re-signed. **The exported package is the only copy that will ever
exist; archive it.** `pnputil /export-driver oem0.inf <dir>` yields five
files, ~300 KB; reinstall with `pnputil /add-driver net_2860_x64.inf
/install`.

**Signing is not the obstacle to a replacement.** The rejection above
turned on machine-wide test-signing being an unacceptable security-posture
change. That reasoning does not carry over, because it conflates two
distinct gates. Kernel Mode Code Signing -- the gate needing an EV
certificate and a Partner Center submission -- governs loading *your own*
`.sys`. Binding the inbox, Microsoft-signed `winusb.sys` (present on every
Windows install, with `winusb.inf`) ships no kernel code at all, leaving
only PnP *package* signing, which a self-signed certificate in Trusted
Root + Trusted Publishers satisfies. No purchase, no Partner Center, no
test-signing mode, no Secure Boot change. "Trust driver packages from this
publisher" is a far narrower grant than "load any unsigned kernel code",
so the objection that killed the eMPIA option does not apply here.

**There is no sensor bring-up to reverse engineer.** Board photography
identified the two chips the 2026-08-26 entry never got far enough in to
see: a **Sony CXD3172AR** ("Signal Processor LSI for Single CCD Color
Camera" -- CCD drive timing, 10-bit ADC, luma/chroma processing, AE/AWB
detection, and ITU-R BT.656 digital output) and a **Silicon Labs
C8051F321** MCU closing the AE/AWB loop. Critically there is **no analog
video decoder anywhere on either board**: the Sony DSP feeds BT.656
straight into the EM2860, occupying the slot where a consumer EM28xx board
has a SAA711x/TVP5150 needing per-board I2C bring-up. The camera images
autonomously -- observed directly, when switching the room light on
produced a correctly exposed frame with no host involvement whatsoever. A
host-side driver would configure the *bridge* only, which is the half
Linux's mainline `em28xx` documents thoroughly.

**Corollary worth stating:** this is why `Net2860Camera` has no
exposure/gain/white-balance control, unlike `IdsCamera`. That is not a gap
in the implementation -- the AE/AWB loop lives in F321 firmware the host
cannot address. A rewrite would inherit exactly the same limitation, so
don't expect `exposure_calibration.py`-style control to come out of one.

**What the cost actually is: isochronous transport.** The configuration
descriptor -- read from the hub non-invasively via
`IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION`, without disturbing the
running driver -- shows one vendor-specific interface with **eight**
alternate settings and **no bulk endpoint at all**. Video is
isochronous-only on EP `0x82`, 11.6 to 24.6 MB/s across alts 2-7,
high-bandwidth (2-3 transactions per microframe). 720x576 x 2 bytes x
25fps = 20.7 MB/s falls between alt 5 and alt 6, so the wire format is
uncompressed YUV422. Sustaining that from Python on Windows -- through
libusb's least-travelled backend, with no retry and no flow control at
8000 microframes per second -- is the whole remaining risk, and it is the
one thing no amount of studying the vendor driver improves.

**Settled by capture, same day:** a USBPcap trace of
`net2860_helper.py` starting up and streaming (archived, with its decoder
scripts, at `vendor/net2860_driver/capture/`) shows the host sends the
F321 and the Sony DSP **nothing at all**. The only vendor requests on the
wire are `bmRequestType 0xC0 / bRequest 0x00` (register read, `wIndex` =
register) and `0x40 / 0x01` (register write, `wIndex` = register,
`wValue` = value) -- exactly Linux `em28xx`'s register protocol, with the
driver reading every register straight back to verify it. Zero I2C
transactions to any slave. **The entire initialization is 62 register
writes** across the EM2860's `0x06`-`0x3f` space, each one recorded in
`capture/init_sequence.txt`, and the alternate-setting selection rides a
`URB_FUNCTION_SELECT_INTERFACE` rather than a control transfer. The
config descriptor the driver fetches matches the one read independently
from the hub, byte for byte.

That is the whole knowledge half of a replacement, and it is replayable
as-is. It also confirms the autonomy argument above from the wire rather
than from inference: nothing configures the imaging, because nothing can.
Capture was non-invasive (USBPcap sits below the driver, which kept
streaming throughout); only *development* needs WinUSB bound, which
unbinds `net2860_usb` and puts the camera out of service until it is
bound back.

**Not being done now, and this entry does not authorize it.** The current
path works and the instrument is discontinued. The estimate is ~1.5-3
weeks, dominated by the isoc pipeline and by hardening to the `BaseCamera`
contract -- not by knowledge of the device. The cheap insurance (archived
driver package, plus testing before any Windows upgrade on a clinic
machine carrying this camera) covers the same risk for nothing. What
changed is that the option is now open and costed rather than foreclosed.


## 2026-09-09 - WinUSB spike: the legacy BIO replacement works, end to end

**Decided:** nothing yet -- this records a spike, not a commitment. But the
risk the entry above called "the whole remaining risk" is retired, so the
estimate it carries is now wrong on the high side.

Bound the camera to Microsoft's inbox `winusb.sys` (via Zadig, which
generates a self-signed INF -- `oem24.inf`, provider `libwdi`, service
`WinUSB`) and drove it from plain CPython through `ctypes` against inbox
`winusb.dll`/`setupapi.dll`. **No libusb, no pyusb, no third-party runtime,
nothing of Keeler's or NET GmbH's.** Code in `spike_net2860_winusb/`.

**Stage 1 -- register protocol.** Replayed the 62 captured writes honouring
their recorded delays, then read every touched register back: **53 of 53
match**, and `reg 0x0a` reads `0x22` exactly as the vendor driver's capture
saw. One correction to the capture reading: a write's SETUP payload is 9
bytes, not 8 -- the value rides in `wValue` *and* in a trailing data byte,
so `write_reg` must send both.

**Stage 2 -- isochronous throughput, the actual unknown.** Alt setting 6,
64 packets per transfer, 8 transfers in flight against one registered
isoch buffer:

    5.01s: 103,828,540 bytes = 20.74 MB/s
           40,000 packets, 0 errors (every USBD status 0)

720x576 YUV422 at 25fps needs 20.74 MB/s. It sustained exactly the video
rate with **zero** packet errors, from ordinary CPython with the GIL and
GC running -- no C shim, no special scheduling. The fear that Python
could not hold an isochronous deadline was unfounded at this rate.

**Stage 3 -- framing.** Every packet carries a 4-byte header: `22 5a <seq>
88` starts a field, `88 88 88 88` continues one (both must be stripped).
Measured over 4s: **50.2 field headers/s at a 20.0 ms interval** -- exactly
PAL -- with 414,720 payload bytes between headers, which is precisely
720x288x2, one field. Interleaving an even/odd pair and running
`cv2.COLOR_YUV2BGR_YUY2` produces a clean 720x576 BGR frame with no shear
or tearing. Re-verified with the BIO illuminator switched on: colour is
correct (no chroma swap), and the frame mean moved 31.4 -> 98.3 from the
light alone with our code sending nothing -- the onboard AE loop works
through WinUSB exactly as through Keeler's driver, which is the last
confirmation that this camera images autonomously. Regenerate the frame by
running `spike_net2860_winusb/spike4.py` (the PNG is deliberately not
committed).

**Unexpected bonus: a real frame counter.** Header byte 2 increments
monotonically 0->127 and wraps, once per field. That is a *source-provided*
sequence number, so a WinUSB implementation could populate `Frame.index`
from the device and get genuine dropped-frame detection -- which neither
`UvcCamera` nor today's `Net2860Camera` has (both self-count, the
documented deviation in CLAUDE.md). `recorder.py`'s drop accounting would
become real for this camera rather than best-effort.

**Revised estimate.** The ~1.5-3 weeks above was dominated by isochronous
risk and frame assembly. Both are now prototyped and working. What remains
is ordinary: wrap it in the `BaseCamera` contract with proper threading and
buffering, reconnect handling, a production INF plus signing story to
replace Zadig's, `config.py`/`settings.py` integration, and tests. Call it
**1-1.5 weeks with no unknowns left**.

**What this buys, restated:** no vendor driver to redistribute (so the
Keeler permission question stops mattering), no 32-bit helper subprocess,
no `.venv32`, no CLSID/`RunOnce` registration gotcha, no SHA-1
cross-signed driver on borrowed time -- and `Net2860Camera` collapses into
an ordinary in-process `BaseCamera` like `uvc_camera.py`. Every one of the
four plug-and-play gaps identified for the vendor path disappears rather
than being worked around.

**State the dev machine is in:** the camera is bound to WinUSB, so `app.py`
cannot use it until the vendor driver is restored (Device Manager -> Update
driver -> `C:\Program Files (x86)\2860_Cam\driver`, byte-identical to
`vendor/net2860_driver/`). Left on WinUSB deliberately for continued work;
the BIO itself is awaiting a repair part regardless.


## 2026-09-10 - The legacy BIO's vendor-driver route is removed

**Decided:** deleted `net2860_camera.py`, `net2860_helper.py`,
`net2860_protocol.py`, `setup_net2860_helper.ps1`,
`requirements-net2860.txt`, their tests, and `tools/smoke_test_net2860_camera.py`.
`kind: "net2860"` is no longer valid. `net2860_winusb_camera.py` is the only
way to this camera.

**Why now.** Yesterday's entries kept both routes deliberately, so a machine
could move over without the old one vanishing underneath it. That caution
has expired: the WinUSB route now records real sessions, is wired through
`config.py`/`settings.py`/`app.py`, ships a signed driver package, and has
been verified to take over cleanly from Keeler's driver in a single
`pnputil /add-driver /install` with no removal step.

The decisive argument is that **the vendor route never worked in a frozen
build at all.** `app.spec` has `datas=[]`, so `net2860_helper.py` was never
bundled, and `net2860_camera.py` resolved its interpreter to
`<_MEIPASS>/.venv32/python.exe` -- a path that cannot exist in a frozen
install. A clinic machine selecting that camera got an error telling it to
run a PowerShell script that wasn't on the machine. Keeping it would have
meant *adding* work -- freezing a 32-bit helper, bundling Keeler's driver,
fixing the `RunOnce` COM-registration gap -- purely to preserve a fallback
the replacement had already superseded.

**What is genuinely lost.** The vendor path was the only other way to reach
this camera, so if the WinUSB route regresses in the field there is no
in-tree fallback. Mitigations: it is all in git history; Keeler's driver
package is archived (and separately backed up) and reinstalling it is a
Device Manager operation; and our INF overrides rather than replaces it in
the driver store, so falling back is non-destructive.

**Migration is named, not generic.** `config.py` rejects `kind: "net2860"`
with a message that says to change it to `"net2860_winusb"` and that
nothing else in the entry needs to change, rather than letting it fall into
the generic unknown-kind error. Anyone meeting it is looking at a config
that used to work, and the useful thing to tell them is the replacement.

**A ROADMAP entry closed rather than carried.** The 2026-08-26
"`Net2860Camera` brightness/gain control (surveyed, not decided)" entry
proposed checking whether the vendor DirectShow filter exposed
`IAMVideoProcAmp`. That question is now moot in a stronger way than
"the module is gone": the AE/AWB loop runs in firmware on the camera board
(Sony CXD3172AR + Silicon Labs C8051F321), the USB capture shows the host
sends it nothing at all, and switching the room light on moved the frame
mean from 31 to 98 with our code sending nothing. **No driver for this
camera can offer exposure control**, so the WinUSB rewrite inherits the
same limitation and the entry is closed rather than inherited.

**Not removed:** `vendor/net2860_driver/` (the archived Keeler package and
the USB capture `net2860_init.py` is generated from) and
`spike_net2860_winusb/`. Both are gitignored or exploratory rather than
product code, and the capture remains the provenance for the init sequence.

Full suite 320 passing, down from 349 -- the removed tests were the vendor
path's own plus duplicates of coverage the WinUSB tests already provide.


## 2026-09-10 - The legacy BIO needs rotate_180, not flip_vertical

**Decided:** `Net2860WinUsbCamera`'s default orientation is
`rotate_180`. Reported from the instrument: the image was reversed on
*both* axes, up/down and left/right. That is one 180-degree rotation, not
two flips -- see camera.py's `VALID_ORIENTATIONS` note.

**Why the earlier value was wrong, and why it looked right.** The camera
was given `flip_vertical` on the reasoning that the removed vendor-driver
helper hardcoded `np.flip(img, axis=0)`, and that the newer BIO's IDS
camera uses `flip_vertical` in `device_presets.py`. Both were real
precedents and both were misleading here.

It was then "confirmed" by looking at a captured frame whose only legible
feature was text running *vertically* up the right-hand edge. For
90-degree-rotated text a vertical flip and a horizontal flip are hard to
tell apart by eye, so that check could not have distinguished
`flip_vertical` from `rotate_180` -- it produced a confident conclusion
from evidence that did not support one. **Orientation needs a scene with
horizontal text or an obvious left/right asymmetry; a featureless glow or
rotated text cannot settle it.** Two later attempts to verify the fix
failed for exactly this reason: the illuminator was pointed at nothing
structured, so all four transforms looked equally plausible.

**Why the vendor path only needed a vertical flip.** Most likely the
vendor's DirectShow filter (`netvecam4.ax`) already mirrored horizontally
before handing frames over, leaving the helper only the vertical flip to
add. The WinUSB path bypasses that filter entirely and sees the raw sensor
output, so it has to do both. Unverified as an explanation -- the filter is
gone and there is nothing left to test it against -- but it accounts for
the discrepancy without either observation being wrong.

**The generalisable point:** the two Vantage Plus BIOs do *not* share an
orientation correction, despite being the same instrument family. The
docs asserted they did, in three places. A shared instrument model is not
evidence of a shared sensor mounting, and the transform belongs to the
camera and its mount rather than to the instrument's name.

`rotate_180` lives in `net2860_winusb_camera.py`'s default rather than in
`device_presets.py`, which keys off IDS model strings this camera does not
have.


## 2026-09-10 - Correction: the legacy BIO needs no orientation transform

**Supersedes the rotate_180 entry directly above.** Captured against a
scene with horizontal text (a "Perkins Tono" label), rendered through all
four `VALID_ORIENTATIONS` and compared: only `none` reads upright and
unmirrored. `flip_vertical`, `flip_horizontal` and `rotate_180` each mirror
or invert it. `Net2860WinUsbCamera` now defaults to `ORIENTATION_NONE`.

**This default was wrong twice.** First `flip_vertical`, reasoned from the
removed vendor helper's `np.flip(axis=0)` and from the newer BIO's
`device_presets.py` preset. Then `rotate_180`, from a report that the
image was reversed on both axes. Both were plausible; neither survived a
scene that could actually distinguish them.

**The method lesson, which cost three attempts:** orientation is only
testable against horizontal text or a strong left/right asymmetry.
Vertically-running text cannot separate a vertical from a horizontal flip.
A featureless field -- which the BIO illuminator produces whenever it is
not pointed at something structured -- cannot separate any of them, and two
verification attempts failed for exactly that reason before a label was put
in front of the instrument. **Do not accept an orientation claim, including
a human one, that was not made against text.**

**Still open, and a domain question rather than a technical one:** whether
"correct" here means *text reads upright* or *matches what the student sees
through the eyepieces*. Indirect ophthalmoscopy inherently presents an
inverted, laterally-reversed image, and students are trained for it. If the
camera shares that optical path, a recording that silently un-inverts it
would no longer match the view the student was working from -- and
CLAUDE.md is explicit that the coordination between the hands and the
instrument view is the entire point of the recording. `none` happens to
satisfy both readings for the bench scene tested here, so nothing is
blocked, but a retina view through the real optics has not been checked.

## 2026-09-10 - The two BIOs' orientation difference was a missing mirror

**Closes the question left open by the entry above.** Two Vantage Plus
BIOs appeared to present images 180 degrees apart through the same driver
and the same VID/PID, which raised a real design question: if units differ
by generation, orientation could be a per-revision preset; if they differ
per-unit, it has to become a technician setting in `config.json`, which
would have reversed the decision to keep it out of the settings dialog.

Neither. The workbench unit had its mirror removed pending a repair. **A
single plane mirror is orientation-reversing** -- it flips handedness, so
the view through one 45-degree mirror is a mirror image of the direct
view, and no rotation restores it. The instinct that "looking down through
a mirror at 45 degrees is the same as looking straight ahead" is wrong for
exactly this reason.

`ORIENTATION_NONE` therefore stands, `orientation` stays out of
`config.json` for this kind, and no per-revision preset is needed.

**Worth keeping from the false alarm:** a 180-degree rotation and a mirror
flip are different failures with different causes, and they are trivially
separable without software -- rotated text becomes readable by turning
your head, mirrored text does not. Every orientation mistake made today
came from evidence that could not distinguish the two cases. That test
should be the first thing tried, not the last.

Also established while chasing it: this camera's USB identity surface is
almost empty -- `bcdDevice 0x0100` is the only populated field, with no
manufacturer, product or serial string. If two units ever *do* differ,
Keeler's Kapture has essentially nothing to detect it with either, which
suggests Kapture does not compensate for orientation at all and simply
displays what the sensor gives. A retina image has no text and no inherent
"up", so such a difference could go unnoticed indefinitely in clinical
use. `tools/net2860_identity.py` dumps the full fingerprint for whenever
that question returns.


## 2026-09-10 - Uninstall scope is now a decision, not an accident

**Decided:** `reflex.iss` gains a `CurUninstallStepChanged` handler
that removes the staged WinUSB driver package and the signing certificate,
and PACKAGING.md documents what survives and why. ROADMAP.md's 2026-08-26
entry had flagged the uninstaller's scope as "never verified or written
down" -- a real open item rather than a shelved idea.

**What forced it now.** Today's driver work added two things no uninstall
touched: a package staged into the Windows driver store, and a self-signed
certificate trusted into Root *and* TrustedPublisher. The second is the
one that matters. Self-signing was justified here on the grounds that
trusting the certificate is a **narrow, revocable** grant -- explicitly
unlike the machine-wide test-signing that DECISIONS.md rejected for the
eMPIA driver. A grant that nothing ever revokes is not narrow, and leaving
a self-signed root certificate on a clinic machine after the software
justifying it is gone would have quietly hollowed out that argument.

**Kept, each for its own reason** rather than by omission:

- **Recordings.** CLAUDE.md is explicit that these are irreplaceable
  student work, not build output. An uninstall must never take them.
- **`config.json`.** Holds a technician's camera assignments and
  calibration. A reinstall that finds them intact is strictly better than
  one that does not.
- **The IDS peak SDK.** Shared -- Keeler's own Kinexis uses the same
  install, so removing it could break unrelated software. It has its own
  uninstaller.

**The published name cannot be remembered or hardcoded.** `pnputil` renames
a staged package to `oemNN.inf`, and the number is assigned at install
time: observed changing from `oem360` to `oem24` across two installs on
this machine as Windows reused a freed slot. So the uninstaller scans
`pnputil /enum-drivers` for the stable original filename and reads the
`Published Name:` above it. That also handles a package staged by
`build_driver_package.ps1 -Install` on a dev box, which the installer
never saw -- a case remembering install-time state would have missed.

**Verified as far as it can be without a target machine:** the lookup
logic was replayed against this machine's real `pnputil` output and
returns `oem24.inf` correctly, and the certificate is present in Root
under the exact subject the uninstaller targets. The installer compiles.
An actual uninstall has not been run end to end -- that needs the clean
machine the install test is also waiting on.

**One Pascal trap, noted where it bit:** `Published` is a reserved word in
Inno's Pascal Script (a class visibility specifier), so a variable of that
name fails to compile with a bare "Identifier expected".

---

## 2026-09-10 — Converge-at-open was the wrong thing to trust for the Keeler BIO

**Decided:** Offer a technician's exposure/gain calibration for *any* IDS
camera whose `ExposureTime`/`Gain` can be written — not only one that
lacks `ExposureAuto`/`GainAuto`. A camera that converges on its own does so
at the wrong moment, and there is no right moment to move it to.

**Why:** The Keeler U3-327xCP-C has working `ExposureAuto`/`GainAuto`, so
`_converge_auto_nodes()` converged it once at camera open and locked the
result for the session. That looked like "nothing for a technician to
calibrate", and `needs_manual_calibration()` reported exactly that, hiding
the sliders and Auto-Calibrate for this camera. The reasoning was wrong
about *when* open happens. `kiosk.select_instrument()` opens the camera the
instant a student taps the instrument on the picker — BIO still on the
desk, illumination off, pointed at nothing. `Once` then leaves that
result locked, and the camera is never reopened, so switching the lamp on
afterwards changes nothing. A whole session gets an exposure metered
against a dark desk.

**Why not converge at a better moment instead:** there isn't one. Start is
no better than picker-tap — students press Start and *then* raise the
instrument to the eye. The only representative moment is during the
recording, which is the one moment nothing may be adjusted: re-converging
there inserts a step that can fail immediately before an irreplaceable
capture, and `Continuous` would let exposure hunt mid-session and drift
against the frame-rate budget (the reason `_converge_auto_nodes()` chose
`Once` in the first place). That is what makes a technician-calibrated
`config.json` value the only answer that is right *at record time*: it is
the same conclusion CLAUDE.md's "the app holds the values, the camera
doesn't" already reaches for the slit lamp, arrived at from the other
direction.

The mechanism needed no new code. `_open()` was already per-axis: an axis
with a config value gets `_ensure_manual_*()` and is dropped from the
converge list, so config already won cleanly and device-side convergence
already remained the fallback for an uncalibrated install. Only the
question `settings.py` asked was wrong, so `needs_manual_calibration()`
("does this camera lack auto?") became `supports_manual_calibration()`
("can a technician set this at all?"), which both cameras answer True.

**Considered, built, and then rejected: a preflight darkness gate.** The
idea was that no exposure value saves "the BIO is switched off", and
nothing in preflight can tell — the camera runs, `Frame.index` climbs, and
sensor noise keeps the pixels changing frame to frame, so liveness, stall
and freshness all read healthy while the session records a black pane. It
metered each active camera's highlight level and disabled Start below 20
of 255.

What killed it is that this is the one failure the existing checks don't
need to cover, because it is *visible*. The freeze check earns its place
precisely because a frozen picture looks like a perfectly good picture. A
black pane looks like nothing at all, on a live preview the student is
already looking at as they reach for Start — and an unlit instrument stops
the student before it stops the app, since they cannot perform the skill
through a dark BIO either. Against that, the gate carried real cost: the
threshold was a guess (nothing here has metered a real fundus reflex), the
student is unsupervised with no instructor to override it, and a false
positive leaves a working room unable to record at all. Refusing to start
is the right answer for what a student cannot see. It is the wrong answer
for the most obvious thing on the screen.

**Also rejected, as the other way to cover the same case:** letting the
camera re-meter continuously in real time. The vendor's auto-exposure
meters an average, and these views are ~98% black by design — the same
reason `METERING_HIGHLIGHT` exists — so it drives exposure to the ceiling
and blows out the beam, and hunts visibly as the beam moves, in exactly
the footage the recording exists to show.

**What makes both safe to leave out:** reviewing the recording *is* the
point of the recording. A student who records a black pane sees it in
Watch seconds later and can record again — this app is self-checking in a
way ordinary capture software is not. The technician's test recording (see
`CALIBRATION.md`) is the same backstop at install time.

**Still open, same bug class:** `BalanceWhiteAuto` also converges at open,
against that same dark desk. `config.json`'s `red_balance_ratio`/
`blue_balance_ratio` exist and `_open()` already honours them, but
`needs_manual_white_balance()` still answers False for the Keeler, and
exercising the manual `BalanceRatio` path means finally verifying code
that has never run against hardware (see `ids_camera.py`'s module
docstring).

---

## 2026-09-10 — A signed driver catalogue is invalidated by a line-ending conversion

**Found:** the clinic installer reported `could not install the older BIO
camera driver (pnputil exit code -536870325)`. That is `0xE000024B`,
`SPAPI_E_FILE_HASH_NOT_IN_CATALOG`: the INF's hash was not in the catalogue
shipped beside it, so Windows treated the package as tampered with and
refused to stage it. Signing, certificate and trust chain were all fine.

**Cause:** `reflex_net2860.cat` was signed against an LF copy of
`reflex_net2860.inf` (2261 bytes); the installer shipped the CRLF copy
(2322 bytes). `core.autocrlf=true` with no `.gitattributes` means git
stores the INF as LF and writes it out as CRLF, so an ordinary checkout —
a branch switch, in this case, a day after the catalogue was built —
rewrote the file and invalidated the hash. The content never changed. 61
bytes of line endings did, and a catalogue hashes bytes.

Confirmed rather than inferred: the same catalogue reports `File not found
in the specified catalog` against the CRLF INF and `Successfully verified`
against an LF copy of the identical text.

**Decided, two parts:**

- `.gitattributes` pins `*.inf` to `text eol=crlf`. The working-tree bytes
  no longer depend on each machine's `core.autocrlf`, which is what a
  signed catalogue needs. Verified end to end: deleting the INF and letting
  git restore it — the exact operation that broke it — now leaves a file
  the rebuilt catalogue still verifies.
- `build_driver_package.ps1` runs `signtool verify /pa /c` on the pair
  immediately after signing and fails the build if the catalogue doesn't
  cover the INF.

**Why the second part, given the first:** pinning the line endings fixes
this cause; the verify catches the *class*. Any future edit to the INF
without a rebuild produces the identical failure, and the whole point of
the check is where the failure surfaces. This one cost a site visit to
learn something one command answers at build time. It is the same reasoning
that already makes a missing `.cat` fail the installer compile loudly — a
stale one was simply the case nobody had thought of.

**On the trust chain:** `signtool verify /pa` checks the certificate as
well as the hash, and a build machine has no reason to trust this
self-signed certificate — the *installer* trusts the shipped `.cer` on the
target. So the check distinguishes the two: a hash mismatch fails the
build, an untrusted chain prints a note and continues. Failing a build over
the build machine's own trust state would be wrong, and would push someone
toward trusting a signing certificate on a machine that has no reason to.

---

## 2026-09-10 — Three icons, not one

**Decided:** `assets/reflex.ico`, `reflex-viewer.ico` and
`reflex-settings.ico`, wired into both installers, all three
PyInstaller specs, and every Qt entry point.

**Why three:** CLAUDE.md's rule that a student must never be pointed at
`settings.py` is enforced by where its shortcut goes — Start menu, no
Desktop icon. But a Start menu lists all three programs together, so the
technician tool is deliberately drab (slate, a gear) while the two
student-facing marks stay teal. The viewer gets its own for a different
reason: the clinic and viewer-only installers can sit on the same
machine, and two identical setup exes in a Downloads folder is a support
call.

**Why each spec names the icon twice:** `icon=` writes the Windows
executable resource (Explorer, taskbar, shortcuts). Qt can't read that
back, so `QApplication.setWindowIcon()` needs the file itself — hence the
`datas` entry putting the same `.ico` under `sys._MEIPASS/assets`. Two
mechanisms; one can break while the other looks fine, so PACKAGING.md's
verification step names both.

**Why a missing icon isn't fatal:** `QIcon` on a nonexistent path is a
null icon, not an exception, and `app_icon.py` doesn't add a check.
Elsewhere this project prefers refusing to start over running degraded,
but that rule exists to protect an irreplaceable recording. A kiosk that
won't launch because a decoration is missing is worse than a plain one.
`setup_wizard.py` needs an explicit guard only because tkinter's
`iconbitmap()` does raise.

---

## 2026-09-11 — The app is now Reflex

**Decided:** every name the app shows or leaves on a machine changes from
`sidebyside` to Reflex — window titles, shortcuts, installer names and
outputs, install folders, `%ProgramData%\Reflex` (config and logs),
`%PUBLIC%\Documents\Reflex\sessions`, the icon filenames, and the legacy
BIO driver package (`reflex_net2860.inf`, its catalogue and certificate,
signed as "NECO Reflex driver signing"), and the GitHub repo and project
folder (`reflex`). Unchanged: Python module names, and the `side_by_side`
layout mode, which names a layout, not the app. Entries above this one were
rewritten to the new names too, so a filename there names today's file;
details such as byte counts still describe that file as it was then.

**Why a clean break rather than an upgrade path:** no recording that
matters exists yet, so carrying the old folder names forward would keep
`sidebyside` on disk indefinitely to protect nothing. The cost is a
one-time manual step per machine that has the old install (PACKAGING.md
step 5): uninstall `sidebyside` — its uninstaller already removes its
driver package and certificate — install Reflex, then restore
`config.json` or redo CALIBRATION.md.

**`AppId` is now pinned** (`reflex`, `reflex-viewer`). The clinic installer
used to leave it unset, so Inno derived it from `AppName` and the old
display name *was* its identity. Keeping old installs upgradable would have
meant pinning it to `sidebyside`; with a clean break happening anyway it is
pinned to the new name instead, and a later display-name change needs no
break at all.

**The driver's `DriverVer` moved to 09/11/2026, 1.0.1.0.** Its INF changed,
and on a machine where the old package was never removed, the newer date is
what makes Windows rank this package above the old one for the same
hardware ID. The first `build_driver_package.ps1` run after this mints a new
signing certificate, because it looks the certificate up by subject.

---

## 2026-09-11 — The Reflex look

**Decided:** a Charcoal-chrome kiosk and viewer (`reflex_style.py`), the
cat-eye mark as the kiosk's recording indicator (`reflex_mark.py`), a
Burgundy placeholder in the empty instrument pane, and new artwork for the
three icons (`branding/build_icons.py`). All brand values come from
`neco_reflex_theme.py`.

**Where the spec lives:** the Notion page "NECO Reflex — Branding
Decisions". It is deliberately not restated here or anywhere else in the
repo, which is public. Brand values (palette hex codes, font names) go only
into the code that consumes them; entries like this one record engineering
reasons, not brand rules.

**The mark is drawn, not rendered from its SVG.** The indicator
interpolates the pupil's width, height and fill together, which a static
SVG can't do, so the widget draws it with QPainter. The SVGs in `branding/`
stay the source for the icons, and `test_reflex_mark.py` parses them
against the widget's constants so the two copies can't drift. One trap:
setting a `QVariantAnimation`'s start or end value re-emits
`valueChanged`, so reading the current progress *after* setting them gets
a clobbered value. Stopping after a finished Start transition computed a
1 ms duration and snapped instead of animating. Read first, then touch the
animation.

**Still three icons.** The reasoning in "Three icons, not one" holds, so
the artwork changed but the count didn't. The kiosk is an Off-White mark on
a Charcoal tile; the viewer is the inverse, with a Sandstone hairline so a
light tile keeps its edge on a white window; settings is a Taupe mark on
Charcoal, the kiosk icon dimmed. The tile is there because an `.ico` has no
surface of its own. These are placeholders: `build_icons.py` prefers
artwork dropped into `branding/icon-sources/`, and the Notion page carries
the request for it.

**`.ico` written with struct, not Pillow.** PySide6's QtSvg rasterises,
and the container is simple enough that packing it would have been
Pillow's only job. The entry layout copies the previous icons' (32bpp DIBs
below 256, PNG at 256) because that layout was already proven against
PyInstaller's exe resource, Inno's `SetupIconFile` and tkinter's
`iconbitmap()`. Tkinter and Qt were re-checked against all three new files.
Sizes are 16/32/48/256. The previous files also carried 20/24/40/64/128,
which Windows now scales from the nearest size at 125%/150% display
scaling.

**Fusion plus a palette, then a small stylesheet.** A stylesheet-only
theme would need a rule for every standard widget (combo arrows, list
selection, progress bars, message boxes). Fusion draws all of them from
the palette, so the stylesheet only covers what a palette can't express:
the primary buttons, the selected instrument, the viewer's scrubber
(Fusion's handle nearly vanishes on Charcoal) and secondary text. A
selected instrument is outlined, never filled, because a solid Off-White
fill means "press this" and must stay unique to Start or Stop.
Widget-level stylesheets still win, which is how the red and amber banners
and the black video panes keep their colors untouched. `settings.py` stays
native: it's the technician tool, and looking plain is part of what
separates it from the student programs.

**The Burgundy placeholder is in `app.py`, not the compositor.** The empty
instrument pane is a pane-sized Burgundy image rather than a `background=`
passed to `side_by_side`. That argument would also have turned the
letterbox bars around real frames Burgundy, and `compositor.py` feeds the
viewer and Export, which this shouldn't reach.

**Fonts fall back.** Franklin Gothic Book comes with Microsoft Office, not
Windows. A clinic PC without Office falls through to Franklin Gothic Medium,
which Windows does ship, then Segoe UI.

---

## 2026-09-11 — First round of student feedback

Five items from the first students to use Reflex in the room. They're
recorded together so the ones not acted on yet aren't lost. Anything built
later gets its own entry.

| | Feedback | Status |
|---|---|---|
| A | Label the buttons "Start Recording" and "Stop Recording" | **Done** |
| B | Room lighting: the stand light alone is too dim to see the student doctor and patient, but the skill needs a dim room | Open — room, not software |
| C | Cap recording length (maybe 10 or 15 minutes) | **Done** — 15 minutes |
| D | Rename the bottom buttons "Watch Past Recordings" and "Watch Last Recording" | **Done** |
| E | Optional name or nickname for the student doctor, to track recordings (FERPA) | Deferred — see below |

**A and D: the buttons say what they do.** "Start" and "Watch" only made
sense to someone who already knew what the app was for. The status line
follows the new wording ("Ready. Press Start Recording."). The viewer's list
dialog is still titled "Past recordings": that title names the list, while
the button names an action.

**C: 15 minutes, stopped the same way as the button, and shown on screen
throughout.**

- *15, not 10.* The feedback offered either. 15 is less likely to cut a
  student off mid-skill, and a cut-off is the worse outcome. The cost is a
  bigger file. If the clinic settles on 10, change
  `kiosk.MAX_SESSION_MINUTES`. Nothing else needs to change.
- *Reaching the limit is a normal stop, not an error.*
  `poll_recording()` calls the same `stop_recording()` as the button, so
  the session is remuxed and verified, lands in IDLE, and Watch Last
  Recording lights up. The summary line says it stopped at the limit. The
  red banner would tell a student something went wrong with a recording
  that is fine. If finalizing fails, that is still reported as the failure
  it is, and `stopped_at_time_limit` stays False.
- *The student sees the limit before reaching it.* While recording, the
  status line reads "Recording... 3:12 of 15:00", with a seconds countdown
  added in the last minute. The Ready line states the limit. A limit a
  student only discovers by being cut off is the kind of surprise "loud and
  early" exists to prevent.
- *The cap is also the disk preflight's session length.* Until now,
  `TARGET_SESSION_MINUTES = 10` was a guess at a typical session and
  nothing enforced it, so a long session could outrun the space the
  preflight had checked for. With one constant, a preflight that passes has
  room for the longest session that can exist. The free space it requires
  rises by half (about 1.5 GB at the fallback canvas and 30fps).
- *In `KioskController`, not `app.py` or `config.json`.* It's a decision,
  so it goes in the controller, where the injectable clock tests it
  headlessly. It isn't config: it's a policy about how students practise,
  not a fact about one room. ROADMAP.md's 2026-08-18 settings survey
  already declined to move this constant to config without a real need.

**Rejected for C:** a warning dialog near the limit, and any way to extend
a session once it has started. During recording, nothing but Stop is
interactive (CLAUDE.md), and both would add a second control.

**B: a room fix first.** The instrument side needs the room dim; the
third-person camera needs enough light on hands and faces. So the answer
is room lighting: a dimmable, indirect source that lights the student and
patient without washing into the instrument's field. It isn't a readiness
gate either. A dim picture is visible in the live preview, and the
2026-09-10 entry keeps visibly wrong pictures out of the gates. One
software lever exists and should be checked before buying lights:
`UvcCamera` locks the webcam's auto-exposure, so what it's locked at
matters. A longer exposure is free brightness, but it also blurs the hands
on every frame — the same frame-rate budget the instrument cameras have.

**E: deferred.** Recorded here are the questions it has to answer before
it's designed:

- *Optional, never a gate.* An unsupervised student who skips the field
  must still be able to record. An empty field can't disable Start.
- *Where the identifier lives.* The session folder name is the obvious
  place, since it's what a technician retrieving files sees. It's also the
  most exposed: folder names show up in Explorer and in backups, and they
  travel with every copied recording (the viewer-only installer exists for
  exactly that). The alternative is `session.json` plus the recordings
  list, which shows it without putting it in any path.
- *FERPA is NECO's call, not the code's.* A nickname keeps a recording
  unidentifiable, and typing a real name into the same field defeats that.
  Whether real names are allowed at all needs an answer from NECO. So does
  who may see whose recordings: today anyone at the kiosk can open Watch
  Past Recordings, and once identifiers appear, that list shows every
  student's.
- *Entry on a kiosk.* It happens before Start, never during recording. It
  needs a keyboard at the station, and sanitizing if it ever reaches a
  filename.

---

## 2026-09-11 — Retired ROADMAP entries: what they decided

**Decided:** ROADMAP.md holds open plans only. Every entry that had been
built, resolved or closed is deleted from it: the build is recorded in the
DECISIONS entries above, and the full text is in git history — `git show
600ef01:ROADMAP.md`. References to ROADMAP entries in older entries above
resolve through that command.

Retired: "Device compatibility & camera setup system" (2026-08-17), "In-app
exposure/gain calibration" (2026-08-18), "Can `setup.ps1` drive the IDS
peak SDK installer?" (2026-08-19), "Distribute a frozen-exe installer"
(2026-08-20), "Vignette-aware metering, white balance, acquisition fps
cap, backlight compensation" and "`Net2860Camera` brightness/gain control"
(2026-08-26), the Recorder/Viewer split and its design (2026-08-26,
2026-09-02), and the uninstall-scope half of the 2026-08-26 health-check
entry (acted on in the 2026-09-10 uninstall-scope entry).

**Recorded nowhere else, so kept here:**

- **The IDS peak licence permits what the clinic installer does.** "License
  Terms for IDS Software Suite und/and IDS peak" (rev. 2020-07-20; the
  German text controls, clause 7.3), served by IDS's download page as
  `ids-license-terms-de-en.pdf`. Clause 2.1.1 grants the right to integrate
  the software into one's own products *that operate only with IDS
  cameras* and distribute them, which Reflex is. Clause 5.1 makes the
  licence effective on download or use, so nothing requires a person to
  click through the installer on each machine. Clause 2.3 forbids reselling
  it standalone, decompiling it, using it with non-IDS cameras, building
  competing control software, and sublicensing — none of which bundling it
  does.
- **`setup.ps1` stays detect-only.** It never launches the IDS installer:
  replaying a response file can drift from a newer installer and install
  the wrong feature set with no error, and launching it interactively saves
  a developer nothing. (The clinic installer does run it silently, with its
  own verification — see the 2026-08-25 silent-install entries.)
- **Extended setup, not IDS Software Suite + runtime setup.** Extended is
  one vendor-tested installer that bundles the uEye drivers the slit lamp
  needs. The alternative chains two installers, one a legacy compatibility
  product, each able to fail or drift separately; its only advantage was a
  smaller footprint. Extended also removes an existing Suite (SETUP.md
  Section 3).
- **Calibration UX.** A one-shot software auto-calibrate with sliders as
  the override, not sliders alone: sliders reduce to "eyeball it", with no
  reproducibility between technicians. Exposure is raised before gain,
  since gain adds noise that students then watch.
- **Camera identity.** USB port or hub topology was rejected as an
  identifier, since ports aren't stable enough here; VID/PID plus the
  single-device fallback is the whole strategy. Hot-reloading config into a
  running kiosk is deliberately out of scope: restart after Save.
- **What "composite live" (2026-08-11) protected, after the split.** Sync
  by construction became shared-clock PTS, which is stronger: a slow camera
  no longer gets duplicated frames. Watching needs no render; the only post
  step is the remux that already existed; the session, not a composite, is
  the deliverable. Explicitly out of scope at the time: latency-offset
  measurement (field reserved), instructor tools, multi-session compare,
  audio.

---

## 2026-09-11 — A calibration belongs to a camera, not a role

**Found:** on the second clinic PC the slit lamp never opened. The banner
said `DSStopAcquisition … Stream is not started!`, which was cleanup
failing: `_open()` failed at `set_gain()`, and `_close()` then stopped a
stream that had never started, replacing the real exception. With cleanup
fixed, the real one was `Gain 25.409727 must be <= 4.000000`. 25.41x is
the Keeler BIO's maximum gain, saved under the slit lamp's serial.

**Cause:** `settings.py`'s `DeviceRow` held calibration per *role* and kept
it when the row's dropdown changed. A slit lamp row once pointed at the
BIO, calibrated in a dark room up to its gain ceiling, then corrected,
saves the BIO's values for the slit lamp. The same config left the BIO
row uncalibrated.

**Decided:**

- Changing a row's camera discards that row's exposure/gain and white
  balance. Values loaded from `config.json` are unaffected: they arrive
  through `set_candidates()`, which blocks signals.
- `_open()` clamps a config gain to the camera's range, with a warning, as
  it already clamped exposure. An out-of-range gain is a wrong picture,
  which the live preview shows; refusing to open strands a student behind
  a disabled Start (2026-09-10's gate reasoning).
- `_close()` stops, flushes and revokes only a stream that started. On the
  uEye Transport Layer `Flush()` of a never-started stream also fails
  (`GC_ERR_IO`); dropping the handles was enough, since the kiosk's 2s
  retry reopened cleanly after every such failure.
- An exception raised by cleanup after a failed open is logged, never
  raised, and `app.py` logs a start failure with its traceback. The
  original error was otherwise lost on the PC that had it.
- Settings' Preview opens IDS cameras with `converge_auto=False`. The
  Keeler, uncalibrated (the other half of the same config), timed out
  converging in a dark room, and a failed open is a Preview with no
  Auto-Calibrate — no way out from the only tool meant to fix it.

**Not changed:** the kiosk still converges an uncalibrated axis and fails
the open if that times out. That is a missing calibration, a technician's
problem, and `CALIBRATION.md` covers it.

---

## 2026-09-11 — The legacy BIO does have host-side picture controls

**Correction.** This repo has said since the WinUSB spike that the older
BIO offers "no exposure/gain/white-balance control" because the camera
images itself. The second half is right; the blanket version is wrong.
The EM2860 bridge has its own video-processing registers, Keeler's driver
writes them, and `net2860_init.py` has been replaying them as opaque
constants.

**What the registers are.** Mainline Linux `em28xx-reg.h` names the block
the capture writes:

| Register | Meaning | Kernel default | Keeler writes |
|---|---|---|---|
| `R20_YGAIN` | contrast | `0x10` | `0x10` |
| `R21_YOFFSET` | brightness, signed | `0x00` | `0x08` |
| `R22_UVGAIN` | saturation | `0x10` | `0x0f` |
| `R23_UOFFSET` | blue balance, signed | `0x00` | `0x00` |
| `R24_VOFFSET` | red balance, signed | `0x00` | `0x00` |
| `R25_SHARPNESS` | sharpness | `0x00` | `0x02` |

Three of the six are deliberately off their defaults, so these are tuning
decisions someone made, not initialisation noise.

**Audited the vendor binaries** in `vendor/net2860_driver/` (strings only,
nothing executed). `netvecam4.ax` implements a "Video-Proc-Amp" page with
exactly Brightness, Contrast, Saturation, Hue and Sharpness, persisted to
`HKCU\Software\NetEmpiaCam4\VideoProcAmp0` — where this machine still
holds `Sharpness 2`, matching the captured `R25 <- 0x02`, so that UI
drives these registers rather than post-processing frames. The binary
contains no `Exposure`, `Gain`, `WhiteBalance`, `Backlight`, `Iris`,
`Shutter`, `AGC`, `AWB` or any `Auto*` string, and no `IAMCameraControl`.
`NET_USBIO_EMP1.dll` does export `NET_USBIO_API_{Get,Set}I2CRaw`, but the
capture uses neither: every vendor request is register read (`0xC0/0x00`)
or write (`0x40/0x01`), and register `0x06` (I2C clock) is configured and
never used.

**So the sensor-side claim is now stronger, not weaker.** Keeler, with the
documentation and the board, shipped a UI with post-processing sliders and
no exposure control — which is better evidence than our capture alone that
there is nothing on the sensor side to expose. What changes is the other
half: brightness, contrast, saturation, colour balance and sharpness *are*
ours to set on this camera, through the bridge.

**Not acted on yet** — see ROADMAP.md's entry. Nothing about the replayed
values changes until each register is confirmed on real hardware.

**Left alone deliberately:** `net2860_init.py`'s `START_WRITES`/
`STOP_WRITES` split sits at these same picture registers, which cannot
switch a bridge off, so the recorded reason for the split ("replaying it
whole switches the bridge straight back off") does not survive knowing
what they are. The split is kept because it is what was verified working
on hardware; the explanation needs re-deriving, not the sequence.

---

## 2026-09-11 - Reflex and sidebyside coexist; no installer guard

**Decided:** Reflex neither detects nor refuses a machine that still has
the old `sidebyside` install. Both stay installed, and the ROADMAP plan to
guard against it is dropped rather than built.

**Why the question existed:** the old installer left `AppId` unset, so Inno
used its name; Reflex's is pinned to `reflex`. Windows therefore treats
them as unrelated programs, and installing Reflex over sidebyside leaves
two kiosks with two Desktop shortcuts. The planned guard was an
`InitializeSetup` in `reflex.iss` refusing to run while the old
`Uninstall\sidebyside_is1` registry key existed.

**Why not:** a refusing installer stops a technician on a machine that
otherwise works, and the old app is not harmful in itself - it is a
complete, working recorder pointed at its own folder. Trading a guaranteed
install-time failure for a possible run-time confusion is the wrong trade
at this scale, with one or two machines involved.

**The accepted risk, stated plainly:** on such a machine a student can
launch the old kiosk. It records into sidebyside's own `sessions_dir`,
which nothing in Reflex lists, so the recording is not lost but is
somewhere nobody looks. Nothing detects this, by choice.

**The mitigation is procedural, not code:** whoever installs Reflex removes
the old app's Desktop shortcut, so the only thing a student can start is
Reflex. PACKAGING.md's step 5 says so. Uninstalling sidebyside outright is
still fine, and cleaner, but is no longer a precondition.

**Why auto-uninstalling was never the alternative:** removing the old
install means removing a kernel driver package and a trusted signing
certificate mid-install, which can fail in ways a refusal cannot.

**Revisit if** a student actually records into the old app. That is the
signal worth acting on - not tidiness.

---

## 2026-09-11 - The manual white-balance path is deleted

**Decided:** remove `needs_manual_white_balance()`, `auto_white_balance()`,
the `BalanceRatio` accessors, `exposure_calibration`'s `channel_medians()`/
`is_white_balanced()`/`next_balance_ratios()`, `config.json`'s
`red_balance_ratio`/`blue_balance_ratio`, `settings.py`'s balance sliders
and Auto White-Balance button, and every test of them. About 330 lines.

**Why:** it was built for a camera shape neither device has - no
`BalanceWhiteAuto` but a writable `BalanceRatio`. `needs_manual_white_balance()`
answers False on both cameras, so none of it has ever executed, and the
2026-09-08 entry had already recorded that it could not be verified here
and never would be. Code that cannot be exercised is worse than absent: it
reappears in every audit as "unverified", and it had shaped a config
pairing rule and a whole Preview control block around a case that does not
occur. The 2026-08-26 reasoning that added it assumed the slit lamp might
lack `BalanceWhiteAuto` *and* expose `BalanceRatio`; the hardware says it
exposes no white-balance node at all.

**What stays:** `BalanceWhiteAuto` itself. The Keeler converges its own
white balance once at open through `_converge_auto_nodes()`, which is
unchanged. The slit lamp has nothing to converge. So no camera lost a
capability here - only Reflex lost code it never ran.

**Config compatibility:** an `ids` entry still carrying the two keys is now
ignored rather than rejected, since `_parse_instrument()` only rejects
unknown keys on the `net2860_winusb` branch. Nothing in the field has them:
`settings.py` wrote them only when a camera reported the manual path.

**If it ever comes back,** this entry plus git history is the design.
Re-adding it against a camera that actually needs it will be cheaper than
having carried it unrun in the meantime.

---

## 2026-09-11 - Closing the legacy BIO orientation question: no transform, either way

**Closes** the "still open, and a domain question" note on 2026-09-10's
"the legacy BIO needs no orientation transform". `ORIENTATION_NONE` stands.

**Evidence:** Keeler's own software displays this camera's image neither
inverted nor laterally reversed - checked against the original application
on 2026-09-11. It applies no transform, and neither do we.

**Why the two readings could never have disagreed.** The inversion that
indirect ophthalmoscopy is known for is produced by the hand-held
condensing lens, out in front of the patient's eye - not by the
instrument. The headset camera looks down the same path as the oculars, so
whatever inversion the student is looking at, the camera is looking at the
same thing. "Matches what the student sees" and "don't touch it" are
therefore the same instruction, and "text reads upright" agrees whenever no
condensing lens is in the path, which is the bench case that was tested.

The question was framed as though we might have to choose. We never could:
any transform we applied would break the correspondence between the hands
and the instrument view, which CLAUDE.md names as the entire point of the
recording. The only defensible value is `none`.

**What would falsify this,** and is worth a minute the next time the
instrument is in front of someone: point it at something asymmetric -
text, a hand - and look through the oculars and at Reflex's preview
together. If they ever disagree, the camera's path has a relay the oculars'
does not, and this entry is wrong. Nothing in the capture or the board
photography suggests one.

---

## 2026-09-12 - Device profiles, with Custom as a first-class option

**Built** the 2026-09-01 ROADMAP plan, which is now deleted from there.
`settings.py` no longer asks a technician to know what a camera is: a
`DeviceProfile` carries the supported camera's name, its orientation and
its pixel clock, and picking a device selects the matching profile.

**Device and profile are separate fields.** The first says what is plugged
in; the second says what it is. Conflating them would mean an unlisted
camera had nowhere to go, and it would make "this is the wrong camera for
this role" indistinguishable from "we don't support this camera".

**Custom is a profile, not a fallback path.** It is always in the list, it
is what an unknown or newer-build profile id resolves to, and it is what
every `config.json` written before profiles *is* -- a typed label and no
profile id. That equivalence is why no migration code exists: the old shape
was always the custom shape. A new instrument therefore never waits on a
code change, which is the point.

**The label is tied to the supported device; the nickname is free.** A
profile supplies the picker name (`picker_label` -- "Slit Lamp", not
"Haag-Streit BI 900 slit lamp", because a student reads a button, not a
catalogue) and the field is read-only unless Custom. `nickname` is the
technician's own text, e.g. "Lane 3", and when set it *is* what students
see: `label` in `config.json` stays the authority for the picker, so
`app.py` needed no change, with `profile`/`nickname` stored beside it so
reopening Settings shows what was chosen rather than only its result.

**Matching is by model token, never serial.** One profile covers every unit
of a model; serials vary per camera. Serial *ranges*, for distinguishing
revisions of one model, are a real future need but not a present one.

**A profile on an unexpected camera warns and saves anyway.** The technician
may know better than this table, and Preview shows the consequence either
way. Refusal is reserved for what a student cannot see.

**The third-person row has no profiles at all,** with a test saying so: any
UVC webcam works, so a supported-model list there would imply a constraint
that does not exist.

**Verified:** 359 tests, including the round-trip through Save and reopen,
the pre-profile config landing on Custom with its typed label intact, and
an unknown id doing the same rather than pretending to be a known camera.

---

## 2026-09-12 - Correction: one name field, not a label and a nickname

**Supersedes** the naming half of the entry above. That version gave a
technician a read-only "name shown on the picker", filled from the profile,
*plus* an optional nickname which silently replaced it. Two boxes, one
question, and the read-only one was a lie: whatever was in the other box
won.

**The shape it should have been, and now is:** three fields per row, two
chosen and one typed.

| field | who decides |
|---|---|
| device | the system -- what it reports is plugged in |
| profile | us -- the supported list we ship, Custom included |
| name | the technician -- what students read on the picker |

**The name is pre-filled from the profile and stays editable.** The common
case needs no typing, and a room that calls the thing something else just
says so. `_label_is_default` tracks whether the text is still ours: a
profile change re-fills a name nobody has touched, and never overwrites one
a technician typed or one restored from `config.json`. Custom leaves it
empty on purpose -- nobody but the technician can name a camera we don't
know, and an empty required field says that better than a placeholder.

**`nickname` is deleted from `config.json`** one day after being added. It
had no reader: it was not a picker name (the picker must say the
instrument, since that is what a student is choosing between), and nothing
else consumed it. The same reasoning as the white-balance removal the day
before -- a field with no reader is speculative, and cheap to add back the
day session traceability actually wants one.

---

## 2026-09-13 - What the two IDS cameras actually offer, measured

Phase 0 of the "use what each camera's stack offers" plan, run against both
cameras with `tools/probe_camera_features.py` (its first execution -- it
worked unmodified) plus a stopped-device inspection.

**Everything that reads as read-only while streaming is writable at open.**
Region of interest, binning, decimation, mirroring, pixel format, the
throughput limit: all of it is `TLParamsLocked`, not a missing capability.
So anything here is Reflex's to set, as long as it is set before
acquisition starts.

| | slit lamp | Keeler BIO |
|---|---|---|
| region of interest | 16-1600 wide, offset fixed at 0 | 256-2056, offset 0-16 |
| binning | 2x2 | **8x8**, plus 2x decimation |
| throughput cap | absent | 400 MB/s, sitting at maximum |
| gamma / LUT | absent | **0.30-3.00, writable while streaming** |
| black level | 0-255 (offset scale) | 0-31.94, on continuous auto |
| mirroring | ReverseX/Y writable | ReverseX/Y writable |

**Three settings reset on every open on the uEye transport layer** -- pixel
clock (already known), and now binning and mirroring. A two-process test
that sets them and reopens measures nothing, which is how the first binning
run produced a null result. They belong in `_open()` beside the pixel clock.

**The Keeler's `AcquisitionFrameRate` was found at 20fps**, from persisted
device state nobody set deliberately: exactly the invisible-state problem
CLAUDE.md's "the app holds the values, the camera doesn't" rule exists for.
Reflex's frame-rate cap overrides it at open, so nothing was wrong in the
app; it is the camera that was carrying a stale answer.

---

## 2026-09-13 - Black level: the slit lamp's default destroys a third of the frame

Measured against a narrow beam on a black focus rod -- a genuinely black
surface at the exact focal distance, which is the hardest case for lifting
a black floor, since it is the thing that must not turn grey.

At a scene-appropriate exposure (7.47ms, gain 1.0, auto-calibrated), the
camera's factory `BlackLevel` of 90 pins **12-60% of the frame to exactly
0**, varying with how much of the field the beam lights. The crush stops
between 90 and 95. At 110 nothing is clipped, the floor sits at 6-7 of 255,
and the beam is unchanged (p99.9 193 -> 198). Side by side, 90 and 110 are
visually identical: the rod still reads black.

**Gain never makes it worse.** The offset scales with gain, so at 2x and 4x
nothing crushes even at 90. A value chosen at gain 1 -- the worst case -- is
safe for every calibration above it. That is what makes this a per-model
preset rather than a per-room one.

**Binning is not the answer to shadows, and this is the useful negative
result.** 2x2 binning does work (1600x1200 becomes 800x600) and roughly
doubles the light: at identical exposure the beam goes from p99 100 to 197.
But it brightens the beam as much as the surround, so it buys nothing for
*dynamic range*, which is the actual problem -- and it costs three quarters
of the pixels on an instrument whose point is fine detail. The scene also
calibrated to 7.47ms of a 30ms budget, so this camera is not short of light
in the first place. Binning is kept in mind for the opposite case: a room
too dim to reach 30fps, where it beats raising gain.

---

## 2026-09-13 - The BIO's on-camera gamma does what higher bit depth was wanted for

Swept `Gamma` at a fixed exposure and gain, so the curve was the only
variable. The direction is the opposite of the usual convention: on this
camera **values above 1.0 lift the shadows**.

| gamma | p25 | p50 | p99.9 | pure black |
|---|---|---|---|---|
| 1.00 (today) | 1 | 2 | 206 | 14.9% |
| 1.50 | 5 | 10 | 220 | 0.5% |
| 2.20 | 13 | 24 | 231 | 0.1% |
| 3.00 | 22 | 40 | 237 | 0.0% |

Shadows rise by an order of magnitude and **the highlight never clips** --
206 to 237, still short of 250 -- because the curve compresses the top as it
expands the bottom. On the camera, before the 8-bit conversion, for no
bandwidth and no host CPU. This is the thing 10/12-bit capture plus a host
tone curve was being considered for, already present and switched off.

**Not yet a value we can adopt.** The test scene was whatever could be
pointed at, and it calibrated to gain 25.2x of 25.4 max -- severely
light-starved, so the lift amplified noise along with signal. Choosing a
number needs a properly lit fundus view. Two implementation notes for when
it happens: gamma changes what `auto_calibrate()` meters, so the order of
the two matters; and the same sweep found `Gamma.SetValue(0.3)` rejected
against a minimum of 0.30000001, so callers must clamp to `Minimum()`
rather than the documented range.

**Hardware mirroring is available on both cameras but unadopted.** It would
retire the per-frame rotation `camera.py` applies today. Two unknowns
first: it does not persist across opens, and `PixelFormat` still reports
`BayerRG8` when mirrored, so whether the colour-filter phase shift is
compensated is untested. A wrong answer there is wrong colour, not a wrong
orientation.

---

## 2026-09-13 - The legacy BIO's picture registers, confirmed and taken over

Swept each register on a live stream with the camera attached, measuring
the frame after every write and reading the value straight back.

| register | what it does, measured |
|---|---|
| `0x20` contrast | 5-bit gain, `0x10` = unity. `0x08` halved the frame mean (86 -> 47). |
| `0x21` brightness | signed offset: `0x00`/`0x08`/`0x20`/`0x40` gave means 77/86/114/151. |
| `0x22` saturation | 5-bit gain. `0x00` produces a **monochrome** frame. |
| `0x23`/`0x24` | blue and red balance offsets. |
| `0x25` sharpness | edge enhancement: moves p95 (118 -> 121) without the mean. |

Linux `em28xx-reg.h`'s map is therefore right about this board. It is also
incomplete in a way that matters: **contrast and saturation mask off bits
above `0x1f`**, so `0x20` reads back as `0x00` and `0x30` as `0x10`. A
value that cannot land now logs a warning instead of passing silently,
which the readback makes free.

**The START/STOP split's stated reason was wrong.** Replaying
`START_WRITES + STOP_WRITES` together streams perfectly, and a normal start
afterwards still works -- tested three ways round. Whatever broke the spike
that day, it was not those four writes, which are picture registers and
cannot stop a bridge. `STOP_WRITES` is kept as the record of where the
vendor's session ended; nothing replays it.

**So the values are now ours rather than inherited.** `PICTURE_DEFAULTS`
holds Keeler's own numbers -- including saturation `0x0f`, where their
session actually ended, rather than the `0x10` the mid-capture left behind,
a difference this repo has been shipping unknowingly. `Net2860WinUsbCamera`
applies them after the bring-up sequence and takes a `picture=` override.
Adopting the vendor's numbers deliberately is the outcome the ROADMAP entry
said to expect: someone chose them with the instrument in front of them.
What changed is that we now know what each one does, and can move them.

---

## 2026-09-13 - Bit depth is free in frame rate, and inert on its own

The last of Phase 0's unknowns, measured with both cameras at a 30fps
target:

| | 8-bit | 10-bit | 12-bit |
|---|---|---|---|
| slit lamp | 30.1fps, 58 MB/s, 45% core | 30.1fps, 116 MB/s | 30.1fps, 116 MB/s, 60% core |
| Keeler | 30.1fps, 95 MB/s, 41% core | 30.1fps packed, 118 MB/s | 30.1fps packed 142 / unpacked 190 MB/s, ~60% core |

Zero dropped frames in every run, counted from each camera's own FrameID.
So depth costs bandwidth and about fifteen points of one core -- not frame
rate, which is what the bandwidth arithmetic in IMAGING.md had left open.

**And on its own it does nothing.** A `BayerRG12` capture reaches consumers
as the same `uint8` BGR8 frame with the same 248 distinct levels as
`BayerRG8`, because `_grab()` converts every buffer immediately. The extra
bits exist for the length of one function call. Any value from higher depth
has to come from tone-mapping *inside* that conversion; switching the
format alone is a bandwidth increase and nothing else. This is worth
knowing before someone "enables 12-bit" and reports no improvement.

**Two parameters stay on `IdsCamera` with no caller yet:** `binning` and
`pixel_format`. Both were added as the apparatus that answered Phase 0, and
both are the mechanism the remaining phases would use -- binning for a room
too dim to reach 30fps, where it beats gain, and pixel_format for the tone
curve above. Kept deliberately rather than by neglect, unlike the manual
white-balance path deleted two days ago: that one could never run on this
hardware, while these two have measurements behind them and named uses
ahead. If neither is adopted, they should go the same way.

---

## 2026-09-13 - The slit lamp's black level is ours now

**Adopted 110**, from the same day's measurements: the factory 90 sits on
this sensor's clipping point and pins 12-60% of a properly exposed frame to
zero, while 110 clips nothing, leaves the floor at 6-7 of 255, and is
indistinguishable from 90 by eye. Verified end to end on the camera -- it
reads back 110, and the frame's crushed fraction goes from 12% to 0.0%.

**It resolves from the model string, not only from a profile.** The first
implementation took it from `DeviceProfile` alone, which quietly meant no
existing install would get it: every `config.json` written before profiles
has none, so the camera would have kept its clipping default until someone
re-saved in Settings. `black_level_for_model()` now mirrors
`orientation_for_model()` -- a config value wins, then the profile, then the
model's own preset -- so the fix reaches a clinic machine on its next start
with nothing for a technician to do.

**The Keeler deliberately gets nothing.** It adjusts its black level
continuously and does not clip; writing one would be taking over a job it
already does correctly. `black_level=None` on that profile is a decision,
and a test says so by name.

**Why a preset rather than a technician setting:** the value is a property
of the sensor, not the room -- and gain only ever moves it away from the
clipping point, so one number chosen at gain 1.0, the worst case, is safe
for every calibration above it. `config.json` keeps a `black_level`
override for the case that would actually need one: a camera swapped for a
different revision, where a technician cannot wait for a release.

---

## 2026-09-13 - A 124ms calibration reached the field, and three things let it

Reported as "motion blur and lagginess" from the BIO. The camera was
delivering **8fps with 124ms of blur on every frame**, and `config.json`
said so: `exposure_time_us: 124314.9`. Three separate failures had to line
up, and each is worth fixing on its own.

**1. Settings' exposure slider was bounded by the sensor, not the budget.**
The Keeler's `ExposureTime` maximum is two seconds, so the slider offered
two seconds. A technician could set -- and save -- an exposure that caps the
camera at 8fps. CLAUDE.md already says exposure is a frame-rate budget and
"belongs in code"; the slider was the one place that had not been told.
It is now bounded by `exposure_budget_us(target_fps)`. A slider that cannot
reach a bad value beats a warning about one that was saved.

**2. `_open()` trusted the saved value.** It clamped to the device range
only, so a bad config produced a bad session forever. It now clamps to the
frame-rate budget too and says so in the log. The config file is left
untouched: it is the technician's, and `config.py`'s startup warning still
names it.

**3. A white-balance convergence timeout failed the camera open outright.**
At 124ms in a dim room `BalanceWhiteAuto` could not converge inside 5s, and
the exception propagated -- the student would see the BIO simply refuse to
start. Whatever the camera reached is locked and usable, and white balance
is *visible* in the preview, so it is not a readiness gate by CLAUDE.md's
own rule. It now warns and continues. This is the "still open, same bug
class" note from 2026-09-10, closed by a real failure rather than a
prediction.

Verified on hardware: the same unchanged `config.json` now opens the camera
and delivers **30.16fps with 0 dropped frames**, exposure clamped 124.3ms
-> 30.0ms.

**How it got there matters less than that nothing stopped it.** The value
was saved from a dim scene during this session's gamma work. What the
episode says is that a warning printed at startup -- which is all
`exposure_fps_warnings()` did -- is not a control. Nobody reads a log on a
kiosk.

---

## 2026-09-13 - A recording holds the machine awake

From the second feedback round: the machine slept about two minutes into a
session. Recording carried on correctly, which is the design working --
capture threads do not care about the display -- but a preview that
vanishes mid-skill looks like a fault, and a machine that suspends fully
takes the USB cameras with it.

`kiosk.set_keep_awake()` asks Windows for `ES_SYSTEM_REQUIRED |
ES_DISPLAY_REQUIRED` when a recording starts and clears it when one ends.
It sits in `KioskController` rather than `app.py` so every path that ends a
session releases it: the Stop button, the time limit, and `_fail()`. A flag
outlives the process that set it, so the failure path matters as much as
the happy one -- there is a test for each.

**Scoped to recording, deliberately.** A kiosk that never sleeps at all is
a facilities decision about someone else's electricity bill; an
in-progress, irreplaceable capture is ours. Between sessions the machine
may sleep as its owner configured it.

**Best-effort, like every other platform call here.** Not Windows, or the
call fails, and recording proceeds exactly as before -- it logs and
continues rather than treating a power hint as a reason to refuse a
student.

**Not documented in CALIBRATION.md**, though the remaining advice (screen
timeout, USB selective suspend on the camera ports) would belong there:
that file is at 184 of its 185-line budget, and the case that could lose a
recording is now handled in code. If the file is ever trimmed, three lines
there would still help a technician.

---

## 2026-09-13 - The kiosk no longer browses past recordings

**Removed "Watch Past Recordings" from `app.py`.** Every session holds
identifiable images of two students -- the one performing the skill and the
peer being examined -- and every session lands in one folder on a shared
machine. The button listed all of them to whoever was standing there, so
any student could watch any other student's recordings. Nothing in the app
scoped a session to the person who made it, and nothing could: there is no
identity in a recording today.

**Watch Last Recording stays.** A student reviewing what they just made is
the whole point of the app, and it is the one recording they were present
for. It reaches `last_session_dir` directly rather than through a list, so
it cannot show anything else.

**`browse_sessions()` stays in `viewer.py`,** which is right: the
viewer-only installer runs on a reviewer's own laptop with recordings they
were given. The shared kiosk is the context that made the list a problem,
not the function.

**This is a mitigation, not the answer.** ROADMAP's "Recordings are PII"
entry holds the real question -- where recordings should live when a
machine is shared, and what a student takes away. When a session belongs to
somebody, a list of *their* sessions is exactly the right feature, and this
comes back.

**Cost, stated plainly:** a student who wants last week's recording can no
longer reach it from the kiosk. That is a real loss of function, accepted
because the alternative is every student reaching everyone else's.

---

## 2026-09-13 - A brightness control, live during recording

From the second feedback round: some views (cornea, lens, 90D, ONH) need
more light than others, and they asked for a toggle like the Ion's. Built
as three named steps -- Normal, Brighter, Brightest -- and, on the
developer's decision, **usable while recording**.

**Why that is allowed when nothing else is.** The rule exists so a student
cannot interrupt an irreplaceable capture. This control cannot: it writes a
camera setting, the stream never stops, no file is touched, and the result
is visible in the preview immediately. The alternative -- stop, adjust,
start again -- costs the take, and the views that need adjusting differ
*within* one session, which is exactly when a student cannot afford to
restart.

**Per-instrument mechanism, one vocabulary.** What a step does depends on
what the camera has, because the student judges the picture, not the
method:

| camera | lever | steps |
|---|---|---|
| Keeler BIO | on-camera gamma | 1.0 / 1.6 / 2.4 |
| slit lamp | light: exposure to the frame-rate budget, then gain | 1x / 2x / 4x |
| older BIO | the bridge's brightness offset | 0x08 / 0x20 / 0x38 |
| synthetic | a gain on the rendered frame | 1.0 / 1.6 / 2.4 |

The slit lamp has no gamma node, so it spends light instead -- through
`next_exposure_gain()`, the same function `auto_calibrate()` uses, which
prefers exposure up to the budget and the least gain that will do. A step
therefore cannot exceed the frame-rate budget or the gain ceiling: a
student can reach a picture that looks different, never one that breaks a
recording.

**Level 0 is always the technician's calibration**, and the level is held
by `KioskController` rather than the camera, so switching instruments
reapplies it -- changing instrument must not silently reset a picture the
student was happy with.

**`SyntheticCamera` gained the same three steps**, so the control is
visible and exercisable with no hardware attached. Without that, the one
feature students asked to *touch* would be the one feature a developer
could not see.

**Not yet verified on the instruments.** The numbers come from the
2026-09-13 measurements, but the cameras were unplugged before this was
built. The BIO's gamma steps in particular want a look against a lit
fundus -- the same measurement its default value is waiting on.

---

## 2026-09-13 - Retention is deleted, because nothing will accumulate

**Removed `retention.py`, its tests, `RetentionConfig`, the config parsing,
the Settings group and the startup pass.** 22 tests went with it.

**Why, and it is not that it was wrong.** Retention answered "an unattended
kiosk must not fill its disk over a semester". The answer to where
recordings live is now that they do not live anywhere: a session exists in
a temp buffer while the student is in front of the app, and what they keep
is a file they exported. Nothing accumulates, so nothing needs sweeping.

**It could not have solved the PII problem anyway.** Two of its rules made
that impossible by design: it never touched a session without a
`session.json` -- which is exactly what a crashed recording leaves -- and
it never deleted the newest session. Both are right for "don't fill the
disk" and wrong for "don't keep student images". A sweep at app startup
also cannot run if the app never starts again, which is the case that
matters after a crash.

**What replaces it** is in ROADMAP's "An ephemeral buffer" entry: a temp
buffer cleared at start and exit, a scheduled task created by the installer
for the crash case, and Export as the only way data leaves the machine.

**Keeping it during the transition was the tempting option, and wrong.**
Two mechanisms deciding when recordings disappear is how a recording gets
deleted by the one nobody remembered. This lands in the same commit as the
plan that replaces it.

## 2026-09-13 — Recordings live in an ephemeral buffer, not a folder

**Decided:** Reflex keeps no recordings. Sessions are written to a buffer
under the user's temp directory (`session_buffer.py`), cleared at app
start, at app exit, and by a SYSTEM scheduled task at logon that the
installer registers (`packaging/clear_reflex_buffer.ps1`). What a student
keeps is what they Export. `sessions_dir`, `resolve_default_sessions_dir()`
and settings.py's recordings-folder field are gone with it.

**Why:** every recording contains two students — the one performing and
the peer being examined — and an eye and a face are PII under FERPA. A
folder of recordings on a shared kiosk is a disclosure waiting for whoever
opens it next; removing the browse button (this date) stopped students
seeing each other's sessions but left the pile. Nothing accumulating is a
stronger property than anything accumulating carefully.

**Why three clearing moments, not one:** start and exit cover the normal
case, but an app cannot clean up after its own crash, and "it gets cleaned
when someone next runs the app" leaves a gap of unbounded length. The logon
task is the only one that closes it without the app running.

**Why not RAM.** Measured: a real session is ~1.4 Mbps, about 0.16 GB per
15 minutes — small enough to hold in memory. Rejected anyway. Windows'
pagefile and hibernation write RAM to disk, so "volatile" isn't, while
BitLocker (on by default on these machines) protects a temp file just as
well. An out-of-memory condition would destroy an irreplaceable
in-progress capture, which CLAUDE.md forbids; and a RAM disk means a
third-party kernel driver needing IT approval.

**Why the export default is a removable drive:** the session folder was
the old suggestion, and on a kiosk that folder is about to be deleted —
suggesting it would suggest saving nowhere. `default_export_dir()` picks
the first removable drive, falling back to home.

**The risk this creates, and what answers it:** a student who does not
export loses their recording. That is the point, but it must never be a
surprise, so the summary line says NOT saved yet until an export succeeds,
and closing with an unexported session asks first
(`app.py`'s `_unexported_session()`). Export failing is recoverable — the
buffer still holds the session; only closing is final.

**`clear_buffer()` refuses any path outside temp.** It deletes folders
outright, so being pointed somewhere real is the one thing that must never
happen. The guard caught exactly this in the test suite, where the default
`output_root` is a literal `sessions`.

**Still open:** where recordings should ultimately go (Panopto, pending
four questions for IT — ROADMAP), and what the viewer-only installer is
for now that nothing persists.

---

## 2026-09-13 — Stopping opens the recording; Export asks which drive

**Decided:** two follow-ons to the ephemeral buffer, both about the moment
a recording ends.

**Stopping opens the viewer, and the Watch button is gone.** With a buffer
that dies with the app, reviewing and exporting is not an optional step
after recording — it is the rest of recording, and a student who walks away
from the kiosk without exporting has lost the take. A button they must know
to press is exactly the wrong shape for that. `_review_last_session()` runs
on every stop the student didn't press too: the session time limit, and a
mid-recording failure that still finalized a session.

Two guards. It opens **once per session**, because
`stopped_at_time_limit` stays set until the next recording starts and the
poll tick would otherwise reopen the viewer every 250ms. And it is
**skipped when `session.json` is missing** — finalizing failed badly enough
that the viewer has nothing to read, so the error banner is the whole
story rather than a second failed dialog on top of it.
`_unexported_session()` skips the same case: warning a student they are
about to lose something they were never able to export asks them to do
something they have no way to do.

**Export asks which removable drive when more than one is plugged in.**
`default_export_dir()` took the first by drive letter, which on a kiosk is
a coin toss between a student's own stick and whatever else is in the
machine — and writing a recording of *two* students onto a stranger's
drive is the disclosure this whole design exists to prevent. One drive
still asks nothing. The chooser lists volume label, letter and free space
(`Drive.describe()`); the letter is there because it is the only part
guaranteed unique, so two sticks both called USB stay tellable apart.
Backing out cancels the export rather than picking one.

**Rejected:** letting the save dialog carry it. A student navigating a file
tree to find their own drive is the failure mode, not the fix; the chooser
names the drives in the words written on them.

**Test consequence worth knowing:** a `KioskWindow` left alive keeps
polling, and tests that force `controller.state` (something only
`start_recording()` does for real) make every later tick raise
`KeyError: None`. `test_app.py`'s `_quiesce()` stops the timers; tests that
want a tick call `_poll_tick()` directly.

---

## 2026-09-13 — Brightness is a slider after all

**Decided:** the three brightness buttons become a three-stop slider with
the step's name beside it. This reverses the same-day entry above, which
chose named buttons over a slider.

**Why the reversal:** the original argument was that "brighter" is a
judgement a student makes from the picture and a number is not. That still
holds — which is why the slider is *not* numeric: it carries the same three
words, showing the one it is on. What the slider adds is a sense of how
much room is left, which three buttons never gave: a student on "Brighter"
could not see whether there was more to come without reading the buttons.

**What the implementation had to work around:**

- **Qt's tick marks do not render.** A stylesheet on a `QSlider` replaces
  the whole subcontrol drawing, and Reflex styles the handle, so
  `TicksBelow` draws nothing. The stops are therefore conveyed by the
  slider's *width* and the step name, not by tick marks.
- **The width is fixed at 220px.** Stretched across the window a
  three-stop slider reads as a continuous fine adjustment and leaves large
  dead zones between stops.
- **`reflex_style.TOUCH_SLIDER` gives it a 30px handle**, roughly twice
  the viewer's scrub bar. This is the one control a student uses mid
  recording, with an instrument at their eye, and a small target there is
  the difference between adjusting and fumbling.
- **`_sync_ui()` reflects the level without writing it back** — it runs
  four times a second, and the camera already holds the value. It also
  skips the update entirely while the handle is held down.

**Unchanged:** the levels themselves, which are still integers meaning
whatever each camera's best lever is (gamma on the Keeler, light on the
slit lamp, the bridge's brightness register on the older BIO), and the
control still disappears for a camera offering one level.

---

## 2026-09-13 — Brightness is continuous, and the writes are throttled

**Decided:** the slider is continuous, `set_brightness(amount)` taking 0.0
to 1.0 in place of an integer level. Third change to this control today;
the previous two entries are the record of how it got here.

**The range is measured, not maximal.** 1.0 is exactly where the old
"Brightest" stop was — gamma 2.4 on the Keeler, ×4 light on the slit lamp,
`R21` 0x38 on the older BIO — so continuity adds positions *between*
values that were checked on hardware, and opens no new territory above
them. 0.0 remains the technician's calibration, and nothing goes below it.

**Per camera the amount buys different things**, because that is what each
one has: a `Gamma` node on the Keeler (a tone curve, lifting shadows
without touching the highlight), exposure-then-gain on the slit lamp
(which publishes no gamma at all), and the bridge's brightness offset on
the older BIO. The slit lamp's mapping is geometric — `4.0 ** amount` — so
equal travel is equal stops rather than half the range being one stop.

**Writes are throttled to one per 100 ms** (`app.py`'s
`BRIGHTNESS_WRITE_MS`), because a drag emits values far faster than a
GenICam node write or a USB control transfer should be issued on a stream
that may be recording. Throttling is in the UI, not the camera: cameras
should not know about drag rates, and `kiosk.py` has no Qt. Two properties
the implementation must keep — the **first** move applies immediately, so
the picture responds the instant they touch it, and the **last** always
applies, so where a student lets go is always what the camera ends on.
`_sync_ui()` therefore skips its reflection while a write is pending, since
the controller is behind the slider by up to one interval.

**What it cost:** the words. "Brighter" was a picture a student could
judge; 62% is not. Only the ends keep names, and those two are the ones
that mean something — the calibration, and the most this model gives.

**The risk accepted:** the top of the slit lamp's travel is a ~33 ms
exposure at 30 fps, a full frame period of motion blur on the hand
movement the recording exists to capture. That was equally true of the old
"Brightest" stop, but a student could see they were at the last stop and
now cannot. If this bites in practice, the answer is to show the ceiling,
not to narrow the range.

---

## 2026-09-14 — The save question belongs at viewer close

**Decided:** when the student closes the auto-opened viewer without having
exported, ask right there — "Save it now" or "Discard it" — and take
"Save it now" straight back into the viewer. The app-close prompt stays
only as a backstop for a session nobody was ever asked about.

**Why:** the prompt fired when the student closed the *app*, which is
after they have closed the viewer. By then the question was unanswerable:
the viewer is the only place Export lives and there is no way to reopen
it, so the warning told them they were about to lose something and offered
no means of not losing it. That is the same failure this codebase already
rejected for manifest-less sessions — never ask a student to do something
they have no way to do.

It also still said "press Watch Last Recording, then Export video", naming
a button deleted earlier the same day when stopping began opening the
viewer by itself. A prompt that names a control that does not exist is
worse than no prompt: it reads as the app being broken.

**Why not simply reword it:** the timing was the defect. Closing the viewer
is the moment the recording is actually at risk, and it is the moment the
student still has the means to act.

**Asked once, not twice.** A deliberate discard is remembered
(`_discarded`), so app close does not raise it again. Being asked the same
question twice teaches students to click through it, which is how the one
that matters gets missed.

**Everything unsafe is on the far side of an explicit choice:** "Save it
now" is the default button, the escape key, and what closing the dialog
outright does — `clickedButton()` is then None, which is not `discard`.

**Consequence for tests:** anything that lets a session finish now meets
this modal. `test_app.py`'s `_answer_save_question()` stands in for it; a
test that patches `app.open_session` but forgets this one hangs.

---

