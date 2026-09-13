# Roadmap

Open plans only — intent before implementation, expected to change as
reality pushes back. The commit that adds a plan's `DECISIONS.md` entry
deletes it from here; for plans already built, see DECISIONS.md's
2026-09-11 "Retired ROADMAP entries" entry. Newest at the bottom.

---

## 2026-08-18 — Other camera/recording settings surveyed, not acted on (yet)

Recorded so they aren't re-derived; neither is committed work.

- **Recording quality** (`libx264`, `crf=23`) is hardcoded in `Recorder`'s
  defaults — a real quality-vs-disk tradeoff that could differ per
  institution. Move it to `config.json`'s `recording` section if a need
  shows up.
- **`kiosk.py`'s constants** (session length, stall timeout, space
  multiplier) are constructor parameters with measured defaults,
  deliberately not config: config for a hypothetical isn't earned.

---

## 2026-08-26 — Health-check tool surveyed, not acted on (yet)

A technician-facing "Doctor": one green/yellow/red report gathering
diagnostics that exist but are scattered and reactive — the installer's SDK
check, `config.py`'s validation, `settings.py`'s "not connected", and
`kiosk.py`'s disk preflight, each of which only speaks when its own moment
arrives.

**Shelved** — possibly overboard at this scale; revisit if
diagnostic-hunting becomes a recurring problem. If built it stays
diagnosis-only, per CLAUDE.md's "loud and early".

---

## 2026-09-13 — An ephemeral buffer, not a sessions folder

**Every recording contains two students** — the one performing and the peer
being examined. An eye and a face are PII, so a nickname solves nothing;
the optional-identifier plan is folded in here.

**The goal, decided 2026-09-13:** nothing containing PII outlives the app.
The kiosk holds a recording only while the student is in front of it, and
what they take away is a file they exported deliberately. There is no
sessions folder, no accumulation, and therefore nothing to sweep —
`retention.py` is already deleted.

### What replaces it

- **A buffer under the user's temp directory**, one folder per session, in
  place of `%PUBLIC%\Documents\Reflex\sessions`. `sessions_dir`, its
  Settings field and `resolve_default_sessions_dir()` all go.
- **Cleared at three moments:** app start, app exit, and — because an app
  cannot clean up after its own crash — a scheduled task created by the
  installer, running at logon. That third one is the answer to "what if it
  crashes and nobody opens it again".
- **Export becomes the deliverable.** The viewer's Export already renders a
  composite MP4 with a partial-file discipline; it gains a destination
  chooser, defaulting to a removable drive. For testing that is a student's
  own drive; later it is Panopto.
- **Watch Last Recording is unaffected** — the buffer exists for as long as
  the app does.
- **The disk preflight points at the buffer**, so a full temp drive still
  refuses Start rather than failing mid-session.

### The part that needs care

**A student who does not export loses the recording.** That is the point,
but it must never be a surprise: say it on Stop, say it again on close, and
do not let a window close quietly on an unexported session. A student who
loses a take to a dialog they did not read has been failed by the app, not
by the policy.

**Export failing is the recoverable case** — no drive, wrong drive, full
drive. The buffer still holds the session, so the answer is "try again",
and only closing the app is final.

### Still to confirm with IT, for the Panopto destination

1. Does NECO have Panopto, and does it cover this use?
2. Can a kiosk get an API credential, and is a service account acceptable?
3. What does the assignment-folder permission model allow?
4. What is the quota, against ~750 MB per 15-minute session?

A service account lands every recording under one identity, which brings
the identifier question straight back. Panopto is the better fit than
Canvas: it is built for multiple simultaneous feeds, which is what a
session is, where Canvas would mean one composited file.

**Open question worth answering before building:** if recordings never
persist and students keep exported composites, what is the viewer-only
installer for? A composite plays in any media player.

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
