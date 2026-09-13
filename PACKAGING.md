# PACKAGING.md

How to build Reflex's two distributable installers — the clinic-machine
one and the viewer-only one (see "Two installers" below). This is a
**developer-only** procedure — nobody installing Reflex does any of
this, they just run the `.exe` it produces. See DECISIONS.md's 2026-08-20
"Frozen-exe installer built" and 2026-09-02 "Recorder/Viewer split, phase
4: two installers" entries for why these exist.

For setting up a *development* machine to work on Reflex's source
instead, see `SETUP.md` — that's a different audience and a different
procedure.

---

## 1. Build-only tooling

From an activated `.venv` that already has `requirements.txt` installed
(see `SETUP.md` Section 1):

```powershell
python -m pip install -r requirements-packaging.txt
```

This installs PyInstaller only — not needed to run or develop the app
itself, only to build the distributable.

You also need **Inno Setup 6** on this machine (not a Python package —
a separate Windows tool): `winget install --id JRSoftware.InnoSetup -e`,
or download from https://jrsoftware.org. Its command-line compiler,
`ISCC.exe`, installs to `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`.

You also need the tools that sign the legacy BIO's WinUSB driver package.
They come from **two different kits**, and installing only the SDK is the
easy mistake — it gets you `signtool` and leaves `Inf2Cat` missing:

| Tool | Kit |
|---|---|
| `signtool.exe` | Windows **SDK** — tick only "Windows SDK Signing Tools for Desktop Apps"; the rest of that installer's 3.6 GB is irrelevant here |
| `Inf2Cat.exe` | Windows **WDK** — `winget install Microsoft.WindowsWDK.10.0.<version>` |

The WDK version must match the **SDK** version you installed, not the OS
build. Install the SDK first; the WDK checks for it as a prerequisite.

Clinic machines need none of this — only the three files step 2b produces.

## Two installers

This procedure produces **two** distributables, for two different
machines.

| | `reflex-setup.exe` | `reflex-viewer-setup.exe` |
|---|---|---|
| For | the clinic room machine | a student's or instructor's own laptop |
| Contains | `app.exe`, `settings.exe` | `viewer.exe` |
| IDS peak SDK | bundled, installed silently | none |
| Size | ~490 MB | ~90 MB |
| Privileges | admin | none (per-user) |
| Steps below | 2, 3, 4, 5 | 2, 6 |

The clinic installer deliberately does **not** ship `viewer.exe`:
`app.exe` already contains the viewer (Watch Last Recording opens it from
inside the kiosk), so a second PySide6+OpenCV+PyAV tree would add hundreds
of MB for no capability that machine lacks.

## 2. Freeze the entry points

```powershell
python -m PyInstaller --distpath packaging\dist --workpath packaging\build packaging\app.spec
python -m PyInstaller --distpath packaging\dist --workpath packaging\build packaging\settings.spec
python -m PyInstaller --distpath packaging\dist --workpath packaging\build packaging\viewer.spec
```

(Only `viewer.spec` is needed for the viewer-only installer; only the
other two for the clinic one.)

All three specs are checked into git (`packaging/*.spec`) — this isn't a
from-scratch step, just replaying a known-working build. See
`DECISIONS.md`'s entry on this for what was actually verified and why
no hidden-imports/`--collect-all` overrides were needed. If a future IDS
peak SDK or dependency version bump breaks the build, start by reading
the PyInstaller warnings file it writes to `packaging/build/<name>/
warn-<name>.txt`.

`viewer.spec` lists `ids_peak`, `ids_peak_ipl`, `pygrabber` and
`comtypes` in `excludes`. That's an assertion, not a size tweak: if a
future edit makes `viewer.py` reach anything camera-facing, this build
fails loudly rather than silently gaining an SDK dependency the review
machine can't satisfy. Confirmed on the 2026-09-02 build — the frozen
`viewer.exe` contains none of `recorder`, `camera`, `kiosk`,
`ids_camera` or `uvc_camera`.

**Verify before continuing**, not just "did it build without error":

- `packaging\dist\app\app.exe --synthetic` and
  `packaging\dist\settings\settings.exe` must open with no import/DLL
  errors.
- `packaging\dist\viewer\viewer.exe <a session folder>` must play it, and
  `viewer.exe` with no arguments must show the picker, opened on the home
  folder and listing nothing — the correct path, not a failure.
