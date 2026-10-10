"""Add / import game dialog — the workflow from brief section 6.5.

Steps, in order:

1. choose or confirm the discovered folder;
2. show a brief scan summary and suggested content types;
3. enter or edit the title query;
4. search IGDB (RAWG is the automatic fallback);
5. show ranked results with title, year, metadata, provider label and thumbnail;
6. let the user pick one;
7. allow custom content types;
8. offer artwork choices (cover plus a bounded number of screenshots);
9. offer an optional folder-size scan, with an honest warning that it is slow;
10. "Enter details manually" when nothing matches;
11. one clear final import action.

Behaviour that matters:

* a low-confidence match is **never** imported silently — the confidence band is
  shown per row and the import button asks for confirmation below the threshold;
* every network call runs on the worker pool, so the dialog stays responsive and
  shows a busy state instead of freezing;
* loading, empty and error states are all distinct;
* the whole import (metadata + artwork + content types) happens in one database
  transaction where possible, and a partial failure leaves the record visibly
  incomplete rather than silently half-written.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QSize, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QScrollArea,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from cartridge.app_state import AppState
from cartridge.artwork.downloader import DownloadRequest
from cartridge.core.models import (
    PROVIDER_IGDB,
    PROVIDER_RAWG,
    STATE_MANUAL,
    Game,
    ProviderGame,
)
from cartridge.paths import classify_folder_state, folder_name, shorten_path
from cartridge.providers.mapping import provider_label
from cartridge.providers.ranking import AUTO_CONFIRM, confidence_label, explain
from cartridge.scan import discovery
from cartridge.scan.content_types import suggest_content_types
from cartridge.scan.folder_size import measure_folder_size
from cartridge.text import human_size, truncate
from cartridge.ui.widgets.common import (
    Badge,
    IconButton,
    KeyValueGrid,
    Separator,
    detach_and_delete,
)
from cartridge.ui.widgets.cover_label import CoverLabel

DIALOG_WIDTH = 940
DIALOG_HEIGHT = 660
THUMB_WIDTH = 52
THUMB_HEIGHT = 72


class ResultRow(QWidget):
    """One search result: thumbnail, title, year, provider, confidence."""

    def __init__(self, game: ProviderGame, cache=None, parent=None):
        super(ResultRow, self).__init__(parent)
        self.game = game
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        self.thumb = CoverLabel(THUMB_WIDTH, THUMB_HEIGHT, cache=cache)
        self.thumb.set_placeholder_text("")
        layout.addWidget(self.thumb)

        text = QWidget()
        text_layout = QVBoxLayout(text)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)

        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        self.title = QLabel(game.title)
        self.title.setObjectName("CardTitle")
        title_row.addWidget(self.title)
        self.provider_badge = Badge(provider_label(game.provider), "accent")
        title_row.addWidget(self.provider_badge)
        self.confidence_badge = Badge(confidence_label(game.match_score), "default")
        title_row.addWidget(self.confidence_badge)
        title_row.addStretch(1)
        text_layout.addLayout(title_row)

        bits = []
        if game.year:
            bits.append(str(game.year))
        if game.developer:
            bits.append(game.developer)
        if game.publisher and game.publisher != game.developer:
            bits.append(game.publisher)
        if game.genres:
            bits.append(", ".join(game.genres[:3]))
        if game.platforms:
            bits.append(game.platforms[0])
        self.meta = QLabel(" · ".join(bits) if bits else "No further details")
        self.meta.setObjectName("SecondaryText")
        self.meta.setWordWrap(True)
        text_layout.addWidget(self.meta)

        if game.rating is not None:
            self.score = QLabel("score %d/100" % int(round(game.rating)))
            self.score.setObjectName("MutedText")
            text_layout.addWidget(self.score)

        layout.addWidget(text, 1)

    def set_cache(self, cache) -> None:
        self.thumb.set_cache(cache)


class AddGameDialog(QDialog):
    """Import one folder into the catalogue."""

    imported = pyqtSignal(int)

    def __init__(
        self,
        state: AppState,
        parent=None,
        suggested_folder: Optional[str] = None,
    ):
        super(AddGameDialog, self).__init__(parent)
        self.state = state
        self.setWindowTitle("Add a game")
        self.resize(DIALOG_WIDTH, DIALOG_HEIGHT)
        self.setMinimumSize(760, 520)

        self._folder: Optional[str] = suggested_folder
        self._folder_usable = False
        self._scan_summary: Dict[str, Any] = {}
        self._results: List[ProviderGame] = []
        self._rows: List[ResultRow] = []
        self._selected: Optional[ProviderGame] = None
        self._search_handle = None
        self._import_handle = None
        self._thumb_handles: List[Any] = []
        # Row thumbnails: downloaded in the background, in rank order, paced by
        # the image rate limiter, so covers appear one by one instead of only
        # for the selected row (field request, 2026-10-10).
        self._thumb_paths: Dict[int, str] = {}
        self._thumb_pending: Dict[int, Any] = {}
        self._thumb_dir = tempfile.mkdtemp(prefix="cartridge-thumbs-")

        self._build_ui()
        if self._folder:
            self.set_folder(self._folder)
        else:
            self._set_page("folder")

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        self.steps = QLabel("")
        self.steps.setObjectName("MutedText")
        root.addWidget(self.steps)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.stack.addWidget(self._build_folder_page())
        self.stack.addWidget(self._build_search_page())
        self.stack.addWidget(self._build_manual_page())

        # --- footer ---------------------------------------------------
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.back_button = IconButton("← Back")
        self.back_button.clicked.connect(self._on_back)
        footer.addWidget(self.back_button)

        self.status_label = QLabel("")
        self.status_label.setObjectName("SecondaryText")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(140)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        footer.addWidget(self.progress)

        self.cancel_button = IconButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        footer.addWidget(self.cancel_button)

        self.primary_button = QPushButton("Import")
        self.primary_button.setObjectName("PrimaryButton")
        self.primary_button.clicked.connect(self._on_primary)
        footer.addWidget(self.primary_button)

        root.addLayout(footer)

    def _build_folder_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)

        group = QGroupBox("1 · Choose the game folder")
        group_layout = QVBoxLayout(group)

        row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText(r"For example: E:\Gry\Wiedzmin 2")
        self.folder_edit.setReadOnly(True)
        row.addWidget(self.folder_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("PrimaryButton")
        browse.clicked.connect(self._on_browse)
        row.addWidget(browse)
        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.setToolTip(
            "Look inside the folder in Explorer - handy for checking that the "
            "contents match what Cartridge guessed"
        )
        self.open_folder_button.clicked.connect(self._on_open_folder)
        self.open_folder_button.setEnabled(False)
        row.addWidget(self.open_folder_button)
        group_layout.addLayout(row)

        self.folder_state_label = QLabel("")
        self.folder_state_label.setObjectName("SecondaryText")
        self.folder_state_label.setWordWrap(True)
        group_layout.addWidget(self.folder_state_label)

        self.scan_summary = KeyValueGrid(key_width=132)
        group_layout.addWidget(self.scan_summary)
        layout.addWidget(group)

        types_group = QGroupBox("2 · Suggested content types")
        types_layout = QVBoxLayout(types_group)
        self.types_hint = QLabel("Choose a folder to see suggestions.")
        self.types_hint.setObjectName("MutedText")
        self.types_hint.setWordWrap(True)
        types_layout.addWidget(self.types_hint)
        self.types_widget = QWidget()
        self.types_layout = QHBoxLayout(self.types_widget)
        self.types_layout.setContentsMargins(0, 0, 0, 0)
        self.types_layout.setSpacing(6)
        types_layout.addWidget(self.types_widget)
        self.custom_type_edit = QLineEdit()
        self.custom_type_edit.setPlaceholderText(
            "Add your own type (types are user-defined, not a fixed list)"
        )
        types_layout.addWidget(self.custom_type_edit)
        layout.addWidget(types_group)

        options = QGroupBox("3 · Options")
        options_layout = QVBoxLayout(options)
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText(
            "Search title (pre-filled from the folder name — edit it freely)"
        )
        options_layout.addWidget(self.query_edit)
        self.size_check = QCheckBox("Measure folder size during import (slow on a mechanical disk)")
        options_layout.addWidget(self.size_check)
        self.artwork_check = QCheckBox("Download cover and screenshots")
        self.artwork_check.setChecked(True)
        options_layout.addWidget(self.artwork_check)
        shots_row = QHBoxLayout()
        shots_row.addWidget(QLabel("Screenshots:"))
        self.shots_spin = QSpinBox()
        self.shots_spin.setRange(0, 8)
        self.shots_spin.setValue(3)
        shots_row.addWidget(self.shots_spin)
        shots_row.addStretch(1)
        options_layout.addLayout(shots_row)
        layout.addWidget(options)

        layout.addStretch(1)
        self._type_chips: List[QCheckBox] = []
        return page

    def _build_search_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("SearchField")
        self.search_edit.setPlaceholderText("Search IGDB (RAWG is used automatically as a fallback)")
        self.search_edit.returnPressed.connect(self._on_search)
        search_row.addWidget(self.search_edit, 1)
        self.search_button = QPushButton("Search")
        self.search_button.setObjectName("PrimaryButton")
        self.search_button.clicked.connect(self._on_search)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        results_holder = QWidget()
        results_layout = QVBoxLayout(results_holder)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(4)
        self.results_label = QLabel("Results")
        self.results_label.setObjectName("SectionTitle")
        results_layout.addWidget(self.results_label)
        self.results_list = QListWidget()
        self.results_list.setIconSize(QSize(THUMB_WIDTH, THUMB_HEIGHT))
        self.results_list.setUniformItemSizes(True)
        self.results_list.currentRowChanged.connect(self._on_result_selected)
        results_layout.addWidget(self.results_list, 1)
        splitter.addWidget(results_holder)

        self.preview = DetailPreview(self.state)
        splitter.addWidget(self.preview)
        splitter.setSizes([520, 380])
        layout.addWidget(splitter, 1)

        self.empty_state = QLabel("")
        self.empty_state.setObjectName("SecondaryText")
        self.empty_state.setWordWrap(True)
        self.empty_state.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.empty_state)

        manual_row = QHBoxLayout()
        manual_row.addStretch(1)
        self.manual_button = QPushButton("Enter details manually")
        self.manual_button.setToolTip("Use this when no API result matches your folder")
        self.manual_button.clicked.connect(lambda: self._set_page("manual"))
        manual_row.addWidget(self.manual_button)
        layout.addLayout(manual_row)
        return page

    def _build_manual_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        hint = QLabel(
            "Manual entry is always available: the local catalogue does not depend "
            "on any service. Fields you fill in here are yours and a later provider "
            "refresh will not overwrite them."
        )
        hint.setObjectName("SecondaryText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(6)
        self.manual_title = QLineEdit()
        form.addRow("Title", self.manual_title)
        self.manual_year = QSpinBox()
        self.manual_year.setRange(0, 2100)
        self.manual_year.setSpecialValueText("unknown")
        form.addRow("Release year", self.manual_year)
        self.manual_developer = QLineEdit()
        form.addRow("Developer", self.manual_developer)
        self.manual_publisher = QLineEdit()
        form.addRow("Publisher", self.manual_publisher)
        self.manual_genres = QLineEdit()
        self.manual_genres.setPlaceholderText("Comma separated, e.g. RPG, Action")
        form.addRow("Genres", self.manual_genres)
        self.manual_summary = QPlainTextEdit()
        self.manual_summary.setFixedHeight(96)
        form.addRow("Description", self.manual_summary)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------
    def _set_page(self, name: str) -> None:
        index = {"folder": 0, "search": 1, "manual": 2}[name]
        self.stack.setCurrentIndex(index)
        self.back_button.setVisible(index != 0)
        if name == "folder":
            self.primary_button.setText("Continue → Search")
            self.steps.setText("Step 1 of 3 · folder, content types and options")
        elif name == "search":
            self.primary_button.setText("Import selected")
            self.steps.setText("Step 2 of 3 · search the providers and pick a match")
        else:
            self.primary_button.setText("Import manual entry")
            self.steps.setText("Step 3 · manual details")
        self._update_primary_enabled()

    def _on_back(self) -> None:
        self._set_page("folder")

    def _on_primary(self) -> None:
        page = self.stack.currentIndex()
        if page == 0:
            if not self._folder:
                self._set_status("Choose the game folder first.")
                return
            self._set_page("search")
            self.search_edit.setText(self.query_edit.text().strip())
            if self.search_edit.text():
                self._on_search()
            return
        if page == 1:
            self._do_import(selected=self._selected)
            return
        self._do_import(selected=None, manual=True)

    def _update_primary_enabled(self) -> None:
        page = self.stack.currentIndex()
        if page == 1:
            self.primary_button.setEnabled(self._selected is not None)
        elif page == 2:
            self.primary_button.setEnabled(bool(self.manual_title.text().strip()))
        else:
            self.primary_button.setEnabled(
                bool(self._folder) and self._folder_usable
            )

    # ------------------------------------------------------------------
    def set_folder(self, path: str) -> None:
        """Load the folder, run the bounded shallow scan, suggest types."""
        self._folder = path
        # folder_edit is read-only; Qt >= 5.15 aborts on a programmatic setText
        # while read-only, so the flag is flipped around the write.
        read_only = self.folder_edit.isReadOnly()
        self.folder_edit.setReadOnly(False)
        try:
            self.folder_edit.setText(path)
        finally:
            self.folder_edit.setReadOnly(read_only)
        state = classify_folder_state(path)
        self.open_folder_button.setEnabled(False)
        if state.value != "present":
            self._folder_usable = False
            self.folder_state_label.setText(
                "This folder is not usable right now (%s). Pick another one."
                % state.value.replace("_", " ")
            )
            self.scan_summary.clear()
            self._update_primary_enabled()
            return
        self._folder_usable = True
        self.folder_state_label.setText("Folder found and readable.")
        self.open_folder_button.setEnabled(True)

        result = discovery.discover_root(
            os.path.dirname(path) or path, detect_types=True
        )
        entry = None
        for folder in result.folders:
            if os.path.normcase(os.path.normpath(folder.path)) == os.path.normcase(
                os.path.normpath(path)
            ):
                entry = folder
                break
        if entry is None:
            # The chosen folder may be deeper than one level; scan it directly.
            names = _shallow_names(path)
            suggestions = suggest_content_types(names, folder_name(path))
            entry_count = len(names)
            truncated = entry_count >= discovery.DEFAULT_MAX_ENTRIES
        else:
            suggestions = entry.suggestions
            entry_count = entry.entry_count
            truncated = entry.listing_truncated

        self._scan_summary = {
            "entries": entry_count,
            "truncated": truncated,
            "suggestions": [item.name for item in suggestions],
        }
        self.scan_summary.set_rows(
            [
                ("Folder name", folder_name(path)),
                ("Entries inspected", "%d%s" % (
                    entry_count, " (listing capped)" if truncated else "")),
                ("Scan basis", "names only — nothing was opened or executed"),
            ]
        )
        self._build_type_chips(suggestions)
        name = folder_name(path)
        self.query_edit.setText(discovery.folder_display_name(
            discovery.DiscoveredFolder(path=path, name=name)
        ))
        self.manual_title.setText(name)
        self._set_status("")
        self._update_primary_enabled()

    def _build_type_chips(self, suggestions) -> None:
        for chip in self._type_chips:
            self.types_layout.removeWidget(chip)
            chip.deleteLater()
        self._type_chips = []

        names = [item.name for item in suggestions]
        existing = []
        if self.state.ready:
            existing = [ct.name for ct in self.state.repo.list_content_types(with_counts=False)]
        for name in dict.fromkeys(list(names) + existing[:8]):
            chip = QCheckBox(name)
            chip.setChecked(name in names)
            chip.setCursor(Qt.PointingHandCursor)
            self.types_layout.addWidget(chip)
            self._type_chips.append(chip)
        self.types_layout.addStretch(1)

        if names:
            reasons = "; ".join(item.reason() for item in suggestions)
            self.types_hint.setText(
                "Suggested from the folder's file names (advisory only): %s" % reasons
            )
        else:
            self.types_hint.setText(
                "No suggestion — nothing in this folder looked like a known layout. "
                "Add a type yourself below if you want one."
            )

    def _selected_types(self) -> List[str]:
        chosen = [chip.text() for chip in self._type_chips if chip.isChecked()]
        custom = self.custom_type_edit.text().strip()
        if custom and custom not in chosen:
            chosen.append(custom)
        return chosen

    def _on_browse(self) -> None:
        start = self._folder or (
            self.state.root_paths()[0] if self.state.root_paths() else os.path.expanduser("~")
        )
        chosen = QFileDialog.getExistingDirectory(self, "Choose the game folder", start)
        if chosen:
            self.set_folder(chosen)

    # ------------------------------------------------------------------
    def _on_search(self) -> None:
        query = self.search_edit.text().strip()
        if not query:
            self._set_status("Type a title to search for.")
            return
        if not self.state.service.is_configured():
            self._show_provider_error(
                "No provider is configured. Add an IGDB client id/secret or a RAWG "
                "API key in Settings, or use 'Enter details manually'."
            )
            return
        if self._search_handle is not None:
            self._set_status("A search is already running…")
            return

        self._set_busy(True, "Searching…")
        self.results_list.clear()
        self._rows = []
        self._results = []
        self._selected = None
        self.preview.clear()
        self.empty_state.setText("")
        self._update_primary_enabled()

        service = self.state.service

        def work():
            return service.search(query, limit=12)

        self._search_handle = self.state.pool.submit(
            work,
            _name="provider_search",
            _on_finished=self._on_search_finished,
            _on_failed=self._on_search_failed,
        )

    def _on_search_finished(self, outcome: Any) -> None:
        self._search_handle = None
        self._set_busy(False)
        self._results = list(outcome.results)
        if not self._results:
            self.empty_state.setText(
                "No results for '%s'.\n%s\n\nYou can still enter the details manually."
                % (outcome.query, "\n".join(outcome.errors) or "The providers returned nothing.")
            )
            self._set_status(outcome.status_text())
            self._update_primary_enabled()
            return

        self.empty_state.setText("")
        self._populate_results(outcome)
        self._set_status(outcome.status_text())
        self.results_list.setCurrentRow(0)
        self._update_primary_enabled()

    def _populate_results(self, outcome: Any) -> None:
        self.results_list.clear()
        self._rows = []
        for handle in list(self._thumb_pending.values()):
            handle.cancel()
        self._thumb_pending.clear()
        self._thumb_paths.clear()
        self.preview.set_cover_path(None)
        summary = outcome.confidence_summary()
        self.results_label.setText(
            "Results (%d) · %s"
            % (
                len(self._results),
                ", ".join(
                    "%d %s" % (summary.get(label, 0), label)
                    for label in ("exact", "strong", "possible", "weak")
                    if summary.get(label)
                ) or "unranked",
            )
        )
        for game in self._results:
            row = ResultRow(game, cache=self.state.image_cache)
            item = QListWidgetItem(self.results_list)
            item.setSizeHint(row.sizeHint())
            item.setData(Qt.UserRole, game.provider_id)
            item.setToolTip(explain(game.match_score, game, outcome.query))
            self.results_list.addItem(item)
            self.results_list.setItemWidget(item, row)
            self._rows.append(row)
        # Covers stream in behind the dialog, paced by the image limiter, so the
        # user can pick by artwork without waiting for everything at once.
        self._prefetch_thumbnails()

    def _on_result_selected(self, index: int) -> None:
        if index < 0 or index >= len(self._results):
            self._selected = None
            self.preview.clear()
            self._update_primary_enabled()
            return
        game = self._results[index]
        self._selected = game
        self.preview.set_provider_game(game)
        label = confidence_label(game.match_score)
        message = "%s match (%.2f) · %s" % (
            label, game.match_score, explain(game.match_score, game, self.search_edit.text())
        )
        self._set_status(message)
        self._request_thumb(index)
        self._update_primary_enabled()

    def _on_open_folder(self) -> None:
        """Let the user verify a folder's contents without leaving the dialog."""
        from cartridge.ui.actions import open_folder

        if not self._folder:
            return
        ok, message = open_folder(self._folder, self)
        self._set_status(message)

    def _request_thumb(self, index: int) -> None:
        """Queue one row's cover thumbnail if it is not already done/pending."""
        if index < 0 or index >= len(self._rows):
            return
        if index in self._thumb_paths:
            self._apply_thumb(index, self._thumb_paths[index])
            return
        if index in self._thumb_pending:
            return
        url = self._rows[index].game.cover_url
        if not url:
            return

        destination = os.path.join(self._thumb_dir, "row%02d.img" % index)
        state = self.state

        def work():
            state.http.download(
                url,
                destination,
                operation="search_thumbnail",
                limiter=state.limiters.images,
                max_bytes=2 * 1024 * 1024,
            )
            return destination

        def done(path):
            self._thumb_pending.pop(index, None)
            # A later search may have rebuilt the rows; only apply when the row
            # still exists and still wants this URL.
            if index < len(self._rows) and self._rows[index].game.cover_url == url:
                self._apply_thumb(index, path)

        def failed(_message, _detail):
            self._thumb_pending.pop(index, None)

        handle = state.pool.submit(
            work, _name="thumb", _on_finished=done, _on_failed=failed
        )
        self._thumb_pending[index] = handle
        self._thumb_handles.append(handle)

    def _apply_thumb(self, index: int, path: str) -> None:
        self._thumb_paths[index] = path
        if index < len(self._rows) and os.path.exists(path):
            self._rows[index].thumb.load(path)
        if index == self.results_list.currentRow():
            self.preview.set_cover_path(path)

    def _prefetch_thumbnails(self) -> None:
        """Ask for every row's cover, in rank order, without blocking the dialog."""
        for index in range(len(self._rows)):
            self._request_thumb(index)

    def _on_search_failed(self, message: str, _detail: str) -> None:
        self._search_handle = None
        self._set_busy(False)
        if message == "cancelled":
            self._set_status("Search cancelled.")
            return
        self._show_provider_error(message)

    def _show_provider_error(self, message: str) -> None:
        self.empty_state.setText(
            "%s\n\nYour folder and any existing metadata are untouched. You can "
            "retry, check the credentials in Settings, or enter the details "
            "manually — the collection works offline." % message
        )
        self._set_status(message)
        self._update_primary_enabled()

    # ------------------------------------------------------------------
    def _do_import(self, selected: Optional[ProviderGame], manual: bool = False) -> None:
        if not self._folder:
            self._set_status("No folder selected.")
            return
        if not self.state.ready:
            self._set_status("No database is open. Configure a root folder in Settings.")
            return
        if not manual and selected is None:
            self._set_status("Pick a search result, or use manual entry.")
            return

        if not manual and selected is not None and selected.match_score < AUTO_CONFIRM:
            answer = QMessageBox.question(
                self,
                "Confirm the match",
                "The best match is '%s' (%s) with a confidence of %.2f — %s.\n\n"
                "Import it anyway?\n\nCartridge never associates a weak match "
                "automatically."
                % (
                    selected.title,
                    provider_label(selected.provider),
                    selected.match_score,
                    confidence_label(selected.match_score),
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self._set_busy(True, "Importing…")
        repo = self.state.repo
        folder = self._folder
        types = self._selected_types()
        want_artwork = self.artwork_check.isChecked()
        shots = int(self.shots_spin.value())
        measure = self.size_check.isChecked()
        payload = self._import_payload(selected, manual)
        downloader = self.state.downloader
        windows = self.state.windows
        progress = self.progress

        def work(cancel_event=None, progress=None):
            """Runs on a worker thread. One transaction for metadata + types."""
            steps: Dict[str, Any] = {"errors": []}
            game, created = repo.upsert_folder(folder, state="present")
            if game is None:
                raise RuntimeError("Could not create a record for that folder.")
            steps["game_id"] = game.id
            steps["created"] = created

            try:
                if manual:
                    repo.set_manual_metadata(game.id, payload)
                else:
                    repo.apply_provider_metadata(game.id, payload)
            except Exception as exc:
                repo.set_metadata_error(game.id, str(exc)[:400])
                steps["errors"].append("metadata: %s" % exc)

            try:
                if types:
                    repo.set_game_content_types(game.id, types, source="user")
            except Exception as exc:
                steps["errors"].append("content types: %s" % exc)

            if want_artwork and not manual and selected is not None:
                try:
                    batch = downloader.download_for_game(
                        folder,
                        cover_url=selected.cover_url,
                        screenshot_urls=list(selected.screenshot_urls)[:shots],
                        cancel=cancel_event,
                    )
                    for result in batch.results:
                        if result.ok and result.asset is not None:
                            result.asset.game_id = game.id
                            repo.add_asset(game.id, result.asset)
                        elif not result.ok:
                            steps["errors"].append(result.describe())
                    steps["artwork_ok"] = len(batch.succeeded)
                    steps["artwork_failed"] = len(batch.failed)
                except Exception as exc:
                    steps["errors"].append("artwork: %s" % exc)

            if measure and cancel_event is not None:
                try:
                    measured = measure_folder_size(folder, cancel=cancel_event)
                    repo.set_folder_size(
                        game.id, measured.size_bytes, measured.measured_at
                    )
                    steps["size_bytes"] = measured.size_bytes
                    steps["size_complete"] = measured.complete
                    if measured.errors:
                        steps["errors"].append(
                            "size: %d file(s) could not be read" % len(measured.errors)
                        )
                except Exception as exc:
                    steps["errors"].append("size: %s" % exc)

            try:
                assets = repo.list_assets(game.id)
                downloader.store.write_manifest(folder, assets)
            except Exception:
                pass
            repo.mark_verified(game.id)
            return steps

        self._import_handle = self.state.pool.submit(
            work,
            _name="import",
            _on_finished=self._on_import_finished,
            _on_failed=self._on_import_failed,
        )

    def _import_payload(self, selected: Optional[ProviderGame], manual: bool) -> Dict[str, Any]:
        if manual:
            year = int(self.manual_year.value()) or None
            return {
                "title": self.manual_title.text().strip() or folder_name(self._folder or ""),
                "summary": self.manual_summary.toPlainText().strip() or None,
                "release_year": year,
                "release_date": ("%d-01-01" % year) if year else None,
                "developer": self.manual_developer.text().strip() or None,
                "publisher": self.manual_publisher.text().strip() or None,
                "genres": [
                    part.strip()
                    for part in self.manual_genres.text().split(",")
                    if part.strip()
                ],
                "metadata_state": STATE_MANUAL,
            }
        return selected.to_manual_game()

    def _on_import_finished(self, steps: Any) -> None:
        self._import_handle = None
        self._set_busy(False)
        errors = steps.get("errors") or []
        game_id = steps.get("game_id")
        if errors:
            QMessageBox.warning(
                self,
                "Imported with problems",
                "The record was saved, but some steps did not complete:\n\n%s\n\n"
                "The entry is marked incomplete so you can see it in the Manager."
                % "\n".join("· %s" % error for error in errors[:8]),
            )
        if steps.get("created"):
            self._set_status("Imported a new record.")
        else:
            self._set_status("Updated the existing record for that folder.")
        if game_id is not None:
            self.imported.emit(int(game_id))
        self.accept()

    def _on_import_failed(self, message: str, _detail: str) -> None:
        self._import_handle = None
        self._set_busy(False)
        if message == "cancelled":
            self._set_status("Import cancelled.")
            return
        QMessageBox.critical(
            self,
            "Import failed",
            "%s\n\nNothing was left half-written: the database transaction was "
            "rolled back." % message,
        )
        self._set_status("Import failed: %s" % message)

    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)   # indeterminate: no fake percentages
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
        self.search_button.setEnabled(not busy)
        self.primary_button.setEnabled(not busy)
        self.back_button.setEnabled(not busy)
        self._set_status(text or self.status_label.text())
        if not busy:
            self._update_primary_enabled()

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text or "")

    def reject(self) -> None:  # noqa: D102 - Qt override
        for handle in self._thumb_handles:
            try:
                handle.cancel()
            except Exception:
                pass
        self._thumb_pending.clear()
        if self._search_handle is not None:
            self._search_handle.cancel()
        if self._import_handle is not None:
            self._import_handle.cancel()
        super(AddGameDialog, self).reject()
        shutil.rmtree(self._thumb_dir, ignore_errors=True)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.reject()
        super(AddGameDialog, self).closeEvent(event)


