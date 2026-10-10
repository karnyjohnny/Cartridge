"""Content-types manager: add, rename, delete user-defined types.

Content types are user data, never a hard-coded enum (brief section 9). This
dialog is the place to maintain the vocabulary itself, as opposed to
``EditGameDialog``'s tab which assigns existing types to one game.

Deleting a type removes it from every game that used it, and says so before doing
it. Game records and files on disk are never touched.
"""

from __future__ import annotations

from typing import List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
    QHeaderView,
    QVBoxLayout,
    QWidget,
)

from cartridge.app_state import AppState
from cartridge.ui.widgets.common import IconButton


class ContentTypesDialog(QDialog):
    """Maintain the vocabulary of content types."""

    def __init__(self, state: AppState, parent=None):
        super(ContentTypesDialog, self).__init__(parent)
        self.state = state
        self.setWindowTitle("Content types")
        self.resize(520, 440)
        self.changed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        hint = QLabel(
            "Content types are your own labels for how a game is stored — GOG, ISO, "
            "Installer, Emulator, Backup, or anything you invent. They are used as "
            "filters in the Collection Browser and shown as badges on cards."
        )
        hint.setObjectName("SecondaryText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Type", "Games", "Created"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        add_row = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("New type name…")
        self.name_edit.returnPressed.connect(self._on_add)
        add_row.addWidget(self.name_edit, 1)
        add = QPushButton("Add")
        add.setObjectName("PrimaryButton")
        add.clicked.connect(self._on_add)
        add_row.addWidget(add)
        layout.addLayout(add_row)

        actions = QHBoxLayout()
        self.rename_button = IconButton("Rename…")
        self.rename_button.clicked.connect(self._on_rename)
        actions.addWidget(self.rename_button)
        self.delete_button = IconButton("Delete…")
        self.delete_button.setObjectName("DangerButton")
        self.delete_button.clicked.connect(self._on_delete)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)

        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        if not self.state.ready:
            self.table.setRowCount(0)
            return
        types = self.state.repo.list_content_types(with_counts=True)
        self.table.setRowCount(len(types))
        for row, content_type in enumerate(types):
            name = QTableWidgetItem(content_type.name)
            name.setData(Qt.UserRole, content_type.id)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem(str(content_type.game_count)))
            self.table.setItem(row, 2, QTableWidgetItem((content_type.created_at or "")[:10]))

    def _selected_id(self) -> Optional[int]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def _selected_name(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return item.text() if item is not None else ""

    def _on_add(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            return
        if not self.state.ready:
            QMessageBox.warning(self, "Content types", "No database is open.")
            return
        try:
            self.state.repo.add_content_type(name)
        except ValueError as exc:
            QMessageBox.warning(self, "Content types", str(exc))
            return
        self.name_edit.clear()
        self.changed = True
        self.refresh()

    def _on_rename(self) -> None:
        target = self._selected_id()
        if target is None or not self.state.ready:
            QMessageBox.information(self, "Content types", "Select a type to rename.")
            return
        current = self._selected_name()
        text, ok = QInputDialog.getText(
            self, "Rename content type", "New name for '%s':" % current, text=current
        )
        if not ok or not text.strip():
            return
        try:
            self.state.repo.rename_content_type(target, text.strip())
        except ValueError as exc:
            QMessageBox.warning(self, "Content types", str(exc))
            return
        self.changed = True
        self.refresh()

    def _on_delete(self) -> None:
        target = self._selected_id()
        if target is None or not self.state.ready:
            QMessageBox.information(self, "Content types", "Select a type to delete.")
            return
        name = self._selected_name()
        row = self.table.currentRow()
        count_item = self.table.item(row, 1)
        count = int(count_item.text()) if count_item and count_item.text().isdigit() else 0
        answer = QMessageBox.question(
            self,
            "Delete content type",
            "Delete '%s'?\n\nIt will be removed from %d game(s). Game records, "
            "metadata and files on disk are not touched." % (name, count),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.state.repo.delete_content_type(target)
        self.changed = True
        self.refresh()


class ContentTypePicker(QDialog):
    """Assign content types to one game (used from the Manager context menu)."""

    def __init__(self, state: AppState, game_id: int, parent=None):
        super(ContentTypePicker, self).__init__(parent)
        self.state = state
        self.game_id = game_id
        self.setWindowTitle("Content types")
        self.resize(360, 420)
        self._chips: List[QCheckBox] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        game = state.repo.get_game(game_id) if state.ready else None
        current = set(game.content_types) if game is not None else set()

        known = (
            [ct.name for ct in state.repo.list_content_types(with_counts=False)]
            if state.ready else []
        )
        for name in dict.fromkeys(list(known) + sorted(current)):
            chip = QCheckBox(name)
            chip.setChecked(name in current)
            chip.setCursor(Qt.PointingHandCursor)
            layout.addWidget(chip)
            self._chips.append(chip)

        row = QHBoxLayout()
        self.new_edit = QLineEdit()
        self.new_edit.setPlaceholderText("New type…")
        row.addWidget(self.new_edit, 1)
        add = QPushButton("Add")
        add.clicked.connect(self._on_add)
        row.addWidget(add)
        layout.addLayout(row)

        layout.addStretch(1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        save = QPushButton("Save")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._on_save)
        footer.addWidget(save)
        layout.addLayout(footer)

    def _on_add(self) -> None:
        name = self.new_edit.text().strip()
        if not name or not self.state.ready:
            return
        try:
            created = self.state.repo.add_content_type(name)
        except ValueError as exc:
            QMessageBox.warning(self, "Content types", str(exc))
            return
        self.new_edit.clear()
        for chip in self._chips:
            if chip.text() == created.name:
                chip.setChecked(True)
                return
        chip = QCheckBox(created.name)
        chip.setChecked(True)
        layout = self.layout()
        layout.insertWidget(layout.count() - 3, chip)
        self._chips.append(chip)

    def _on_save(self) -> None:
        if not self.state.ready:
            self.reject()
            return
        chosen = [chip.text() for chip in self._chips if chip.isChecked()]
        try:
            self.state.repo.set_game_content_types(self.game_id, chosen, source="user")
        except Exception as exc:
            QMessageBox.critical(self, "Content types", "Could not save: %s" % exc)
            return
        self.accept()