- Each exe must show its icon in Explorer **and** in its own title bar
  once running — those are two different mechanisms (exe resource vs.
  the bundled `.ico`), so one can be broken while the other looks fine.
  The `.ico` files are generated: after changing the mark or dropping
  artwork into `branding\icon-sources\`, run
  `.venv\Scripts\python.exe branding\build_icons.py` and commit
  `assets\*.ico` before building.

## 2b. Build and sign the legacy BIO driver package

```powershell
powershell -File packaging\net2860_winusb\build_driver_package.ps1
```

Produces three files next to that script, which `reflex.iss` copies
into the install:

```
reflex_net2860.inf   binds Microsoft's inbox winusb.sys to the camera
reflex_net2860.cat   its catalogue, signed and timestamped
reflex_net2860.cer   the public certificate, trusted at install time
```

**The `.cat` and `.cer` are gitignored build outputs**, so a fresh checkout
does not have them and `ISCC.exe` will fail at step 5 with a missing-source
error if this step is skipped. That is deliberate — a missing file at
compile time is a much better failure than an installer that silently ships
without a driver.

**A *stale* `.cat` is the failure that actually happened**, and it is quieter
than a missing one: the catalogue hashes the INF's bytes, so anything that
rewrites that file afterwards leaves both files present, the installer
compiling happily, and `pnputil` refusing the package on the clinic machine
with exit code `-536870325` (`0xE000024B`,
`SPAPI_E_FILE_HASH_NOT_IN_CATALOG`). Two things now stand against it, and
neither removes the need to **re-run this step after any change to the
INF**:

- `.gitattributes` pins `*.inf` to CRLF in the working tree, so a checkout
  or branch switch can no longer change the bytes the catalogue was signed
  against — a git line-ending conversion is what did it (see DECISIONS.md).
- the script itself now runs `signtool verify /pa /c` on the pair and fails
  the build if the catalogue doesn't cover the INF. On a build machine that
  has never trusted the signing certificate it prints a note about the trust
  chain and continues; that is expected, since the installer trusts the
  shipped `.cer` on the target.

The script is re-runnable and reuses an existing certificate. Its private
key lives only in the build user's certificate store and is **deliberately
not backed up** (decided 2026-09-10): lose this machine and the next build
signs as a *different* publisher. Nothing bricks — the installer ships and
trusts the new `.cer`, so a normal reinstall self-heals — but a
driver-only update becomes a full reinstall everywhere. That was judged
better than backing up a private key indefinitely or buying a commercial
OV certificate whose CA can re-issue. `Export-PfxCertificate` (the command
is in the script's header) preserves it if that ever changes.

Self-signed is enough because the package ships no binaries of its own
(every install section is an `Include`/`Needs` into the inbox
`winusb.inf`), so Kernel Mode Code Signing — the gate needing an EV
certificate — never applies. See DECISIONS.md's 2026-09-09 entries.

## 3. Get the IDS peak extended installer into `vendor/`

`packaging/reflex.iss` expects the IDS peak **extended** setup
installer (not standard, not runtime — see DECISIONS.md's "Retired ROADMAP
entries" entry for why extended) at exactly:

```
vendor\ids-peak-win-extended-setup-64.exe
```

Download the current version from
https://en.ids-imaging.com/download-peak.html and place/rename it there.
`vendor/` is gitignored (same convention as `vendor/ids_peak_api.txt` —
see `CLAUDE.md`'s Environment section), so this is a manual, per-build-
machine step, not something `git clone` gives you. Always use this exact
filename regardless of the version number in what you downloaded, so
`reflex.iss` never needs editing just because IDS shipped a new
release.

## 4. Record a silent-install response file for that exact installer

`reflex.iss` drives the IDS peak installer **silently** (see
`DECISIONS.md`'s "Silent IDS peak install" entry for why, and the
verification it relies on instead of a technician watching the wizard).
That needs an InstallShield response file recorded from a real install of
the *same* installer placed in step 3, at:

```
vendor\ids-peak-response.iss
```

**Record it on a machine with no prior IDS peak install** — a fresh
Windows Sandbox session is the easiest way to guarantee that (a
recording taken on a machine that already has IDS peak gets InstallShield's
Modify/Repair/Remove dialog flow instead of a fresh-install flow, which
records the wrong thing entirely). In that clean environment:

```
<the exact filename you downloaded>.exe /r /f1"C:\ids-peak-response.iss"
```

This runs the real, interactive wizard while recording every answer —
it's a genuine install, not a dry run. Click through it for real:
**Custom** install, leave every component checked (or at minimum ensure
**uEye Transport Layer** — listed as `UEyeSupport` in the 26.x-series
component tree — stays checked), leave the destination at its default.
**At the final restart prompt, choose "No, I will restart later"** — this
is not optional: since the replay is silent, whatever gets recorded here
fires automatically and unprompted on every real clinic machine this
response file is later replayed on. Recording "restart now" would mean
every future silent install triggers an unannounced reboot on a real
clinic machine.

Copy the resulting `C:\ids-peak-response.iss` out of the sandbox to
`vendor\ids-peak-response.iss` on the build machine. Gitignored, same
convention as the installer `.exe` itself in step 3.

**Must be re-recorded** (the same way, in a fresh clean environment)
every time the installer `.exe` in step 3 is bumped to a new IDS release
— a response file recorded against one version's dialog layout can
silently produce the wrong result when replayed against a different
version's layout. `IdsPeakAlreadyInstalled()`'s post-install re-check in
`reflex.iss` is the safety net if this step gets missed, not a
substitute for actually doing it.

## 5. Compile the clinic installer

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\reflex.iss
```

