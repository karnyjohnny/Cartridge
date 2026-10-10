"""Edit game dialog: user overrides, notes, favourite, content types, artwork.

The distinction that drives this dialog is between *provider* values and *user*
values (DECISIONS D-004). Every editable field shows what the provider said and
what the user has overridden it with, and offers "Use provider value" to clear the
override. That way a later metadata refresh can never silently destroy an edit,
and the user can always see which fields are theirs.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cartridge.app_state import AppState
from cartridge.core.models import Game
from cartridge.paths import shorten_path
from cartridge.text import human_size
from cartridge.ui.widgets.common import IconButton, KeyValueGrid, Separator


class OverrideField(QWidget):
    """A line edit that shows the provider value and can clear the override."""

    changed = pyqtSignal()

    def __init__(self, label: str, provider_value: Optional[str], user_value: Optional[str],
                 numeric: bool = False, parent=None):
        super(OverrideField, self).__init__(parent)
        self.label = label
        self.numeric = numeric
        self.provider_value = provider_value

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        if numeric:
            self.editor: Any = QDoubleSpinBox()
            self.editor.setRange(0.0, 100.0)
            self.editor.setDecimals(1)
            self.editor.setSpecialValueText("not set")
            self.editor.setValue(float(user_value) if user_value is not None else 0.0)
            self._numeric_set = user_value is not None
            self.editor.valueChanged.connect(self._on_numeric_changed)
        else:
            self.editor = QLineEdit()
            self.editor.setText(user_value if user_value is not None else "")
            self.editor.setPlaceholderText(
                "provider: %s" % (provider_value or "none")
            )
            self.editor.textChanged.connect(lambda _t: self.changed.emit())

        layout.addWidget(self.editor, 1)

        self.override_check = QCheckBox("override")
        self.override_check.setToolTip(
            "Tick to store your own value. A metadata refresh never overwrites it."
        )
        self.override_check.setChecked(user_value is not None)
        self.override_check.toggled.connect(self._on_toggle)
        layout.addWidget(self.override_check)

        self.provider_label = QLabel(provider_value or "—")
        self.provider_label.setObjectName("MutedText")
        self.provider_label.setFixedWidth(180)
        self.provider_label.setWordWrap(True)
        self.provider_label.setToolTip("Value from %s" % (provider_value or "no provider"))
        layout.addWidget(self.provider_label)

    def _on_numeric_changed(self, _value: float) -> None:
        self._numeric_set = True
        self.override_check.setChecked(True)
        self.changed.emit()

    def _on_toggle(self, checked: bool) -> None:
        self.editor.setEnabled(checked)
        if not checked and self.numeric:
            self._numeric_set = False
        self.changed.emit()

    def is_override(self) -> bool:
        return self.override_check.isChecked()

    def value(self) -> Any:
        if not self.override_check.isChecked():
            return None
        if self.numeric:
            return float(self.editor.value()) if self._numeric_set else None
        text = self.editor.text().strip()
        return text or None


class EditGameDialog(QDialog):
    """Edit one game's user-owned data."""

    saved = pyqtSignal(int)

    def __init__(self, state: AppState, game: Game, parent=None):
        super(EditGameDialog, self).__init__(parent)
        self.state = state
        self.game = game
        self.setWindowTitle("Edit — %s" % (game.effective_title() or "game"))
        self.resize(720, 620)
        self.setMinimumSize(600, 480)
        self._type_chips: List[QCheckBox] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        header = QLabel(game.effective_title() or "(untitled)")
        header.setObjectName("DetailTitle")
        header.setWordWrap(True)
        root.addWidget(header)

        location = QLabel(shorten_path(game.folder_path, 96))
        location.setObjectName("MutedText")
        location.setTextInteractionFlags(Qt.TextSelectableByMouse)
        location.setToolTip(game.folder_path)
        root.addWidget(location)

        if game.has_user_overrides():
            note = QLabel(
                "You have edited some fields. Those values are stored separately and "
                "a provider refresh will not overwrite them."
            )
            note.setObjectName("BadgeSuccess")
            note.setWordWrap(True)
            root.addWidget(note)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_details_tab(), "Details")
        self.tabs.addTab(self._build_types_tab(), "Content types")
        self.tabs.addTab(self._build_notes_tab(), "Notes")
        self.tabs.addTab(self._build_artwork_tab(), "Artwork")

        footer = QHBoxLayout()
        self.favourite_check = QCheckBox("Favourite")
        self.favourite_check.setChecked(bool(game.favourite))
        footer.addWidget(self.favourite_check)
        footer.addStretch(1)
        cancel = IconButton("Cancel")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        save = QPushButton("Save changes")
        save.setObjectName("PrimaryButton")
        save.setDefault(True)
        save.clicked.connect(self._on_save)
        footer.addWidget(save)
        root.addLayout(footer)

    # ------------------------------------------------------------------
    def _build_details_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        hint = QLabel(
            "The right-hand column shows what the provider returned. Tick "
            "'override' to replace it with your own value."
        )
        hint.setObjectName("MutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(6)

        self.f_title = OverrideField("Title", self.game.title, self.game.user_title)
        form.addRow("Title", self.f_title)

        self.f_year = QSpinBox()
        self.f_year.setRange(0, 2100)
        self.f_year.setSpecialValueText("not set")
        self.f_year.setValue(int(self.game.user_release_year or self.game.release_year or 0))
        self._year_override = self.game.user_release_year is not None
        self.year_check = QCheckBox("override")
        self.year_check.setChecked(self._year_override)
        self.year_check.toggled.connect(lambda on: self.f_year.setEnabled(on))
        self.f_year.setEnabled(self._year_override)
        form.addRow("Release year", self._pair(self.f_year, self.year_check,
                                              self.game.release_year))

        self.f_rating = OverrideField(
            "Score", 
            "%.1f" % self.game.rating if self.game.rating is not None else None,
            self.game.user_rating,
            numeric=True,
        )
        form.addRow("Score (0-100)", self.f_rating)

        self.f_developer = OverrideField("Developer", self.game.developer, self.game.user_developer)
        form.addRow("Developer", self.f_developer)

        self.f_publisher = OverrideField("Publisher", self.game.publisher, self.game.user_publisher)
        form.addRow("Publisher", self.f_publisher)

        self.f_franchise = OverrideField("Franchise", self.game.franchise, self.game.user_franchise)
        form.addRow("Franchise / series", self.f_franchise)

        self.f_age = OverrideField("Age rating", self.game.age_rating, self.game.user_age_rating)
        form.addRow("Age rating", self.f_age)

        self.f_summary = QPlainTextEdit()
        self.f_summary.setPlainText(self.game.user_summary or "")
        self.f_summary.setPlaceholderText(
            "provider: %s" % ((self.game.summary or "")[:200] or "none")
        )
        self.f_summary.setFixedHeight(96)
        self.summary_check = QCheckBox("override")
        self.summary_check.setChecked(self.game.user_summary is not None)
        self.summary_check.toggled.connect(lambda on: self.f_summary.setEnabled(on))
        self.f_summary.setEnabled(self.game.user_summary is not None)
        form.addRow("Description", self._pair(self.f_summary, self.summary_check,
                                             (self.game.summary or "")[:60]))

        layout.addLayout(form)

        genres_group = QGroupBox("Genres")
        genres_layout = QVBoxLayout(genres_group)
        provider_genres = QLabel(
            "Provider: %s" % (", ".join(self.game.genres) or "none")
        )
        provider_genres.setObjectName("MutedText")
        provider_genres.setWordWrap(True)
        genres_layout.addWidget(provider_genres)
        self.genre_edit = QLineEdit(", ".join(self.game.user_genres))
        self.genre_edit.setPlaceholderText(
            "Comma separated. Leave empty to use the provider's genres."
        )
        genres_layout.addWidget(self.genre_edit)
        layout.addWidget(genres_group)

        layout.addStretch(1)
        return page

    def _pair(self, editor: QWidget, check: QCheckBox, provider_value: Any) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(editor, 1)
        layout.addWidget(check)
        label = QLabel("" if provider_value in (None, "") else str(provider_value))
        label.setObjectName("MutedText")
        label.setFixedWidth(180)
        label.setWordWrap(True)
        layout.addWidget(label)
        return holder

    def _build_types_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        hint = QLabel(
            "Content types are your own labels, not a fixed list. Add, rename or "
            "remove them here; the same names are offered as filters in the "
            "Collection Browser."
        )
        hint.setObjectName("MutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        current = set(self.game.content_types)
        known: List[str] = []
        if self.state.ready:
            known = [ct.name for ct in self.state.repo.list_content_types(with_counts=True)]
        for name in dict.fromkeys(list(known) + sorted(current)):
            chip = QCheckBox(name)
            chip.setChecked(name in current)
            chip.setCursor(Qt.PointingHandCursor)
            layout.addWidget(chip)
            self._type_chips.append(chip)

        row = QHBoxLayout()
        self.new_type_edit = QLineEdit()
        self.new_type_edit.setPlaceholderText("New type name…")
        row.addWidget(self.new_type_edit, 1)
        add = QPushButton("Add type")
        add.clicked.connect(self._on_add_type)
        row.addWidget(add)
        layout.addLayout(row)

        manage = QHBoxLayout()
        self.rename_button = IconButton("Rename selected…")
        self.rename_button.clicked.connect(self._on_rename_type)
        manage.addWidget(self.rename_button)
        self.delete_button = IconButton("Delete selected…", "Games keep their records")
        self.delete_button.setObjectName("DangerButton")
        self.delete_button.clicked.connect(self._on_delete_type)
        manage.addWidget(self.delete_button)
        manage.addStretch(1)
        layout.addLayout(manage)

        layout.addStretch(1)
        return page

    def _build_notes_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        layout.addWidget(QLabel("Your notes (never sent anywhere, never overwritten)"))
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlainText(self.game.notes or "")
        layout.addWidget(self.notes_edit, 1)

        facts = KeyValueGrid(key_width=140)
        facts.set_rows(
            [
                ("Folder", shorten_path(self.game.folder_path, 70)),
                ("Folder state", self.game.folder_state.replace("_", " ")),
                ("Folder size", human_size(self.game.folder_size_bytes)),
                ("Provider", "%s · %s" % (self.game.provider or "none",
                                          self.game.provider_id or "—")),
                ("Metadata state", self.game.metadata_state),
                ("Added", self.game.added_at or ""),
                ("Last verified", self.game.last_verified_at or ""),
            ]
        )
        layout.addWidget(facts)
        return page

    def _build_artwork_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        assets = self.game.assets
        summary = QLabel(
            "%d artwork file(s) stored locally · state: %s"
            % (len(assets), self.game.artwork_state())
        )
        summary.setObjectName("SecondaryText")
        layout.addWidget(summary)

        facts = KeyValueGrid(key_width=110)
        rows = []
        for asset in assets[:12]:
            rows.append(
                (
                    asset.kind,
                    "%s · %s · %s"
                    % (
                        os.path.basename(asset.rel_path),
                        human_size(asset.size_bytes),
                        asset.state,
                    ),
                )
            )
        facts.set_rows(rows or [("Artwork", "none stored")])
        layout.addWidget(facts)

        note = QLabel(
            "Artwork lives in '%s' inside the game folder, so the collection keeps "
            "working offline. Cartridge never writes anywhere else in that folder."
            % shorten_path(os.path.join(self.game.folder_path, "_cartridge", "assets"), 60)
        )
        note.setObjectName("MutedText")
        note.setWordWrap(True)
        layout.addWidget(note)

        row = QHBoxLayout()
        self.open_assets_button = IconButton("Open artwork folder")
        self.open_assets_button.clicked.connect(self._on_open_assets)
        row.addWidget(self.open_assets_button)
        self.cleanup_button = IconButton("Remove interrupted downloads")
        self.cleanup_button.setToolTip("Deletes leftover .part files only")
        self.cleanup_button.clicked.connect(self._on_cleanup)
        row.addWidget(self.cleanup_button)
        row.addStretch(1)
        layout.addLayout(row)

        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------
    def _on_add_type(self) -> None:
        name = self.new_type_edit.text().strip()
        if not name:
            return
        if not self.state.ready:
            return
        try:
            created = self.state.repo.add_content_type(name)
        except ValueError as exc:
            QMessageBox.warning(self, "Content type", str(exc))
            return
        self.new_type_edit.clear()
        for chip in self._type_chips:
            if chip.text() == created.name:
                chip.setChecked(True)
                return
        chip = QCheckBox(created.name)
        chip.setChecked(True)
        chip.setCursor(Qt.PointingHandCursor)
        self.tabs.widget(1).layout().insertWidget(self.tabs.widget(1).layout().count() - 3, chip)
        self._type_chips.append(chip)

    def _selected_type_chips(self) -> List[QCheckBox]:
        return [chip for chip in self._type_chips if chip.isChecked()]

    def _on_rename_type(self) -> None:
        chip = self._single_checked_chip()
        if chip is None:
            return
        from PyQt5.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(
            self, "Rename content type", "New name for '%s':" % chip.text(),
            text=chip.text(),
        )
        if not ok or not text.strip() or not self.state.ready:
            return
        target = self._find_type_id(chip.text())
        if target is None:
            return
        try:
            self.state.repo.rename_content_type(target, text.strip())
        except ValueError as exc:
            QMessageBox.warning(self, "Rename content type", str(exc))
            return
        chip.setText(text.strip())

    def _on_delete_type(self) -> None:
        chip = self._single_checked_chip()
        if chip is None:
            return
        target = self._find_type_id(chip.text())
        if target is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete content type",
            "Delete the type '%s'?\n\nIt is removed from every game that used it. "
            "Game records and files are not touched." % chip.text(),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes or not self.state.ready:
            return
        self.state.repo.delete_content_type(target)
        self._type_chips.remove(chip)
        chip.deleteLater()

    def _single_checked_chip(self) -> Optional[QCheckBox]:
        checked = self._selected_type_chips()
        if len(checked) != 1:
            QMessageBox.information(
                self, "Content type", "Tick exactly one type to rename or delete."
            )
            return None
        return checked[0]

    def _find_type_id(self, name: str) -> Optional[int]:
        if not self.state.ready:
            return None
        for content_type in self.state.repo.list_content_types(with_counts=False):
            if content_type.name == name:
                return content_type.id
        return None

    def _on_open_assets(self) -> None:
        from cartridge.ui.actions import open_folder

        target = self.state.asset_store.paths_for(self.game.folder_path).asset_dir
        if not os.path.isdir(target):
            QMessageBox.information(
                self, "Artwork folder", "No artwork has been downloaded for this game yet."
            )
            return
        _ok, message = open_folder(target, self)
        if not _ok:
            QMessageBox.warning(self, "Artwork folder", message)

    def _on_cleanup(self) -> None:
        removed = self.state.asset_store.cleanup(self.game.folder_path)
        QMessageBox.information(
            self,
            "Interrupted downloads",
            "Removed %d leftover .part file(s). Validated artwork was not touched."
            % removed,
        )

    # ------------------------------------------------------------------
    def _on_save(self) -> None:
        if not self.state.ready:
            QMessageBox.warning(self, "Edit", "No database is open.")
            return
        fields: Dict[str, Any] = {
            "user_title": self.f_title.value(),
            "user_developer": self.f_developer.value(),
            "user_publisher": self.f_publisher.value(),
            "user_franchise": self.f_franchise.value(),
            "user_age_rating": self.f_age.value(),
            "user_rating": self.f_rating.value(),
            "user_release_year": (
                int(self.f_year.value()) if self.year_check.isChecked() else None
            ),
            "user_summary": (
                self.f_summary.toPlainText().strip() or None
            ) if self.summary_check.isChecked() else None,
            "user_genres": [
                part.strip()
                for part in self.genre_edit.text().split(",")
                if part.strip()
            ],
        }
        try:
            self.state.repo.update_user_fields(self.game.id, fields)
            self.state.repo.set_notes(self.game.id, self.notes_edit.toPlainText().strip() or None)
            self.state.repo.set_favourite(self.game.id, self.favourite_check.isChecked())
            chosen = [chip.text() for chip in self._type_chips if chip.isChecked()]
            self.state.repo.set_game_content_types(self.game.id, chosen, source="user")
        except Exception as exc:
            QMessageBox.critical(self, "Edit", "Could not save: %s" % exc)
            return
        self.saved.emit(int(self.game.id))
        self.accept()
