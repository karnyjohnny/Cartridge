"""Diagnostics view: real measurements, sanitized by default.

Brief section 6.7 lists what belongs here and section 15 constrains what may
leave the machine. The default report is the *shareable* one: versions, database
health, worker and cache counters, latency samples, API connectivity state and
rate-limit budgets. It contains no credentials, no absolute paths and no game
titles. A second, explicitly labelled local export adds paths and titles for the
user's own debugging, with a warning shown before it is written.

Nothing here invents a number. A measurement that was never taken reads
"not measured" — never an estimate and never a sample from a different machine.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from cartridge import diagnostics
from cartridge.app_state import AppState
from cartridge.text import human_size
from cartridge.ui.widgets.common import (
    IconButton,
    KeyValueGrid,
    Separator,
    detach_and_delete,
)


class DiagnosticsView(QWidget):
    """Read-only measurements plus the two export actions."""

    counts_changed = pyqtSignal(str)
    busy_changed = pyqtSignal(bool, str)
    status_message = pyqtSignal(str)
    navigate_requested = pyqtSignal(str)

    def __init__(self, state: AppState, parent=None):
        super(DiagnosticsView, self).__init__(parent)
        self.state = state
        self._report: Dict[str, Any] = {}
        self._local_mode = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)

        header = QLabel("Diagnostics")
        header.setObjectName("SectionTitle")
        layout.addWidget(header)

        note = QLabel(
            "Everything below is measured on this machine right now. Values that were "
            "never measured say so — nothing here is an estimate or a sample value."
        )
        note.setObjectName("SecondaryText")
        note.setWordWrap(True)
        layout.addWidget(note)

        actions = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.setObjectName("PrimaryButton")
        refresh.clicked.connect(self.refresh)
        actions.addWidget(refresh)

        self.check_db_button = QPushButton("Check database integrity")
        self.check_db_button.clicked.connect(self._on_integrity)
        actions.addWidget(self.check_db_button)

        copy_button = IconButton("Copy shareable report", "Copies text with no paths, titles or credentials")
        copy_button.clicked.connect(self._on_copy)
        actions.addWidget(copy_button)

        save_button = IconButton("Save shareable…")
        save_button.clicked.connect(lambda: self._on_save(local=False))
        actions.addWidget(save_button)

        self.local_button = IconButton("Save detailed (local)…")
        self.local_button.setObjectName("DangerButton")
        self.local_button.setToolTip("Includes absolute paths and game titles")
        self.local_button.clicked.connect(lambda: self._on_save(local=True))
        actions.addWidget(self.local_button)

        actions.addStretch(1)
        layout.addLayout(actions)

        layout.addWidget(Separator())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(scroll, 1)

        body = QWidget()
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(0, 0, 8, 0)
        self.body_layout.setSpacing(10)
        scroll.setWidget(body)

        self.sections: List[KeyValueGrid] = []

        self.text_view = QPlainTextEdit()
        self.text_view.setReadOnly(True)
        self.text_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text_view.setVisible(False)
        layout.addWidget(self.text_view, 1)

        toggle = QHBoxLayout()
        toggle.addStretch(1)
        self.text_button = IconButton("Show raw text")
        self.text_button.setCheckable(True)
        self.text_button.toggled.connect(self._on_toggle_text)
        toggle.addWidget(self.text_button)
        layout.addLayout(toggle)

    # ------------------------------------------------------------------
    def view_shown(self) -> None:
        self.refresh()

    def view_hidden(self) -> None:
        pass

    def focus_search(self) -> None:
        pass

    def describe_counts(self) -> str:
        return "Diagnostics"

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self._report = diagnostics.collect(self.state, self.state.redactor)
        self._render()
        collection = self._report.get("collection", {})
        self.counts_changed.emit(
            "Diagnostics · %s games · schema v%s"
            % (
                collection.get("games", "—") if collection.get("available") else "no database",
                self._report.get("database", {}).get("schema_version", "?"),
            )
        )

    def _render(self) -> None:
        for section in self.sections:
            self.body_layout.removeWidget(section)
            detach_and_delete(section)
        self.sections = []

        for title, rows in self._section_rows():
            group = KeyValueGrid(key_width=190)
            label = QLabel(title)
            label.setObjectName("SectionTitle")
            self.body_layout.addWidget(label)
            self.sections.append(label)
            group.set_rows(rows)
            self.body_layout.addWidget(group)
            self.sections.append(group)

        self.body_layout.addStretch(1)
        self.text_view.setPlainText(diagnostics.render_text(self._report))

    def _section_rows(self) -> List[tuple]:
        report = self._report
        app = report.get("app", {})
        memory = report.get("memory", {})
        database = report.get("database", {})
        collection = report.get("collection", {})
        workers = report.get("workers", {})
        cache = report.get("image_cache", {})
        thumbnails = report.get("thumbnails", {})
        http = report.get("http", {})
        limits = report.get("rate_limits", {})
        providers = report.get("providers", {})
        store = report.get("secret_store", {})
        latency = report.get("latency", {})
        startup = report.get("startup", {})

        sections: List[tuple] = []
        sections.append((
            "Application",
            [
                ("Version", app.get("version")),
                ("Python", app.get("python")),
                ("Platform", app.get("platform")),
                ("Machine", app.get("machine")),
                ("CPU cores", app.get("cpu_count")),
                ("Packaged build", app.get("frozen")),
                ("Session uptime", _seconds(startup.get("seconds_since_startup"))),
            ],
        ))
        sections.append((
            "Memory (this process)",
            [
                ("Working set", _mb(memory.get("working_set_bytes"))),
                ("Peak working set", _mb(memory.get("peak_working_set_bytes"))),
                ("Private bytes", _mb(memory.get("private_bytes"))),
                ("Source", memory.get("source")),
            ],
        ))
        db_rows = [
            ("Open", database.get("open")),
            ("File", database.get("filename")),
            ("Drive", database.get("drive")),
            ("Size", _mb(database.get("size_bytes"))),
            ("Schema version", database.get("schema_version")),
            ("Latest supported", database.get("latest_supported_schema")),
            ("Journal mode", database.get("journal_mode")),
            ("Foreign keys", database.get("foreign_keys_enabled")),
            ("integrity_check", database.get("integrity")),
        ]
        for error in database.get("open_errors") or []:
            db_rows.append(("Open error", error))
        sections.append(("Database", db_rows))

        if collection.get("available"):
            sections.append((
                "Collection",
                [
                    ("Games", collection.get("games")),
                    ("Favourites", collection.get("favourites")),
                    ("Incomplete metadata", collection.get("incomplete")),
                    ("With a cover", collection.get("with_cover")),
                    ("Without a cover", collection.get("without_cover")),
                    ("Missing folders", collection.get("missing_folder")),
                    ("Artwork files", collection.get("assets")),
                    ("Root folders", collection.get("roots")),
                    ("Content types", collection.get("content_types")),
                ],
            ))

        sections.append((
            "Background workers",
            [
                ("Pool width", workers.get("max_threads")),
                ("Active threads", workers.get("active_threads")),
                ("Pending tasks", workers.get("pending")),
                ("Submitted", workers.get("submitted")),
                ("Completed", workers.get("completed")),
                ("Failed", workers.get("failed")),
                ("Cancelled", workers.get("cancelled")),
            ],
        ))
        sections.append((
            "Image cache",
            [
                ("Entries", "%s / %s" % (cache.get("entries"), cache.get("max_entries"))),
                ("Decoded bytes", _mb(cache.get("bytes"))),
                ("Ceiling", _mb(cache.get("max_bytes"))),
                ("Hits", cache.get("hits")),
                ("Misses", cache.get("misses")),
                ("Hit rate", cache.get("hit_rate")),
                ("Evictions", cache.get("evictions")),
                ("Thumbnails built", thumbnails.get("thumbnails_built")),
                ("Thumbnails reused", thumbnails.get("thumbnails_reused")),
            ],
        ))
        sections.append((
            "HTTP",
            [
                ("Requests", http.get("requests")),
                ("Retries", http.get("retries")),
                ("Throttled by limiter", http.get("throttled")),
                ("Connect timeout", "%s s" % http.get("timeouts", {}).get("connect")),
                ("Read timeout", "%s s" % http.get("timeouts", {}).get("read")),
            ],
        ))

        limit_rows: List[tuple] = []
        for name in sorted(limits):
            stats = limits[name]
            limit_rows.append((
                "%s (per minute)" % name,
                "%s / %s" % (stats.get("used_this_minute"), stats.get("max_per_minute")),
            ))
            limit_rows.append((
                "%s (per day)" % name,
                "%s / %s" % (stats.get("used_today"), stats.get("max_per_day")),
            ))
            limit_rows.append((
                "%s refusals / cooldowns" % name,
                "%s / %s" % (stats.get("refused"), stats.get("server_cooldowns")),
            ))
        sections.append(("API rate-limit budgets", limit_rows))

        igdb = providers.get("igdb", {})
        rawg = providers.get("rawg", {})
        sections.append((
            "Providers",
            [
                ("Configured", ", ".join(providers.get("configured") or []) or "none"),
                ("IGDB configured", igdb.get("configured")),
                ("IGDB authenticated", igdb.get("authenticated")),
                ("IGDB token expires in", _seconds(igdb.get("token_seconds_until_expiry"))),
                ("IGDB last error", igdb.get("last_error") or ""),
                ("RAWG configured", rawg.get("configured")),
                ("RAWG last error", rawg.get("last_error") or ""),
                ("Connectivity", providers.get("connectivity")),
            ],
        ))

        latency_rows = []
        for operation in sorted(latency):
            stats = latency[operation]
            latency_rows.append((
                operation,
                "n=%s · avg %s ms · max %s ms"
                % (stats.get("n"), stats.get("avg_ms"), stats.get("max_ms")),
            ))
        sections.append((
            "Measured operation latency (this session)",
            latency_rows or [("No samples yet", "")],
        ))

        sections.append((
            "Credential storage",
            [
                ("Backend", store.get("backend")),
                ("Encrypted", store.get("encrypted")),
                ("File", store.get("filename")),
                ("Keys present", ", ".join(store.get("keys_present") or []) or "none"),
                ("Warning", store.get("warning") or ""),
            ],
        ))
        return sections

    # ------------------------------------------------------------------
    def _on_integrity(self) -> None:
        if self.state.db is None:
            QMessageBox.information(self, "Database", "No database is open.")
            return
        ok, message = self.state.db.integrity_check()
        orphans = self.state.db.foreign_key_check()
        QMessageBox.information(
            self,
            "Database integrity",
            "PRAGMA integrity_check: %s\nForeign key violations: %d\n\n%s"
            % (message, len(orphans), self.state.db_path or "(path unknown)"),
        )
        self.status_message.emit(
            "Integrity check: %s" % ("ok" if ok else "FAILED - see the dialog")
        )
        self.refresh()

    def _on_copy(self) -> None:
        from PyQt5.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is None:
            self.status_message.emit("This system has no clipboard.")
            return
        clipboard.setText(diagnostics.render_text(self._report))
        self.status_message.emit(
            "Shareable report copied to the clipboard (no paths, titles or credentials)."
        )

    def _on_save(self, local: bool) -> None:
        if local:
            answer = QMessageBox.warning(
                self,
                "Detailed report contains private information",
                "The detailed report includes absolute folder paths and the titles "
                "of the games in your collection.\n\nIt never contains API "
                "credentials.\n\nOnly save this where you would keep your collection "
                "itself. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            report = diagnostics.collect_local(self.state, self.state.redactor)
            default_name = "cartridge-diagnostics-local.txt"
        else:
            report = self._report
            default_name = "cartridge-diagnostics.txt"

        chosen = QFileDialog.getSaveFileName(
            self,
            "Save diagnostics",
            os.path.join(os.path.expanduser("~"), default_name),
            "Text file (*.txt);;JSON (*.json)",
        )[0]
        if not chosen:
            return
        try:
            if chosen.lower().endswith(".json"):
                import json

                payload = json.dumps(report, indent=2, sort_keys=True, default=str)
            else:
                payload = diagnostics.render_text(report)
            with open(chosen, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Save diagnostics", "Could not write the file: %s" % exc)
            return
        self.status_message.emit("Diagnostics written to %s" % chosen)

    def _on_toggle_text(self, checked: bool) -> None:
        self.text_view.setVisible(checked)
        self.text_button.setText("Hide raw text" if checked else "Show raw text")
        for widget in self.sections:
            widget.setVisible(not checked)


def _mb(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return "%.1f MB" % (float(value) / (1024.0 * 1024.0))
    except (TypeError, ValueError):
        return None


def _seconds(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 90:
        return "%.0f s" % number
    return "%.1f min" % (number / 60.0)
