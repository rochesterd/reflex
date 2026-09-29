# Camera setup after an install

For the technician setting up a clinic machine, after the installer has
run. Takes about 15 minutes for a two-instrument room. No imaging or
photography knowledge is needed — everything you have to judge is written
on screen in plain numbers.

Do this **at the instruments**, switched on and set up the way a student
will use them: the app records what the camera sees, and it can only be
set up correctly against a real view.

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
| `<-- at the gain ceiling: more gain would only add grain. Add light at the instrument.` | The camera has hit the most gain its sensor can use cleanly. Past it the picture fills with flickering coloured grain that no setting removes. Add light and press Auto-Calibrate again. |

Normal gain depends on the instrument. The slit lamp should sit at 1–2x (its usable ceiling is ~3.3x of
a 4x maximum; reaching it means too little light); the BIO's dimmer view runs in the teens of its 25x. Grain wants light, never gain.

**If the picture is still too dark after Auto-Calibrate**, use the Preview's
**Picture** box, applied live. With **Apply Reflex's picture processing** *unticked*
(the default) you see the camera's own picture — get that right first: set
**Auto-Calibrate exposes for** to *the whole view* and calibrate again; set **Frame
rate this camera must keep** to 15 fps (twice the light, some blur on this view only)
and calibrate again. Only then tick the processing on and compare: **Steady the dark
tones** (frame averaging; turn it down if moving hands smear), **Dark tones**,
**Shadow lift**, and **Remove the sensor's own black level**. Save keeps what you leave.

### 6. Close Preview and repeat for the other instrument

Close the Preview window. Repeat steps 3–5 for the second instrument.

The older BIO has no Auto-Calibrate and no sliders: check picture and focus, and close.

### 7. Save

Press **Save**. Nothing you did in Preview is stored until you do.

Save refuses if one camera is assigned to two roles, and warns if a role
is empty — an empty role means students won't see that instrument.

### 8. Restart the recorder, with the room in its normal state

Close Settings and start the recorder app from its Desktop shortcut.

**Have the room lit normally and the third-person camera aimed at the
student's working position before you launch it.** That camera fixes its
own exposure in its first couple of seconds and holds it all session; if
the room lighting changes a lot later, restart the app.

### 9. Do one real test recording

This is the step that actually proves the install:

1. Pick an instrument. The status line should start with **Ready. Press
   Start Recording.**
2. **Glance at both panes before you press Start Recording.** They're live.
   A black instrument pane means its illumination is off — the app won't
   stop you. This is the one check left to the eye; tell instructors.
3. Press **Start Recording**, and spend 20–30 seconds doing the real
   skill — hands moving, beam moving.
4. Press **Stop Recording**. The recording opens by itself; closing it
   unexported asks whether to save or discard.
5. Check both panes: right way up, in focus, bright enough to see what the
   hands and the optics are doing, and moving smoothly.
6. With a USB drive plugged in, press **Export video...** and save to it —
   how students keep a recording, so it is part of the test.

Repeat for the second instrument. If both play back well, the room is
ready.

**Tell instructors:** Reflex keeps nothing. A recording lives only until
the app is closed; a student who does not Export loses it.

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

Settings is a normal, repeatable tool; running it again is always safe. A
recalibration costs minutes; a stale one costs students recordings.
