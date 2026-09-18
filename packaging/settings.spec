# PyInstaller spec for settings.py, the technician tool -- see
# PACKAGING.md and app.spec's comment for the full reasoning (identical
# here: same empirically-confirmed dependency set, same upx=False
# rationale). Confirmed by actually running the frozen settings.exe --
# opens cleanly with no import errors.

# The icon is listed twice on purpose: `icon=` writes the exe resource
# (Explorer, taskbar, shortcuts), `datas` ships the file itself for
# QApplication.setWindowIcon(), which can't read that resource back.

a = Analysis(
    ['../settings.py'],
    pathex=[],
    binaries=[],
    datas=[('../assets/reflex-settings.ico', 'assets')],
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
    name='settings',
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
    icon='../assets/reflex-settings.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='settings',
)
