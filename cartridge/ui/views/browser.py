"""Collection Browser: search, facets, sorting, cover grid / list, details.

This is the everyday view and the one that has to stay smooth on a Core 2 Duo with
2 GB of RAM and a mechanical disk, so the design decisions here are about cost:

* **Cards are recycled, not rebuilt.** A pool of :class:`GameCard` widgets the size
  of the viewport is re-populated as the user scrolls. Browsing 1,000 or 10,000
  games therefore costs the same widget count as browsing 20 - rebuilding every
  card per keystroke is exactly what brief section 12 warns against.
* **The database does the filtering.** Search and facets become one parameterized
  query (:mod:`cartridge.core.filtering`); no full-collection scan in Python.
* **Search is debounced.** Typing does not issue a query per character.
* **Artwork is loaded from thumbnails** through the bounded cache in
  :mod:`cartridge.artwork.cache`.
* **Long work goes to the worker pool.** A rescan, a metadata refresh or a folder
  measurement never blocks painting; the view shows a busy state and stays
  interactive, and controls are always re-enabled on failure or cancellation.

When the details column does not fit (window narrower than the reference layout),
it collapses into a slide-over panel rather than crushing the grid, so 1280x800
and smaller both stay usable.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractScrollArea,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from cartridge.app_state import AppState
from cartridge.core import filtering
from cartridge.core.models import Game
from cartridge.ui.widgets.common import (
    Chip,
    EmptyState,
    IconButton,
    Separator,
    detach_and_delete,
)
from cartridge.ui.widgets.detail_pane import DetailPane
from cartridge.ui.widgets.game_card import CARD_WIDTH, GameCard, GameRow

SEARCH_DEBOUNCE_MS = 220
VIEW_GRID = "grid"
VIEW_LIST = "list"

SORT_LABELS = (
    ("title", "Title"),
    ("year", "Release year"),
    ("rating", "Score"),
    ("added", "Date added"),
    ("developer", "Developer"),
    ("folder", "Folder name"),
    ("size", "Folder size"),
)

VIEW_LABELS = (
    (filtering.VIEW_ALL, "Collection"),
    (filtering.VIEW_FAVOURITES, "Favourites"),
    (filtering.VIEW_RECENT, "Recently added"),
    (filtering.VIEW_NEEDS_ATTENTION, "Needs attention"),
    (filtering.VIEW_INCOMPLETE, "Incomplete metadata"),
    (filtering.VIEW_MISSING_ARTWORK, "Missing artwork"),
    (filtering.VIEW_MISSING_FOLDER, "Missing folders"),
)


class FilterSidebar(QWidget):
    """Search field, view selector and facet chips."""

    filters_changed = pyqtSignal()
    clear_requested = pyqtSignal()

    def __init__(self, parent=None):
        super(FilterSidebar, self).__init__(parent)
        self.setObjectName("FilterPanel")
        # QWidget subclasses need this to paint their stylesheet background.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(196)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.search = QLineEdit()
        self.search.setObjectName("SearchField")
        self.search.setPlaceholderText("Search titles, people, tags…")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)

        self.view_combo = QComboBox()
        for value, label in VIEW_LABELS:
            self.view_combo.addItem(label, value)
        layout.addWidget(self.view_combo)

        self.clear_button = IconButton("Clear filters", "Reset search and facets")
        self.clear_button.setObjectName("GhostButton")
        layout.addWidget(self.clear_button)

        layout.addWidget(Separator())

        self.sort_label = QLabel("Sort by")
        self.sort_label.setObjectName("SecondaryText")
        layout.addWidget(self.sort_label)

        self.sort_combo = QComboBox()
        for value, label in SORT_LABELS:
            self.sort_combo.addItem(label, value)
        layout.addWidget(self.sort_combo)

        self.descending = QPushButton("Ascending")
        self.descending.setObjectName("GhostButton")
        self.descending.setCheckable(True)
        self.descending.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.descending)

        layout.addWidget(Separator())

        self.facet_scroll = QScrollArea()
        self.facet_scroll.setWidgetResizable(True)
        self.facet_scroll.setFrameShape(QFrame.NoFrame)
        self.facet_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.facet_scroll, 1)

        self.facet_body = QWidget()
        self.facet_layout = QVBoxLayout(self.facet_body)
        self.facet_layout.setContentsMargins(0, 0, 4, 0)
        self.facet_layout.setSpacing(6)
        self.facet_layout.addStretch(1)
        self.facet_scroll.setWidget(self.facet_body)

        self.genre_chips: List[Chip] = []
        self.type_chips: List[Chip] = []
        self.decade_chips: List[Chip] = []
        self._facet_groups: Dict[str, List[Chip]] = {}

        self.clear_button.clicked.connect(self.clear_requested.emit)
        self.view_combo.currentIndexChanged.connect(lambda _i: self.filters_changed.emit())
        self.sort_combo.currentIndexChanged.connect(lambda _i: self.filters_changed.emit())
        self.descending.toggled.connect(self._on_direction_toggled)

    # ------------------------------------------------------------------
    def _on_direction_toggled(self, checked: bool) -> None:
        self.descending.setText("Descending" if checked else "Ascending")
        self.filters_changed.emit()

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def spec(self) -> filtering.FilterSpec:
        """Build the current filter specification from the widgets."""
        return filtering.FilterSpec(
            query=self.search.text().strip(),
            view=str(self.view_combo.currentData() or filtering.VIEW_ALL),
            genres=[chip.value for chip in self.genre_chips if chip.isChecked()],
            content_types=[chip.value for chip in self.type_chips if chip.isChecked()],
            decades=[chip.value for chip in self.decade_chips if chip.isChecked()],
            sort=str(self.sort_combo.currentData() or filtering.DEFAULT_SORT),
            descending=self.descending.isChecked(),
        )

    def apply_spec(self, spec: filtering.FilterSpec) -> None:
        """Push a spec into the widgets (used to restore saved state)."""
        self.search.blockSignals(True)
        self.search.setText(spec.query)
        self.search.blockSignals(False)
        index = self.view_combo.findData(spec.view)
        if index >= 0:
            self.view_combo.setCurrentIndex(index)
        index = self.sort_combo.findData(spec.sort)
        if index >= 0:
            self.sort_combo.setCurrentIndex(index)
        self.descending.setChecked(spec.descending)
        for chip in self.genre_chips:
            chip.setChecked(chip.value in spec.genres)
        for chip in self.type_chips:
            chip.setChecked(chip.value in spec.content_types)
        for chip in self.decade_chips:
            chip.setChecked(chip.value in spec.decades)

    def clear(self) -> None:
        self.search.clear()
        self.view_combo.setCurrentIndex(0)
        for chips in self._facet_groups.values():
            for chip in chips:
                chip.setChecked(False)

    # ------------------------------------------------------------------
    def set_facets(self, facets: Dict[str, List[tuple]]) -> None:
        """Rebuild the facet chips from database counts."""
        for group in list(self._facet_groups.keys()):
            self._clear_group(group)

        self._build_group("Content type", "content_types", facets.get("content_types", []), 12)
        self._build_group("Genre", "genres", facets.get("genres", []), 12)
        self._build_group("Decade", "decades", facets.get("decades", []), 10)

    def _build_group(self, title: str, key: str, rows: List[tuple], limit: int) -> None:
        if not rows:
            return
        label = QLabel(title)
        label.setObjectName("SectionTitle")
        self.facet_layout.insertWidget(self.facet_layout.count() - 1, label)

        chips: List[Chip] = []
        for name, count in rows[:limit]:
            chip = Chip(str(name), int(count))
            chip.toggled.connect(lambda _state: self.filters_changed.emit())
            self.facet_layout.insertWidget(self.facet_layout.count() - 1, chip)
            chips.append(chip)

        if len(rows) > limit:
            more = QLabel("+%d more" % (len(rows) - limit))
            more.setObjectName("MutedText")
            self.facet_layout.insertWidget(self.facet_layout.count() - 1, more)

        self._facet_groups[key] = chips
        if key == "genres":
            self.genre_chips = chips
        elif key == "content_types":
            self.type_chips = chips
        elif key == "decades":
            self.decade_chips = chips

    def _clear_group(self, key: str) -> None:
        chips = self._facet_groups.pop(key, [])
        for chip in chips:
            self.facet_layout.removeWidget(chip)
            detach_and_delete(chip)
        # Remove the group heading and any "+N more" label that followed it.
        for index in reversed(range(self.facet_layout.count())):
            item = self.facet_layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, QLabel) and widget.objectName() in ("SectionTitle", "MutedText"):
                text = widget.text()
                if text in ("Content type", "Genre", "Decade") or text.endswith("more"):
                    self.facet_layout.removeWidget(widget)
                    detach_and_delete(widget)
        if key == "genres":
            self.genre_chips = []
        elif key == "content_types":
            self.type_chips = []
        elif key == "decades":
            self.decade_chips = []


class CardGrid(QWidget):
    """Recycling cover grid.

    Only the widgets that fit in the viewport exist. Scrolling re-populates them
    from the current page of results, which keeps both widget count and memory
    flat regardless of collection size.
    """

    selection_changed = pyqtSignal(object)
    activated = pyqtSignal(object)
    favourite_toggled = pyqtSignal(object, bool)

    def __init__(self, state: AppState, mode: str = VIEW_GRID, parent=None):
        super(CardGrid, self).__init__(parent)
        self.state = state
        self.mode = mode
        self._games: List[Game] = []
        self._selected: Optional[int] = None
        self._pool: List[QWidget] = []
        self._columns = 5
        self._rows_visible = 3

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._on_scroll)
        self.scroll.verticalScrollBar().valueChanged.connect(lambda _v: self._render())

        self.canvas = QWidget()
        self.canvas.setObjectName("CoverGridCanvas")
        self.canvas.setAttribute(Qt.WA_StyledBackground, True)
        self.grid = QGridLayout(self.canvas)
        self.grid.setContentsMargins(8, 8, 8, 8)
        self.grid.setSpacing(8)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.canvas)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.scroll)

    # ------------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        self._release_pool()
        self._render()

    def set_games(self, games: List[Game]) -> None:
        self._games = list(games)
        self.canvas.setMinimumHeight(self._total_height())
        self._render()

    def games(self) -> List[Game]:
        return list(self._games)

    def selected_id(self) -> Optional[int]:
        return self._selected

    def select(self, game_id: Optional[int], scroll_to: bool = True) -> None:
        self._selected = game_id
        if scroll_to and game_id is not None:
            self._scroll_to(game_id)
        self._render()

    def clear_selection(self) -> None:
        self.select(None, scroll_to=False)

    # ------------------------------------------------------------------
    def _total_height(self) -> int:
        if not self._games:
            return 0
        item_height = self._item_height()
        if self.mode == VIEW_LIST:
            return len(self._games) * (item_height + 2)
        rows = (len(self._games) + max(1, self._columns) - 1) // max(1, self._columns)
        return rows * (item_height + 8) + 16

    def _item_height(self) -> int:
        return GameRow.HEIGHT if self.mode == VIEW_LIST else 320

    def _visible_range(self):
        """Indices of the results currently in (or just outside) the viewport."""
        if not self._games:
            return 0, 0
        bar = self.scroll.verticalScrollBar()
        item_height = self._item_height() + (2 if self.mode == VIEW_LIST else 8)
        viewport_height = max(120, self.scroll.viewport().height())
        first = max(0, int(bar.value() // max(1, item_height)))
        per_row = 1 if self.mode == VIEW_LIST else max(1, self._columns)
        visible_rows = max(1, int(viewport_height // max(1, item_height)) + 1)
        # One extra row above and below: scrolling then never shows empty space.
        first = max(0, first - 1) * per_row
        last = min(len(self._games), first + (visible_rows + 2) * per_row)
        return first, last

    def _ensure_pool(self, needed: int) -> None:
        while len(self._pool) < needed:
            widget = self._make_item()
            self._pool.append(widget)

    def _make_item(self) -> QWidget:
        if self.mode == VIEW_LIST:
            row = GameRow(cache=self.state.image_cache)
            row.clicked.connect(self._on_clicked)
            row.double_clicked.connect(self._on_activated)
            return row
        card = GameCard(cache=self.state.image_cache)
        card.clicked.connect(self._on_clicked)
        card.double_clicked.connect(self._on_activated)
        card.favourite_toggled.connect(self._on_favourite)
        return card

    def _release_pool(self) -> None:
        for widget in self._pool:
            self.grid.removeWidget(widget)
            widget.deleteLater()
        self._pool = []

    def _render(self) -> None:
        if not self._games:
            self._release_pool()
            return
        first, last = self._visible_range()
        needed = max(1, last - first)
        self._ensure_pool(needed + 2)

        columns = 1 if self.mode == VIEW_LIST else max(1, self._columns)
        for index, widget in enumerate(self._pool):
            position = first + index
            if position >= last or position >= len(self._games):
                widget.setVisible(False)
                continue
            game = self._games[position]
            widget.set_game(game)
            if hasattr(widget, "set_selected"):
                widget.set_selected(game.id == self._selected)
            row, column = divmod(index, columns)
            self.grid.removeWidget(widget)
            self.grid.addWidget(widget, row, column)
            widget.setVisible(True)

    def _on_scroll(self, _minimum: int, _maximum: int) -> None:
        self._render()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super(CardGrid, self).resizeEvent(event)
        if self.mode == VIEW_GRID:
            width = max(120, self.scroll.viewport().width() - 16)
            columns = max(1, min(8, width // (CARD_WIDTH + 8)))
            if columns != self._columns:
                self._columns = columns
            self.canvas.setMinimumHeight(self._total_height())
        self._render()

    def _scroll_to(self, game_id: int) -> None:
        try:
            index = [game.id for game in self._games].index(game_id)
        except ValueError:
            return
        item_height = self._item_height() + (2 if self.mode == VIEW_LIST else 8)
        per_row = 1 if self.mode == VIEW_LIST else max(1, self._columns)
        row = index // per_row
        self.scroll.verticalScrollBar().setValue(max(0, row * item_height - item_height))

    # ------------------------------------------------------------------
    def _on_clicked(self, game_id: int) -> None:
        self._selected = game_id
        game = self._find(game_id)
        self._render()
        self.selection_changed.emit(game)

    def _on_activated(self, game_id: int) -> None:
        game = self._find(game_id)
        if game is not None:
            self.activated.emit(game)

    def _on_favourite(self, game_id: int, _state: bool) -> None:
        game = self._find(game_id)
        if game is not None:
            self.favourite_toggled.emit(game, not game.favourite)

    def _find(self, game_id: int) -> Optional[Game]:
        for game in self._games:
            if game.id == game_id:
                return game
        return None

    # ------------------------------------------------------------------
    def move_selection(self, delta: int) -> None:
        """Keyboard navigation through the visible results."""
        if not self._games:
            return
        ids = [game.id for game in self._games]
        if self._selected not in ids:
            self._on_clicked(ids[0])
            return
        index = max(0, min(len(ids) - 1, ids.index(self._selected) + delta))
        self._on_clicked(ids[index])

    def move_selection_page(self, direction: int) -> None:
        step = max(1, self._columns if self.mode == VIEW_GRID else 10)
        self.move_selection(step * direction)


class BrowserView(QWidget):
    """The Collection Browser (and, with ``favourites_only``, the Favourites view)."""

    counts_changed = pyqtSignal(str)
    busy_changed = pyqtSignal(bool, str)
    status_message = pyqtSignal(str)
    navigate_requested = pyqtSignal(str)
    rescan_requested = pyqtSignal()

    def __init__(self, state: AppState, favourites_only: bool = False, parent=None):
        super(BrowserView, self).__init__(parent)
        self.state = state
        self.favourites_only = favourites_only
        self._loaded: List[Game] = []
        self._busy = False
        self._pending_refresh = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = FilterSidebar()
        if favourites_only:
            self.sidebar.view_combo.setCurrentIndex(
                max(0, self.sidebar.view_combo.findData(filtering.VIEW_FAVOURITES))
            )
            self.sidebar.view_combo.setEnabled(False)
        layout.addWidget(self.sidebar)

        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 8, 10, 8)
        toolbar_layout.setSpacing(6)

        self.result_label = QLabel("—")
        self.result_label.setObjectName("SecondaryText")
        toolbar_layout.addWidget(self.result_label)
        toolbar_layout.addStretch(1)

        self.grid_button = IconButton("▦ Grid", "Cover grid")
        self.grid_button.setCheckable(True)
        self.grid_button.setChecked(True)
        self.list_button = IconButton("☰ List", "Compact list")
        self.list_button.setCheckable(True)
        toolbar_layout.addWidget(self.grid_button)
        toolbar_layout.addWidget(self.list_button)

        self.refresh_button = IconButton("⟳", "Reload the collection from the database")
        self.refresh_button.clicked.connect(self.refresh)
        toolbar_layout.addWidget(self.refresh_button)

        main_layout.addWidget(toolbar)
        main_layout.addWidget(Separator())

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(False)

        self.grid = CardGrid(state, mode=VIEW_GRID)
        self.splitter.addWidget(self.grid)

        self.details = DetailPane(cache=state.image_cache)
        self.splitter.addWidget(self.details)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([820, 380])
        main_layout.addWidget(self.splitter, 1)

        self.empty = EmptyState(
            "No games yet",
            "Add a games root folder in Settings, then rescan to catalogue it. "
            "Nothing is moved or renamed — Cartridge only reads.",
            "Open Settings",
        )
        self.empty.action_requested.connect(lambda: self.navigate_requested.emit("settings"))
        self.empty.setVisible(False)
        main_layout.addWidget(self.empty, 1)

        layout.addWidget(main, 1)

        self.grid.selection_changed.connect(self._on_selection_changed)
        self.grid.activated.connect(self._on_activated)
        self.grid.favourite_toggled.connect(self._on_favourite)
        self.details.open_folder_requested.connect(self._on_open_folder_id)
        self.details.edit_requested.connect(self._on_edit_id)
        self.details.favourite_requested.connect(self._on_favourite_id)
        self.details.refresh_metadata_requested.connect(self._on_refresh_metadata)
        self.details.measure_size_requested.connect(self._on_measure_size)
        self.details.screenshot_clicked.connect(self._on_screenshot_clicked)

        self.sidebar.filters_changed.connect(self._on_filters_changed)
        self.sidebar.clear_requested.connect(self._on_clear_filters)
        self.grid_button.toggled.connect(self._on_mode_toggled)
        self.list_button.toggled.connect(self._on_mode_toggled)

        # Debounce keystrokes so a fast typist does not issue a query per
        # character (brief section 12).
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._run_query)
        self.sidebar.search.textChanged.connect(self._on_search_text_changed)

    # ------------------------------------------------------------------
    # public API used by MainWindow
    # ------------------------------------------------------------------
    def view_shown(self) -> None:
        if self._pending_refresh or not self._loaded:
            self.refresh()

    def view_hidden(self) -> None:
        self._pending_refresh = False

    def focus_search(self) -> None:
        self.sidebar.focus_search()

    def describe_counts(self) -> str:
        return "%d games shown · %d in collection" % (
            len(self._loaded), self._total_count()
        )

    def refresh(self) -> None:
        """Reload facets and results from the database."""
        if not self.state.ready:
            self._show_empty(
                "No collection loaded",
                "Choose a games root folder in Settings to create or open a collection "
                "database.",
                "Open Settings",
            )
            self.result_label.setText("No database")
            return
        self._load_facets()
        self._run_query()

    # ------------------------------------------------------------------
    def _load_facets(self) -> None:
        try:
            facets = self.state.repo.facets()
        except Exception as exc:
            self.status_message.emit("Could not load filters: %s" % exc)
            return
        self.sidebar.set_facets(facets)
        # Re-apply the current spec: rebuilding chips clears their checked state.
        self.sidebar.apply_spec(self._current_spec())

    def _current_spec(self) -> filtering.FilterSpec:
        spec = self.sidebar.spec()
        if self.favourites_only:
            spec.view = filtering.VIEW_FAVOURITES
        return spec

    def _on_search_text_changed(self, _text: str) -> None:
        self._debounce.start()

    def _on_filters_changed(self) -> None:
        self._debounce.start()

    def _on_clear_filters(self) -> None:
        self.sidebar.clear()
        if self.favourites_only:
            self.sidebar.view_combo.setCurrentIndex(
                max(0, self.sidebar.view_combo.findData(filtering.VIEW_FAVOURITES))
            )
        self._run_query()

    def _on_mode_toggled(self, checked: bool) -> None:
        if not checked:
            return
        if self.sender() is self.grid_button:
            self.list_button.setChecked(False)
            self.grid.set_mode(VIEW_GRID)
            self.state.set_setting("ui.browser_mode", VIEW_GRID)
        else:
            self.grid_button.setChecked(False)
            self.grid.set_mode(VIEW_LIST)
            self.state.set_setting("ui.browser_mode", VIEW_LIST)

    # ------------------------------------------------------------------
    def _run_query(self) -> None:
        self._debounce.stop()
        if not self.state.ready:
            return
        spec = self._current_spec()
        started = filtering_now()
        try:
            with self.state.time("browser_query"):
                games = self.state.repo.list_games(spec)
                total = self.state.repo.count_games(spec)
        except Exception as exc:
            self.status_message.emit("Search failed: %s" % exc)
            self._show_error()
            return
        elapsed_ms = (filtering_now() - started) * 1000.0

        self._loaded = games
        self.grid.set_games(games)
        if games:
            self.empty.setVisible(False)
            self.grid.setVisible(True)
        else:
            self._show_empty_for(spec)

        self.result_label.setText(
            "%d %s · %.0f ms" % (total, "game" if total == 1 else "games", elapsed_ms)
        )
        self.counts_changed.emit(self.describe_counts())

        # Keep a sensible selection: the previously selected game if it is still
        # in the result set, otherwise the first result.
        selected = self.grid.selected_id()
        ids = [game.id for game in games]
        if selected in ids:
            self.grid.select(selected, scroll_to=False)
        elif games:
            self.grid.select(games[0].id, scroll_to=False)
            self._on_selection_changed(games[0])
        else:
            self.grid.clear_selection()
            self.details.set_game(None)

    def _total_count(self) -> int:
        if not self.state.ready:
            return 0
        try:
            return int(self.state.repo.count_games())
        except Exception:
            return 0

    def _show_empty_for(self, spec: filtering.FilterSpec) -> None:
        if spec.query or not spec.is_empty():
            self._show_empty(
                "Nothing matches those filters",
                "Try a shorter search term, or clear the filters to see the whole "
                "collection.",
                "Clear filters",
            )
            self.empty.action_requested.disconnect()
            self.empty.action_requested.connect(self._on_clear_filters)
        elif self._total_count() == 0:
            self._show_empty(
                "No games yet",
                "Add a games root folder in Settings, then rescan. Cartridge reads "
                "your folders; it never moves, renames or deletes anything.",
                "Open Settings",
            )
        else:
            self._show_empty("This view is empty", "", "")

    def _show_empty(self, title: str, body: str, action: str) -> None:
        self.empty.set_state(title, body, action)
        self.empty.setVisible(True)
        self.grid.setVisible(False)

    def _show_error(self) -> None:
        self.empty.set_state(
            "Something went wrong",
            "The collection could not be read. Diagnostics has the database path and "
            "an integrity check.",
            "Open Diagnostics",
        )
        self.empty.setVisible(True)
        self.grid.setVisible(False)

    # ------------------------------------------------------------------
    def _on_selection_changed(self, game: Optional[Game]) -> None:
        if game is None:
            self.details.set_game(None)
            return
        try:
            full = self.state.repo.get_game(game.id) or game
        except Exception:
            full = game
        self.details.set_game(full)

    def _on_activated(self, game: Game) -> None:
        self._open_folder(game.folder_path)

    def _on_open_folder_id(self, game_id: int) -> None:
        game = self._by_id(game_id)
        if game is not None:
            self._open_folder(game.folder_path)

    def _on_edit_id(self, game_id: int) -> None:
        from cartridge.ui.dialogs.edit_game import EditGameDialog

        game = self._by_id(game_id)
        if game is None or not self.state.ready:
            return
        dialog = EditGameDialog(self.state, game, self)
        if dialog.exec_():
            self.refresh()

    def _on_favourite(self, game: Game, value: bool) -> None:
        self._set_favourite(game.id, value)

    def _on_favourite_id(self, game_id: int, value: bool) -> None:
        self._set_favourite(game_id, value)

    def _set_favourite(self, game_id: Optional[int], value: bool) -> None:
        if game_id is None or not self.state.ready:
            return
        try:
            self.state.repo.set_favourite(game_id, value)
        except Exception as exc:
            self.status_message.emit("Could not update the favourite: %s" % exc)
            return
        self.status_message.emit("Favourite %s" % ("added" if value else "removed"))
        self.refresh()

    def _on_refresh_metadata(self, game_id: int) -> None:
        from cartridge.ui.dialogs.add_game import refresh_metadata_for_game

        game = self._by_id(game_id)
        if game is None:
            return
        refresh_metadata_for_game(self, self.state, game, done=self.refresh)

    def _on_measure_size(self, game_id: int) -> None:
        from cartridge.ui.dialogs.add_game import measure_size_for_game

        game = self._by_id(game_id)
        if game is None:
            return
        measure_size_for_game(self, self.state, game, done=self.refresh)

    def _on_screenshot_clicked(self, path: str) -> None:
        from cartridge.ui.dialogs.image_viewer import ImageViewerDialog

        dialog = ImageViewerDialog(path, self)
        dialog.exec_()

    def _by_id(self, game_id: int) -> Optional[Game]:
        if not self.state.ready:
            return None
        try:
            return self.state.repo.get_game(game_id)
        except Exception:
            return None

    # ------------------------------------------------------------------
    def _open_folder(self, path: str) -> None:
        from cartridge.ui.actions import open_folder

        ok, message = open_folder(path, self)
        self.status_message.emit(message)

    # ------------------------------------------------------------------
    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if key == Qt.Key_Escape:
            self._on_clear_filters()
            return
        if key in (Qt.Key_Down, Qt.Key_Right):
            self.grid.move_selection(1)
            return
        if key in (Qt.Key_Up, Qt.Key_Left):
            self.grid.move_selection(-1)
            return
        if key == Qt.Key_PageDown:
            self.grid.move_selection_page(1)
            return
        if key == Qt.Key_PageUp:
            self.grid.move_selection_page(-1)
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            selected = self.grid.selected_id()
            game = self.grid._find(selected) if selected is not None else None
            if game is not None:
                self._on_activated(game)
            return
        super(BrowserView, self).keyPressEvent(event)


def filtering_now() -> float:
    """Monotonic clock for latency display (never persisted)."""
    from cartridge.core.clock import monotonic

    return monotonic()
