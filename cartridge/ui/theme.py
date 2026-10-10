"""Dark theme: palette, font stack and stylesheet for Cartridge.

The visual language is taken from section 6.1 of the project brief and from the
approved `ui-reference.html` concept: a compact, flat dark theme with 1 px
borders, no blur, no drop shadows, no filters and no animation, because the
target GPU is an Intel GMA 4500MHD driving 1280x800 over a mechanical disk.

Colors are design tokens, not a promise to reproduce the CSS pixel-for-pixel.
They are exposed as a dict so tests can assert on them and so a future theme
variant can override them without touching widget code.
"""

from __future__ import annotations

from typing import Dict

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QPalette
from PyQt5.QtWidgets import QApplication, QToolTip

# --------------------------------------------------------------------------
# Design tokens (brief section 6.1)
# --------------------------------------------------------------------------
COLORS: Dict[str, str] = {
    "background": "#14161A",
    "surface": "#1B1E24",
    "surface_secondary": "#22262E",
    "surface_tertiary": "#2A2F39",
    "border": "#2E343F",
    "border_strong": "#3C4351",
    "text_primary": "#E8EAED",
    "text_secondary": "#A0A8B4",
    "text_muted": "#6B7280",
    "accent": "#4EA1FF",
    "accent_dark": "#2C6FBF",
    "success": "#4ADE80",
    "warning": "#FBBF24",
    "error": "#F87171",
}

# Extra derived tokens used by the implementation (not in the brief, documented
# here so they stay reviewable).
COLORS.update(
    {
        "accent_soft": "#1E2A3A",
        "selection": "#26384F",
        "hover": "#232833",
        "disabled_text": "#565C66",
        "scrollbar": "#333A45",
        "scrollbar_hover": "#3F4753",
    }
)

RADIUS_SM = 4
RADIUS_MD = 6
RADIUS_LG = 10

# Segoe UI ships with Windows; the fallbacks keep the app legible elsewhere.
FONT_FAMILIES = ("Segoe UI", "Segoe UI Variable Text", "Tahoma", "DejaVu Sans", "Sans Serif")

FONT_SIZE_BASE = 9  # pt - Qt's default on Windows is 9pt for Segoe UI
FONT_SIZE_SMALL = 8
FONT_SIZE_TITLE = 13


def qcolor(token: str) -> QColor:
    """Return a QColor for a design token name."""
    return QColor(COLORS[token])


def app_font(base_size: int = FONT_SIZE_BASE) -> QFont:
    """Build the application font, preferring Segoe UI when it is installed.

    QFontDatabase requires a QGuiApplication, and touching it before one exists
    makes Qt abort the process. So the installed-family lookup only happens when
    an application instance is already running; otherwise we ask Qt for the
    first family in the list and let fontconfig/GDI substitute.
    """
    family = FONT_FAMILIES[0]
    if QCoreApplication.instance() is not None:
        available = set()
        try:
            available = set(QFontDatabase().families())
        except Exception:
            available = set()
        if available:
            for candidate in FONT_FAMILIES:
                if candidate in available:
                    family = candidate
                    break
    font = QFont(family, base_size)
    font.setStyleStrategy(QFont.PreferAntialias)
    font.setHintingPreference(QFont.PreferFullHinting)
    return font


def dark_palette() -> QPalette:
    """A QPalette that matches the stylesheet.

    The palette matters as well as the QSS: widgets that are not covered by a
    selector (and native dialogs such as the folder picker) fall back to it, so
    an unpainted white dialog would immediately betray a half-applied theme.
    """
    palette = QPalette()
    bg = qcolor("background")
    surface = qcolor("surface")
    surface_secondary = qcolor("surface_secondary")
    text = qcolor("text_primary")
    secondary = qcolor("text_secondary")
    muted = qcolor("text_muted")
    accent = qcolor("accent")
    border = qcolor("border")

    palette.setColor(QPalette.Window, bg)
    palette.setColor(QPalette.WindowText, text)
    palette.setColor(QPalette.Base, surface)
    palette.setColor(QPalette.AlternateBase, surface_secondary)
    palette.setColor(QPalette.ToolTipBase, surface_secondary)
    palette.setColor(QPalette.ToolTipText, text)
    palette.setColor(QPalette.Text, text)
    palette.setColor(QPalette.Button, surface_secondary)
    palette.setColor(QPalette.ButtonText, text)
    palette.setColor(QPalette.BrightText, accent)
    palette.setColor(QPalette.Link, accent)
    palette.setColor(QPalette.LinkVisited, qcolor("accent_dark"))
    palette.setColor(QPalette.Highlight, qcolor("selection"))
    palette.setColor(QPalette.HighlightedText, text)
    palette.setColor(QPalette.PlaceholderText, muted)

    disabled = QPalette.Disabled
    palette.setColor(disabled, QPalette.Text, muted)
    palette.setColor(disabled, QPalette.WindowText, muted)
    palette.setColor(disabled, QPalette.ButtonText, muted)
    palette.setColor(disabled, QPalette.Highlight, surface_secondary)
    palette.setColor(disabled, QPalette.HighlightedText, secondary)

    return palette


