# Camera setup after an install

For the technician setting up a clinic machine, after the installer has
run. Takes about 15 minutes for a two-instrument room. No imaging or
photography knowledge is needed — everything you have to judge is written
on screen in plain numbers.

Do this **at the instruments**, with them switched on and set up the way a
student will actually use them. That is the whole reason this step exists:
the app records what the camera sees, and it can only be set up correctly
against a real view.

You need:

- Every camera plugged in.
- Both instruments powered, with their illumination on.
- Someone (or a practice/model eye) to look at through each instrument.
- The room lit the way it is during a normal session.

What each camera needs:

| Camera | Needs calibrating | Why |
|---|---|---|
| Slit lamp | **Yes** — Auto-Calibrate | It has no automatic exposure of its own. |
| BIO (newer Keeler, digital) | **Yes** — Auto-Calibrate | It has automatic exposure, but it runs the instant a student taps the instrument on the picker — before they've picked up the instrument or switched its light on. Calibrating here replaces that guess with a real measurement. |
| BIO (older Keeler) | No — there is no button | Exposure is handled inside the camera itself, where no software can reach it. Nothing is missing; there is genuinely nothing to set. |
| Third-person (webcam) | No | It sets its own exposure in the first two seconds after the recorder app starts. See step 8. |

---

## The procedure

### 1. Open Settings

Start menu → **Reflex Settings**. You'll see one row per
camera: **Slit Lamp**, **BIO**, **Third-Person**.

If a row's dropdown is empty or missing a camera, plug it in and press
**Rescan**.

### 2. Fill in each row: device, profile, name

An instrument row has three fields. You choose the first two and type the
third.

| Field | What to do |
|---|---|
| **Device** | Pick the camera that's plugged in. Not sure which physical camera an entry is? Press **Preview** and look. |
| **Profile** | Which instrument this is. Picking the device usually fills this in for you — check that it's right. |
| **Name** | What students see on the button in the recorder. It fills in from the profile ("Slit Lamp", "BIO"); change it if your students call it something else. |

The third-person row has no profile — any webcam works — so it's just the
device.

**Read the line under the row.** It tells you what that instrument needs,
for example that the slit lamp has no automatic exposure of its own. If it
says the profile usually reports a different camera model, you've most
likely got the device or the profile wrong — check both, then carry on if
you're sure; it won't stop you saving.

**If your camera isn't in the profile list,** choose **Custom (unlisted
camera)** and type a name yourself. Everything else works the same; the app
just has nothing pre-set for that model, so check the picture is the right
way up in Preview.

### 3. Set the instrument up for real — before you calibrate

Go to the instrument and:

- Switch its illumination on and set it to the brightness students are
  taught to use.
- Put a subject in front of it and get a normal working view — the beam on
  an eye for the slit lamp, a fundus reflex for the BIO.

**Make a note of that illumination setting, and tell instructors it's the
one to teach.** The calibration you're about to do is matched to it. Wildly
different settings will give students a dark or washed-out recording.

### 4. Preview, and focus

With that view live, press **Preview** on the instrument's row.

- Confirm it's the right camera, and that the picture is the right way up.
- Focus the instrument until the picture is sharp, then **tighten the lens
  focus ring** so it can't drift. There is no autofocus on these cameras.

### 5. Press Auto-Calibrate, then read the line underneath it

The dialog shows a line like:

```
Calibrated.   exposure 12.5ms   |   gain 2.6x of 4.0 max   |   allows ~80fps
```

That line is the whole check. Compare it against this:

| What it says | What to do |
|---|---|
| `allows ~30fps` or higher | Good. Move on. |
| `<-- BELOW the 30fps recording target, and blurs motion. Add light at the instrument.` | **Don't accept it.** Turn the instrument's illumination up, or open its aperture, and press Auto-Calibrate again. If you save this, students get juddery, blurred recordings of exactly the hand movement they're trying to study. |
| `Couldn't reach target brightness automatically` | Use the **Exposure** slider by hand: raise it until the bright part of the picture looks bright but not washed out to white. Keep **Gain** as low as you can — gain adds grain. |

A high gain number (say, above half the "of X max" figure) with plenty of
light available usually means the instrument's illumination is lower than
it needs to be. More light at the instrument always beats more gain.

### 6. Close Preview and repeat for the other instrument

Close the Preview window. Repeat steps 3–5 for the second instrument.

On the older BIO there is no Auto-Calibrate button and no sliders — that's
expected. Just check the picture and the focus, and close.

### 7. Save

Press **Save**. Nothing you did in Preview is stored until you do.

Save refuses if the same camera is assigned to two roles, and warns you if
a role is left empty — an empty role means students won't see that
instrument at all.

### 8. Restart the recorder, with the room in its normal state

Close Settings and start the recorder app from its Desktop shortcut.

**Have the room lit normally and the third-person camera aimed at the
student's working position before you launch it.** That camera fixes its
own exposure in the first couple of seconds and then holds it for the whole
session. If the room lighting changes a lot later (blinds opened, lights
switched off), restart the app.

### 9. Do one real test recording

This is the step that actually proves the install:

1. Pick an instrument. The status line should start with **Ready. Press
   Start Recording.**
2. **Glance at both panes before you press Start Recording.** They're live. If the
   instrument pane is black, its illumination is off or turned right down —
   the app won't stop you, and you'd record a black pane. This is the one
   check that's left to the eye, because a black picture is the most
   obvious thing on the screen; worth passing on to instructors as
   something to tell students.
3. Press **Start Recording**, and spend 20–30 seconds doing the real
   skill — hands moving, beam moving.
4. Press **Stop Recording**, then **Watch Last Recording**.
5. Check both panes: right way up, in focus, bright enough to see what the
   hands and the optics are doing, and moving smoothly.
6. With a USB drive plugged in, press **Export video...** and save to it.
   This is how students keep a recording, so it is part of the test.

Repeat for the second instrument. If both play back well, the room is
ready.

**Tell instructors this, because students need to hear it:** Reflex keeps
nothing. A recording lives only until the app is closed, and a student who
does not Export to their own drive loses it. The app warns before closing,
but the habit is what matters.

---

## If the recorder won't let you press Start Recording

The status line always names the reason. The common ones:

| Status line | What it means |
|---|---|
| "Select an instrument to begin." | Nothing picked yet. |
| "…the picture is frozen — the camera is on but not seeing anything." | The camera is delivering the same image over and over. Unplug and replug it; if it persists, that camera has a fault. |
| "Waiting for: cameras live…" | A camera isn't running. Check it's plugged in, then restart the app. |
| "Waiting for: free disk space…" | Not enough room for a session. Clear space on the machine's drive. |

---

## When to do this again

Re-run the whole procedure whenever:

- A camera is replaced, moved, or refocused.
- An instrument's illumination is serviced, or its bulb is changed.
- The room's lighting is changed substantially.

Settings is a normal, repeatable tool, not a one-time installer — running
it again is always safe. Re-doing a calibration costs a couple of minutes;
a stale one costs students recordings they can't use.
