"""Settings view: roots, API credentials, connectivity, artwork and cache options.

Credential handling follows brief sections 6.6 and 15 exactly:

* both fields are **masked** (``QLineEdit.Password`` with a reveal toggle);
* values are written to the secret store — DPAPI on Windows, a 0600 file
  elsewhere, session-only if neither is available — and **never** to SQLite;
* the view states which backend is active, including the honest warning when the
  store is unencrypted, rather than implying protection that was not verified;
* "Test connection" performs one small authenticated query per provider and
  reports latency plus the provider's own error text, redacted.

The root-folder list is where the database location is made visible, and where an
unwritable root is reported with an explicit alternative instead of being silently
relocated.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from cartridge.app_state import AppState
from cartridge.config import resolve_db_path
from cartridge.paths import shorten_path
from cartridge.ui.widgets.common import Badge, IconButton, KeyValueGrid, Separator


class SettingsView(QWidget):
    """Application settings."""

    counts_changed = pyqtSignal(str)
    busy_changed = pyqtSignal(bool, str)
    status_message = pyqtSignal(str)
    navigate_requested = pyqtSignal(str)
    roots_changed = pyqtSignal()

    def __init__(self, state: AppState, parent=None):
        super(SettingsView, self).__init__(parent)
        self.state = state
        self._test_handles: List[Any] = []
        #: preference widgets are loaded before their signals are connected, so
        #: applying a saved value cannot immediately write it back.
        self._prefs_ready = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        body = QWidget()
        self.layout_body = QVBoxLayout(body)
        self.layout_body.setContentsMargins(16, 14, 16, 18)
        self.layout_body.setSpacing(12)
        scroll.setWidget(body)

        self._build_roots_group()
        self._build_credentials_group()
        self._build_artwork_group()
        self._build_data_group()
        self.layout_body.addStretch(1)
        self._wire_preferences()

    # ------------------------------------------------------------------
    def _build_roots_group(self) -> None:
        group = QGroupBox("Game folders")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)

        hint = QLabel(
            "Each immediate subfolder of a root is treated as one game. Cartridge "
            "reads these folders; it never moves, renames or deletes anything "
            "inside them. The only files it writes are its own, inside "
            "'.cartridge' (database) and '_cartridge' (artwork)."
        )
        hint.setObjectName("SecondaryText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.roots_list = QWidget()
        self.roots_layout = QVBoxLayout(self.roots_list)
        self.roots_layout.setContentsMargins(0, 0, 0, 0)
        self.roots_layout.setSpacing(4)
        layout.addWidget(self.roots_list)

        row = QHBoxLayout()
        self.add_root_button = QPushButton("Add root folder…")
        self.add_root_button.setObjectName("PrimaryButton")
        self.add_root_button.clicked.connect(self._on_add_root)
        row.addWidget(self.add_root_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.db_label = QLabel("")
        self.db_label.setObjectName("DetailValue")
        self.db_label.setWordWrap(True)
        self.db_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.db_label)

        self.layout_body.addWidget(group)

    def _build_credentials_group(self) -> None:
        group = QGroupBox("Metadata providers")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)

        note = QLabel(
            "IGDB is the primary source and needs a Twitch application's client id "
            "and secret (OAuth2 client-credentials). RAWG is the optional fallback "
            "and needs an API key. Both are used only to search and download "
            "metadata and artwork; your collection works offline once imported."
        )
        note.setObjectName("SecondaryText")
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignLeft)

        self.igdb_id = QLineEdit()
        self.igdb_id.setPlaceholderText("Twitch application client id")
        self.igdb_id.setEchoMode(QLineEdit.Password)
        form.addRow("IGDB client id", self._with_reveal(self.igdb_id))

        self.igdb_secret = QLineEdit()
        self.igdb_secret.setPlaceholderText("Twitch application client secret")
        self.igdb_secret.setEchoMode(QLineEdit.Password)
        form.addRow("IGDB client secret", self._with_reveal(self.igdb_secret))

        self.rawg_key = QLineEdit()
        self.rawg_key.setPlaceholderText("RAWG API key")
        self.rawg_key.setEchoMode(QLineEdit.Password)
        form.addRow("RAWG API key", self._with_reveal(self.rawg_key))

        layout.addLayout(form)

        self.store_label = QLabel("")
        self.store_label.setObjectName("MutedText")
        self.store_label.setWordWrap(True)
        self.store_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.store_label)

        row = QHBoxLayout()
        self.save_credentials_button = QPushButton("Save credentials")
        self.save_credentials_button.setObjectName("PrimaryButton")
        self.save_credentials_button.clicked.connect(self._on_save_credentials)
        row.addWidget(self.save_credentials_button)

        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._on_test_connection)
        row.addWidget(self.test_button)

        self.clear_credentials_button = IconButton("Remove saved", "Delete stored credentials")
        self.clear_credentials_button.setObjectName("DangerButton")
        self.clear_credentials_button.clicked.connect(self._on_clear_credentials)
        row.addWidget(self.clear_credentials_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.connection_result = KeyValueGrid(key_width=132)
        layout.addWidget(self.connection_result)

        limits = QLabel(
            "Rate limits are enforced locally: IGDB is paced to at most ~3 requests "
            "per second and 1,500 per day (published limits are 4/s and 40,000/day), "
            "RAWG to 250 per day so the 20,000/month free quota cannot be exhausted "
            "by accident. A 'Retry-After' from either service is honoured."
        )
        limits.setObjectName("MutedText")
        limits.setWordWrap(True)
        layout.addWidget(limits)

        self.layout_body.addWidget(group)

    def _with_reveal(self, field: QLineEdit) -> QWidget:
        """A masked field plus an explicit reveal toggle."""
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(field, 1)
        toggle = IconButton("Show", "Reveal this value on screen")
        toggle.setCheckable(True)

        def on_toggled(checked: bool) -> None:
            field.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
            toggle.setText("Hide" if checked else "Show")

        toggle.toggled.connect(on_toggled)
        layout.addWidget(toggle)
        return holder

    def _build_artwork_group(self) -> None:
        group = QGroupBox("Artwork")
        form = QFormLayout(group)
        form.setSpacing(6)

        self.screenshots_spin = QSpinBox()
        self.screenshots_spin.setRange(0, 8)
        self.screenshots_spin.setValue(3)
        self.screenshots_spin.setToolTip(
            "How many screenshots to download per game. Fewer means less disk and "
            "memory use on a machine with a mechanical drive."
        )
        form.addRow("Screenshots per game", self.screenshots_spin)

        self.download_artwork = QCheckBox("Download cover and screenshots during import")
        self.download_artwork.setChecked(True)
        form.addRow("", self.download_artwork)

        self.auto_measure_size = QCheckBox(
            "Measure folder size automatically after a rescan (can be slow on a HDD)"
        )
        self.auto_measure_size.setChecked(False)
        form.addRow("", self.auto_measure_size)

        cache_row = QHBoxLayout()
        self.cache_entries = QSpinBox()
        self.cache_entries.setRange(16, 512)
        self.cache_entries.setValue(96)
        self.cache_entries.setToolTip("Decoded covers kept in memory")
        cache_row.addWidget(QLabel("Cover cache:"))
        cache_row.addWidget(self.cache_entries)
        self.clear_cache_button = IconButton("Clear now", "Free the decoded-image cache")
        self.clear_cache_button.clicked.connect(self._on_clear_cache)
        cache_row.addWidget(self.clear_cache_button)
        cache_row.addStretch(1)
        form.addRow("", self._wrap(cache_row))

        self.layout_body.addWidget(group)

    def _build_data_group(self) -> None:
        group = QGroupBox("Collection data")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)

        row = QHBoxLayout()
        self.backup_button = QPushButton("Back up database…")
        self.backup_button.setToolTip(
            "Copy the collection database to a file you choose. Artwork is already "
            "stored next to each game."
        )
        self.backup_button.clicked.connect(self._on_backup)
        row.addWidget(self.backup_button)

        self.export_button = QPushButton("Export collection (JSON)…")
        self.export_button.setToolTip("Titles, tags, notes and asset references. No credentials.")
        self.export_button.clicked.connect(self._on_export)
        row.addWidget(self.export_button)

        self.integrity_button = QPushButton("Run integrity check")
        self.integrity_button.clicked.connect(self._on_integrity)
        row.addWidget(self.integrity_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.data_label = QLabel("")
        self.data_label.setObjectName("DetailValue")
        self.data_label.setWordWrap(True)
        self.data_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.data_label)

        self.layout_body.addWidget(group)

    @staticmethod
    def _wrap(layout) -> QWidget:
        holder = QWidget()
        holder.setLayout(layout)
        return holder

    # ------------------------------------------------------------------
    def view_shown(self) -> None:
        self.refresh()

    def view_hidden(self) -> None:
        pass

    def focus_search(self) -> None:
        self.igdb_id.setFocus()

    def describe_counts(self) -> str:
        return "Settings"

    def refresh(self) -> None:
        self._load_roots()
        self._load_credentials()
        self._load_preferences()
        self._refresh_data_label()

    # ------------------------------------------------------------------
    def _load_roots(self) -> None:
        while self.roots_layout.count():
            item = self.roots_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        roots = self.state.roots()
        if not roots:
            empty = QLabel("No root folders configured yet.")
            empty.setObjectName("MutedText")
            self.roots_layout.addWidget(empty)
        for root in roots:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)

            enabled = QCheckBox()
            enabled.setChecked(bool(root.enabled))
            enabled.setToolTip("Include this folder when scanning")
            enabled.toggled.connect(
                lambda checked, rid=root.id: self._on_toggle_root(rid, checked)
            )
            layout.addWidget(enabled)

            label = QLabel(shorten_path(root.path, 62))
            label.setObjectName("DetailValue")
            label.setToolTip(root.path)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(label, 1)

            if root.last_scanned_at:
                from cartridge.core.clock import age_text

                stamp = QLabel("scanned %s" % age_text(root.last_scanned_at))
                stamp.setObjectName("MutedText")
                layout.addWidget(stamp)

            remove = IconButton("Remove", "Detach this root (records are kept)")
            remove.setObjectName("DangerButton")
            remove.clicked.connect(lambda _c=False, rid=root.id: self._on_remove_root(rid))
            layout.addWidget(remove)

            self.roots_layout.addWidget(row)

        self._refresh_db_label()

    def _refresh_db_label(self) -> None:
        info = self.state.db_info()
        path = info.get("path") or ""
        if not path:
            roots = self.state.root_paths()
            if roots:
                resolved = resolve_db_path(roots[0], windows=self.state.windows)
                path = resolved.get("db_path", "")
        if not path:
            self.db_label.setText("Database: not created yet (add a root folder).")
            return
        size = info.get("size_bytes")
        text = "Database: %s" % path
        if size:
            from cartridge.text import human_size

            text += "  (%s)" % human_size(size)
        if info.get("open"):
            text += "  · schema v%s · journal %s" % (
                info.get("schema_version"), info.get("journal_mode")
            )
        if info.get("errors"):
            text += "\nProblem: %s" % info["errors"][-1]
        self.db_label.setText(text)

    def _on_add_root(self) -> None:
        start = os.path.expanduser("~")
        existing = self.state.root_paths()
        if existing:
            start = existing[0]
        chosen = QFileDialog.getExistingDirectory(self, "Choose a games root folder", start)
        if not chosen:
            return
        try:
            self._add_root_chosen(chosen)
        except Exception as exc:
            # An exception escaping a Qt slot only reaches stderr, which a
            # windowed build never shows - the user sees "nothing happens".
            # Surface it as a dialog instead, redacted.
            message = self.state.redactor.text(str(exc))
            QMessageBox.critical(self, "Add root folder", message)
            self.status_message.emit("Adding the root folder failed: %s" % message)

    def _add_root_chosen(self, chosen: str) -> None:
        resolved = resolve_db_path(chosen, windows=self.state.windows)
        using_fallback = False
        if not resolved.get("writable"):
            alternative = resolved.get("fallback") or ""
            answer = QMessageBox.question(
                self,
                "Folder is not writable",
                "Cartridge cannot create its database folder inside:\n%s\n\n%s\n\n"
                "Use the alternative location instead?\n%s"
                % (
                    chosen,
                    resolved.get("reason") or "",
                    alternative or "(none available)",
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes or not alternative:
                self.status_message.emit("Root folder not added: the location is not writable.")
                return
            using_fallback = True
            target_db = alternative
        else:
            target_db = resolved.get("db_path")

        # A root can be added from Settings on a completely fresh install where
        # no database exists yet (the user skipped the first-run dialog). Open
        # or create it first, at the same location the first-run dialog uses.
        if not self.state.ready:
            if not target_db:
                QMessageBox.critical(
                    self,
                    "Add root folder",
                    "Cartridge could not work out where to put the collection "
                    "database for:\n%s" % chosen,
                )
                return
            if not self.state.open_database(target_db):
                errors = "\n".join(self.state.open_errors[-3:])
                QMessageBox.critical(
                    self,
                    "Could not open the database",
                    "%s\n\nChoose a different games folder, or check that the "
                    "drive is writable." % errors,
                )
                return
        if using_fallback:
            self.state.set_setting("db.path_override", target_db)

        self.state.repo.add_root(chosen)
        self.status_message.emit("Added root folder %s" % chosen)
        self.roots_changed.emit()
        self.refresh()

    def _on_toggle_root(self, root_id: int, enabled: bool) -> None:
        if not self.state.ready:
            return
        try:
            self.state.db.execute(
                "UPDATE roots SET enabled = ? WHERE id = ?", (1 if enabled else 0, root_id)
            )
        except Exception as exc:
            self.status_message.emit("Could not update the root folder: %s" % exc)
        self.roots_changed.emit()

    def _on_remove_root(self, root_id: int) -> None:
        answer = QMessageBox.question(
            self,
            "Remove root folder",
            "Detach this root from the collection?\n\nCatalogue records are kept "
            "(they will show as 'folder missing'). Choose 'Also delete records' only "
            "if you are sure.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.state.repo.remove_root(root_id, delete_games=False)
        except Exception as exc:
            self.status_message.emit("Could not remove the root: %s" % exc)
            return
        self.roots_changed.emit()
        self.refresh()

    # ------------------------------------------------------------------
    def _load_credentials(self) -> None:
        credentials = self.state.credentials
        self.igdb_id.setText(credentials.igdb_client_id)
        self.igdb_secret.setText(credentials.igdb_client_secret)
        self.rawg_key.setText(credentials.rawg_api_key)

        describe = self.state.secret_store.describe()
        backend = describe.get("backend")
        warning = describe.get("warning") or ""
        path = describe.get("path") or ""
        if backend == "dpapi":
            text = (
                "Stored with Windows DPAPI (encrypted for your Windows account) at %s."
                % path
            )
        elif backend == "file":
            text = "Stored in a local file at %s. %s" % (path, warning)
        else:
            text = "Session only: %s" % warning
        text += " Credentials are never written to the collection database, to logs, " \
                "or to a diagnostics export."
        self.store_label.setText(text)

    def _on_save_credentials(self) -> None:
        try:
            written = self.state.save_credentials(
                igdb_client_id=self.igdb_id.text(),
                igdb_client_secret=self.igdb_secret.text(),
                rawg_api_key=self.rawg_key.text(),
            )
        except Exception as exc:
            self.status_message.emit("Could not save credentials: %s" % exc)
            return
        backend = self.state.secret_store.describe().get("backend")
        if not written:
            self.status_message.emit("Nothing to save: all credential fields are empty.")
            return
        self.status_message.emit(
            "Saved %d credential value(s) using the %s store." % (len(written), backend)
        )
        self._load_credentials()

    def _on_clear_credentials(self) -> None:
        answer = QMessageBox.question(
            self,
            "Remove saved credentials",
            "Delete the stored IGDB and RAWG credentials from this machine?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.state.clear_saved_credentials()
        self.igdb_id.clear()
        self.igdb_secret.clear()
        self.rawg_key.clear()
        self.connection_result.clear()
        self.status_message.emit("Saved credentials removed.")
        self._load_credentials()

    def _on_test_connection(self) -> None:
        """One small authenticated query per provider, on a worker thread."""
        if self._test_handles:
            self.status_message.emit("A connection test is already running.")
            return

        # Use whatever is typed in the fields, so a test validates the values the
        # user is about to save rather than the ones already stored.
        client_id = self.igdb_id.text().strip()
        client_secret = self.igdb_secret.text().strip()
        rawg_key = self.rawg_key.text().strip()
        if not (client_id and client_secret) and not rawg_key:
            self.status_message.emit("Enter at least one set of credentials to test.")
            return

        self.connection_result.set_rows([("Status", "Testing…")])
        self.busy_changed.emit(True, "Testing provider connections…")
        service = self.state.service
        redactor = self.state.redactor

        def work():
            """Runs on a worker thread; touches no widgets."""
            previous = (
                service.igdb._client_id,
                service.igdb._client_secret,
                service.rawg._api_key,
            )
            try:
                service.igdb.set_credentials(client_id, client_secret)
                service.rawg.set_credentials(rawg_key)
                service.igdb.invalidate_token()
                results = service.test_connections()
            finally:
                # Restore whatever was configured before, so an unsaved test does
                # not silently change the application's credentials.
                service.igdb.set_credentials(previous[0], previous[1])
                service.rawg.set_credentials(previous[2])
                service.igdb.invalidate_token()
            return results

        handle = self.state.pool.submit(
            work,
            _name="connection_test",
            _on_finished=self._on_test_finished,
            _on_failed=self._on_test_failed,
        )
        self._test_handles.append(handle)

    def _on_test_finished(self, results: Any) -> None:
        self._test_handles = []
        self.busy_changed.emit(False, "")
        rows: List[tuple] = []
        for result in results or ():
            provider = str(result.get("provider", "?")).upper()
            if result.get("ok"):
                rows.append(
                    (
                        provider,
                        "OK · %s ms · %s result(s)"
                        % (
                            result.get("latency_ms"),
                            result.get("sample_results", 0),
                        ),
                    )
                )
            else:
                stage = result.get("stage", "")
                error = result.get("error") or "unknown error"
                rows.append((provider, "FAILED at %s · %s" % (stage, error)))
            budget = result.get("rate_limit")
            if budget:
                rows.append(
                    (
                        "%s budget" % provider,
                        "%s/%s requests used today"
                        % (budget.get("used_today"), budget.get("max_per_day")),
                    )
                )
        self.connection_result.set_rows(rows)
        self.status_message.emit(
            "Connection test finished: %s"
            % ", ".join(
                "%s %s" % (str(r.get("provider", "?")).upper(), "OK" if r.get("ok") else "failed")
                for r in (results or [])
            )
        )

    def _on_test_failed(self, message: str, _detail: str) -> None:
        self._test_handles = []
        self.busy_changed.emit(False, "")
        self.connection_result.set_rows([("Status", "Test failed: %s" % message)])
        self.status_message.emit("Connection test failed: %s" % message)

    # ------------------------------------------------------------------
    def _load_preferences(self) -> None:
        repo = self.state.repo
        if repo is None:
            return
        self.screenshots_spin.setValue(repo.get_int("artwork.screenshots", 3))
        self.download_artwork.setChecked(repo.get_bool("artwork.download", True))
        self.auto_measure_size.setChecked(repo.get_bool("scan.auto_measure_size", False))
        self.cache_entries.setValue(repo.get_int("ui.cache_entries", 96))
        self.state.image_cache.max_entries = max(1, int(self.cache_entries.value()))
        self._prefs_ready = True

    def _wire_preferences(self) -> None:
        """Connect preference signals exactly once, for the lifetime of the view."""
        self.screenshots_spin.valueChanged.connect(
            lambda value: self._save_pref("artwork.screenshots", value)
        )
        self.download_artwork.toggled.connect(
            lambda value: self._save_pref("artwork.download", value)
        )
        self.auto_measure_size.toggled.connect(
            lambda value: self._save_pref("scan.auto_measure_size", value)
        )
        self.cache_entries.valueChanged.connect(self._on_cache_entries_changed)

    def _save_pref(self, key: str, value: Any) -> None:
        if not self._prefs_ready:
            return
        try:
            self.state.set_setting(key, value)
        except Exception as exc:
            self.status_message.emit("Could not save the preference: %s" % exc)

    def _on_cache_entries_changed(self, value: int) -> None:
        # The cache bound is applied even while loading, so the UI and the cache
        # can never disagree about the limit.
        self.state.image_cache.max_entries = max(1, int(value))
        self._save_pref("ui.cache_entries", value)

    def _on_clear_cache(self) -> None:
        before = self.state.image_cache.stats()["entries"]
        self.state.image_cache.clear()
        self.status_message.emit("Cleared %d cached image(s)." % before)

    # ------------------------------------------------------------------
    def _refresh_data_label(self) -> None:
        if not self.state.ready:
            self.data_label.setText("No database is open.")
            return
        stats = self.state.repo.stats()
        self.data_label.setText(
            "%d games · %d favourites · %d incomplete · %d without a cover · "
            "%d artwork files · %d root folder(s) · %d content types"
            % (
                stats["games"], stats["favourites"], stats["incomplete"],
                stats["without_cover"], stats["assets"], stats["roots"],
                stats["content_types"],
            )
        )

    def _on_backup(self) -> None:
        if self.state.db is None:
            self.status_message.emit("No database is open.")
            return
        suggested = os.path.join(
            os.path.expanduser("~"),
            "cartridge-backup-%s.db" % _stamp(),
        )
        chosen = QFileDialog.getSaveFileName(
            self, "Back up the collection database", suggested, "SQLite database (*.db)"
        )[0]
        if not chosen:
            return
        try:
            self.state.db.backup(chosen)
        except Exception as exc:
            self.status_message.emit("Backup failed: %s" % exc)
            return
        self.status_message.emit("Backup written to %s" % chosen)

    def _on_export(self) -> None:
        if not self.state.ready:
            self.status_message.emit("No database is open.")
            return
        import json

        chosen = QFileDialog.getSaveFileName(
            self,
            "Export the collection",
            os.path.join(os.path.expanduser("~"), "cartridge-collection-%s.json" % _stamp()),
            "JSON (*.json)",
        )[0]
        if not chosen:
            return
        try:
            payload = self.state.repo.export_collection()
            with open(chosen, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
        except (OSError, ValueError) as exc:
            self.status_message.emit("Export failed: %s" % exc)
            return
        self.status_message.emit(
            "Exported %d game(s) to %s" % (len(payload.get("games", [])), chosen)
        )

    def _on_integrity(self) -> None:
        if self.state.db is None:
            self.status_message.emit("No database is open.")
            return
        ok, message = self.state.db.integrity_check()
        orphans = self.state.db.foreign_key_check()
        QMessageBox.information(
            self,
            "Database integrity",
            "PRAGMA integrity_check: %s\nForeign key violations: %d\nPath: %s"
            % (message, len(orphans), self.state.db_path or "(unknown)"),
        )
        self.status_message.emit(
            "Integrity check: %s (%d foreign key issue(s))"
            % ("ok" if ok else "FAILED", len(orphans))
        )


def _stamp() -> str:
    import time

    return time.strftime("%Y%m%d-%H%M%S")