def build_stylesheet() -> str:
    """Return the full application stylesheet.

    Kept as one generated string (rather than a .qrc resource) so the tokens and
    the CSS cannot drift apart, and so tests can assert on the produced text.
    Flat surfaces + 1 px borders only: no qlineargradient with blur, no
    border-image, no animation properties.
    """
    ctx = dict(COLORS)
    # Short aliases so the stylesheet reads naturally ("{text}" instead of
    # "{text_primary}") without duplicating the token table.
    ctx["text"] = COLORS["text_primary"]
    return """
/* ---------------- base ----------------
   Order matters here: Qt CSS resolves equal-specificity type selectors by
   position, so the generic QWidget rule must come FIRST and the window/dialog
   backgrounds AFTER it. With the reverse order every dialog painted transparent
   (white on a light compositor, black under the Win7 classic theme) because the
   QWidget rule overrode the QDialog rule. */
* {{
    font-family: "{font}";
    outline: none;
}}
QWidget {{
    background-color: transparent;
    color: {text};
    font-size: {base}pt;
}}
QMainWindow, QDialog {{
    background-color: {background};
    color: {text};
}}
QToolTip {{
    background-color: {surface_secondary};
    color: {text};
    border: 1px solid {border_strong};
    border-radius: {radius_sm}px;
    padding: 4px 6px;
}}

/* ---------------- text roles ---------------- */
QLabel#AppTitle {{
    color: {text};
    font-size: {title}pt;
    font-weight: 600;
}}
QLabel#SectionTitle {{
    color: {text};
    font-size: 11pt;
    font-weight: 600;
}}
QLabel#CardTitle {{
    color: {text};
    font-size: 9pt;
    font-weight: 600;
}}
QLabel#SecondaryText, QLabel#DetailLabel {{
    color: {text_secondary};
}}
QLabel#MutedText, QLabel#DetailValue {{
    color: {text_muted};
}}
QLabel#AccentText {{
    color: {accent};
}}
QLabel#SuccessText {{ color: {success}; }}
QLabel#WarningText {{ color: {warning}; }}
QLabel#ErrorText   {{ color: {error}; }}

/* ---------------- root surfaces ---------------- */
QWidget#RootSurface, QWidget#CoverGridCanvas {{
    background-color: {background};
}}

/* ---------------- title bar ---------------- */
QWidget#TitleBar {{
    background-color: {surface};
    border-bottom: 1px solid {border};
}}
QLabel#RootPath {{
    color: {text_muted};
    font-size: {small}pt;
}}

/* ---------------- navigation rail ---------------- */
QWidget#NavRail {{
    background-color: {surface};
    border-right: 1px solid {border};
}}
QPushButton#NavButton {{
    background-color: transparent;
    color: {text_secondary};
    border: none;
    border-left: 2px solid transparent;
    border-radius: 0px;
    padding: 9px 12px;
    text-align: left;
    font-size: 9pt;
}}
QPushButton#NavButton:hover {{
    background-color: {hover};
    color: {text};
}}
QPushButton#NavButton:checked {{
    background-color: {accent_soft};
    color: {accent};
    border-left: 2px solid {accent};
    font-weight: 600;
}}

/* ---------------- surfaces / panels ---------------- */
QWidget#SurfacePanel {{
    background-color: {surface};
    border: 1px solid {border};
    border-radius: {radius_md}px;
}}
QWidget#FilterPanel {{
    background-color: {surface};
    border-right: 1px solid {border};
}}
QWidget#DetailPanel {{
    background-color: {surface};
    border-left: 1px solid {border};
}}
QGroupBox {{
    background-color: {surface};
    border: 1px solid {border};
    border-radius: {radius_md}px;
    margin-top: 12px;
    padding: 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {text_secondary};
}}
QFrame#Separator {{
    background-color: {border};
    max-height: 1px;
    border: none;
}}

/* ---------------- buttons ---------------- */
QPushButton {{
    background-color: {surface_secondary};
    color: {text};
    border: 1px solid {border_strong};
    border-radius: {radius_md}px;
    padding: 6px 12px;
    min-height: 18px;
}}
QPushButton:hover {{
    background-color: {surface_tertiary};
    border-color: {accent_dark};
}}
QPushButton:pressed {{
    background-color: {accent_dark};
}}
QPushButton:disabled {{
    color: {disabled_text};
    border-color: {border};
    background-color: {surface};
}}
QPushButton#PrimaryButton {{
    background-color: {accent_dark};
    border: 1px solid {accent};
    color: #FFFFFF;
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{
    background-color: {accent};
    color: #10131A;
}}
QPushButton#PrimaryButton:disabled {{
    background-color: {surface_secondary};
    border-color: {border};
    color: {disabled_text};
}}
QPushButton#GhostButton {{
    background-color: transparent;
    border: 1px solid {border};
    color: {text_secondary};
}}
QPushButton#GhostButton:hover {{
    border-color: {border_strong};
    color: {text};
    background-color: {hover};
}}
QPushButton#DangerButton {{
    background-color: transparent;
    border: 1px solid {border_strong};
    color: {error};
}}
QPushButton#DangerButton:hover {{
    background-color: #2A1F22;
    border-color: {error};
}}
QPushButton#IconButton {{
    background-color: transparent;
    border: none;
    color: {text_secondary};
    padding: 4px 6px;
}}
QPushButton#IconButton:hover {{ color: {accent}; background-color: {hover}; }}
QPushButton#FavouriteButton:checked {{ color: {warning}; }}

/* ---------------- inputs ---------------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {surface_secondary};
    color: {text};
    border: 1px solid {border_strong};
    border-radius: {radius_md}px;
    padding: 5px 8px;
    selection-background-color: {accent_dark};
    selection-color: #FFFFFF;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {accent};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {{
    color: {disabled_text};
    background-color: {surface};
}}
QLineEdit#SearchField {{
    background-color: {surface_secondary};
    border: 1px solid {border_strong};
    border-radius: {radius_md}px;
    padding: 6px 10px;
    font-size: 10pt;
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {text_secondary};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {surface_secondary};
    color: {text};
    border: 1px solid {border_strong};
    selection-background-color: {selection};
    selection-color: {text};
    outline: none;
}}
QCheckBox, QRadioButton {{
    color: {text_secondary};
    spacing: 7px;
    background: transparent;
    border: none;
    padding: 2px;
}}
QCheckBox:hover, QRadioButton:hover {{ color: {text}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {border_strong};
    background-color: {surface_secondary};
}}
QCheckBox::indicator {{ border-radius: 3px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked {{
    background-color: {accent};
    border-color: {accent};
}}
QRadioButton::indicator:checked {{
    background-color: {accent};
    border-color: {accent};
}}

/* ---------------- views ---------------- */
QListView, QTreeView, QTableView, QListWidget, QTreeWidget, QTableWidget {{
    background-color: {background};
    alternate-background-color: {surface};
    color: {text};
    border: 1px solid {border};
    border-radius: {radius_md}px;
    selection-background-color: {selection};
    selection-color: {text};
    gridline-color: {border};
}}
QListView::item, QTreeView::item, QTableView::item {{
    border: none;
    padding: 3px 4px;
}}
QListView::item:selected, QTreeView::item:selected, QTableView::item:selected {{
    background-color: {selection};
    color: {text};
}}
QListView::item:hover, QTreeView::item:hover {{
    background-color: {hover};
}}
QHeaderView::section {{
    background-color: {surface_secondary};
    color: {text_secondary};
    border: none;
    border-right: 1px solid {border};
    border-bottom: 1px solid {border};
    padding: 6px 8px;
    font-weight: 600;
}}
QHeaderView::section:hover {{ color: {text}; }}
QTableCornerButton::section {{
    background-color: {surface_secondary};
    border: none;
    border-right: 1px solid {border};
    border-bottom: 1px solid {border};
}}

/* game cover grid cards live in a QListView in IconMode */
QListView#CoverGrid {{
    background-color: {background};
    border: none;
    padding: 4px;
}}
QListView#CoverGrid::item {{
    background-color: {surface};
    border: 1px solid {border};
    border-radius: {radius_md}px;
    margin: 5px;
    padding: 0px;
    color: {text};
}}
QListView#CoverGrid::item:selected {{
    border: 1px solid {accent};
    background-color: {accent_soft};
}}
QListView#CoverGrid::item:hover {{
    border-color: {border_strong};
}}

/* ---------------- scrollbars ---------------- */
QScrollBar:vertical {{
    background-color: {background};
    width: 11px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:vertical {{
    background-color: {scrollbar};
    min-height: 28px;
    border-radius: 5px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background-color: {scrollbar_hover}; }}
QScrollBar:horizontal {{
    background-color: {background};
    height: 11px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background-color: {scrollbar};
    min-width: 28px;
    border-radius: 5px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{ background-color: {scrollbar_hover}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------------- progress / status ---------------- */
QProgressBar {{
    background-color: {surface_secondary};
    border: 1px solid {border};
    border-radius: {radius_sm}px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background-color: {accent_dark};
    border-radius: 3px;
}}
QStatusBar {{
    background-color: {surface};
    color: {text_muted};
    border-top: 1px solid {border};
    font-size: {small}pt;
}}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {text_muted}; font-size: {small}pt; }}

/* ---------------- tabs / splitter / menu ---------------- */
QTabWidget::pane {{
    border: 1px solid {border};
    border-radius: {radius_md}px;
    background-color: {surface};
    top: -1px;
}}
QTabBar::tab {{
    background-color: {surface_secondary};
    color: {text_secondary};
    border: 1px solid {border};
    border-bottom: none;
    padding: 6px 14px;
    margin-right: 2px;
    border-top-left-radius: {radius_md}px;
    border-top-right-radius: {radius_md}px;
}}
QTabBar::tab:selected {{
    background-color: {surface};
    color: {accent};
    font-weight: 600;
}}
QTabBar::tab:hover {{ color: {text}; }}
QSplitter::handle {{ background-color: {border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QMenu {{
    background-color: {surface_secondary};
    color: {text};
    border: 1px solid {border_strong};
    padding: 4px;
}}
QMenu::item {{ padding: 5px 22px 5px 12px; border-radius: {radius_sm}px; }}
QMenu::item:selected {{ background-color: {selection}; }}
QMenu::separator {{ height: 1px; background-color: {border}; margin: 4px 6px; }}

/* ---------------- badges ---------------- */
QLabel#Badge {{
    background-color: {surface_tertiary};
    color: {text_secondary};
    border: 1px solid {border_strong};
    border-radius: 3px;
    padding: 1px 5px;
    font-size: 8pt;
    min-width: 0px;
    max-height: 16px;
}}
QLabel#BadgeSuccess {{
    background-color: #16281D;
    min-width: 0px;
    max-height: 16px;
    font-size: 8pt;
    color: {success};
    border: 1px solid #24462F;
    border-radius: 3px;
    padding: 1px 5px;
    font-size: 8pt;
}}
QLabel#BadgeWarning {{
    background-color: #2A2415;
    min-width: 0px;
    max-height: 16px;
    font-size: 8pt;
    color: {warning};
    border: 1px solid #4A3D1C;
    border-radius: 3px;
    padding: 1px 5px;
    font-size: 8pt;
}}
QLabel#BadgeError {{
    background-color: #2A1B1D;
    min-width: 0px;
    max-height: 16px;
    font-size: 8pt;
    color: {error};
    border: 1px solid #4A262A;
    border-radius: 3px;
    padding: 1px 5px;
    font-size: 8pt;
}}
QLabel#BadgeAccent {{
    background-color: {accent_soft};
    min-width: 0px;
    max-height: 16px;
    font-size: 8pt;
    color: {accent};
    border: 1px solid {accent_dark};
    border-radius: 3px;
    padding: 1px 5px;
    font-size: 8pt;
}}

/* ---------------- cards ---------------- */
QWidget#GameCard {{
    background-color: {surface};
    border: 1px solid {border};
    border-radius: {radius_md}px;
}}
QWidget#GameCard[selected="true"] {{
    border: 1px solid {accent};
    background-color: {accent_soft};
}}
QWidget#GameCard:hover {{ border-color: {border_strong}; }}
QLabel#CoverPlaceholder {{
    background-color: {surface_secondary};
    color: {text_muted};
    border: 1px solid {border};
    border-radius: {radius_sm}px;
}}
QLabel#CoverMissing {{
    background-color: #221C1C;
    color: {error};
    border: 1px solid #4A262A;
    border-radius: {radius_sm}px;
}}

/* ---------------- detail pane ---------------- */
QLabel#DetailTitle {{
    color: {text};
    font-size: 14pt;
    font-weight: 600;
}}
QLabel#DetailHero {{
    background-color: {surface_secondary};
    border: 1px solid {border};
    border-radius: {radius_md}px;
}}
QScrollArea#DetailScroll {{
    background-color: {surface};
    border: none;
}}
""".format(
        font=FONT_FAMILIES[0],
        base=FONT_SIZE_BASE,
        small=FONT_SIZE_SMALL,
        title=FONT_SIZE_TITLE,
        radius_sm=RADIUS_SM,
        radius_md=RADIUS_MD,
        **ctx
    )


def apply_theme(app: QApplication) -> None:
    """Apply palette, font, stylesheet and tooltip palette to an application."""
    app.setStyle("Fusion")  # consistent metrics across platforms, no native theming surprises
    app.setPalette(dark_palette())
    app.setFont(app_font())
    app.setStyleSheet(build_stylesheet())
    tip_palette = QToolTip.palette()
    tip_palette.setColor(QPalette.ToolTipBase, qcolor("surface_secondary"))
    tip_palette.setColor(QPalette.ToolTipText, qcolor("text_primary"))
    QToolTip.setPalette(tip_palette)
