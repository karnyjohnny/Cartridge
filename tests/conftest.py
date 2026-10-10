"""Shared pytest fixtures.

The suite is offline and credential-free by design (brief section 16): no test
here touches the network, and opt-in live tests live in tests/test_live_providers.py
and skip unless CARTRIDGE_LIVE=1 plus real credentials are present.
"""

from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Never let a stray environment credential leak into the default suite. The
# opt-in live module is the single exception: it only runs when CARTRIDGE_LIVE is
# explicitly set, and it needs the real values from the environment.
if str(os.environ.get("CARTRIDGE_LIVE", "")).strip().lower() not in ("1", "true", "yes", "on"):
    os.environ.pop("CARTRIDGE_IGDB_CLIENT_ID", None)
    os.environ.pop("CARTRIDGE_IGDB_CLIENT_SECRET", None)
    os.environ.pop("CARTRIDGE_RAWG_API_KEY", None)


def pytest_configure(config):
    """Make Qt headless-safe for any test that needs widgets."""
    if not sys.platform.startswith("win") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """A single QApplication for the whole session (Qt allows only one)."""
    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([sys.argv[0], "-platform", os.environ.get("QT_QPA_PLATFORM", "offscreen")])
    yield app


@pytest.fixture
def fake_root(tmp_path):
    """A synthetic game root that mimics ``E:\\Gry`` on Windows.

    Layout:
        root/
            Wiedzmin 2/            <- a normal installed game (exe + gog info)
            Some ISO Game/         <- a disc image folder
            _cartridge/assets/     <- app-managed, must be ignored by discovery
            empty folder/
            Nested/Inner Game/     <- not an immediate child; discovery depth test
    """
    root = tmp_path / "Gry"
    root.mkdir()

    game_a = root / "Wiedzmin 2"
    game_a.mkdir()
    (game_a / "witcher2.exe").write_bytes(b"MZ\x90\x00fake")
    (game_a / "goggame-1234.info").write_text('{"name": "The Witcher 2"}', encoding="utf-8")
    (game_a / "README.txt").write_text("manual", encoding="utf-8")

    game_b = root / "Some ISO Game"
    game_b.mkdir()
    (game_b / "game.iso").write_bytes(b"\x00" * 64)

    managed = root / "_cartridge" / "assets"
    managed.mkdir(parents=True)
    (managed / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

    (root / "empty folder").mkdir()

    nested = root / "Nested" / "Inner Game"
    nested.mkdir(parents=True)
    (nested / "inner.exe").write_bytes(b"MZfake")

    return root


@pytest.fixture
def png_factory():
    """Return a callable that writes a valid PNG using only the stdlib."""
    from tests._png import write_png

    return write_png
