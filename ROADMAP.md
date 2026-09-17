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

## 2026-09-13 — Where exported recordings should ultimately go

The buffer itself is built (DECISIONS 2026-09-13): nothing outlives the
app, and a student keeps what they Export. **For testing that export goes
to their own flash drive, which is a stopgap, not the answer** — handing a
student the file discloses their peer's image to them and takes it outside
any retention, deletion or breach process NECO governs.

**Intended destination: Panopto.** Institutional systems solve the
governance half — access control, retention, audit, and an owner. Panopto
looks the closer fit than Canvas: it is built for *multiple simultaneous
feeds*, which is exactly what a session is, and its assignment folders give
each student a space only they and instructors see. Canvas would most
likely mean one composited file, and students would lose the layout picker
and independent angles.

**Blocking questions for IT, none of them technical for us:**

1. Does NECO have Panopto, and does it cover this use?
2. Can a kiosk get an API credential, and is a service account acceptable?
3. What does the assignment-folder permission model actually allow?
4. What is the storage quota? Budget ~0.16 GB per 15-minute session as
   measured, not the 750 MB the disk preflight reserves.

A service account is simplest but lands every recording under one identity,
which brings the identifier question straight back. Per-student login at
the kiosk attributes correctly and is heavier for an unsupervised student.

**The engineering is the same either way**, and already half-built: export,
verify, and only then let the buffer go. Keep the destination pluggable — a
drive and an upload are the same operation with a different target.

**Still not to be built until this settles:** audio (it adds voices to data
we cannot place correctly) and any identifier feature (a nickname does not
make a face less identifying).

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
- **Choose the two resting tone curves.** The mechanism is built
  (DECISIONS 2026-09-17) and both `DeviceProfile.gamma` values are still
  `None`. With the camera on the dev laptop and a real view:
  `tools/measure_picture.py <serial> gamma 1.0 1.4 1.8 2.2 2.6`, adding
  `--pixel-format BayerRG12` for the slit lamp. Pick by eye for noise as
  well as by number, then set `gamma` (and the slit lamp's
  `pixel_format`) in its profile. Recalibrate afterwards: a curve changes
  what Auto-Calibrate meters. The Keeler's LUT only if one exponent
  proves too blunt.
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
of spending light, and `GammaCorrector.SetDigitalBlack`, untried.

**Stop conditions worth stating up front:** a measurement that shows no
improvement ends that step, and the DECISIONS entry records the numbers
that killed it. Inheriting the vendor's value is a legitimate outcome for
the legacy BIO. "We could set it" was never the argument; "the picture is
better and we can show it" is.