Produces `packaging\installer_output\reflex-setup.exe` — this is the
one file a technician actually needs. Gitignored, like the rest of
`packaging/`'s generated output (`packaging/build/`, `packaging/dist/`).

**A machine that still has the old `sidebyside` install** keeps it: this
installer neither upgrades nor detects it, because the rename changed the
`AppId`, the install and data folders, and the driver package's name.
**Delete the old Desktop shortcut** so a student can only start Reflex —
the old kiosk still works, and records into its own folder, which nothing
in Reflex lists. Uninstalling sidebyside (Settings → Apps) is cleaner and
still fine; its uninstaller removes its own driver package and signing
certificate. Either way redo `CALIBRATION.md` rather than copying
`%ProgramData%\sidebyside\config.json` over — it carries an
unproven calibration. The same shortcut rule
applies to a laptop's old `sidebyside Viewer`. See DECISIONS.md's
2026-09-11 coexistence entry.

**Before handing this to anyone**, actually run it on a real (or
disposable/VM) Windows machine and confirm: both shortcuts appear and
launch their respective `.exe`s, the IDS peak SDK installs correctly
(check `%ProgramFiles%\IDS\ids_peak` — no wizard appears, this happens
silently now), the Finished page shows the native "restart now / restart
later" choice (not a separate popup — see `DECISIONS.md`'s "Silent IDS
peak install: native restart page" entry for why the install runs
*before* Reflex's own files specifically to make this work), and
choosing "restart now" genuinely restarts the machine. Confirm the buffer
cleanup task registered — its quoting is only exercised at install time:
`schtasks /Query /TN "Reflex buffer cleanup" /V /FO LIST` must name
`clear_reflex_buffer.ps1`, and `/Run` on it must empty the buffer under
`%LOCALAPPDATA%\Temp\Reflex`. And — on a machine
that already has a current-enough IDS peak installed — re-running
`reflex-setup.exe` skips reinstalling it (`IdsPeakAlreadyInstalled` in
`packaging/reflex.iss`'s `[Code]` section) and the Finished page shows
no restart choice, since nothing changed. If the silent install fails or
can't be verified, the installer shows an explicit error dialog rather
than continuing silently — confirm that path too by temporarily renaming
`vendor\ids-peak-response.iss` before a test run.

## What an uninstall removes

Scope is deliberate, not whatever Inno's defaults happen to do — see
DECISIONS.md's 2026-09-10 "Uninstall scope is now a decision" entry.

**Removed:**

- Everything under `{app}` — both frozen exes and the driver package files
- The staged WinUSB driver package (`pnputil /delete-driver ... /uninstall`)
- The signing certificate, from both Trusted Root and Trusted Publishers
- The **Reflex buffer cleanup** task — a folder-deleting scheduled task must not outlive its script

The driver's published name (`oemNN.inf`) is assigned at install time and
changes between installs, so uninstall finds it by scanning
`pnputil /enum-drivers` for the original filename — which also catches a
package staged by `build_driver_package.ps1 -Install` on a dev box.

**Kept:** `config.json` (a reinstall finding camera assignments and
calibration intact is strictly better) and the IDS peak SDK (shared —
Keeler's Kinexis uses the same install, and it has its own uninstaller).
There is nothing else: recordings never persist.

## What the technician does next

The clinic installer leaves a machine with the software in place and no
camera assigned to any role. `CALIBRATION.md` is the ~15-minute procedure
that finishes the job: assign each role, calibrate each instrument camera
against a real view through the instrument, and prove the room with one
test recording. Hand that document over with the installer — it assumes no
imaging knowledge and no access to this repo.

## 6. Compile the viewer-only installer

Needs only step 2's `viewer.spec` build — no `vendor/` contents, no
response file, nothing from steps 3–5.

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\reflex-viewer.iss
```

Produces `packaging\installer_output\reflex-viewer-setup.exe` (~90 MB
as of 2026-09-02, against the clinic installer's ~490 MB).

This is what goes to a student or instructor who wants to review
recordings on their own machine. It installs per-user under
`%LOCALAPPDATA%\Reflex Viewer` with `PrivilegesRequired=lowest`, so
it raises no UAC prompt and needs no admin rights, and it creates a
Desktop shortcut — unlike `settings.exe`, students *are* this program's
audience.

**Before handing it to anyone**, install it on a machine that has never
had Reflex on it and confirm:

- it installs without prompting for admin,
- the Desktop shortcut opens the viewer,
- with no recordings present it shows the picker saying "No recordings in
  this folder" rather than erroring — the normal state, since recordings
  never persist anywhere,
- **"Open a recording folder…" finds a session copied from elsewhere**
  (a USB stick, Downloads). This is the only path that matters here.
- playback, the layout picker and Export all work on that copied session.

Both installers use distinct `AppId`s and install locations, so they
coexist on one machine. Verify that too if you install both: neither
should uninstall or upgrade over the other. **Never change either
`AppId`** (`reflex`, `reflex-viewer`) once shipped — it is how Windows
recognises an existing install as upgradable. Both are pinned rather
than derived from `AppName`, so the display name can change freely.