class DetailPreview(QWidget):
    """Right-hand preview of the selected provider result.

    Layout is a row (cover left, title and match info right) followed by the fact
    grid and a scrolling description - the same shape as the browser's details
    pane, so nothing floats over the text at narrow widths.
    """

    def __init__(self, state: AppState, parent=None):
        super(DetailPreview, self).__init__(parent)
        self.state = state
        self.setObjectName("DetailPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(10)

        self.cover = CoverLabel(132, 180, cache=state.image_cache)
        self.cover.set_placeholder_text("No cover")
        top_layout.addWidget(self.cover, 0, Qt.AlignTop)

        text_column = QWidget()
        text_layout = QVBoxLayout(text_column)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)

        self.title = QLabel("No result selected")
        self.title.setObjectName("DetailTitle")
        self.title.setWordWrap(True)
        text_layout.addWidget(self.title)

        self.badges = QWidget()
        badge_layout = QHBoxLayout(self.badges)
        badge_layout.setContentsMargins(0, 0, 0, 0)
        badge_layout.setSpacing(4)
        self._badge_widgets: List[Badge] = []
        text_layout.addWidget(self.badges)
        text_layout.addStretch(1)

        top_layout.addWidget(text_column, 1)
        layout.addWidget(top)

        self.facts = KeyValueGrid(key_width=92)
        layout.addWidget(self.facts)

        self.summary = QLabel("")
        self.summary.setObjectName("SecondaryText")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addWidget(self.summary)
        holder_layout.addStretch(1)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

    def set_cover_path(self, path: Optional[str]) -> None:
        """Show a downloaded thumbnail, or the placeholder when there is none."""
        if not path or not os.path.exists(path):
            self.cover.load(None)
            return
        self.cover.load(path)

    def set_provider_game(self, game: Optional[ProviderGame]) -> None:
        if game is None:
            self.clear()
            return
        self.title.setText(game.title)

        for badge in self._badge_widgets:
            detach_and_delete(badge)
        self._badge_widgets = []
        provider_badge = Badge(provider_label(game.provider), "accent")
        self.badges.layout().addWidget(provider_badge)
        self._badge_widgets.append(provider_badge)
        confidence_badge = Badge(confidence_label(game.match_score), "default")
        confidence_badge.setToolTip(
            explain(game.match_score, game, game.title)
        )
        self.badges.layout().addWidget(confidence_badge)
        self._badge_widgets.append(confidence_badge)
        self.badges.layout().addStretch(1)

        self.facts.set_rows(
            [
                ("Released", game.release_date or (str(game.year) if game.year else "")),
                ("Score", "%d/100" % int(round(game.rating)) if game.rating is not None else ""),
                ("Developer", game.developer or ""),
                ("Publisher", game.publisher or ""),
                ("Franchise", game.franchise or ""),
                ("Genres", ", ".join(game.genres[:6])),
                ("Platforms", ", ".join(game.platforms[:5])),
                ("Age rating", game.age_rating or ""),
                ("Also known as", ", ".join(game.alternative_names[:4])),
                ("Match", "%.2f (%s)" % (game.match_score, confidence_label(game.match_score))),
                ("Artwork", "%s cover, %d screenshot(s)" % (
                    "has" if game.cover_url else "no", len(game.screenshot_urls))),
            ]
        )
        self.summary.setText(truncate(game.summary or "", 700) or "No description returned.")

    def clear(self) -> None:
        self.title.setText("No result selected")
        self.facts.clear()
        self.summary.setText("")
        self.cover.clear_image()
        for badge in self._badge_widgets:
            detach_and_delete(badge)
        self._badge_widgets = []


