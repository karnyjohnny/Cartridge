"""First-run dialog: choose where the collection lives.

Shown when no root folder is configured. It exists so the database location is a
*visible choice* rather than something that happened silently (brief section 7):
the dialog states exactly which folder will be created and where the database will
live, and refuses to proceed when the location is not writable — offering the
fallback path explicitly instead of using it behind the user's back.
"""

from __future__ import annotations

import os
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cartridge.app_state import AppState
from cartridge.config import resolve_db_path
from cartridge.db.connection import is_writable_location
from cartridge.paths import asset_dir_for_game, db_path_for_root, shorten_path
from cartridge.text import human_size


class FirstRunDialog(QDialog):
    """Pick the games root folder and confirm where Cartridge will write."""

    def __init__(self, state: AppState, parent=None, suggested: Optional[str] = None):
        super(FirstRunDialog, self).__init__(parent)
        self.state = state
        self.chosen_root: Optional[str] = None
        self.chosen_db: Optional[str] = None

        self.setWindowTitle("Welcome to Cartridge")
        self.resize(660, 470)
        self.setMinimumSize(540, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel("Set up your collection")
        title.setObjectName("DetailTitle")
        layout.addWidget(title)

        intro = QLabel(
            "Cartridge catalogues the game folders you already have. It reads them "
            "and stores metadata and artwork; it never moves, renames or deletes "
            "your files, and it never runs anything it finds."
        )
        intro.setObjectName("SecondaryText")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        self.root_edit = QLineEdit()
        self.root_edit.setPlaceholderText(r"For example: E:\Gry")
        self.root_edit.setReadOnly(True)
        if suggested:
            # Programmatic setText on a read-only QLineEdit aborts in Qt >= 5.15,
            # so flip the flag around any programmatic write.
            self._set_root_text(suggested)
        row.addWidget(self.root_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("PrimaryButton")
        browse.clicked.connect(self._on_browse)
        row.addWidget(browse)
        layout.addLayout(row)

        self.explain = QLabel("")
        self.explain.setObjectName("DetailValue")
        self.explain.setWordWrap(True)
        self.explain.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.explain)

        self.warning = QLabel("")
        self.warning.setObjectName("BadgeError")
        self.warning.setWordWrap(True)
        # Hidden until a problem is found. Using hide() (not setVisible(False))
        # keeps isHidden() truthful for tests and for the window's own logic.
        self.warning.hide()
        layout.addWidget(self.warning)

        layout.addStretch(1)

        footer = QHBoxLayout()
        self.later = QPushButton("Skip for now")
        self.later.setToolTip("Open Cartridge without a collection; you can add a root later")
        self.later.clicked.connect(self.reject)
        footer.addWidget(self.later)
        footer.addStretch(1)
        self.continue_button = QPushButton("Create collection")
        self.continue_button.setObjectName("PrimaryButton")
        self.continue_button.setEnabled(False)
        self.continue_button.clicked.connect(self._on_continue)
        footer.addWidget(self.continue_button)
        layout.addLayout(footer)

        self.root_edit.textChanged.connect(self._on_root_changed)
        if suggested:
            self._on_root_changed(suggested)

    def _set_root_text(self, text: str) -> None:
        """Write the field without tripping Qt's read-only setText assertion."""
        read_only = self.root_edit.isReadOnly()
        self.root_edit.setReadOnly(False)
        try:
            self.root_edit.setText(text)
        finally:
            self.root_edit.setReadOnly(read_only)

    # ------------------------------------------------------------------
    def _on_browse(self) -> None:
        start = self.root_edit.text() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose your games folder", start
        )
        if chosen:
            self._set_root_text(chosen)

    def _on_root_changed(self, text: str) -> None:
        path = (text or "").strip()
        if not path:
            self.explain.setText("")
            self.continue_button.setEnabled(False)
            return

        resolved = resolve_db_path(path, windows=self.state.windows)
        db_path = resolved.get("db_path") or db_path_for_root(path, self.state.windows)
        assets = asset_dir_for_game(path, self.state.windows)

        self.explain.setText(
            "Cartridge will create:\n"
            "  • %s   (the collection database)\n"
            "  • %s   (artwork, one folder per game, created on import)\n\n"
            "Both are inside folders you already have and are excluded from game "
            "discovery. Nothing else is written, and your game files are never "
            "modified."
            % (db_path, shorten_path(assets.replace(path, "<game folder>"), 64))
        )

        writable = bool(resolved.get("writable"))
        if not os.path.isdir(path):
            self.warning.setText(
                "That folder does not exist. Create it first, or choose the folder "
                "that actually holds your games."
            )
            self.warning.show()
            self.continue_button.setEnabled(False)
            return

        if not writable:
            alternative = resolved.get("fallback") or ""
            self.warning.setText(
                "Cartridge cannot write to that folder (%s).\n\nAlternative location: "
                "%s\n\nYou can accept it below, or choose a different games folder."
                % (resolved.get("reason") or "not writable", alternative or "none available")
            )
            self.warning.show()
            self.continue_button.setEnabled(bool(alternative))
            self._fallback = alternative
            return

        self.warning.hide()
        self.continue_button.setEnabled(True)
        self._fallback = None

    def _on_continue(self) -> None:
        path = self.root_edit.text().strip()
        if not path:
            return
        fallback = getattr(self, "_fallback", None)

        # The database lives with the collection unless the root turned out to
        # be unwritable and the user accepted the alternative. Forgetting this
        # made a fresh install fail with "No database path is configured" even
        # though the dialog had just explained where it would go
        # (reported from a real Windows 7 run, 2026-10-10).
        resolved = resolve_db_path(path, windows=self.state.windows)
        target_db = fallback or resolved.get("db_path")
        if not target_db:
            QMessageBox.critical(
                self,
                "Set up your collection",
                "Cartridge could not work out where to put the collection "
                "database for:\n%s\n\nChoose a different folder, or set the "
                "location yourself in Settings." % path,
            )
            return

        if fallback:
            answer = QMessageBox.question(
                self,
                "Use the alternative location?",
                "The database will be stored at:\n%s\n\n(not inside your games "
                "folder, because that folder is not writable)" % target_db,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return

        try:
            if not self.state.open_database(target_db):
                errors = "\n".join(self.state.open_errors[-3:])
                QMessageBox.critical(
                    self,
                    "Could not open the database",
                    "%s\n\nChoose a different location in Settings." % errors,
                )
                return
            if fallback:
                self.state.set_setting("db.path_override", target_db)
            self.state.repo.add_root(path)
        except Exception as exc:
            QMessageBox.critical(
                self, "Setup failed", self.state.redactor.text(str(exc))
            )
            return

        self.chosen_root = path
        self.chosen_db = target_db
        self.accept()
