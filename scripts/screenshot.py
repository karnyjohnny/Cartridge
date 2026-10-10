#!/usr/bin/env python
"""Capture real screenshots of the running application.

These are genuine renders of the live PyQt5 UI, not mock-ups. The distinction
matters because brief section 20 forbids passing the ``ui-reference.html``
concept off as a screenshot of the finished product: every image this script
writes was produced by ``QWidget.grab()`` on a window the application actually
built and painted.

On a headless machine Qt's ``offscreen`` platform plugin is used, which renders
through the same code path as a real desktop session (same palette, same
stylesheet, same layout, same font metrics) but without a display server. The
output filename records which platform plugin was in use, and the accompanying
``screenshots.json`` records the host, so nobody can mistake a sandbox render for
a Windows 7 desktop capture.

    python scripts/screenshot.py --db build/demo/.cartridge/demo-collection.db
    python scripts/screenshot.py --db ... --view manager --out docs/img
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from cartridge import __version__                          # noqa: E402
from cartridge.app import build_state, qt_platform_hint     # noqa: E402
from cartridge.app_state import AppState                    # noqa: E402
from cartridge.ui.main_window import (                      # noqa: E402
    NAV_COLLECTION,
    NAV_DIAGNOSTICS,
    NAV_FAVOURITES,
    NAV_MANAGER,
    NAV_SETTINGS,
    MainWindow,
)

VIEW_KEYS = {
    "collection": NAV_COLLECTION,
    "manager": NAV_MANAGER,
    "favourites": NAV_FAVOURITES,
    "settings": NAV_SETTINGS,
    "diagnostics": NAV_DIAGNOSTICS,
}


def capture(options, out_dir: str, width: int, height: int) -> int:
    from PyQt5.QtWidgets import QApplication

    from cartridge.ui.theme import apply_theme

    plugin = qt_platform_hint() or os.environ.get("QT_QPA_PLATFORM") or "native"
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Cartridge")
    apply_theme(app)

    state = build_state({"db_path": options.db, "root": options.root, "offline": True})
    if options.db and not state.ready:
        print("could not open %s: %s" % (options.db, "; ".join(state.open_errors)))
        return 1

    window = MainWindow(state)
    window.resize(width, height)
    window.show()
    app.processEvents()
    window.on_ready()
    app.processEvents()

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    wanted = options.views.split(",") if options.views else list(VIEW_KEYS)
    records = []
    for name in wanted:
        key = VIEW_KEYS.get(name.strip())
        if key is None:
            print("unknown view: %s" % name)
            continue
        window.show_view(key)
        # Give the view a moment to run any deferred load, then let layout and
        # painting settle before grabbing.
        for _ in range(6):
            app.processEvents()
            time.sleep(0.02)
        view = window.current_view()
        refresh = getattr(view, "refresh", None)
        if callable(refresh):
            refresh()
        for _ in range(6):
            app.processEvents()
            time.sleep(0.02)

        filename = "cartridge-%s-%dx%d-%s.png" % (name.strip(), width, height, plugin)
        path = os.path.join(out_dir, filename)
        pixmap = window.grab()
        if pixmap.isNull():
            print("grab failed for %s" % name)
            continue
        pixmap.save(path, "PNG")
        records.append(
            {
                "view": name.strip(),
                "file": os.path.relpath(path, REPO_ROOT),
                "size": [pixmap.width(), pixmap.height()],
                "qt_platform_plugin": plugin,
                "host": platform.platform(),
                "python": sys.version.split()[0],
                "app_version": __version__,
                "database": state.db_path,
                "games_in_database": (
                    state.repo.stats()["games"] if state.ready else 0
                ),
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "note": (
                    "Rendered with Qt's %s plugin on %s. This is a real render of "
                    "the running application, not a mock-up, but it is NOT a "
                    "Windows 7 desktop capture."
                    % (plugin, platform.system())
                ),
            }
        )
        print("wrote %s" % os.path.relpath(path, REPO_ROOT))

    with open(os.path.join(out_dir, "screenshots.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "generated_by": "scripts/screenshot.py",
                "qt_platform_plugin": plugin,
                "records": records,
            },
            handle,
            indent=2,
        )

    window.close()
    app.processEvents()
    state.shutdown()
    return 0 if records else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Capture real screenshots of the app")
    parser.add_argument("--db", help="collection database to open")
    parser.add_argument("--root", help="games root folder")
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "artifacts", "screenshots"))
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument(
        "--views", default="", help="comma separated subset of: %s" % ",".join(VIEW_KEYS)
    )
    options = parser.parse_args(argv)
    return capture(options, options.out, options.width, options.height)


if __name__ == "__main__":
    sys.exit(main())
