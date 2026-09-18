# PyInstaller spec for app.py, the kiosk entry point -- see PACKAGING.md
# for how this fits into the full installer build, and DECISIONS.md's
# "Frozen-exe installer built" entry for why this exists at all.
#
# Empty hiddenimports/binaries/datas below aren't a placeholder -- this
# spec is the result of an actual empirical build: PySide6, cv2 (OpenCV),
# av (PyAV), and comtypes (via pygrabber, used by uvc_enumeration.py) all
# had working PyInstaller hooks already, and ids_peak/ids_peak_ipl's
# native DLLs were picked up automatically by PyInstaller's own binary
# dependency scan (no vendor-specific hook needed). Confirmed by actually
# running the frozen app.exe --synthetic (all-synthetic path) and
# --third-person-synthetic (forces the real `from ids_camera import
# IdsCamera` import at startup) -- both ran cleanly with no import or
# DLL-load errors. If a future IDS peak SDK version bump breaks this,
# that's the first place to look.
#
# upx=False, deliberately, on both EXE and COLLECT below: UPX-compressed
# executables are a known source of false-positive antivirus flags,
# which is a real risk on a locked-down clinic machine and not worth the
# smaller file size.

# The icon is listed twice on purpose: `icon=` writes the exe resource
# (Explorer, taskbar, shortcuts), `datas` ships the file itself for
# QApplication.setWindowIcon(), which can't read that resource back.

a = Analysis(
    ['../app.py'],
    pathex=[],
    binaries=[],
    datas=[('../assets/reflex.ico', 'assets')],
    hiddenimports=[
        # Imported lazily by feature (stream mode, microphone, Panopto
        # upload), so the static scan can miss them -- name them.
        "pyvirtualcam", "sounddevice", "boto3",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='app',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='../assets/reflex.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='app',
)
