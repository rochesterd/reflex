# SETUP.md

**Developer/source-checkout setup**, for working on Reflex's source. A
clinic machine gets the installer built via `PACKAGING.md` instead — see
DECISIONS.md's 2026-08-20 "Frozen-exe installer built" entry for why those
are two audiences.

One-time per dev machine, from bare Windows to both cameras enumerating.
Follow the steps in order; the reasons live in `CLAUDE.md` and
`DECISIONS.md`, not here.

**Shortcut:** `setup.ps1` (repo root) scripts Section 1 and the
`requirements-ids.txt` half of Section 2, and is safe to re-run. Read
Sections 2-3 only if it says the IDS peak SDK runtime still needs
installing. `python setup_wizard.py` runs the same script behind a GUI (it
works with the system Python, before the venv exists). Neither replaces
Section 6 (`settings.py`), which stays a separate, manual step.

---

## 1. Python and the virtual environment

1. Install **Python 3.13** (get the Windows installer from python.org). Not
   3.11 — see the "Python 3.13, not 3.11" entry in `DECISIONS.md` if you're
   tempted to match an old wheel tag; 3.13 works fine with those wheels.
2. From the repo root, create the venv:
   ```powershell
   python -m venv .venv
   ```
3. Activate it (PowerShell):
   ```powershell
   .venv\Scripts\Activate.ps1
   ```
   If PowerShell refuses to run the script (execution policy), run this
   once first, then retry activation:
   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
   ```
4. Install the project's Python dependencies:
   ```powershell
   python -m pip install -r requirements.txt
   ```

At this point `python app.py --synthetic` and `python settings.py
--synthetic` both run with no hardware -- the latter offering fake cameras
(two matching device profiles, one matching none) through the real window.
The rest of this file is only for the real cameras.

---

## 2. IDS peak SDK (required for both cameras)

The Keeler (`U3-327xCP-C`) needs only this section. The slit lamp camera
also needs Section 3 — read it first, since it decides step 2's variant.

1. Create a free myIDS account at ids-imaging.com if you don't have one.
2. Download the **IDS peak** Windows installer and run it. IDS ships three
   setup variants — standard, runtime, and extended:
   - If this machine will drive the slit lamp camera (`UI-3250CP-C-HQ`, a
     uEye-family model), choose the **extended** setup — it bundles the
     uEye camera drivers that model needs. Read Section 3 first if this
     machine already has IDS Software Suite installed.
   - Otherwise (Keeler only), **standard** is enough.
   - Whichever variant, choose **Custom** during setup and enable the
     **uEye Transport Layer** component. If you're not sure which machine
     this is, enable it anyway; it's a no-op for the Keeler.
3. Install the Python bindings from PyPI, pinned to match the installed
   SDK runtime. It's a separate requirements file so machines without IDS
   peak can still install everything else (`DECISIONS.md`'s 2026-08-12
   entry):
   ```powershell
   python -m pip install -r requirements-ids.txt
   ```

---

## 3. Slit lamp camera only: uEye camera drivers

The Haag-Streit BI 900's camera (`UI-3250CP-C-HQ Rev. 2`) is a legacy
uEye-family camera, not native USB3 Vision, so it needs uEye camera
drivers the Keeler doesn't. Skip this whole section on a machine that only
drives the Keeler. There are two ways to get the drivers — pick one before
you install Section 2:

- **Extended setup** (Section 2, step 2): bundles the uEye camera drivers
  directly, so no separate install is needed. If you're choosing this,
  nothing else in this section applies. **Watch for:** running the
  extended setup automatically **uninstalls an existing IDS Software
  Suite** on that machine, if one is present — don't install the Suite
  first and then switch to extended expecting to keep both.
- **Standard/runtime setup + IDS Software Suite**: if you installed
  standard or runtime instead, install **IDS Software Suite, version 4.94
  or later**, in addition to IDS peak. uEye camera drivers aren't bundled
  with standard/runtime IDS peak — without the Suite, the `UI-` camera
  never shows up at all, regardless of Section 2.

Either way, **the uEye Transport Layer** (Section 2, step 2 — Custom
setup) is what makes a `UI-` model camera appear on the same GenICam
interface IDS peak otherwise reserves for native USB3 Vision devices like
the Keeler. If you already ran the default (non-Custom) installer, re-run
the IDS peak installer, choose Modify, and enable the component there.

**Watch for:** the uEye Transport Layer only exposes a basic feature set —
freerun/triggered acquisition, exposure, pixel clock. If the camera module
for this camera ever needs something beyond that (advanced trigger modes,
certain GenICam features the Keeler's native path exposes fine), that's
the first thing to suspect, not a bug in this codebase.

---

## 4. Verify the install

`ids_camera.list_ids_devices()` and `settings.py` (Section 6 below) are
the normal way to see what's attached once the app itself works. Before
that, to check the SDK/runtime install in isolation, run:

```powershell
python tools\check_ids.py
```

(standalone — it imports `ids_peak` directly, none of this project's
camera modules). Two checks, depending on the machine:

**Check 1 — bindings/runtime match, any machine:** the script should run
to completion without raising. On a development machine with no cameras
attached, **0 devices found is the expected, correct result** — it means
the bindings loaded and matched the installed runtime.

**Check 2 — camera enumeration, machine with hardware attached only:**
expect exactly 2 devices, the `UI-3250CP-C-HQ` and the `U3-327xCP-C`, each
with a serial number printed. (If `tools\check_ids.py` errors on a method
name, check the locally-installed API docs under Start Menu → IDS → IDS
peak — they're versioned with the SDK, unlike anything on the web.)

**Confirm by serial number, not by list position** — index order changes
across reboots and USB ports (`CLAUDE.md`'s Architecture section).

---

## 5. Troubleshooting: camera enumerates but the frame rate looks wrong

If a camera shows up in Section 4 but frames arrive slower than expected
once you're actually capturing, **suspect bandwidth or exposure before the
driver.** USB3 Vision degrades by silently dropping frames rather than
raising an error. Only one instrument camera streams at a time, so the
real load is one instrument plus the third-person webcam — at most about
115 MB/s, the slit lamp at 12-bit — against a realistic 350-400 MB/s per USB 3.0
host controller, and that combination has measured clean (see
`SUPPORTED_HARDWARE.md`'s bandwidth table). A low frame rate with no
errors usually means something else shares the controller, or an exposure
longer than the frame-rate budget (the "allows ~Nfps" figure in
`CALIBRATION.md` step 5).

Before reinstalling anything:

- Put the instrument camera on its **own host controller** if the machine
  has more than one, and keep other USB 3.0 devices off it.
- Treat **measured throughput as authoritative over datasheet numbers**.
- Keep `recording.fps` at 30 rather than a camera's native ~58-60fps
  ceiling; the bandwidth figures above assume it.

---

## 6. Assign roles with settings.py

Once the SDK/driver checks above pass, run:

```
python settings.py
```

Each row (Slit Lamp, BIO, Third-Person) shows a dropdown of currently-
detected candidates. Pick the right device for each role, give the two
instrument rows a label (what the picker button on the kiosk screen will
say), and use **Preview** on a highlighted selection if you're not sure
which physical camera it is. **Rescan** re-checks what's attached (e.g.
after plugging in a camera that wasn't connected yet); **Save** writes
`config.json`. `app.py` refuses to start with a clear error if
`config.json` is missing or malformed — see `config.py`.

Re-run `settings.py` any time a camera is replaced or a setting needs to
change; restart `app.py` afterward, since Save does not hot-reload.

Instrument cameras have no autofocus — once focus looks right in Preview,
tighten the lens focus ring so it can't drift during a session.

Role assignment is only half of what a room needs: each instrument camera
also has to be calibrated against a real view through it. Follow
`CALIBRATION.md` — the technician's procedure works the same on a dev
machine with hardware attached.
