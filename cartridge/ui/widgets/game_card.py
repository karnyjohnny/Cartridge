"""Game card for the cover grid.

Built as a delegate-painted item would be faster, but a widget per card is what
makes the reference design's card layout (cover, title, year, score, developer,
content badges) straightforward to keep legible at 1280x800 - and the card count
on screen is bounded by the viewport, not by the collection size, because the
browser recycles cards through a scroll-area pool rather than instantiating one
widget per game.

Costs are kept low on purpose: no per-card effects, no gradients, no hover
animation. The cover is loaded from the thumbnail cache and a missing or corrupt
file renders a flat placeholder.
"""

from __future__ import annotations

from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from cartridge.artwork.cache import ImageCache
from cartridge.core.models import Game
from cartridge.text import decade_label, human_size, truncate
from cartridge.ui.widgets.common import Badge, RatingLabel
from cartridge.ui.widgets.cover_label import CoverLabel

CARD_WIDTH = 176
COVER_WIDTH = 160
COVER_HEIGHT = 218
MAX_BADGES = 2


class GameCard(QFrame):
    """One cover-grid card. Emits ``clicked`` / ``double_clicked`` / ``favourite``."""

    clicked = pyqtSignal(int)          # game id
    double_clicked = pyqtSignal(int)
    favourite_toggled = pyqtSignal(int, bool)
    open_folder_requested = pyqtSignal(int)

    def __init__(self, cache: Optional[ImageCache] = None, parent=None):
        super(GameCard, self).__init__(parent)
        self.setObjectName("GameCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(CARD_WIDTH, 320)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._game_id: Optional[int] = None
        self._selected = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(5)

        # --- cover row: image + missing-artwork marker --------------------
        cover_box = QWidget()
        cover_layout = QVBoxLayout(cover_box)
        cover_layout.setContentsMargins(0, 0, 0, 0)
        cover_layout.setSpacing(0)

        self.cover = CoverLabel(COVER_WIDTH, COVER_HEIGHT, cache=cache)
        self.cover.set_placeholder_text("No cover")
        cover_layout.addWidget(self.cover)
        layout.addWidget(cover_box)

        # --- title --------------------------------------------------------
        self.title_label = QLabel("")
        self.title_label.setObjectName("CardTitle")
        self.title_label.setWordWrap(False)
        self.title_label.setTextFormat(Qt.PlainText)
        layout.addWidget(self.title_label)

        # --- meta row: year + score ---------------------------------------
        meta_row = QWidget()
        meta_layout = QHBoxLayout(meta_row)
        meta_layout.setContentsMargins(0, 0, 0, 0)
        meta_layout.setSpacing(6)

        self.year_label = QLabel("")
        self.year_label.setObjectName("SecondaryText")
        meta_layout.addWidget(self.year_label)
        meta_layout.addStretch(1)

        self.rating = RatingLabel()
        self.rating.set_rating(None)
        meta_layout.addWidget(self.rating)
        layout.addWidget(meta_row)

        # --- developer ----------------------------------------------------
        self.developer_label = QLabel("")
        self.developer_label.setObjectName("MutedText")
        layout.addWidget(self.developer_label)

        # --- badges -------------------------------------------------------
        # BadgeRow caps the count and adds a "+N" chip: laying every badge out in
        # a fixed-width card squashed them into single letters.
        from cartridge.ui.widgets.common import BadgeRow

        self.badges = BadgeRow(max_badges=MAX_BADGES)
        layout.addWidget(self.badges)
        layout.addStretch(1)

        # --- state line (missing folder / incomplete) ---------------------
        self.state_label = QLabel("")
        self.state_label.setObjectName("ErrorText")
        self.state_label.setVisible(False)
        layout.addWidget(self.state_label)

    # ------------------------------------------------------------------
    def set_game(self, game: Game, image_base: str = "") -> None:
        """Populate the card. Never raises on missing artwork or metadata."""
        self._game_id = game.id
        self.title_label.setText(truncate(game.effective_title(), 34))
        self.title_label.setToolTip(game.effective_title())

        year = game.effective_year()
        self.year_label.setText(str(year) if year else "—")

        self.rating.set_rating(game.effective_rating())

        developer = game.effective_developer() or ""
        self.developer_label.setText(truncate(developer, 26) if developer else "—")
        self.developer_label.setToolTip(developer)

        self._set_badges(game)
        self._set_state(game)
        self._set_cover(game, image_base)
        self.set_selected(self._selected)

    def _set_badges(self, game: Game) -> None:
        items = list(game.content_types)
        prefix = ["★"] if game.favourite else []
        self.badges.set_items(prefix + items, style="accent")
        if game.favourite:
            # The star chip is a marker, not a content type: keep it visible even
            # when the type list is long.
            star = self.badges.layout().itemAt(0)
            if star is not None and star.widget() is not None:
                star.widget().setObjectName("BadgeWarning")
                star.widget().style().unpolish(star.widget())
                star.widget().style().polish(star.widget())
                star.widget().setToolTip("Favourite")

    def _set_state(self, game: Game) -> None:
        from cartridge.paths import FolderState

        if game.folder_state == FolderState.MISSING.value:
            text = "Folder missing"
        elif game.folder_state == FolderState.UNAVAILABLE_DRIVE.value:
            text = "Drive unavailable"
        elif game.folder_state == FolderState.ACCESS_DENIED.value:
            text = "Not accessible"
        elif game.metadata_state in ("none", "error"):
            text = "Incomplete metadata"
        else:
            text = ""
        self.state_label.setText(text)
        self.state_label.setVisible(bool(text))

    def _set_cover(self, game: Game, image_base: str) -> None:
        import os

        cover = game.cover()
        if cover is None:
            self.cover.load(None)
            return
        # Asset paths are stored relative to the game folder.
        candidate = os.path.join(game.folder_path, cover.rel_path.replace("/", os.sep))
        thumb = None
        if image_base:
            thumb = os.path.join(image_base, os.path.splitext(cover.rel_path.replace("/", os.sep))[0] + ".jpg")
        self.cover.load(candidate if os.path.exists(candidate) else candidate, thumb)

    # ------------------------------------------------------------------
    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def game_id(self) -> Optional[int]:
        return self._game_id

    def invalidate_cover(self) -> None:
        self.cover.invalidate()

    # ------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self._game_id is not None:
            self.clicked.emit(self._game_id)
        super(GameCard, self).mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self._game_id is not None:
            self.double_clicked.emit(self._game_id)
        super(GameCard, self).mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self._game_id is not None:
            self.double_clicked.emit(self._game_id)
            return
        if event.key() == Qt.Key_Space and self._game_id is not None:
            self.favourite_toggled.emit(self._game_id, True)
            return
        super(GameCard, self).keyPressEvent(event)


class GameRow(QWidget):
    """Compact list-view row: small cover, title, year, developer, badges."""

    clicked = pyqtSignal(int)
    double_clicked = pyqtSignal(int)

    HEIGHT = 46

    def __init__(self, cache: Optional[ImageCache] = None, parent=None):
        super(GameRow, self).__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self._game_id: Optional[int] = None
        self._selected = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(8)

        self.cover = CoverLabel(30, 40, cache=cache)
        self.cover.set_placeholder_text("")
        layout.addWidget(self.cover)

        self.title_label = QLabel("")
        self.title_label.setObjectName("CardTitle")
        self.title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.title_label, 3)

        self.year_label = QLabel("")
        self.year_label.setObjectName("SecondaryText")
        self.year_label.setFixedWidth(42)
        layout.addWidget(self.year_label)

        self.developer_label = QLabel("")
        self.developer_label.setObjectName("MutedText")
        self.developer_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.developer_label, 2)

        self.rating = RatingLabel()
        layout.addWidget(self.rating)

        self.state_badge = Badge("", "error")
        self.state_badge.setVisible(False)
        layout.addWidget(self.state_badge)

    def set_game(self, game: Game, image_base: str = "") -> None:
        self._game_id = game.id
        self.title_label.setText(truncate(game.effective_title(), 60))
        self.title_label.setToolTip(game.effective_title())
        year = game.effective_year()
        self.year_label.setText(str(year) if year else "—")
        developer = game.effective_developer() or "—"
        self.developer_label.setText(truncate(developer, 40))
        self.rating.set_rating(game.effective_rating())

        from cartridge.paths import FolderState

        if game.folder_state != FolderState.PRESENT.value:
            self.state_badge.set_text("folder")
            self.state_badge.set_style("error")
            self.state_badge.setVisible(True)
            self.state_badge.setToolTip(game.folder_state.replace("_", " "))
        elif game.metadata_state in ("none", "error"):
            self.state_badge.set_text("incomplete")
            self.state_badge.set_style("warning")
            self.state_badge.setVisible(True)
        else:
            self.state_badge.setVisible(False)

        import os

        cover = game.cover()
        if cover is None:
            self.cover.load(None)
        else:
            self.cover.load(os.path.join(game.folder_path, cover.rel_path.replace("/", os.sep)))
        self.set_selected(self._selected)

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self.setStyleSheet(
            "background-color: #26384F; border-radius: 4px;" if selected else ""
        )

    def game_id(self) -> Optional[int]:
        return self._game_id

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self._game_id is not None:
            self.clicked.emit(self._game_id)
        super(GameRow, self).mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._game_id is not None:
            self.double_clicked.emit(self._game_id)
        super(GameRow, self).mouseDoubleClickEvent(event)
