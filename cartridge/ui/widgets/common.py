"""Small reusable widgets: badges, chips, empty states, rating stars.

All of these are deliberately cheap to construct and paint, because the browser
may build a hundred of them per scroll on a two-core machine with software
rendering. No custom painting, no effects, no animation - flat surfaces, 1 px
borders and text, driven entirely by the stylesheet in :mod:`cartridge.ui.theme`.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

BADGE_STYLES = {
    "default": "Badge",
    "accent": "BadgeAccent",
    "success": "BadgeSuccess",
    "warning": "BadgeWarning",
    "error": "BadgeError",
}


class Badge(QLabel):
    """A small coloured label used for content types and states."""

    def __init__(self, text: str = "", style: str = "default", parent=None):
        super(Badge, self).__init__(text, parent)
        self.set_style(style)
        self.setTextFormat(Qt.PlainText)

    def set_style(self, style: str) -> None:
        self.setObjectName(BADGE_STYLES.get(style, BADGE_STYLES["default"]))
        # Re-polish so the new objectName's rules apply immediately.
        self.style().unpolish(self)
        self.style().polish(self)

    def set_text(self, text: str) -> None:
        self.setText(text or "")


class BadgeRow(QWidget):
    """A horizontal flow of badges that clips instead of expanding forever."""

    def __init__(self, parent=None, max_badges: int = 4):
        super(BadgeRow, self).__init__(parent)
        self.max_badges = max(1, int(max_badges))
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set_items(self, items: Iterable[str], style: str = "default") -> None:
        self.clear()
        shown: List[str] = []
        overflow = 0
        for text in items or ():
            if not text:
                continue
            if len(shown) < self.max_badges:
                shown.append(str(text))
            else:
                overflow += 1
        for text in shown:
            badge = Badge(text, style, self)
            badge.setMaximumWidth(96)
            badge.setToolTip(text)
            self._layout.addWidget(badge)
        if overflow:
            self._layout.addWidget(Badge("+%d" % overflow, "default", self))
        self._layout.addStretch(1)

    def clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            detach_and_delete(item.widget())


class RatingLabel(QLabel):
    """Shows a 0-100 score as a number plus a tint, or a muted dash."""

    def __init__(self, parent=None):
        super(RatingLabel, self).__init__("", parent)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedWidth(38)

    def set_rating(self, rating: Optional[float]) -> None:
        if rating is None:
            self.setText("–")
            self.setObjectName("MutedText")
            self.setStyleSheet("color: %s;" % "#6B7280")
            return
        value = max(0.0, min(100.0, float(rating)))
        self.setText("%d" % int(round(value)))
        if value >= 80:
            color, name = "#4ADE80", "SuccessText"
        elif value >= 60:
            color, name = "#FBBF24", "WarningText"
        else:
            color, name = "#F87171", "ErrorText"
        self.setObjectName(name)
        self.setStyleSheet(
            "color: %s; background-color: #22262E; border: 1px solid #2E343F;"
            "border-radius: 4px; font-weight: 600;" % color
        )


class Chip(QCheckBox):
    """A toggleable filter chip (used in the sidebar facets)."""

    def __init__(self, text: str, count: int = 0, parent=None):
        label = text if count <= 0 else "%s  %d" % (text, count)
        super(Chip, self).__init__(label, parent)
        self.value = text
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set_count(self, count: int) -> None:
        self.setText(self.value if count <= 0 else "%s  %d" % (self.value, count))


class EmptyState(QWidget):
    """The panel shown when a view has nothing to display.

    Empty, loading, error and offline states are deliberately distinct (brief
    section 6.5 and 9): "your library is empty" and "the API is unreachable" lead
    to completely different user actions.
    """

    action_requested = pyqtSignal()

    def __init__(
        self,
        title: str = "Nothing here yet",
        body: str = "",
        action_text: str = "",
        parent=None,
    ):
        super(EmptyState, self).__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(8)
        layout.addStretch(1)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("SectionTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.body_label = QLabel(body)
        self.body_label.setObjectName("SecondaryText")
        self.body_label.setAlignment(Qt.AlignCenter)
        self.body_label.setWordWrap(True)
        self.body_label.setMaximumWidth(420)
        layout.addWidget(self.body_label, 0, Qt.AlignHCenter)

        self.action_button = QPushButton(action_text)
        self.action_button.setObjectName("PrimaryButton")
        self.action_button.setCursor(Qt.PointingHandCursor)
        self.action_button.clicked.connect(self.action_requested.emit)
        self.action_button.setVisible(bool(action_text))
        layout.addWidget(self.action_button, 0, Qt.AlignHCenter)

        layout.addStretch(2)

    def set_state(self, title: str, body: str = "", action_text: str = "") -> None:
        self.title_label.setText(title)
        self.body_label.setText(body)
        self.body_label.setVisible(bool(body))
        self.action_button.setText(action_text)
        self.action_button.setVisible(bool(action_text))


class Separator(QFrame):
    """A 1 px horizontal rule."""

    def __init__(self, parent=None):
        super(Separator, self).__init__(parent)
        self.setObjectName("Separator")
        self.setFrameShape(QFrame.HLine)
        self.setFixedHeight(1)


class IconButton(QPushButton):
    """A compact text button for row actions (no icon files to ship)."""

    def __init__(self, text: str, tooltip: str = "", style: str = "IconButton", parent=None):
        super(IconButton, self).__init__(text, parent)
        self.setObjectName(style)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        if tooltip:
            self.setToolTip(tooltip)


def detach_and_delete(widget: Optional[QWidget]) -> None:
    """Remove a recycled widget from its parent *now* and delete it later.

    ``deleteLater()`` on its own leaves the widget parented (and painted at its
    old geometry) until the deferred-delete event is processed, which on a
    recycled card row shows up as ghost chips. Detaching the parent removes it
    from the paint tree immediately; the deletion still happens safely on the
    event loop.
    """
    if widget is None:
        return
    try:
        widget.hide()
        widget.setParent(None)
    except RuntimeError:
        return
    widget.deleteLater()


def elided(text: str, font_metrics: QFontMetrics, width: int) -> str:
    """Truncate ``text`` with an ellipsis so it fits ``width`` pixels."""
    if not text:
        return ""
    return font_metrics.elidedText(text, Qt.ElideRight, max(10, int(width)))


class KeyValueGrid(QWidget):
    """Two-column label/value list used by the details pane and diagnostics."""

    def __init__(self, parent=None, key_width: int = 108):
        super(KeyValueGrid, self).__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(3)
        self._rows: List[tuple] = []
        self.key_width = key_width

    def set_rows(self, rows: Iterable[tuple]) -> None:
        """``rows`` is an iterable of ``(label, value)`` pairs; empty values hide."""
        self.clear()
        for label, value in rows or ():
            if value is None or value == "" or value == "-":
                continue
            row = QWidget(self)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)

            key = QLabel(str(label))
            key.setObjectName("SecondaryText")
            key.setFixedWidth(self.key_width)
            key.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            layout.addWidget(key)

            val = QLabel(str(value))
            val.setObjectName("DetailValue")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(val, 1)

            self._layout.addWidget(row)
            self._rows.append(row)

    def clear(self) -> None:
        for row in self._rows:
            self._layout.removeWidget(row)
            detach_and_delete(row)
        self._rows = []
