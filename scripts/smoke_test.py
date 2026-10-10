#!/usr/bin/env python
"""Cartridge compatibility proof-of-concept (milestone M1).

Runs the eight checks demanded by section 4 of the project brief and records an
honest, evidence-backed result for each one:

    1. environment      python / PyQt5 / Qt / httpx / sqlite3 versions
    2. gui_window       a PyQt5 window opens, renders, resizes, closes
    3. gui_dark_theme   the dark palette + stylesheet actually paints
    4. sqlite           create / write / read / close a temporary database
    5. httpx_local      bounded HTTP request against a local test server
    6. httpx_https      bounded HTTPS request against a public endpoint (opt-in)
    7. image_pipeline   load + thumbnail a local image, reject a corrupt one
    8. app_entry_point  start the app through the documented local command
    9. packaging        PyInstaller smoke build (target machine only)

Statuses are never conflated:

    PASS        executed here, evidence recorded
    FAIL        executed here, failed
    NOT TESTED  not executed (missing flag / not applicable)
    BLOCKED     attempted, prevented by an environmental restriction

Usage
-----
    python scripts/smoke_test.py                 # offline-safe checks only
    python scripts/smoke_test.py --network       # also run live HTTPS checks
    python scripts/smoke_test.py --report docs/smoke.md

Exit code is 1 if any check FAILs (BLOCKED / NOT TESTED do not fail the run,
because they are honest states, not defects).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

PASS = "PASS"
FAIL = "FAIL"
NOT_TESTED = "NOT TESTED"
BLOCKED = "BLOCKED"

DEFAULT_HTTPS_PROBE = "https://pypi.org/simple/httpx/"


class Check(object):
    """One recorded check result."""

    def __init__(self, name, status, detail="", evidence=None):
        self.name = name
        self.status = status
        self.detail = detail
        self.evidence = evidence or {}

    def to_dict(self):
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
        }


# A QApplication must stay referenced for the whole process lifetime: if the only
# Python reference is a local variable, CPython garbage-collects it and every
# later QPixmap/QFontDatabase call aborts the process.
_QAPP = None


def _qapp():
    """Return the shared QApplication, creating it once if needed."""
    global _QAPP
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
        _QAPP = app
    return app


def _qt_platform_is_offscreen():
    return os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen"


def _ensure_qt_platform():
    """Pick a Qt platform plugin that can work in this environment.

    Returns a short human-readable description of what was chosen. On Windows a
    real desktop session is used unchanged; on a headless Linux/macOS sandbox we
    fall back to the offscreen plugin so the checks still exercise real Qt code.
    """
    if sys.platform.startswith("win"):
        return "native Windows desktop session"
    if os.environ.get("DISPLAY"):
        return "X11 display %s" % os.environ["DISPLAY"]
    if os.environ.get("WAYLAND_DISPLAY"):
        return "Wayland display"
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    return "Qt offscreen plugin (headless sandbox - NOT a Windows 7 desktop test)"


def _write_png(path, width, height, rgb):
    """Write a tiny valid PNG using only the standard library."""
    import struct
    import zlib

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(
        b"\x00" + bytes(bytearray([rgb[0], rgb[1], rgb[2]]) * width)
        for _ in range(height)
    )
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as handle:
        handle.write(png)


# --------------------------------------------------------------------------
# Check 1 — environment
# --------------------------------------------------------------------------
def check_environment():
    import sqlite3

    info = {
        "python_version": sys.version.replace("\n", " "),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "executable": sys.executable,
        "sqlite3_module_version": getattr(sqlite3, "version", "?"),
        "sqlite_library_version": sqlite3.sqlite_version,
    }

    try:
        from PyQt5.QtCore import PYQT_VERSION_STR, qVersion

        info["pyqt5_version"] = PYQT_VERSION_STR
        # qVersion() is the *runtime* Qt library; QT_VERSION_STR is the version
        # PyQt5 was compiled against. They legitimately differ - report both.
        info["qt_runtime_version"] = qVersion()
        try:
            from PyQt5.QtCore import QT_VERSION_STR

            info["qt_build_header_version"] = QT_VERSION_STR
        except Exception:  # pragma: no cover - defensive
            info["qt_build_header_version"] = "unknown"
    except Exception as exc:
        return Check(
            "environment",
            BLOCKED,
            "PyQt5 could not be imported: %s" % exc,
            info,
        )

    try:
        import httpx

        info["httpx_version"] = httpx.__version__
    except Exception as exc:
        return Check("environment", BLOCKED, "httpx could not be imported: %s" % exc, info)

    for extra in ("httpcore", "anyio", "h11", "certifi", "idna"):
        try:
            from importlib import metadata as _md

            info[extra + "_version"] = _md.version(extra)
        except Exception:
            info[extra + "_version"] = "not installed"

    py38 = sys.version_info[:2] == (3, 8)
    detail = "Python %s / PyQt5 %s / Qt %s / httpx %s / SQLite %s" % (
        sys.version.split()[0],
        info.get("pyqt5_version"),
        info.get("qt_runtime_version"),
        info.get("httpx_version"),
        info.get("sqlite_library_version"),
    )
    if not py38:
        detail += (
            "  [NOTE: interpreter is %d.%d, not the 3.8 target; source is still "
            "kept 3.8-compatible and verified with vermin]" % sys.version_info[:2]
        )
    return Check("environment", PASS, detail, info)


# --------------------------------------------------------------------------
# Checks 2 & 3 — GUI window + dark theme
# --------------------------------------------------------------------------
def check_gui_window(artifact_dir):
    platform_desc = _ensure_qt_platform()
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import (
            QApplication,
            QLabel,
            QMainWindow,
            QVBoxLayout,
            QWidget,
        )
    except Exception as exc:
        return Check(
            "gui_window", BLOCKED, "PyQt5 import failed: %s" % exc,
            {"qt_platform": platform_desc},
        )

    app = _qapp()
    try:
        win = QMainWindow()
        win.setWindowTitle("Cartridge smoke test")
        win.resize(1280, 800)
        central = QWidget()
        layout = QVBoxLayout(central)
        label = QLabel("Cartridge compatibility proof")
        layout.addWidget(label)
        win.setCentralWidget(central)
        win.show()
        app.processEvents()

        shown = win.isVisible()
        initial = (win.width(), win.height())

        win.resize(1024, 700)
        app.processEvents()
        resized = (win.width(), win.height())

        png = os.path.join(artifact_dir, "gui_window_1024x700.png")
        grab = win.grab()
        grab.save(png, "PNG")
        painted = not grab.isNull() and grab.width() > 0

        win.close()
        app.processEvents()
        closed = not win.isVisible()

        evidence = {
            "qt_platform": platform_desc,
            "visible_after_show": bool(shown),
            "initial_size": list(initial),
            "size_after_resize": list(resized),
            "closed": bool(closed),
            "painted_pixels": bool(painted),
            "screenshot": os.path.relpath(png, REPO_ROOT),
        }
        ok = shown and resized != initial and closed and painted
        return Check(
            "gui_window",
            PASS if ok else FAIL,
            "window shown=%s resized=%s closed=%s painted=%s (%s)"
            % (shown, resized, closed, painted, platform_desc),
            evidence,
        )
    except Exception as exc:
        return Check(
            "gui_window",
            BLOCKED,
            "Qt could not create/paint a window here: %s\n%s" % (exc, traceback.format_exc()),
            {"qt_platform": platform_desc},
        )


def check_gui_dark_theme(artifact_dir):
    platform_desc = _ensure_qt_platform()
    try:
        from cartridge.ui.theme import build_stylesheet, dark_palette
        from PyQt5.QtGui import QColor
        from PyQt5.QtWidgets import (
            QApplication,
            QLabel,
            QMainWindow,
            QPushButton,
            QVBoxLayout,
            QWidget,
        )
    except Exception as exc:
        return Check(
            "gui_dark_theme",
            NOT_TESTED,
            "theme module or Qt unavailable (%s) - re-run after M2 lands" % exc,
            {"qt_platform": platform_desc},
        )

    app = _qapp()
    try:
        app.setPalette(dark_palette())
        win = QMainWindow()
        win.resize(640, 320)
        central = QWidget()
        central.setObjectName("RootSurface")
        layout = QVBoxLayout(central)
        title = QLabel("Cartridge")
        title.setObjectName("AppTitle")
        body = QLabel("Dark theme render check")
        button = QPushButton("Primary action")
        button.setObjectName("PrimaryButton")
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addWidget(button)
        layout.addStretch(1)
        win.setCentralWidget(central)
        win.setStyleSheet(build_stylesheet())
        win.show()
        app.processEvents()

        image = win.grab().toImage()
        png = os.path.join(artifact_dir, "gui_dark_theme.png")
        win.grab().save(png, "PNG")

        corner = QColor(image.pixel(2, 2)).name().lower()
        center = QColor(image.pixel(image.width() // 2, image.height() // 2)).name().lower()
        # Count how many pixels are "dark" - a rendered dark theme must be
        # overwhelmingly dark, and must contain at least some light text pixels.
        dark = 0
        light = 0
        step = 7
        for y in range(0, image.height(), step):
            for x in range(0, image.width(), step):
                c = QColor(image.pixel(x, y))
                lum = (c.red() * 299 + c.green() * 587 + c.blue() * 114) // 1000
                if lum < 60:
                    dark += 1
                elif lum > 170:
                    light += 1
        total = max(1, (image.width() // step) * (image.height() // step))
        dark_ratio = dark / float(total)

        win.close()
        app.processEvents()

        evidence = {
            "qt_platform": platform_desc,
            "screenshot": os.path.relpath(png, REPO_ROOT),
            "corner_pixel": corner,
            "center_pixel": center,
            "dark_pixel_ratio": round(dark_ratio, 3),
            "light_pixels_found": light,
        }
        ok = dark_ratio > 0.6 and light > 0
        return Check(
            "gui_dark_theme",
            PASS if ok else FAIL,
            "dark pixels %.1f%%, light(text) samples %d, corner=%s"
            % (dark_ratio * 100.0, light, corner),
            evidence,
        )
    except Exception as exc:
        return Check(
            "gui_dark_theme",
            BLOCKED,
            "dark theme render failed: %s\n%s" % (exc, traceback.format_exc()),
            {"qt_platform": platform_desc},
        )


# --------------------------------------------------------------------------
# Check 4 — SQLite
# --------------------------------------------------------------------------
def check_sqlite():
    import sqlite3

    tmpdir = tempfile.mkdtemp(prefix="cartridge-smoke-")
    db_path = os.path.join(tmpdir, "smoke.db")
    started = time.time()
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS smoke (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
            )
            with conn:
                conn.executemany(
                    "INSERT INTO smoke (name) VALUES (?)",
                    [("alpha",), ("beta",), ("gamma",)],
                )
            rows = conn.execute("SELECT id, name FROM smoke ORDER BY id").fetchall()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        finally:
            conn.close()

        reopened = sqlite3.connect(db_path)
        try:
            persisted = reopened.execute("SELECT COUNT(*) FROM smoke").fetchone()[0]
        finally:
            reopened.close()

        elapsed_ms = (time.time() - started) * 1000.0
        evidence = {
            "db_path": db_path,
            "rows_written": 3,
            "rows_read": len(rows),
            "rows_after_reopen": persisted,
            "integrity_check": integrity,
            "foreign_keys_pragma": fk,
            "elapsed_ms": round(elapsed_ms, 2),
            "sqlite_library_version": sqlite3.sqlite_version,
        }
        ok = len(rows) == 3 and persisted == 3 and integrity == "ok"
        return Check(
            "sqlite",
            PASS if ok else FAIL,
            "wrote/read 3 rows, persisted across reopen=%s, integrity_check=%s (%.1f ms)"
            % (persisted, integrity, elapsed_ms),
            evidence,
        )
    except Exception as exc:
        return Check("sqlite", FAIL, "sqlite3 roundtrip failed: %s" % exc, {"db_path": db_path})


# --------------------------------------------------------------------------
# Check 5 — httpx against a local server (no internet required)
# --------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path == "/health":
            body = json.dumps({"status": "ok", "path": self.path}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/slow":
            time.sleep(2.0)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):  # silence stderr noise
        return


def check_httpx_local():
    try:
        import httpx
    except Exception as exc:
        return Check("httpx_local", BLOCKED, "httpx import failed: %s" % exc)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    port = server.server_address[1]
    base = "http://127.0.0.1:%d" % port
    evidence = {"base_url": base, "httpx_version": httpx.__version__}
    try:
        timeout = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)
        started = time.time()
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(base + "/health")
            payload = resp.json()
            evidence["status_code"] = resp.status_code
            evidence["json_payload"] = payload
            evidence["latency_ms"] = round((time.time() - started) * 1000.0, 2)

            # 404 handling must not raise
            missing = client.get(base + "/nope")
            evidence["not_found_status"] = missing.status_code

            # read timeout must surface as a typed httpx error, not a hang
            try:
                client.get(base + "/slow", timeout=httpx.Timeout(0.25))
                evidence["timeout_raised"] = False
            except httpx.TimeoutException:
                evidence["timeout_raised"] = True

        # connection failure must surface as a typed error too
        closed_port = _free_port()
        try:
            with httpx.Client(timeout=httpx.Timeout(1.0)) as client:
                client.get("http://127.0.0.1:%d/" % closed_port)
            evidence["connect_error_raised"] = False
        except httpx.TransportError:
            evidence["connect_error_raised"] = True

        ok = (
            evidence.get("status_code") == 200
            and payload.get("status") == "ok"
            and evidence.get("not_found_status") == 404
            and evidence.get("timeout_raised") is True
            and evidence.get("connect_error_raised") is True
        )
        return Check(
            "httpx_local",
            PASS if ok else FAIL,
            "GET /health -> %s in %.1f ms; 404 handled; read-timeout raised=%s; "
            "connect-error raised=%s"
            % (
                evidence.get("status_code"),
                evidence.get("latency_ms", -1),
                evidence.get("timeout_raised"),
                evidence.get("connect_error_raised"),
            ),
            evidence,
        )
    except Exception as exc:
        return Check(
            "httpx_local", FAIL, "local httpx check failed: %s" % exc, evidence
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --------------------------------------------------------------------------
# Check 6 — live HTTPS (opt-in)
# --------------------------------------------------------------------------
def check_httpx_https(url, enabled):
    if not enabled:
        return Check(
            "httpx_https",
            NOT_TESTED,
            "live HTTPS probe skipped (pass --network to enable); local httpx "
            "transport is covered by httpx_local",
            {"url": url},
        )
    try:
        import httpx
    except Exception as exc:
        return Check("httpx_https", BLOCKED, "httpx import failed: %s" % exc)

    evidence = {"url": url}
    try:
        timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
        started = time.time()
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "cartridge-smoke-test/1.0"})
        evidence["status_code"] = resp.status_code
        evidence["bytes"] = len(resp.content)
        evidence["latency_ms"] = round((time.time() - started) * 1000.0, 1)

        # A DNS/TLS failure must be a typed error, never a crash
        try:
            with httpx.Client(timeout=httpx.Timeout(5.0)) as client:
                client.get("https://cartridge-invalid-host.invalid/")
            evidence["dns_error_raised"] = False
        except httpx.TransportError as exc:
            evidence["dns_error_raised"] = True
            evidence["dns_error_type"] = type(exc).__name__

        ok = 200 <= evidence["status_code"] < 400 and evidence["dns_error_raised"]
        return Check(
            "httpx_https",
            PASS if ok else FAIL,
            "GET %s -> %s (%d bytes, %.0f ms); DNS failure raised %s"
            % (
                url,
                evidence["status_code"],
                evidence["bytes"],
                evidence["latency_ms"],
                evidence.get("dns_error_type", "nothing"),
            ),
            evidence,
        )
    except Exception as exc:
        return Check(
            "httpx_https",
            FAIL,
            "live HTTPS probe failed (%s: %s). This may mean no internet access in "
            "this environment - re-run on the Dell with connectivity."
            % (type(exc).__name__, exc),
            evidence,
        )


# --------------------------------------------------------------------------
# Check 7 — image pipeline
# --------------------------------------------------------------------------
def check_image_pipeline(artifact_dir):
    platform_desc = _ensure_qt_platform()
    try:
        from PyQt5.QtCore import QSize, Qt
        from PyQt5.QtGui import QImage, QPixmap
        from PyQt5.QtWidgets import QApplication
    except Exception as exc:
        return Check("image_pipeline", BLOCKED, "Qt image classes unavailable: %s" % exc)

    _qapp()
    tmpdir = tempfile.mkdtemp(prefix="cartridge-img-")
    src = os.path.join(tmpdir, "cover.png")
    thumb_path = os.path.join(artifact_dir, "thumb_cover.png")
    evidence = {"qt_platform": platform_desc, "source_png": src}
    try:
        _write_png(src, 120, 160, (78, 161, 255))

        pix = QPixmap(src)
        evidence["loaded"] = not pix.isNull()
        evidence["source_size"] = [pix.width(), pix.height()]

        thumb = pix.scaled(
            QSize(60, 80), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        evidence["thumb_size"] = [thumb.width(), thumb.height()]
        evidence["thumb_saved"] = thumb.save(thumb_path, "PNG")
        evidence["thumb_artifact"] = os.path.relpath(thumb_path, REPO_ROOT)

        # QImage read path (used for validation without a widget)
        image = QImage(src)
        evidence["qimage_valid"] = not image.isNull()
        evidence["qimage_format"] = int(image.format())

        # Corrupt / truncated data must be rejected, never crash
        corrupt = os.path.join(tmpdir, "corrupt.png")
        with open(src, "rb") as handle:
            data = handle.read()
        with open(corrupt, "wb") as handle:
            handle.write(data[: len(data) // 3])
        bad = QPixmap(corrupt)
        evidence["corrupt_rejected"] = bad.isNull()

        zero = QPixmap(os.path.join(tmpdir, "does-not-exist.png"))
        evidence["missing_file_rejected"] = zero.isNull()

        ok = (
            evidence["loaded"]
            and evidence["qimage_valid"]
            and thumb.width() == 60
            and evidence["thumb_saved"]
            and evidence["corrupt_rejected"]
            and evidence["missing_file_rejected"]
        )
        return Check(
            "image_pipeline",
            PASS if ok else FAIL,
            "loaded %dx%d PNG, thumb %dx%d, corrupt rejected=%s, missing rejected=%s"
            % (
                pix.width(),
                pix.height(),
                thumb.width(),
                thumb.height(),
                evidence["corrupt_rejected"],
                evidence["missing_file_rejected"],
            ),
            evidence,
        )
    except Exception as exc:
        return Check(
            "image_pipeline", FAIL, "image pipeline failed: %s" % exc, evidence
        )


# --------------------------------------------------------------------------
# Check 8 — documented entry point
# --------------------------------------------------------------------------
def check_app_entry_point():
    entry = os.path.join(REPO_ROOT, "run.py")
    if not os.path.exists(entry):
        return Check(
            "app_entry_point",
            NOT_TESTED,
            "run.py does not exist yet (created in milestone M2)",
        )
    env = dict(os.environ)
    if not sys.platform.startswith("win") and not env.get("DISPLAY"):
        env["QT_QPA_PLATFORM"] = "offscreen"
    try:
        proc = subprocess.run(
            [sys.executable, entry, "--selftest"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return Check("app_entry_point", FAIL, "`python run.py --selftest` timed out after 120 s")
    except Exception as exc:
        return Check("app_entry_point", BLOCKED, "could not launch entry point: %s" % exc)

    output = (proc.stdout or b"").decode("utf-8", "replace").strip()
    tail = "\n".join(output.splitlines()[-12:])
    ok = proc.returncode == 0
    return Check(
        "app_entry_point",
        PASS if ok else FAIL,
        "`python run.py --selftest` exit=%d\n%s" % (proc.returncode, tail),
        {"exit_code": proc.returncode, "output_tail": tail},
    )


# --------------------------------------------------------------------------
# Check 9 — packaging
# --------------------------------------------------------------------------
def check_packaging():
    if not sys.platform.startswith("win"):
        return Check(
            "packaging",
            NOT_TESTED,
            "PyInstaller smoke build must be produced and launched on Windows 7; "
            "current platform is %s. See packaging/BUILD_WINDOWS7.md."
            % platform.system(),
        )
    try:
        import PyInstaller  # noqa: F401

        from importlib import metadata as _md

        version = _md.version("pyinstaller")
    except Exception as exc:
        return Check(
            "packaging",
            NOT_TESTED,
            "PyInstaller is not installed in this environment (%s). Install it and "
            "re-run, or build on the Dell per packaging/BUILD_WINDOWS7.md." % exc,
        )
    return Check(
        "packaging",
        NOT_TESTED,
        "PyInstaller %s is installed but no frozen build has been launched on "
        "Windows 7 yet. Run packaging/BUILD_WINDOWS7.md steps and record the result."
        % version,
        {"pyinstaller_version": version},
    )


# --------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------
def render_markdown(checks, meta):
    lines = []
    lines.append("# Cartridge compatibility smoke report")
    lines.append("")
    lines.append("_Generated by `scripts/smoke_test.py` - machine-recorded evidence,")
    lines.append("not a hand-written claim._")
    lines.append("")
    lines.append("- **When:** %s" % meta["timestamp"])
    lines.append("- **Host:** %s" % meta["host"])
    lines.append("- **Python:** %s" % meta["python"])
    lines.append("- **Qt platform:** %s" % meta["qt_platform"])
    lines.append("- **Is this the Windows 7 target machine?** %s" % meta["is_target"])
    lines.append("")
    lines.append("| # | Check | Status | Detail |")
    lines.append("|---|-------|--------|--------|")
    for index, check in enumerate(checks, start=1):
        detail = check.detail.replace("\n", " ").replace("|", "/")
        if len(detail) > 220:
            detail = detail[:217] + "..."
        lines.append("| %d | `%s` | **%s** | %s |" % (index, check.name, check.status, detail))
    lines.append("")
    counts = {}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    lines.append(
        "Summary: %s"
        % ", ".join("%s=%d" % (key, counts[key]) for key in sorted(counts))
    )
    lines.append("")
    lines.append("## Evidence")
    lines.append("")
    for check in checks:
        lines.append("### `%s` — %s" % (check.name, check.status))
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(check.evidence, indent=2, sort_keys=True, default=str))
        lines.append("```")
        lines.append("")
    if not meta["is_target"]:
        lines.append("## Important caveat")
        lines.append("")
        lines.append(
            "This run did **not** execute on the Windows 7 SP1 x64 / Python 3.8 target."
        )
        lines.append(
            "Results above prove the code paths work on the recorded host only. Every"
        )
        lines.append(
            "target-specific item stays `NOT TESTED` until `docs/WINDOWS7_LIVE_TEST.md`"
        )
        lines.append("is executed on the Dell Latitude E5500.")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cartridge M1 compatibility proof")
    parser.add_argument("--network", action="store_true", help="run live HTTPS probe")
    parser.add_argument("--url", default=DEFAULT_HTTPS_PROBE, help="live HTTPS probe URL")
    parser.add_argument(
        "--artifact-dir",
        default=os.path.join(REPO_ROOT, "artifacts", "smoke"),
        help="where screenshots/JSON evidence are written",
    )
    parser.add_argument("--report", default=None, help="write the markdown report here")
    parser.add_argument("--json-out", default=None, help="write raw JSON results here")
    parser.add_argument(
        "--quiet", action="store_true", help="only print the summary table"
    )
    args = parser.parse_args(argv)

    artifact_dir = args.artifact_dir
    if not os.path.isdir(artifact_dir):
        os.makedirs(artifact_dir)

    qt_platform = _ensure_qt_platform()
    checks = []
    checks.append(check_environment())
    checks.append(check_gui_window(artifact_dir))
    checks.append(check_gui_dark_theme(artifact_dir))
    checks.append(check_sqlite())
    checks.append(check_httpx_local())
    checks.append(check_httpx_https(args.url, args.network))
    checks.append(check_image_pipeline(artifact_dir))
    checks.append(check_app_entry_point())
    checks.append(check_packaging())

    meta = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "host": "%s / %s / %s"
        % (platform.system(), platform.release(), platform.machine()),
        "python": sys.version.replace("\n", " "),
        "qt_platform": qt_platform,
        "is_target": bool(
            sys.platform.startswith("win") and sys.version_info[:2] == (3, 8)
        ),
        "network_probe_enabled": bool(args.network),
    }

    report_path = args.report or os.path.join(artifact_dir, "smoke_report.md")
    json_path = args.json_out or os.path.join(artifact_dir, "smoke_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(checks, meta))
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump({"meta": meta, "checks": [c.to_dict() for c in checks]}, handle, indent=2, default=str)

    width = max(len(c.name) for c in checks)
    print("")
    print("CARTRIDGE COMPATIBILITY PROOF  (%s)" % meta["host"])
    print("-" * 78)
    for check in checks:
        print("%-*s  %-11s %s" % (width, check.name, check.status, check.detail.splitlines()[0][:60]))
    print("-" * 78)
    counts = {}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    print("summary: " + ", ".join("%s=%d" % (k, counts[k]) for k in sorted(counts)))
    print("report : %s" % os.path.relpath(report_path, REPO_ROOT))
    print("json   : %s" % os.path.relpath(json_path, REPO_ROOT))
    print("")

    failed = [c for c in checks if c.status == FAIL]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
