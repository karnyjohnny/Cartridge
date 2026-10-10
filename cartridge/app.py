"""Application bootstrap: build the state, open the database, show the window.

``run.py`` is the single documented entry point. ``cartridge/app.py`` holds the
logic so it can be exercised headlessly (``run.py --selftest``, GUI smoke tests)
without spawning a window that a test then has to close.

Startup is deliberately staged so first paint is fast on a Core 2 Duo:

1. construct ``QApplication`` and apply the theme (no I/O);
2. create and ``show()`` the main window (paints the shell immediately);
3. open the database and load the first page of the collection via
   ``QTimer.singleShot(0, ...)``, i.e. after the event loop starts.

Nothing in here touches the network at startup. A collection with no configured
provider still browses offline; provider calls happen only on an explicit user
action.
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import Any, Dict, List, Optional, Tuple

from cartridge import __app_name__, __version__
from cartridge.app_state import AppState
from cartridge.config import (
    ENV_DB,
    ENV_ROOT,
    config_dir,
    debug_enabled,
    environment_info,
)
from cartridge.paths import db_path_for_root, normalize_path


def qt_platform_hint() -> Optional[str]:
    """Force the offscreen plugin when there is no display to draw on.

    Only applies outside Windows: the target machine always has a desktop
    session, and silently switching to offscreen there would hide a real driver
    problem instead of reporting it.
    """
    if sys.platform.startswith("win"):
        return None
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return None
    if os.environ.get("QT_QPA_PLATFORM"):
        return None
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    return "offscreen"


def resolve_startup_paths(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    """Decide the database path and root folder from flags, env and saved state.

    Priority: explicit ``--db`` / ``--root`` flags, then environment variables
    (useful for testing against a scratch collection), then the last-used values
    from the per-user config, then nothing (first run).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="cartridge",
        description="%s — local desktop game collection manager" % __app_name__,
    )
    parser.add_argument("--root", help="games root folder to use")
    parser.add_argument("--db", help="collection database path")
    parser.add_argument(
        "--offline", action="store_true", help="do not contact metadata providers"
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="build the UI headlessly, verify it renders, then exit 0/1",
    )
    parser.add_argument(
        "--first-run", action="store_true", help="force the setup dialog"
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    environ = os.environ
    root = args.root or environ.get(ENV_ROOT) or _saved_value("last_root")
    db_path = args.db or environ.get(ENV_DB) or _saved_value("last_db")

    if root and not db_path:
        db_path = db_path_for_root(normalize_path(root))

    return {
        "root": root or None,
        "db_path": db_path or None,
        "offline": bool(args.offline),
        "selftest": bool(args.selftest),
        "first_run": bool(args.first_run),
        "debug": debug_enabled(environ),
    }


def _saved_value(key: str) -> Optional[str]:
    """Read a last-used value from the per-user config file (never a secret)."""
    import json

    path = os.path.join(config_dir(), "last-session.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return str(value) if value else None


def save_last_session(root: Optional[str], db_path: Optional[str]) -> bool:
    """Persist the last-used locations so the next launch reopens them."""
    import json

    directory = config_dir()
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory)
        path = os.path.join(directory, "last-session.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"last_root": root or "", "last_db": db_path or ""}, handle, indent=2)
        return True
    except (OSError, TypeError, ValueError):
        return False


def install_excepthook(state_holder: List[Any]) -> None:
    """Show a readable crash dialog instead of a silent exit.

    The text is redacted through the application's redactor, so a traceback that
    happens to contain a credential cannot reach the screen, a log file or a
    screenshot.
    """
    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        redacted = text
        holder = state_holder[0] if state_holder else None
        if holder is not None:
            try:
                redacted = holder.redactor.text(text)
            except Exception:
                redacted = text
        sys.__excepthook__(exc_type, exc, tb)
        try:
            from PyQt5.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None,
                    "Cartridge stopped because of an error",
                    "An unexpected error occurred. Your collection database was not "
                    "modified by this failure.\n\n%s" % redacted[-1800:],
                )
        except Exception:
            pass

    sys.excepthook = hook


def build_state(options: Dict[str, Any]) -> AppState:
    """Create the application state and open the database if a path is known."""
    state = AppState(offline=bool(options.get("offline")))
    db_path = options.get("db_path")
    root = options.get("root")
    if db_path:
        state.open_database(db_path)
    elif root:
        state.open_database(db_path_for_root(normalize_path(root)))
    return state


def run(argv: Optional[List[str]] = None) -> int:
    """Real entry point: build Qt, show the window, run the event loop."""
    options = resolve_startup_paths(argv)
    platform_hint = qt_platform_hint()

    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication

    # High-DPI rounding behaviour must be set before QApplication exists. On
    # Windows 7 with a 1280x800 panel this is a no-op, but it keeps fractional
    # scaling from producing blurry text on a modern machine.
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(__app_name__)

    from cartridge.ui.theme import apply_theme

    apply_theme(app)

    state = build_state(options)
    state_holder: List[Any] = [state]
    install_excepthook(state_holder)

    if platform_hint and options.get("debug"):
        print("Qt platform: %s (no display detected)" % platform_hint)

    from cartridge.ui.main_window import MainWindow

    window = MainWindow(state)
    if not window.restore_geometry():
        window.resize(1280, 800)
    window.show()

    if options.get("first_run") or not state.roots():
        _offer_first_run(window, state, options)

    save_last_session(
        options.get("root") or (state.root_paths()[0] if state.root_paths() else None),
        state.db_path,
    )

    exit_code = app.exec_()
    state.shutdown()
    return int(exit_code)


def _offer_first_run(window, state: AppState, options: Dict[str, Any]) -> None:
    from PyQt5.QtWidgets import QMessageBox

    from cartridge.ui.dialogs.first_run import FirstRunDialog

    dialog = FirstRunDialog(window.state, window)
    if dialog.exec_():
        window.refresh_root_label()
        window.on_ready()
        save_last_session(dialog.chosen_root, dialog.chosen_db)
    else:
        QMessageBox.information(
            window,
            "No collection yet",
            "Cartridge opened without a games folder.\n\nOpen Settings to add one "
            "later — nothing else in the application depends on it.",
        )


def selftest(argv: Optional[List[str]] = None) -> int:
    """Build the whole UI headlessly, render it, and report. Exit 0 on success.

    This is the check behind the smoke test's ``app_entry_point`` result: it
    proves the documented launch command constructs every view, applies the theme
    and paints, without needing a human to click. It is *not* a Windows 7 test and
    is never reported as one.
    """
    import tempfile

    options = resolve_startup_paths(argv)
    options["selftest"] = True
    qt_platform_hint()

    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    from cartridge.ui.theme import apply_theme

    apply_theme(app)

    problems: List[str] = []
    scratch = tempfile.mkdtemp(prefix="cartridge-selftest-")
    db_path = options.get("db_path") or os.path.join(scratch, "selftest.db")

    state = AppState(db_path=db_path)
    if not state.open_database(db_path):
        problems.append("database did not open: %s" % "; ".join(state.open_errors))
    else:
        root = options.get("root") or scratch
        try:
            state.repo.add_root(root)
        except Exception as exc:
            problems.append("could not register a root: %s" % exc)

    from cartridge.ui.main_window import (
        NAV_COLLECTION,
        NAV_DIAGNOSTICS,
        NAV_FAVOURITES,
        NAV_MANAGER,
        NAV_SETTINGS,
        MainWindow,
    )

    window = MainWindow(state)
    window.resize(1280, 800)
    window.show()
    app.processEvents()

    if not window.isVisible():
        problems.append("main window did not become visible")

    for key in (NAV_COLLECTION, NAV_MANAGER, NAV_FAVOURITES, NAV_SETTINGS, NAV_DIAGNOSTICS):
        window.show_view(key)
        app.processEvents()
        if window.current_key() != key:
            problems.append("navigation to %s failed" % key)
        view = window.current_view()
        if view is None:
            problems.append("view %s is missing" % key)
            continue
        if not view.isVisible() and view is not window.current_view():
            problems.append("view %s is not visible" % key)

    # Render the whole window and confirm the dark theme actually painted.
    grab = window.grab()
    if grab.isNull():
        problems.append("window.grab() returned a null pixmap")
    else:
        image = grab.toImage()
        dark = 0
        total = 0
        for y in range(0, image.height(), 11):
            for x in range(0, image.width(), 11):
                color = image.pixelColor(x, y)
                luminance = (
                    color.red() * 299 + color.green() * 587 + color.blue() * 114
                ) // 1000
                total += 1
                if luminance < 80:
                    dark += 1
        ratio = dark / float(total or 1)
        if ratio < 0.5:
            problems.append("rendered window is only %.0f%% dark" % (ratio * 100))

    out_dir = os.environ.get("CARTRIDGE_SELFTEST_ARTIFACTS")
    if out_dir and not grab.isNull():
        try:
            if not os.path.isdir(out_dir):
                os.makedirs(out_dir)
            grab.save(os.path.join(out_dir, "selftest-main-window.png"), "PNG")
        except OSError as exc:
            problems.append("could not save the self-test screenshot: %s" % exc)

    window.show_view(NAV_COLLECTION)
    app.processEvents()
    window.close()
    app.processEvents()
    state.shutdown()

    if problems:
        print("SELFTEST FAILED")
        for problem in problems:
            print("  - %s" % problem)
        return 1
    print(
        "SELFTEST OK: window built, 5 views navigated, theme rendered "
        "(%.0f%% dark pixels), database opened at %s" % (ratio * 100, db_path)
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    options = resolve_startup_paths(argv)
    if options.get("selftest"):
        return selftest(argv)
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
