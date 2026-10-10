"""Manager view: the collection's state, folder by folder.

The Manager exists to make problems visible and distinguishable (brief section
6.4 and 13). Five states are never collapsed into one ambiguous "error":

====================  =======================================================
``unmatched``         a folder on disk with no catalogue record
``incomplete``        a record with no or failed metadata
``missing artwork``   a record with no local cover
``missing folder``    a record whose folder was deleted or renamed
``drive unavailable`` a record on a disk that is not mounted
====================  =======================================================

Records are never deleted automatically when a folder disappears: the filesystem
state is updated and the metadata is kept, because a user who unplugs a drive has
not asked to lose their catalogue.

Filters offered: Needs attention, Missing artwork, Missing folders, All entries,
Root folders, Content types.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cartridge.app_state import AppState
from cartridge.core import filtering
from cartridge.core.models import FolderEntry, Game
from cartridge.paths import FolderState, classify_folder_state, shorten_path
from cartridge.scan import discovery
from cartridge.text import human_size, truncate
from cartridge.ui.widgets.common import EmptyState, IconButton, Separator

FILTER_LABELS = (
    ("attention", "Needs attention"),
    ("unmatched", "Unmatched folders"),
    ("artwork", "Missing artwork"),
    ("missing", "Missing folders"),
    ("drives", "Unavailable drives"),
    ("incomplete", "Incomplete metadata"),
    ("all", "All entries"),
    ("roots", "Root folders"),
    ("types", "Content types"),
)

STATE_TEXT = {
    FolderState.PRESENT.value: "folder ok",
    FolderState.MISSING.value: "folder missing",
    FolderState.UNAVAILABLE_DRIVE.value: "drive unavailable",
    FolderState.ACCESS_DENIED.value: "access denied",
    FolderState.NOT_A_DIRECTORY.value: "not a folder",
    FolderState.UNKNOWN.value: "unknown",
}

COLUMNS = (
    "Folder", "Title", "State", "Artwork", "Content types", "Size", "Verified",
)


class ManagerView(QWidget):
    """Table of discovered folders and their catalogue state."""

    counts_changed = pyqtSignal(str)
    busy_changed = pyqtSignal(bool, str)
    status_message = pyqtSignal(str)
    navigate_requested = pyqtSignal(str)
    rescan_requested = pyqtSignal()

    def __init__(self, state: AppState, parent=None):
        super(ManagerView, self).__init__(parent)
        self.state = state
        self._entries: List[FolderEntry] = []
        self._busy = False
        self._scan_handle = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        # --- toolbar ----------------------------------------------------
        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(6)

        self.filter_combo = QComboBox()
        for value, label in FILTER_LABELS:
            self.filter_combo.addItem(label, value)
        toolbar_layout.addWidget(self.filter_combo)

        self.search = QLineEdit()
        self.search.setObjectName("SearchField")
        self.search.setPlaceholderText("Filter folders and titles…")
        self.search.setClearButtonEnabled(True)
        toolbar_layout.addWidget(self.search, 1)

        self.rescan_button = QPushButton("Rescan roots")
        self.rescan_button.setObjectName("PrimaryButton")
        self.rescan_button.setToolTip("Re-scan the configured root folders (F5)")
        self.rescan_button.clicked.connect(self.start_rescan)
        toolbar_layout.addWidget(self.rescan_button)

        self.add_button = QPushButton("Add game…")
        self.add_button.setToolTip("Import a folder into the catalogue")
        self.add_button.clicked.connect(self._on_add_game)
        toolbar_layout.addWidget(self.add_button)

        layout.addWidget(toolbar)

        self.summary_label = QLabel("—")
        self.summary_label.setObjectName("SecondaryText")
        layout.addWidget(self.summary_label)

        layout.addWidget(Separator())

        # --- table ------------------------------------------------------
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemDoubleClicked.connect(lambda _item: self._on_edit_selected())
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Interactive)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(1, 220)
        self.table.setColumnWidth(4, 150)
        layout.addWidget(self.table, 1)

        self.empty = EmptyState(
            "Nothing to manage yet",
            "Add a games root folder in Settings, then rescan. Discovered folders "
            "appear here with their match and artwork state.",
            "Open Settings",
        )
        self.empty.action_requested.connect(lambda: self.navigate_requested.emit("settings"))
        self.empty.setVisible(False)
        layout.addWidget(self.empty, 1)

        self.filter_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        self.search.textChanged.connect(self._on_search_changed)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._debounce.timeout.connect(self._apply_filter)

    # ------------------------------------------------------------------
    def view_shown(self) -> None:
        self.refresh()

    def view_hidden(self) -> None:
        pass

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def describe_counts(self) -> str:
        return "%d entries shown" % len(self._visible_entries())

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild entries from the database plus a live folder-state probe."""
        if not self.state.ready:
            self.empty.set_state(
                "No collection loaded",
                "Choose a games root folder in Settings to create or open a database.",
                "Open Settings",
            )
            self.empty.setVisible(True)
            self.table.setVisible(False)
            self.summary_label.setText("No database")
            return

        entries = self._build_entries()
        self._entries = entries
        self._apply_filter()

    def _build_entries(self) -> List[FolderEntry]:
        """One row per catalogue record, with live filesystem state."""
        entries: List[FolderEntry] = []
        try:
            games = self.state.repo.list_games(
                filtering.FilterSpec(sort="title"), with_assets=True
            )
        except Exception as exc:
            self.status_message.emit("Could not read the collection: %s" % exc)
            return entries

        for game in games:
            entries.append(
                FolderEntry(
                    path=game.folder_path,
                    name=os.path.basename(game.folder_path.rstrip("\\/")) or game.folder_path,
                    key=game.folder_key,
                    state=game.folder_state,
                    game_id=game.id,
                    game_title=game.effective_title(),
                    association="db_only" if game.metadata_state == "none" else "matched",
                    artwork_state=game.artwork_state(),
                    metadata_state=game.metadata_state,
                    content_types=list(game.content_types),
                    last_verified_at=game.last_verified_at,
                    size_bytes=game.folder_size_bytes,
                    size_measured_at=game.folder_size_measured_at,
                )
            )

        # Unmatched folders: present on disk, absent from the catalogue. These are
        # discovered live so a brand-new download shows up without a full rescan.
        known = {entry.key for entry in entries}
        for root_path in self.state.root_paths():
            try:
                result = discovery.discover_root(root_path, detect_types=True)
            except Exception:
                continue
            for folder in result.folders:
                if folder.key in known:
                    continue
                entries.append(
                    FolderEntry(
                        path=folder.path,
                        name=folder.name,
                        key=folder.key,
                        state=folder.state,
                        association="unmatched",
                        artwork_state="missing",
                        metadata_state="none",
                        suggested_types=folder.suggested_names,
                        root_path=root_path,
                    )
                )
        return entries

    # ------------------------------------------------------------------
    def _visible_entries(self) -> List[FolderEntry]:
        mode = str(self.filter_combo.currentData() or "all")
        query = self.search.text().strip().lower()

        def matches(entry: FolderEntry) -> bool:
            if mode == "attention":
                return entry.needs_attention()
            if mode == "unmatched":
                return entry.association == "unmatched"
            if mode == "artwork":
                return entry.artwork_state == "missing" and entry.game_id is not None
            if mode == "missing":
                return entry.state == FolderState.MISSING.value
            if mode == "drives":
                return entry.state == FolderState.UNAVAILABLE_DRIVE.value
            if mode == "incomplete":
                return entry.metadata_state in ("none", "error", "partial")
            if mode == "roots":
                return entry.game_id is None and entry.association == "unmatched"
            if mode == "types":
                return bool(entry.content_types or entry.suggested_types)
            return True

        rows = [entry for entry in self._entries if matches(entry)]
        if query:
            rows = [
                entry for entry in rows
                if query in (entry.name or "").lower()
                or query in (entry.game_title or "").lower()
                or query in (entry.path or "").lower()
            ]
        return rows

    def _on_search_changed(self, _text: str) -> None:
        self._debounce.start()

    def _apply_filter(self) -> None:
        self._debounce.stop()
        rows = self._visible_entries()
        self._populate(rows)
        total = len(self._entries)
        self.summary_label.setText(
            "%d of %d entries · %d unmatched · %d missing folders · %d drives unavailable"
            % (
                len(rows),
                total,
                sum(1 for e in self._entries if e.association == "unmatched"),
                sum(1 for e in self._entries if e.state == FolderState.MISSING.value),
                sum(1 for e in self._entries if e.state == FolderState.UNAVAILABLE_DRIVE.value),
            )
        )
        self.counts_changed.emit(self.describe_counts())
        has_rows = bool(rows)
        self.table.setVisible(has_rows)
        self.empty.setVisible(not has_rows)
        if not has_rows and total:
            self.empty.set_state(
                "Nothing matches this filter",
                "Every entry in the collection is in a good state for this view.",
                "",
            )

    def _populate(self, rows: List[FolderEntry]) -> None:
        self.table.setRowCount(len(rows))
        for index, entry in enumerate(rows):
            self.table.setItem(index, 0, self._item(shorten_path(entry.path, 70), entry))
            self.table.setItem(
                index, 1, self._item(entry.game_title or "— (not catalogued)", entry)
            )
            state_item = self._item(self._state_text(entry), entry)
            state_item.setForeground(self._state_color(entry))
            self.table.setItem(index, 2, state_item)
            self.table.setItem(index, 3, self._item(self._artwork_text(entry), entry))
            types = entry.content_types or entry.suggested_types
            label = ", ".join(types) if types else "—"
            if entry.suggested_types and not entry.content_types:
                label = "%s (suggested)" % ", ".join(entry.suggested_types)
            self.table.setItem(index, 4, self._item(label, entry))
            self.table.setItem(index, 5, self._item(human_size(entry.size_bytes), entry))
            self.table.setItem(index, 6, self._item(self._verified_text(entry), entry))

    def _item(self, text: str, entry: FolderEntry) -> QTableWidgetItem:
        item = QTableWidgetItem(str(text))
        item.setData(Qt.UserRole, entry.path)
        item.setData(Qt.UserRole + 1, entry.game_id)
        item.setToolTip(entry.path)
        return item

    def _state_text(self, entry: FolderEntry) -> str:
        if entry.association == "unmatched":
            return "unmatched folder"
        return STATE_TEXT.get(entry.state, entry.state)

    def _artwork_text(self, entry: FolderEntry) -> str:
        return {
            "ok": "cover + shots",
            "partial": "partial",
            "missing": "no artwork",
        }.get(entry.artwork_state, entry.artwork_state)

    def _verified_text(self, entry: FolderEntry) -> str:
        from cartridge.core.clock import age_text

        if entry.last_verified_at is None:
            return "never"
        return age_text(entry.last_verified_at)

    def _state_color(self, entry: FolderEntry):
        from PyQt5.QtGui import QColor

        if entry.association == "unmatched":
            return QColor("#FBBF24")
        if entry.state == FolderState.PRESENT.value:
            return QColor("#A0A8B4")
        if entry.state == FolderState.UNAVAILABLE_DRIVE.value:
            return QColor("#4EA1FF")
        return QColor("#F87171")

    # ------------------------------------------------------------------
    def selected_entry(self) -> Optional[FolderEntry]:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._visible_entries()):
            return None
        return self._visible_entries()[row]

    def _on_selection_changed(self) -> None:
        entry = self.selected_entry()
        if entry is None:
            return
        self.status_message.emit(entry.path)

    def _show_context_menu(self, position) -> None:
        entry = self.selected_entry()
        if entry is None:
            return
        menu = QMenu(self)
        # Ordered by how often the field report says each is actually used:
        # looking inside a folder is the everyday action; destructive or
        # corrective actions sit lower and are only offered when they apply.
        open_action = menu.addAction("Open folder")
        menu.addSeparator()
        if entry.game_id is None:
            add_action = menu.addAction("Add to catalogue…")
            edit_action = None
        else:
            add_action = None
            edit_action = menu.addAction("Edit metadata…")
        types_action = menu.addAction("Edit content types…")
        measure_action = menu.addAction("Measure folder size")
        if entry.game_id is not None and entry.state != FolderState.PRESENT.value:
            reassociate_action = menu.addAction("Re-associate with a folder…")
        else:
            reassociate_action = None
        if entry.game_id is not None:
            menu.addSeparator()
            delete_action = menu.addAction("Delete record…")
        else:
            delete_action = None

        chosen = menu.exec_(self.table.viewport().mapToGlobal(position))
        if chosen is None:
            return
        if chosen is open_action:
            self._open_entry(entry)
        elif add_action is not None and chosen is add_action:
            self._on_add_game(entry.path)
        elif edit_action is not None and chosen is edit_action:
            self._on_edit_selected()
        elif chosen is types_action:
            self._edit_content_types(entry)
        elif chosen is measure_action:
            self._measure_size(entry)
        elif reassociate_action is not None and chosen is reassociate_action:
            self._reassociate(entry)
        elif delete_action is not None and chosen is delete_action:
            self._delete_record(entry)

    # ------------------------------------------------------------------
    def _open_entry(self, entry: FolderEntry) -> None:
        from cartridge.ui.actions import open_folder

        ok, message = open_folder(entry.path, self)
        self.status_message.emit(message)

    def _on_add_game(self, folder: Optional[str] = None) -> None:
        from cartridge.ui.dialogs.add_game import AddGameDialog

        target = folder
        if target is None:
            entry = self.selected_entry()
            target = entry.path if entry is not None else None
        if target is None and self.state.root_paths():
            target = self.state.root_paths()[0]
        dialog = AddGameDialog(self.state, self, suggested_folder=target)
        if dialog.exec_():
            self.refresh()

    def _on_edit_selected(self) -> None:
        entry = self.selected_entry()
        if entry is None or entry.game_id is None:
            self.status_message.emit("Select a catalogued entry to edit.")
            return
        from cartridge.ui.dialogs.edit_game import EditGameDialog

        game = self.state.repo.get_game(entry.game_id)
        if game is None:
            return
        dialog = EditGameDialog(self.state, game, self)
        if dialog.exec_():
            self.refresh()

    def _edit_content_types(self, entry: FolderEntry) -> None:
        if entry.game_id is None:
            # A status-bar line alone read as "nothing happened" on the target
            # machine. Ask a real question instead, with the useful next step.
            answer = QMessageBox.question(
                self,
                "Not in the catalogue yet",
                "'%s' has no catalogue record, so there is nothing to tag yet.\n\n"
                "Add it to the catalogue now? You can pick its content types in "
                "the same flow." % entry.name,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Yes:
                self._on_add_game(entry.path)
            else:
                self.status_message.emit(
                    "Content types need a catalogue record; nothing was changed."
                )
            return
        from cartridge.ui.dialogs.content_types import ContentTypePicker

        dialog = ContentTypePicker(self.state, entry.game_id, self)
        if dialog.exec_():
            self.refresh()

    def _measure_size(self, entry: FolderEntry) -> None:
        if entry.game_id is None:
            # Still useful: show the size, just do not store it anywhere.
            from cartridge.scan.folder_size import measure_folder_size

            result = measure_folder_size(entry.path)
            QMessageBox.information(
                self,
                "Folder size",
                "%s\n%d files · %.0f ms\n\n(not stored - the folder is not in "
                "the catalogue yet)"
                % (
                    human_size(result.size_bytes),
                    result.files,
                    result.duration_ms,
                ),
            )
            return
        from cartridge.ui.dialogs.add_game import measure_size_for_game

        game = self.state.repo.get_game(entry.game_id)
        if game is None:
            return
        measure_size_for_game(self, self.state, game, done=self.refresh)

    def _reassociate(self, entry: FolderEntry) -> None:
        """Point a record at a different folder after a user-confirmed move."""
        if entry.game_id is None:
            self.status_message.emit("Only a catalogued record can be re-associated.")
            return
        from PyQt5.QtWidgets import QFileDialog

        start = self.state.root_paths()[0] if self.state.root_paths() else entry.path
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose the folder this record now points at", start
        )
        if not chosen:
            return
        confirm = QMessageBox.question(
            self,
            "Re-associate record",
            "Point '%s' at\n%s\n\nThe old path will no longer be tracked. "
            "Metadata, favourites and tags are kept."
            % (entry.game_title or entry.name, chosen),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self.state.repo.reassociate_folder(entry.game_id, chosen)
        except Exception as exc:
            self.status_message.emit("Could not re-associate: %s" % exc)
            return
        self.status_message.emit("Record now points at %s" % chosen)
        self.refresh()

    def _delete_record(self, entry: FolderEntry) -> None:
        """Explicit deletion only — never a side effect of a missing folder."""
        if entry.game_id is None:
            return
        confirm = QMessageBox.warning(
            self,
            "Delete catalogue record",
            "Delete the record for '%s'?\n\nMetadata, tags and favourites for this "
            "entry are removed from the database. Your game files on disk are NOT "
            "touched, and downloaded artwork inside _cartridge stays where it is."
            % (entry.game_title or entry.name),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self.state.repo.delete_game(entry.game_id)
        except Exception as exc:
            self.status_message.emit("Could not delete the record: %s" % exc)
            return
        self.status_message.emit("Record deleted.")
        self.refresh()

    # ------------------------------------------------------------------
    def start_rescan(self) -> None:
        """Scan every configured root on a worker thread."""
        if self._busy:
            self.status_message.emit("A scan is already running.")
            return
        if not self.state.ready:
            self.status_message.emit("Configure a games root in Settings first.")
            self.navigate_requested.emit("settings")
            return
        roots = self.state.root_paths()
        if not roots:
            self.status_message.emit("No root folders are configured.")
            self.navigate_requested.emit("settings")
            return

        self._set_busy(True, "Scanning %d root folder(s)…" % len(roots))
        repo = self.state.repo
        windows = self.state.windows

        def work(cancel_event=None):
            """Runs on a worker thread: its own SQLite connection, no GUI calls."""
            summaries = []
            for root in roots:
                if cancel_event is not None and cancel_event():
                    break
                root_record = repo.get_root_by_path(root) if hasattr(repo, "get_root_by_path") else None
                root_id = None
                for record in repo.list_roots():
                    if record.path == root:
                        root_id = record.id
                        break
                result = discovery.discover_root(
                    root, windows=windows, cancel=cancel_event
                )
                summary = discovery.reconcile(repo, result, root_id, windows=windows)
                if root_id is not None:
                    repo.mark_root_scanned(root_id)
                summaries.append(summary)
            return summaries

        self._scan_handle = self.state.pool.submit(
            work,
            _name="rescan",
            _on_finished=self._on_scan_finished,
            _on_failed=self._on_scan_failed,
        )

    def _on_scan_finished(self, summaries: Any) -> None:
        self._set_busy(False)
        if not summaries:
            self.status_message.emit("Scan produced no results.")
            self.refresh()
            return
        seen = sum(summary.folders_seen for summary in summaries)
        new = sum(summary.new_folders for summary in summaries)
        matched = sum(summary.matched for summary in summaries)
        missing = sum(summary.missing_folders for summary in summaries)
        drives = sum(summary.unavailable_drives for summary in summaries)
        errors = sum(len(summary.errors) for summary in summaries)
        took = sum(summary.duration_ms for summary in summaries)
        self.status_message.emit(
            "Scan finished in %.0f ms: %d folders seen, %d new, %d matched, "
            "%d missing, %d drives unavailable, %d problem(s)"
            % (took, seen, new, matched, missing, drives, errors)
        )
        self.refresh()

    def _on_scan_failed(self, message: str, _detail: str) -> None:
        self._set_busy(False)
        if message == "cancelled":
            self.status_message.emit("Scan cancelled.")
        else:
            self.status_message.emit("Scan failed: %s" % message)
        self.refresh()

    def cancel_scan(self) -> None:
        if self._scan_handle is not None:
            self._scan_handle.cancel()
            self.status_message.emit("Cancelling the scan…")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        self.rescan_button.setEnabled(not busy)
        self.rescan_button.setText("Scanning…" if busy else "Rescan roots")
        self.busy_changed.emit(busy, text)
