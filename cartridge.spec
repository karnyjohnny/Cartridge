# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Cartridge (Windows 7 SP1 x64 / Python 3.8 target).

Read packaging/BUILD_WINDOWS7.md before using this. Two things matter more than
the spec file itself:

1. **Build on the oldest Windows you must support**, or at least verify the result
   there. A frozen exe built on Windows 10/11 can silently depend on newer
   Universal CRT behaviour; building on the Dell-class machine (or a Win7 VM with
   the same Python) is the only way to know.
2. **PyInstaller must be the last 3.8-compatible release.** Newer PyInstaller
   bootloaders are compiled with newer MSVC and may refuse to start on Win7. Pin
   it in requirements-dev.txt after checking its wheel metadata, and re-run
   docs/WINDOWS7_LIVE_TEST.md §4 with the frozen build.

The bundle is a *folder* (onedir), not a single exe: on a mechanical disk a onedir
start is faster and a broken update never leaves an unbootable single file.
"""

block_cipher = None

# Modules PyInstaller's static analysis routinely misses for PyQt5 on Windows.
hiddenimports = [
    "PyQt5",
    "PyQt5.QtCore",
    "PyQt5.QtGui",
    "PyQt5.QtWidgets",
    "PyQt5.sip",
    "httpx",
    "httpcore",
    "httpcore._backends",
    "httpcore._backends.sync",
    "anyio",
    "anyio._backends",
    "anyio._backends._asyncio",
    "h11",
    "certifi",
    "idna",
    "sniffio",
    "sqlite3",
]

# Deliberately excluded. Every one of these would add megabytes or a Visual
# Studio dependency to a machine that has neither to spare.
excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "scipy",
    "pandas",
    "PIL",
    "PyQt6",
    "PySide6",
    "pytest",
    "vermin",
    "setuptools",
    "pip",
]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    # The demo generator, the live-API harness and the test fixtures must never
    # ship: the fixtures are sanitized captures, but there is no reason for a
    # release artifact to carry them.
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Cartridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-compressed binaries trip some AV heuristics on Win7
    console=False,      # a GUI app; diagnostics go to the Diagnostics view, not a console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Cartridge",
)
