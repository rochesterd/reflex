"""Where the app's .ico files live.

Stdlib only, no Qt import: setup_wizard.py is tkinter (it runs before
requirements.txt installs PySide6) and needs the same path.
"""

from __future__ import annotations

import sys
from pathlib import Path

ICON_APP = "reflex.ico"
ICON_SETTINGS = "reflex-settings.ico"


def icon_path(name: str = ICON_APP) -> Path:
    """assets/ beside this file, or sys._MEIPASS/assets in a frozen exe.

    Resolved from __file__, not the CWD -- settings.py is routinely
    launched from somewhere else.
    """
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent
    return root / "assets" / name
