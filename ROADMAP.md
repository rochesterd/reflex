# Roadmap

Open plans only — intent before implementation, expected to change as
reality pushes back. The commit that adds a plan's `DECISIONS.md` entry
deletes it from here; for plans already built, see DECISIONS.md's
2026-09-11 "Retired ROADMAP entries" entry. Newest at the bottom.

---

## 2026-08-18 — Other camera/recording settings surveyed, not acted on (yet)

Surfaced alongside the UVC autofocus/auto-exposure lock (see DECISIONS.md);
recorded so it isn't re-derived later, not committed work.

- **Recording quality (`codec`/`crf`/`preset`)** is hardcoded in
  `Recorder`'s defaults (`libx264`, `crf=23`), never wired to
  `config.json` — the category `fps` was in before it moved, and a real
  quality-vs-disk tradeoff that could differ by an institution's storage
  budget. Move it to the `recording` section if a real need shows up.
- **`MAX_SESSION_MINUTES`, `DEFAULT_STALL_TIMEOUT_S`,
  `REQUIRED_SPACE_MULTIPLIER`** (`kiosk.py`) are constructor parameters
  with measured defaults, deliberately not config: nobody has needed a
  different value, and config for a hypothetical isn't earned.

---

## 2026-08-26 — Health-check tool surveyed, not acted on (yet)

A technician-facing "Doctor": one glanceable green/yellow/red report
consolidating diagnostics that exist but are scattered and reactive — the
SDK version check buried in `packaging/reflex.iss` (install-time only),
`config.py`'s validation (only when `app.exe` launches), `settings.py`'s
"not connected" detection (only if a technician opens it), `kiosk.py`'s
disk preflight (only just before Start).

**Shelved** — possibly overboard at this scale; revisit if
diagnostic-hunting becomes a real recurring problem, not preemptively. If
built it stays diagnosis-only (no silent auto-fix), per CLAUDE.md's "loud
and early" — the one plausible exception being a technician-clicked
"re-run the IDS peak install", which only re-exposes what
`InstallIdsPeakSilently()` already does once, safely.

---

## 2026-09-13 — Recordings are PII, and they pool on a shared machine

Raised while answering the second feedback round's audio question, and it
turned out to be the larger issue. **Every recording already contains
identifiable images of two students** — the one performing the skill and the
peer acting as patient. An eye and a face are PII; a nickname does not
change that, which is why the "optional identifier" plan is folded into
this entry rather than kept separately.

**The exposure that exists today, before any new feature.** Sessions are
written to one folder on the kiosk (`%PUBLIC%\Documents\Reflex\sessions`),
and **Watch Past Recordings lists all of them to whoever is standing
there**. Any student can watch any other student's session. Nothing in the
app scopes a recording to the person who made it.

**Direction, from the developer 2026-09-13:** stop pooling recordings in a
local folder; have each student take their own away — a personal flash
drive is the current thinking. The workflow questions are real and
unanswered: what happens when the drive is absent or full mid-session, who
owns the `sessions_dir` default, whether anything may remain on the kiosk
between sessions, and what the app should do about recordings already
sitting there.

**What must not be built before that is settled:** audio (it adds voices to
data we cannot yet place correctly), and any identifier feature (it makes
recordings more findable while the storage question is open). Neither is a
technical blocker; both would deepen a problem we have not solved.

**Worth considering as immediate mitigations,** each small and independent:

- Disable or remove **Watch Past Recordings**, leaving Watch Last Recording
  for the session a student just made. One flag; removes the cross-student
  browsing entirely.
- Make `sessions_dir` a removable drive, and refuse to start when it is
  absent — the disk preflight already has the shape for this.
- Retention: today's opt-in cleanup is a blunt instrument for this, but a
  "clear the kiosk between students" pass is the same machinery.

The full analysis the developer describes — everything this app can capture,
and how each piece must be handled — belongs in its own DECISIONS entry
once NECO's requirements are known. This entry is the placeholder, and the
statement that the app is not currently built for the answer.

---

## 2026-09-12 — Use what each camera's stack actually offers (planned, not built)

Replaces the separate "legacy BIO picture registers" and "shadow detail on
the slit lamp" entries: they were the same question asked of two devices.
`IMAGING.md` lists what each stack exposes; this closes the distance between
that and what Reflex writes. Order is by cost, and three of the four
candidates cost nothing at all.

**Rules for the whole program.** Every step is a measurement before it is a
change, and each lands separately so a regression has one suspect. Anything
that turns out to be a per-model fact becomes a `DeviceProfile` field, not a
technician knob — CLAUDE.md's ownership table — and `config.json` gains an
override only where the answer is genuinely per-room. Nothing here may gate
Start: every one of these is visible in the live preview, and the
technician's test recording is the backstop.

### Phase 1 — the free writes, one at a time

Each is a value the hardware already has and Reflex has never written.
Measure the frame before and after (median, clipped fraction, p99.9) and
keep the numbers in the DECISIONS entry.

- **Digital BIO gamma, then its lookup table.** The camera can apply the
  tone curve that would rescue its shadows, on board, for no bandwidth and
  no host CPU. Gamma first because it is one number; the LUT only if one
  exponent proves too blunt.
- **Legacy BIO picture registers.** Confirm what each of `R20`-`R25` does
  before changing any: we inherit five values from a vendor capture and
  have checked none. Then own them in a profile rather than replaying
  constants. While the camera is attached, also re-derive the START/STOP
  split — those four writes are picture registers and cannot stop a bridge,
  so something else did.
- **Hands camera exposure.** Today the two-second warmup's result is frozen,
  so every session starts from whatever the room looked like. Have
  `settings.py` record the converged value at calibration time and
  `uvc_camera` apply it at every start — a per-room value, so `config.json`
  is its right home.

### Phase 2 — only if Phase 1 is not enough

Higher bit depth plus a tone curve, and only for a camera that cannot curve
for itself -- which now means the slit lamp alone, since the Keeler's own
gamma does the job on board. Phase 0 measured the cost: both cameras hold
30fps at 12-bit with nothing dropped, for 2x the bandwidth and about 15
more points of one core.

**The part that is not optional:** `_grab()` converts to BGR8 the moment a
buffer arrives, so capturing 12-bit and changing nothing else delivers the
same 248 levels as 8-bit -- measured, not predicted. The curve has to be
applied *in* that conversion, with `ids_peak_ipl`'s `GammaCorrector` (it
has `SetDigitalBlack` too), or the extra depth is thrown away a line later.
If Phase 1 recovers the shadows, write that down and stop here.

**Stop conditions worth stating up front:** a measurement that shows no
improvement ends that step, and the DECISIONS entry records the numbers
that killed it. Inheriting the vendor's value is a legitimate outcome for
the legacy BIO. "We could set it" was never the argument; "the picture is
better and we can show it" is.
