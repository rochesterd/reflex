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

- **Confirm the BIO's field metering against a model eye.** The p95 / 185
  rule (DECISIONS 2026-09-14) was chosen on a glossy box. Re-run the gain
  sweep on a fundus target, check the lit fraction through a real pupil,
  and check noise at the gain it picks. Adjust the numbers or record that
  they held.
- **Confirm the resting tone curves on real subjects** (DECISIONS
  2026-09-17). Both are set and both provisional: the slit lamp's (gamma
  1.8, 12-bit, digital black 0.025 per gain) was chosen on a matte-black
  focus rod, the Keeler's (1.5) on skin. Check each on an eye / a fundus,
  at full resolution for noise as well as brightness;
  `tools/measure_picture.py <serial> gamma ... --calibrate` re-sweeps one
  in seconds. No recalibration is needed after a change: Auto-Calibrate
  meters with the curve off. The Keeler's LUT only if one exponent proves
  too blunt.
- **Hands camera exposure.** Today the two-second warmup's result is frozen,
  so every session starts from whatever the room looked like. Have
  `settings.py` record the converged value at calibration time and
  `uvc_camera` apply it at every start — a per-room value, so `config.json`
  is its right home.

### Phase 2 — built; see Phase 1's first step for what remains

Higher bit depth plus a host tone curve for the slit lamp, the one camera
that cannot curve for itself, landed 2026-09-17 after the first clinic
calibration showed room-lit backgrounds going black. Still open here:
whether the slit lamp's Brightness slider should drive that curve instead
of spending light, and the lifted floor's slight purple cast (the floor
is uneven per channel; `SetDigitalBlack` subtracts one master value).

**Stop conditions worth stating up front:** a measurement that shows no
improvement ends that step, and the DECISIONS entry records the numbers
that killed it. Inheriting the vendor's value is a legitimate outcome for
the legacy BIO. "We could set it" was never the argument; "the picture is
better and we can show it" is.

---

## 2026-09-17 — Uploading a session to Panopto (planned, not built)

Replaces the 2026-09-13 "where exported recordings should ultimately go"
entry: the destination is settled. The Notion brief ("Recording Storage &
Access Decision Brief") owns the institutional half and is the source for
anything policy-shaped; this is only what Reflex builds. **No credentials
yet — all of it is written to be built and tested without them.**

**Shape.** An upload sits where the drive export sits: after record, after
verification, and the buffer is released only once it succeeds. It is
*not* `export_session()` pointed elsewhere — Panopto takes both streams as
one session with an offset manifest, so no composite is rendered on this
path. `compositor.py` stays for the drive.

**Boundary, the rule drawn around the IDS SDK.** Nothing outside these
modules may import an HTTP client or know a token exists:

- `panopto_api.py` — OAuth and REST. Knows HTTP, not sessions.
- `panopto_upload.py` — takes a `Session` and a folder, returns the new
  Panopto session's URL. Same `progress_cb`/`cancel_cb` signature as
  `export_session()`, so `viewer.py`'s `_ExportWorker` drives either
  without learning a second shape.
- `config.py` gains a `panopto` section (host, client id/secret, parent
  folder, access model), validated at load like the rest — a missing
  credential fails before `QApplication` exists.

**Don't invent the API.** The brief verified these endpoints exist; the
request and manifest formats are *not* verified. Write them from the real
spec or a captured exchange, and mark anything guessed — the rule that
governs `vendor/ids_peak_api.txt` applies here too.

**Phase 1 — upload, no identity.** One parent folder, service account,
every session lands there; builds end to end without the sign-in question
being answered. Failure is loud and in-session: a warning, a Retry, and
the session stays unexported so `app.py`'s `_unexported_session()` refuses
a silent discard on close. No retry queue and nothing persisted past exit
— the buffer's rules do not change.

**Phase 2 — identity and filing.** Kiosk sign-in, then the student's own
folder made on first visit with a view-only grant. Gated on whether Canvas
is in that path at all, and on access model A vs B, which decides whether
per-student folders exist.

**Testing without a site.** A local double at the `panopto_api.py` seam is
part of phase 1, not an afterthought — the reason `SyntheticCamera`
exists. Record a two-stream session with `SyntheticCamera`, upload it to
the double, assert what was sent. `tools/panopto_probe.py` is then the
first thing to run when tokens arrive: auth, list folders, create one,
grant, upload one short session, print what came back.

**Still not to be built:** audio, the email notification, and any
identifier feature. Unchanged from the entry this replaces.
