"""The Reflex look for the student-facing windows.

Qt's Fusion style with a palette and app-wide font built from
neco_reflex_theme.py, plus a short stylesheet for what a palette can't say:
the kiosk's big primary buttons, the selected instrument, the viewer's
scrubber, secondary text, the brand rule. Applied
once per process, by app.main() and viewer.main(). settings.py (technician
tool) and preview.py (dev tool) deliberately stay native. See DECISIONS.md's
"The Reflex look" entry for why Fusion rather than a stylesheet for
everything.

A widget's own stylesheet still beats this one, which is how the error and
warning banners and the black video panes keep their colors.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

import neco_reflex_theme as theme

BODY_POINT_SIZE = 11
HEADING_POINT_SIZE = 20

# Object names the stylesheet keys on.
PRIMARY = "primary"  # the one action a screen is about: Start, Stop
CHOICE = "choice"  # a pick-one option: the instrument picker
STATUS = "status"  # the kiosk's live status line
SECONDARY = "secondary"  # supporting text: summaries, session details
BRAND_RULE = "brandRule"
# A slider a student uses mid-recording, with an instrument at their eye:
# the handle is twice the scrub bar's, because a small target there is the
# difference between adjusting and fumbling. See app.py's brightness row.
TOUCH_SLIDER = "touchSlider"


def _font(families: tuple[str, ...], point_size: int) -> QFont:
    font = QFont()
    font.setFamilies(list(families))
    font.setPointSize(point_size)
    return font


def body_font(point_size: int = BODY_POINT_SIZE) -> QFont:
    return _font(theme.BODY_FONTS, point_size)


def heading_font(point_size: int = HEADING_POINT_SIZE) -> QFont:
    return _font(theme.HEADING_FONTS, point_size)


def _rgba(color: str, alpha: int) -> str:
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def palette() -> QPalette:
    chrome = QColor(theme.CHARCOAL)
    text = QColor(theme.OFF_WHITE)
    muted = QColor(theme.TAUPE)
    Role = QPalette.ColorRole

    p = QPalette()
    for role, color in (
        (Role.Window, chrome),
        (Role.WindowText, text),
        (Role.Base, chrome.lighter(125)),
        (Role.AlternateBase, chrome.lighter(145)),
        (Role.Button, chrome.lighter(150)),
        (Role.ButtonText, text),
        (Role.Text, text),
        (Role.BrightText, text),
        (Role.Light, chrome.lighter(220)),
        (Role.Midlight, chrome.lighter(180)),
        (Role.Mid, chrome.lighter(120)),
        (Role.Dark, chrome.darker(150)),
        (Role.Shadow, chrome.darker(300)),
        (Role.Highlight, text),
        (Role.HighlightedText, chrome),
        (Role.ToolTipBase, chrome.lighter(150)),
        (Role.ToolTipText, text),
        (Role.PlaceholderText, muted),
        (Role.Link, text),
        (Role.LinkVisited, muted),
    ):
        p.setColor(role, color)

    disabled = QPalette.ColorGroup.Disabled
    for role in (Role.WindowText, Role.Text, Role.ButtonText):
        p.setColor(disabled, role, muted.darker(160))
    p.setColor(disabled, Role.Highlight, chrome.lighter(180))
    return p


def stylesheet() -> str:
    off, charcoal, taupe = theme.OFF_WHITE, theme.CHARCOAL, theme.TAUPE
    # Order matters where specificity ties: :disabled comes after :hover and
    # :pressed so a disabled button under the mouse still reads disabled.
    # A checked button is outlined, never filled: a solid Off-White fill
    # means "press this" (PRIMARY), and a selected instrument beside an
    # enabled Start must not look like a second Start.
    return f"""
QPushButton {{
    background-color: {_rgba(off, 18)};
    color: {off};
    border: 1px solid {_rgba(off, 70)};
    border-radius: 8px;
    padding: 6px 18px;
    font-size: 12pt;
}}
QPushButton:hover {{ background-color: {_rgba(off, 34)}; }}
QPushButton:pressed {{ background-color: {_rgba(off, 56)}; }}
QPushButton:checked {{ background-color: {_rgba(off, 46)}; border: 2px solid {off}; }}
QPushButton:disabled {{
    background-color: transparent;
    color: {_rgba(taupe, 110)};
    border-color: {_rgba(off, 28)};
}}
QPushButton:checked:disabled {{
    background-color: {_rgba(off, 14)};
    color: {_rgba(off, 150)};
    border: 2px solid {_rgba(off, 110)};
}}
QPushButton#{CHOICE} {{ font-size: 14pt; }}

QPushButton#{PRIMARY} {{
    background-color: {off};
    color: {charcoal};
    border-color: {off};
    font-size: 15pt;
}}
QPushButton#{PRIMARY}:hover {{ background-color: {_rgba(off, 225)}; }}
QPushButton#{PRIMARY}:pressed {{ background-color: {_rgba(off, 190)}; }}
QPushButton#{PRIMARY}:disabled {{
    background-color: transparent;
    color: {_rgba(taupe, 110)};
    border-color: {_rgba(off, 28)};
}}

QSlider::groove:horizontal {{ height: 4px; background: {_rgba(off, 50)}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {off}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {off};
    width: 16px;
    height: 16px;
    margin: -6px 0;
    border-radius: 8px;
}}

QSlider#{TOUCH_SLIDER}::groove:horizontal {{ height: 8px; border-radius: 4px; }}
QSlider#{TOUCH_SLIDER}::handle:horizontal {{
    width: 30px;
    height: 30px;
    margin: -11px 0;
    border-radius: 15px;
}}

QLabel#{STATUS} {{ font-size: 13pt; }}
QLabel#{SECONDARY} {{ color: {taupe}; }}
QFrame#{BRAND_RULE} {{ background-color: {theme.GOLDEN}; border: none; }}
"""


def apply(app: QApplication) -> None:
    """Style every window this process opens from here on."""
    app.setStyle("Fusion")
    app.setPalette(palette())
    app.setFont(body_font())
    app.setStyleSheet(stylesheet())
