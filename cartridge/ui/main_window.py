"""Main window: title bar, navigation rail, stacked views, status bar.

Layout follows the approved reference concept (brief section 6.2): a compact title
bar carrying app identity and the current root path, a left navigation rail, a
stacked content area, and a status bar. Widths come from layouts and size
policies, not hard-coded pixel geometry, so the window stays usable at 1280x800
and adapts when it is resized smaller or larger.

The window owns no business logic: it wires views to :class:`AppState` and routes
the navigation and status signals between them.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QShortcut,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from cartridge import __app_name__, __version__
from cartridge.app_state import AppState
from cartridge.paths import shorten_path
from cartridge.ui.theme import app_font

NAV_COLLECTION = "collection"
NAV_MANAGER = "manager"
NAV_FAVOURITES = "favourites"
NAV_SETTINGS = "settings"
NAV_DIAGNOSTICS = "diagnostics"

NAV_ITEMS = (
    (NAV_COLLECTION, "Collection", "Browse and search your games"),
    (NAV_MANAGER, "Manager", "Folders, matches and attention states"),
    (NAV_FAVOURITES, "Favourites", "Games you marked as favourites"),
    (NAV_SETTINGS, "Settings", "Roots, API credentials, artwork options"),
    (NAV_DIAGNOSTICS, "Diagnostics", "Versions, database health, measurements"),
)

NAV_WIDTH = 132
MIN_WINDOW_WIDTH = 1024
MIN_WINDOW_HEIGHT = 640
TARGET_WIDTH = 1280
TARGET_HEIGHT = 800


class TitleBar(QWidget):
    """Compact identity bar: app name, version, current root, rescan action."""

    rescan_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super(TitleBar, self).__init__(parent)
        self.setObjectName("TitleBar")
        # A QWidget subclass does not paint its stylesheet background unless this
        # attribute is set (Qt's own classes get it automatically). Without it the
        # bar renders transparent, which reads as 'white' in a light-theme viewer.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(44)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)

        title = QLabel(__app_name__)
        title.setObjectName("AppTitle")
        layout.addWidget(title)

        version = QLabel("v" + __version__)
        version.setObjectName("MutedText")
        layout.addWidget(version)

        layout.addSpacing(8)
        separator = QFrame()
        separator.setFrameShape(QFrame.VLine)
        separator.setObjectName("Separator")
        layout.addWidget(separator)

        self.root_label = QLabel("No games root configured")
        self.root_label.setObjectName("RootPath")
        self.root_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.root_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.root_label, 1)

        self.rescan_button = QPushButton("Rescan")
        self.rescan_button.setObjectName("GhostButton")
        self.rescan_button.setToolTip("Re-scan the configured root folders (F5)")
        self.rescan_button.clicked.connect(self.rescan_requested.emit)
        layout.addWidget(self.rescan_button)

    def set_root_text(self, text: str) -> None:
        self.root_label.setText(text)
        self.root_label.setToolTip(text)

    def set_busy(self, busy: bool) -> None:
        self.rescan_button.setEnabled(not busy)
        self.rescan_button.setText("Scanning..." if busy else "Rescan")


class NavRail(QWidget):
    """Left navigation column."""

    navigation_changed = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super(NavRail, self).__init__(parent)
        self.setObjectName("NavRail")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(NAV_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(2)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: Dict[str, QPushButton] = {}

        for key, label, tooltip in NAV_ITEMS:
            button = QPushButton(label)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setToolTip(tooltip)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, k=key: self._select(k))
            self.group.addButton(button)
            self.buttons[key] = button
            layout.addWidget(button)

        layout.addStretch(1)

        footer = QLabel("local-first\nno telemetry")
        footer.setObjectName("MutedText")
        footer.setAlignment(Qt.AlignCenter)
        footer.setFont(app_font(7))
        layout.addWidget(footer)

    def _select(self, key: str) -> None:
        self.set_current(key)
        self.navigation_changed.emit(key)

    def set_current(self, key: str) -> None:
        button = self.buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)

    def set_badge(self, key: str, count: int) -> None:
        """Show an attention count next to a nav label (0 hides it)."""
        button = self.buttons.get(key)
        if button is None:
            return
        label = dict((item[0], item[1]) for item in NAV_ITEMS).get(key, key)
        button.setText("%s  (%d)" % (label, count) if count > 0 else label)


class StatusBar(QStatusBar):
    """Result counts, busy state and keyboard hints."""

    def __init__(self, parent: Optional[QWidget] = None):
        super(StatusBar, self).__init__(parent)
        self.setSizeGripEnabled(False)

        self.count_label = QLabel("Ready")
        self.addWidget(self.count_label, 1)

        self.busy_label = QLabel("")
        self.busy_label.setObjectName("MutedText")
        self.addWidget(self.busy_label)

        self.hint_label = QLabel(
            "Ctrl+F search · F5 rescan · Enter open · Esc clear"
        )
        self.hint_label.setObjectName("MutedText")
        self.addPermanentWidget(self.hint_label)

        self.progress = None  # created on demand to avoid painting when idle

    def set_counts(self, text: str) -> None:
        self.count_label.setText(text)

    def set_busy(self, busy: bool, text: str = "") -> None:
        self.busy_label.setText(text if busy else "")

    def set_message(self, text: str, timeout_ms: int = 6000) -> None:
        if text:
            self.showMessage(text, timeout_ms)
        else:
            self.clearMessage()


class MainWindow(QMainWindow):
    """The application's single top-level window."""

    def __init__(self, state: AppState, parent: Optional[QWidget] = None):
        super(MainWindow, self).__init__(parent)
        self.state = state
        self._views: Dict[str, QWidget] = {}
        self._current_view = NAV_COLLECTION

        self.setWindowTitle("%s — local game collection manager" % __app_name__)
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.resize(TARGET_WIDTH, TARGET_HEIGHT)

        central = QWidget()
        central.setObjectName("RootSurface")
        central.setAttribute(Qt.WA_StyledBackground, True)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.title_bar = TitleBar()
        root_layout.addWidget(self.title_bar)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self.nav = NavRail()
        self.nav.navigation_changed.connect(self.show_view)
        body_layout.addWidget(self.nav)

        self.stack = QStackedWidget()
        body_layout.addWidget(self.stack, 1)

        root_layout.addWidget(body, 1)
        self.setCentralWidget(central)

        self.status = StatusBar()
        self.setStatusBar(self.status)

        self._build_views()
        self._install_shortcuts()
        self.refresh_root_label()

        # Restore the last used view if the settings database is available.
        saved_view = state.setting("ui.view", NAV_COLLECTION)
        if saved_view in self._views:
            self.show_view(str(saved_view))
        else:
            self.show_view(NAV_COLLECTION)

        # One deferred refresh keeps first paint fast on the target machine:
        # the window appears immediately, the collection loads a moment later.
        QTimer.singleShot(0, self.on_ready)

    # ------------------------------------------------------------------
    def _build_views(self) -> None:
        """Create the views lazily-but-eagerly enough to keep startup honest.

        Importing inside the method keeps ``cartridge.ui.main_window`` importable
        in a headless test that only needs the shell, and makes the dependency
        direction explicit (views depend on the shell, never the reverse).
        """
        from cartridge.ui.views.browser import BrowserView
        from cartridge.ui.views.diagnostics import DiagnosticsView
        from cartridge.ui.views.manager import ManagerView
        from cartridge.ui.views.settings import SettingsView

        favourites = BrowserView(self.state, favourites_only=True)
        collection = BrowserView(self.state)

        views = [
            (NAV_COLLECTION, collection),
            (NAV_MANAGER, ManagerView(self.state)),
            (NAV_FAVOURITES, favourites),
            (NAV_SETTINGS, SettingsView(self.state)),
            (NAV_DIAGNOSTICS, DiagnosticsView(self.state)),
        ]
        for key, view in views:
            self._views[key] = view
            self.stack.addWidget(view)
            self._wire_view(key, view)

    def _wire_view(self, key: str, view: QWidget) -> None:
        """Connect the signals every view is expected to expose."""
        counts = getattr(view, "counts_changed", None)
        if counts is not None:
            counts.connect(self._on_counts_changed)
        busy = getattr(view, "busy_changed", None)
        if busy is not None:
            busy.connect(self._on_busy_changed)
        message = getattr(view, "status_message", None)
        if message is not None:
            message.connect(self.status.set_message)
        navigate = getattr(view, "navigate_requested", None)
        if navigate is not None:
            navigate.connect(self.show_view)
        rescan = getattr(view, "rescan_requested", None)
        if rescan is not None:
            rescan.connect(self.on_rescan)

    # ------------------------------------------------------------------
    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.focus_search)
        QShortcut(QKeySequence("F5"), self, activated=self.on_rescan)
        QShortcut(QKeySequence("Ctrl+1"), self,
                  activated=lambda: self.show_view(NAV_COLLECTION))
        QShortcut(QKeySequence("Ctrl+2"), self,
                  activated=lambda: self.show_view(NAV_MANAGER))
        QShortcut(QKeySequence("Ctrl+3"), self,
                  activated=lambda: self.show_view(NAV_FAVOURITES))
        QShortcut(QKeySequence("Ctrl+4"), self,
                  activated=lambda: self.show_view(NAV_SETTINGS))
        QShortcut(QKeySequence("Ctrl+5"), self,
                  activated=lambda: self.show_view(NAV_DIAGNOSTICS))

    def focus_search(self) -> None:
        view = self.current_view()
        focus = getattr(view, "focus_search", None)
        if callable(focus):
            focus()
        else:
            self.status.set_message("This view has no search field.")

    # ------------------------------------------------------------------
    def show_view(self, key: str) -> None:
        view = self._views.get(key)
        if view is None:
            return
        previous = self._current_view
        self._current_view = key
        self.stack.setCurrentWidget(view)
        self.nav.set_current(key)
        self.state.set_setting("ui.view", key)

        # Ask the outgoing view to release transient work and the incoming one to
        # refresh, so a background scan cannot paint into a hidden view.
        leaving = getattr(self._views.get(previous), "view_hidden", None)
        if callable(leaving):
            leaving()
        entering = getattr(view, "view_shown", None)
        if callable(entering):
            entering()
        self._update_counts_for_view(key)

    def current_view(self) -> Optional[QWidget]:
        return self._views.get(self._current_view)

    def current_key(self) -> str:
        return self._current_view

    # ------------------------------------------------------------------
    def refresh_root_label(self) -> None:
        roots = self.state.roots()
        if not roots:
            self.title_bar.set_root_text("No games root configured — open Settings")
            self.status.set_counts("No collection loaded")
            return
        enabled = [root for root in roots if root.enabled]
        if len(enabled) == 1:
            text = shorten_path(enabled[0].path, 78)
        elif enabled:
            text = "%d roots · %s" % (len(enabled), shorten_path(enabled[0].path, 60))
        else:
            text = "%d roots (all disabled)" % len(roots)
        self.title_bar.set_root_text(text)

    def on_ready(self) -> None:
        """Called once after the window is shown: load data without blocking paint."""
        self.refresh_root_label()
        view = self.current_view()
        refresh = getattr(view, "refresh", None)
        if callable(refresh):
            refresh()
        self._update_all_badges()

    def on_rescan(self) -> None:
        if not self.state.ready:
            self.status.set_message("Open a games root in Settings first.")
            self.show_view(NAV_SETTINGS)
            return
        view = self._views.get(NAV_MANAGER)
        rescan = getattr(view, "start_rescan", None)
        if callable(rescan):
            self.show_view(NAV_MANAGER)
            rescan()
        else:
            self.status.set_message("Rescan is not available right now.")

    # ------------------------------------------------------------------
    def _on_counts_changed(self, text: str) -> None:
        self.status.set_counts(str(text))

    def _on_busy_changed(self, busy: bool, text: str = "") -> None:
        self.status.set_busy(bool(busy), str(text or ""))
        self.title_bar.set_busy(bool(busy))

    def _update_counts_for_view(self, key: str) -> None:
        view = self._views.get(key)
        describe = getattr(view, "describe_counts", None)
        if callable(describe):
            self.status.set_counts(str(describe()))

    def _update_all_badges(self) -> None:
        if self.state.repo is None:
            return
        try:
            attention = self.state.repo.count_games(
                _attention_spec()
            )
        except Exception:
            return
        self.nav.set_badge(NAV_MANAGER, attention)
        try:
            favourites = self.state.repo.count_games(_favourites_spec())
        except Exception:
            favourites = 0
        self.nav.set_badge(NAV_FAVOURITES, favourites)

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Persist geometry and shut the pool down cleanly."""
        try:
            self.state.set_setting("ui.geometry", str(self.saveGeometry().toHex().data(), "ascii"))
            self.state.set_setting("ui.window_state", str(self.saveState().toHex().data(), "ascii"))
        except Exception:
            pass
        view = self.current_view()
        leaving = getattr(view, "view_hidden", None)
        if callable(leaving):
            leaving()
        self.state.shutdown()
        super(MainWindow, self).closeEvent(event)

    def restore_geometry(self) -> bool:
        """Apply a previously saved geometry. Returns False when none exists."""
        raw = self.state.setting("ui.geometry")
        if not raw:
            return False
        try:
            from PyQt5.QtCore import QByteArray

            return bool(self.restoreGeometry(QByteArray.fromHex(raw.encode("ascii"))))
        except Exception:
            return False


def _attention_spec():
    from cartridge.core import filtering

    return filtering.FilterSpec(view=filtering.VIEW_NEEDS_ATTENTION)


def _favourites_spec():
    from cartridge.core import filtering

    return filtering.FilterSpec(view=filtering.VIEW_FAVOURITES)
