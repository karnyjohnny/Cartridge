"""Game details pane: everything known about the selected game.

Content follows brief section 6.3: cover, title, release details, genres/tags,
franchise, alternative names, age rating, folder path, measured folder size,
provider attribution, description and a small set of screenshots — plus Open
folder / Edit / Favourite actions and a clear missing-artwork or missing-folder
state.

Nothing here touches the network. The pane renders from the local database and
local files only, which is what makes offline browsing work (brief section 12).
"""

from __future__ import annotations

import os
from typing import List, Optional

from PyQt5.QtCore import Qt, QSize, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from cartridge.artwork.cache import ImageCache
from cartridge.core.clock import age_text
from cartridge.core.models import Game
from cartridge.paths import FolderState, shorten_path
from cartridge.text import decade_label, human_size, truncate
from cartridge.ui.widgets.common import (
    Badge,
    IconButton,
    KeyValueGrid,
    RatingLabel,
    Separator,
    detach_and_delete,
)
from cartridge.ui.widgets.cover_label import CoverLabel

DETAIL_COVER_SIZE = (200, 272)
SCREENSHOT_SIZE = (150, 86)
MAX_SCREENSHOTS_SHOWN = 4


class DetailPane(QWidget):
    """Right-hand details column of the Collection Browser."""

    open_folder_requested = pyqtSignal(int)
    edit_requested = pyqtSignal(int)
    favourite_requested = pyqtSignal(int, bool)
    refresh_metadata_requested = pyqtSignal(int)
    measure_size_requested = pyqtSignal(int)
    screenshot_clicked = pyqtSignal(str)

    def __init__(self, cache: Optional[ImageCache] = None, parent=None):
        super(DetailPane, self).__init__(parent)
        self.setObjectName("DetailPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(320)
        self.setMaximumWidth(460)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self._game: Optional[Game] = None
        self._cache = cache
        self._screenshot_labels: List[CoverLabel] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("DetailScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll)

        self.body = QWidget()
        self.layout_body = QVBoxLayout(self.body)
        self.layout_body.setContentsMargins(14, 14, 14, 14)
        self.layout_body.setSpacing(10)
        self.scroll.setWidget(self.body)

        self._build_header()
        self._build_actions()
        self._build_state_banner()
        self._build_facts()
        self._build_summary()
        self._build_screenshots()
        self.layout_body.addStretch(1)

        self.clear()

    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.cover = CoverLabel(DETAIL_COVER_SIZE[0], DETAIL_COVER_SIZE[1], cache=self._cache)
        self.cover.set_placeholder_text("No cover")
        layout.addWidget(self.cover, 0, Qt.AlignTop)

        text_column = QWidget()
        text_layout = QVBoxLayout(text_column)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)

        self.title_label = QLabel("")
        self.title_label.setObjectName("DetailTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text_layout.addWidget(self.title_label)

        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("SecondaryText")
        self.subtitle_label.setWordWrap(True)
        text_layout.addWidget(self.subtitle_label)

        self.rating_row = QWidget()
        rating_layout = QHBoxLayout(self.rating_row)
        rating_layout.setContentsMargins(0, 0, 0, 0)
        rating_layout.setSpacing(6)
        self.rating = RatingLabel()
        rating_layout.addWidget(self.rating)
        self.rating_hint = QLabel("")
        self.rating_hint.setObjectName("MutedText")
        rating_layout.addWidget(self.rating_hint)
        rating_layout.addStretch(1)
        text_layout.addWidget(self.rating_row)

        self.badge_row = QWidget()
        self.badge_layout = QHBoxLayout(self.badge_row)
        self.badge_layout.setContentsMargins(0, 0, 0, 0)
        self.badge_layout.setSpacing(4)
        text_layout.addWidget(self.badge_row)
        self._badges: List[Badge] = []
        text_layout.addStretch(1)

        layout.addWidget(text_column, 1)
        self.layout_body.addWidget(header)

    def _build_actions(self) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.open_button = IconButton("Open folder", "Open this game's folder in Explorer")
        self.open_button.setObjectName("PrimaryButton")
        self.open_button.clicked.connect(self._on_open_folder)
        layout.addWidget(self.open_button)

        self.edit_button = IconButton("Edit", "Edit title, notes and metadata")
        self.edit_button.clicked.connect(self._on_edit)
        layout.addWidget(self.edit_button)

        self.favourite_button = IconButton("☆ Favourite", "Toggle favourite")
        self.favourite_button.setCheckable(True)
        self.favourite_button.setObjectName("GhostButton")
        self.favourite_button.clicked.connect(self._on_favourite)
        layout.addWidget(self.favourite_button)

        layout.addStretch(1)

        self.more_button = IconButton("⋯", "More actions")
        self.more_button.clicked.connect(self._show_more_menu)
        layout.addWidget(self.more_button)

        self.layout_body.addWidget(row)
        self.layout_body.addWidget(Separator())

    def _build_state_banner(self) -> None:
        self.banner = QLabel("")
        self.banner.setObjectName("BadgeWarning")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.layout_body.addWidget(self.banner)

    def _build_facts(self) -> None:
        self.facts = KeyValueGrid(key_width=104)
        self.layout_body.addWidget(self.facts)

    def _build_summary(self) -> None:
        self.summary_title = QLabel("Description")
        self.summary_title.setObjectName("SectionTitle")
        self.summary_title.setVisible(False)
        self.layout_body.addWidget(self.summary_title)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("SecondaryText")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.summary_label.setVisible(False)
        self.layout_body.addWidget(self.summary_label)

    def _build_screenshots(self) -> None:
        self.shots_title = QLabel("Screenshots")
        self.shots_title.setObjectName("SectionTitle")
        self.shots_title.setVisible(False)
        self.layout_body.addWidget(self.shots_title)

        self.shots_widget = QWidget()
        self.shots_layout = QGridLayout(self.shots_widget)
        self.shots_layout.setContentsMargins(0, 0, 0, 0)
        self.shots_layout.setSpacing(6)
        self.shots_widget.setVisible(False)
        self.layout_body.addWidget(self.shots_widget)

    # ------------------------------------------------------------------
    def set_game(self, game: Optional[Game]) -> None:
        """Render one game. ``None`` shows the empty state."""
        self._game = game
        if game is None:
            self.clear()
            return

        self.title_label.setText(game.effective_title())
        self.subtitle_label.setText(self._subtitle(game))
        self.rating.set_rating(game.effective_rating())
        self.rating_hint.setText(
            self._rating_hint(game)
        )
        self._set_badges(game)
        self._set_banner(game)
        self._set_facts(game)
        self._set_summary(game)
        self._set_cover(game)
        self._set_screenshots(game)

        usable = game.is_folder_usable()
        self.open_button.setEnabled(usable)
        self.open_button.setToolTip(
            "Open this game's folder" if usable else "The folder is not available"
        )
        self.favourite_button.setChecked(bool(game.favourite))
        self.favourite_button.setText(
            "★ Favourite" if game.favourite else "☆ Favourite"
        )

    def clear(self) -> None:
        self._game = None
        self.title_label.setText("No game selected")
        self.subtitle_label.setText("Pick a title from the collection to see its details.")
        self.rating.set_rating(None)
        self.rating_hint.setText("")
        self.cover.clear_image()
        self.facts.clear()
        self.summary_label.setVisible(False)
        self.summary_title.setVisible(False)
        self.shots_widget.setVisible(False)
        self.shots_title.setVisible(False)
        self.banner.setVisible(False)
        for badge in self._badges:
            detach_and_delete(badge)
        self._badges = []
        for label in self._screenshot_labels:
            detach_and_delete(label)
        self._screenshot_labels = []
        self.open_button.setEnabled(False)
        self.edit_button.setEnabled(False)
        self.favourite_button.setEnabled(False)
        self.more_button.setEnabled(False)

    # ------------------------------------------------------------------
    def _subtitle(self, game: Game) -> str:
        bits = []
        year = game.effective_year()
        if year:
            bits.append(str(year))
        developer = game.effective_developer()
        if developer:
            bits.append(developer)
        franchise = game.effective_franchise()
        if franchise:
            bits.append(franchise)
        return " · ".join(bits) if bits else "Release details unknown"

    def _rating_hint(self, game: Game) -> str:
        if game.effective_rating() is None:
            return "no score"
        if game.user_rating is not None:
            return "your score"
        return "provider score"

    def _set_badges(self, game: Game) -> None:
        for badge in self._badges:
            detach_and_delete(badge)
        self._badges = []
        layout = self.badge_layout

        for name in game.content_types[:6]:
            badge = Badge(name, "accent")
            layout.addWidget(badge)
            self._badges.append(badge)
        if game.metadata_state == "manual":
            badge = Badge("manual entry", "default")
            layout.addWidget(badge)
            self._badges.append(badge)
        elif game.provider:
            badge = Badge(game.provider.upper(), "default")
            badge.setToolTip("Metadata from %s" % game.provider.upper())
            layout.addWidget(badge)
            self._badges.append(badge)
        if game.has_user_overrides():
            badge = Badge("edited", "success")
            badge.setToolTip("You edited some of these fields; a refresh will not overwrite them")
            layout.addWidget(badge)
            self._badges.append(badge)
        layout.addStretch(1)

    def _set_banner(self, game: Game) -> None:
        from cartridge.paths import FolderState

        messages = []
        if game.folder_state == FolderState.MISSING.value:
            messages.append(
                "The folder for this game no longer exists. Metadata is kept; the "
                "record is not deleted. Use Manager → re-associate if you moved it."
            )
        elif game.folder_state == FolderState.UNAVAILABLE_DRIVE.value:
            messages.append(
                "The drive holding this game is not available right now (an unplugged "
                "disk or an offline network share). Nothing was changed."
            )
        elif game.folder_state == FolderState.ACCESS_DENIED.value:
            messages.append("This folder could not be read (access denied).")
        if game.artwork_state() == "missing":
            messages.append("No artwork stored locally yet.")
        if game.metadata_state == "error" and game.metadata_error:
            messages.append("Last metadata refresh failed: %s" % game.metadata_error)
        elif game.metadata_state == "none":
            messages.append("No metadata imported yet.")

        if messages:
            self.banner.setText("  ".join(messages))
            self.banner.setObjectName(
                "BadgeError" if game.folder_state != FolderState.PRESENT.value
                else "BadgeWarning"
            )
            self.banner.style().unpolish(self.banner)
            self.banner.style().polish(self.banner)
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)

    def _set_facts(self, game: Game) -> None:
        rows = [
            ("Release", self._release_text(game)),
            ("Genres", ", ".join(game.effective_genres()) or ""),
            ("Platforms", ", ".join(game.platforms[:6]) or ""),
            ("Developer", game.effective_developer() or ""),
            ("Publisher", game.effective_publisher() or ""),
            ("Franchise", game.effective_franchise() or ""),
            ("Age rating", game.effective_age_rating() or ""),
            ("Also known as", ", ".join(game.alternative_names[:4]) or ""),
            ("Folder", shorten_path(game.folder_path, 62) if game.folder_path else ""),
            ("Folder size", self._size_text(game)),
            ("Source", self._source_text(game)),
            ("Added", age_text(game.added_at)),
            ("Last verified", age_text(game.last_verified_at)),
        ]
        if game.notes:
            rows.append(("Notes", game.notes))
        self.facts.set_rows(rows)
        # The full path is useful and long: keep it selectable via tooltip.
        for index in range(self.facts.layout().count()):
            item = self.facts.layout().itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setToolTip(game.folder_path if game.folder_path else "")

    def _release_text(self, game: Game) -> str:
        if game.release_date:
            return game.release_date
        year = game.effective_year()
        if year:
            return "%d (%s)" % (year, decade_label(year) or "")
        return ""

    def _size_text(self, game: Game) -> str:
        if game.folder_size_bytes is None:
            return "not measured"
        when = age_text(game.folder_size_measured_at)
        return "%s (measured %s)" % (human_size(game.folder_size_bytes), when)

    def _source_text(self, game: Game) -> str:
        if game.provider and game.provider_id:
            return "%s · id %s" % (game.provider.upper(), game.provider_id)
        if game.metadata_state == "manual":
            return "Entered manually"
        return ""

    def _set_summary(self, game: Game) -> None:
        text = game.effective_summary()
        if not text:
            self.summary_title.setVisible(False)
            self.summary_label.setVisible(False)
            return
        self.summary_title.setText(
            "Description (your text)" if game.user_summary else "Description"
        )
        self.summary_label.setText(truncate(text, 1400))
        self.summary_title.setVisible(True)
        self.summary_label.setVisible(True)

    def _set_cover(self, game: Game) -> None:
        path = self._absolute(game, game.cover())
        if not path:
            self.cover.load(None)
            return
        thumb = self._thumb_path(game, game.cover())
        self.cover.load(path, thumb)

    def _set_screenshots(self, game: Game) -> None:
        for label in self._screenshot_labels:
            detach_and_delete(label)
        self._screenshot_labels = []
        while self.shots_layout.count():
            item = self.shots_layout.takeAt(0)
            detach_and_delete(item.widget())

        shots = game.screenshots()[:MAX_SCREENSHOTS_SHOWN]
        if not shots:
            self.shots_widget.setVisible(False)
            self.shots_title.setVisible(False)
            return

        for index, asset in enumerate(shots):
            label = CoverLabel(SCREENSHOT_SIZE[0], SCREENSHOT_SIZE[1], cache=self._cache)
            label.set_placeholder_text("screenshot")
            path = self._absolute(game, asset)
            if path:
                label.load(path, self._thumb_path(game, asset))
            label.setCursor(Qt.PointingHandCursor)
            if path:
                label.mousePressEvent = self._screenshot_click_handler(path)
            self.shots_layout.addWidget(label, index // 2, index % 2)
            self._screenshot_labels.append(label)

        self.shots_widget.setVisible(True)
        self.shots_title.setVisible(True)
        self.shots_title.setText(
            "Screenshots (%d stored)" % len(game.screenshots())
        )

    def _screenshot_click_handler(self, path: str):
        def handler(event):
            if event.button() == Qt.LeftButton:
                self.screenshot_clicked.emit(path)
        return handler

    # ------------------------------------------------------------------
    def _absolute(self, game: Game, asset) -> Optional[str]:
        if asset is None or not game.folder_path:
            return None
        candidate = os.path.join(
            game.folder_path, asset.rel_path.replace("/", os.sep)
        )
        return candidate if os.path.exists(candidate) else None

    def _thumb_path(self, game: Game, asset) -> Optional[str]:
        if asset is None or not game.folder_path:
            return None
        base = os.path.splitext(asset.rel_path.replace("/", os.sep))[0]
        candidate = os.path.join(game.folder_path, base + ".thumbs.jpg")
        asset_dir = os.path.join(game.folder_path, "_cartridge", "assets", "thumbs")
        preferred = os.path.join(
            asset_dir, os.path.splitext(os.path.basename(asset.rel_path))[0] + ".jpg"
        )
        if os.path.exists(preferred):
            return preferred
        return candidate if os.path.exists(candidate) else None

    # ------------------------------------------------------------------
    def refresh_cover(self) -> None:
        """Re-read artwork from disk (after a download finished)."""
        if self._game is not None:
            self._set_cover(self._game)
            self._set_screenshots(self._game)

    def _on_open_folder(self) -> None:
        if self._game is not None and self._game.id is not None:
            self.open_folder_requested.emit(self._game.id)

    def _on_edit(self) -> None:
        if self._game is not None and self._game.id is not None:
            self.edit_requested.emit(self._game.id)

    def _on_favourite(self, checked: bool) -> None:
        if self._game is not None and self._game.id is not None:
            self.favourite_requested.emit(self._game.id, bool(checked))

    def _show_more_menu(self) -> None:
        from PyQt5.QtWidgets import QMenu

        menu = QMenu(self)
        refresh = menu.addAction("Refresh metadata from provider")
        measure = menu.addAction("Measure folder size")
        menu.addSeparator()
        copy_path = menu.addAction("Copy folder path")
        chosen = menu.exec_(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))
        if chosen is None or self._game is None or self._game.id is None:
            return
        if chosen is refresh:
            self.refresh_metadata_requested.emit(self._game.id)
        elif chosen is measure:
            self.measure_size_requested.emit(self._game.id)
        elif chosen is copy_path:
            from PyQt5.QtWidgets import QApplication

            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(self._game.folder_path or "")
