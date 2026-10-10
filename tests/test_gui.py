"""GUI tests: build and drive the real widgets offscreen (marked ``gui``).

These are *not* a substitute for a Windows 7 session — they run on the
development host through Qt's offscreen platform. What they do prove is that the
UI code constructs, lays out, wires signals and updates from the database without
raising, and that user-visible state (filters, selection, badges, redaction) is
correct. Anything that needs a real desktop stays ``NOT TESTED`` in
``docs/WINDOWS7_LIVE_TEST.md``.
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.gui

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QColor  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from cartridge.app_state import AppState  # noqa: E402
from cartridge.core import filtering  # noqa: E402
from cartridge.core.models import STATE_MANUAL  # noqa: E402
from cartridge.core.secret_store import SecretStore  # noqa: E402
from cartridge.db.repository import Repository  # noqa: E402
from cartridge.ui.theme import apply_theme  # noqa: E402
from cartridge.ui.main_window import (  # noqa: E402
    NAV_COLLECTION,
    NAV_DIAGNOSTICS,
    NAV_FAVOURITES,
    NAV_MANAGER,
    NAV_SETTINGS,
    MainWindow,
)


def spin(app, ms=60):
    """Pump the event loop so timers, queued signals and deferred deletes land."""
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


@pytest.fixture
def store_dir(tmp_path):
    return SecretStore(directory=str(tmp_path / "secrets"), preferred="memory")


@pytest.fixture
def seeded(tmp_path, store_dir):
    """A small real database with five games, artwork states and one missing folder."""
    db_path = str(tmp_path / "collection.db")
    state = AppState(db_path=db_path, secret_store=store_dir, windows=False)
    assert state.open_database(db_path)

    root = tmp_path / "Gry"
    root.mkdir()
    state.repo.add_root(str(root))

    games = []
    specs = [
        ("Alpha Game", 1998, 80.0, ["RPG"], ["GOG"], True),
        ("Beta Quest", 2005, 90.0, ["Action"], ["ISO"], False),
        ("Gamma Tales", None, None, ["RPG"], [], False),
        ("Delta Story", 2011, 60.0, ["Adventure"], ["Installer"], False),
        ("Epsilon Run", 2015, 70.0, ["Racing"], ["EXE"], False),
    ]
    for index, (title, year, rating, genres, types, fav) in enumerate(specs):
        folder = root / ("game%02d" % index)
        folder.mkdir()
        (folder / "game.exe").write_bytes(b"MZ")
        game, _created = state.repo.upsert_folder(str(folder))
        state.repo.apply_provider_metadata(
            game.id,
            {
                "title": title, "release_year": year, "rating": rating,
                "genres": genres, "developer": "Studio %d" % index,
                "provider": "igdb", "provider_id": str(9000 + index),
            },
        )
        state.repo.set_game_content_types(game.id, types)
        state.repo.set_favourite(game.id, fav)
        # upsert_folder returns the record as it was created (folder-derived
        # title); re-read so the test sees the metadata that was just applied.
        games.append(state.repo.get_game(game.id))

    # one record whose folder vanished
    gone = root / "gone-game"
    gone.mkdir()
    lost, _ = state.repo.upsert_folder(str(gone))
    state.repo.apply_provider_metadata(
        lost.id, {"title": "Lost Game", "provider": "igdb", "provider_id": "9100"}
    )
    gone.rmdir()
    state.repo.set_folder_state(lost.id, "missing")

    yield state, games
    state.shutdown()


@pytest.fixture
def window(qapp, seeded):
    state, games = seeded
    # The real entry point applies the theme before any window exists; the
    # fixture must do the same or the assertions would measure Qt's default
    # (light) palette instead of the application's.
    apply_theme(qapp)
    win = MainWindow(state)
    win.resize(1280, 800)
    win.show()
    spin(qapp)
    win.on_ready()
    spin(qapp)
    yield win, state, games
    win.close()
    spin(qapp)


# --------------------------------------------------------------------------
# shell
# --------------------------------------------------------------------------
def test_window_builds_and_paints_dark(window):
    win, state, games = window
    assert win.isVisible()
    image = win.grab().toImage()
    dark = 0
    total = 0
    for y in range(0, image.height(), 9):
        for x in range(0, image.width(), 9):
            color = image.pixelColor(x, y)
            luminance = (color.red() * 299 + color.green() * 587 + color.blue() * 114) // 1000
            total += 1
            if luminance < 80:
                dark += 1
    assert dark / float(total) > 0.85, "the shell must render the dark theme"


def test_navigation_switches_views(window, qapp):
    win, state, games = window
    for key, view_type in (
        (NAV_COLLECTION, "BrowserView"),
        (NAV_MANAGER, "ManagerView"),
        (NAV_FAVOURITES, "BrowserView"),
        (NAV_SETTINGS, "SettingsView"),
        (NAV_DIAGNOSTICS, "DiagnosticsView"),
    ):
        win.show_view(key)
        spin(qapp, 30)
        assert win.current_key() == key
        assert type(win.current_view()).__name__ == view_type


def test_title_bar_shows_the_root(window):
    win, state, games = window
    assert "Gry" in win.title_bar.root_label.text()


def test_nav_badges_report_attention_and_favourites(window, qapp):
    win, state, games = window
    win._update_all_badges()
    spin(qapp, 20)
    manager_text = win.nav.buttons[NAV_MANAGER].text()
    favourites_text = win.nav.buttons[NAV_FAVOURITES].text()
    assert "(1)" in favourites_text
    assert "(" in manager_text


# --------------------------------------------------------------------------
# browser
# --------------------------------------------------------------------------
def browser(window):
    win, state, games = window
    win.show_view(NAV_COLLECTION)
    return win.current_view()


def test_browser_lists_every_game(window, qapp):
    view = browser(window)
    spin(qapp, 40)
    assert len(view.grid.games()) == 6
    assert "6 games" in view.result_label.text()


def test_search_filters_the_grid(window, qapp):
    view = browser(window)
    spin(qapp, 40)
    view.sidebar.search.setText("beta")
    spin(qapp, 400)          # debounce + query
    titles = [game.effective_title() for game in view.grid.games()]
    assert titles == ["Beta Quest"]
    view.sidebar.search.setText("")
    spin(qapp, 400)
    assert len(view.grid.games()) == 6


def test_view_switch_to_favourites_filter(window, qapp):
    view = browser(window)
    spin(qapp, 40)
    view.sidebar.view_combo.setCurrentIndex(
        view.sidebar.view_combo.findData(filtering.VIEW_FAVOURITES)
    )
    spin(qapp, 300)
    assert [g.effective_title() for g in view.grid.games()] == ["Alpha Game"]


def test_facet_chips_are_built_from_the_database(window, qapp):
    view = browser(window)
    spin(qapp, 60)
    names = [chip.value for chip in view.sidebar.type_chips]
    assert "GOG" in names and "ISO" in names
    genre_names = [chip.value for chip in view.sidebar.genre_chips]
    assert "RPG" in genre_names


def test_clicking_a_facet_chip_filters(window, qapp):
    view = browser(window)
    spin(qapp, 60)
    chip = [c for c in view.sidebar.type_chips if c.value == "ISO"][0]
    chip.setChecked(True)
    spin(qapp, 400)
    assert [g.effective_title() for g in view.grid.games()] == ["Beta Quest"]
    chip.setChecked(False)
    spin(qapp, 400)
    assert len(view.grid.games()) == 6


def test_clear_filters_restores_everything(window, qapp):
    view = browser(window)
    spin(qapp, 40)
    view.sidebar.search.setText("gamma")
    spin(qapp, 400)
    assert len(view.grid.games()) == 1
    view._on_clear_filters()
    spin(qapp, 400)
    assert len(view.grid.games()) == 6


def test_selecting_a_card_populates_the_details_pane(window, qapp):
    view = browser(window)
    spin(qapp, 40)
    target = [g for g in view.grid.games() if g.effective_title() == "Beta Quest"][0]
    view.grid.select(target.id)
    view._on_selection_changed(target)
    spin(qapp, 30)
    assert view.details.title_label.text() == "Beta Quest"
    assert view.details.rating is not None


def test_details_pane_shows_missing_folder_state(window, qapp):
    view = browser(window)
    spin(qapp, 40)
    lost = [g for g in view.grid.games() if g.effective_title() == "Lost Game"]
    assert lost, "the vanished folder's record must still be browsable"
    view.grid.select(lost[0].id)
    view._on_selection_changed(lost[0])
    spin(qapp, 30)
    assert view.details.banner.isVisible()
    assert "no longer exists" in view.details.banner.text()
    assert view.details.open_button.isEnabled() is False


def test_favourite_toggle_through_the_details_pane(window, qapp):
    view = browser(window)
    spin(qapp, 40)
    target = [g for g in view.grid.games() if g.effective_title() == "Beta Quest"][0]
    view._on_favourite_id(target.id, True)
    spin(qapp, 300)
    assert view.state.repo.get_game(target.id).favourite is True


def test_list_mode_switch(window, qapp):
    view = browser(window)
    spin(qapp, 40)
    view.list_button.setChecked(True)
    spin(qapp, 60)
    assert view.grid.mode == "list"
    view.grid_button.setChecked(True)
    spin(qapp, 60)
    assert view.grid.mode == "grid"


def test_empty_state_when_nothing_matches(window, qapp):
    view = browser(window)
    spin(qapp, 40)
    view.sidebar.search.setText("zzzz-no-such-game")
    spin(qapp, 400)
    assert view.empty.isVisible()
    assert "Nothing matches" in view.empty.title_label.text()


# --------------------------------------------------------------------------
# manager
# --------------------------------------------------------------------------
def test_manager_lists_records_with_states(window, qapp):
    win, state, games = window
    win.show_view(NAV_MANAGER)
    view = win.current_view()
    spin(qapp, 80)
    assert view.table.rowCount() >= 6
    texts = []
    for row in range(view.table.rowCount()):
        texts.append(view.table.item(row, 2).text())
    assert "folder missing" in texts


def test_manager_unmatched_filter(window, qapp):
    win, state, games = window
    win.show_view(NAV_MANAGER)
    view = win.current_view()
    spin(qapp, 60)
    index = view.filter_combo.findData("unmatched")
    view.filter_combo.setCurrentIndex(index)
    spin(qapp, 60)
    assert view.table.rowCount() == 0, "every demo folder is catalogued"


def test_manager_rescan_creates_records_for_new_folders(window, qapp, tmp_path):
    win, state, games = window
    root = state.root_paths()[0]
    new_folder = os.path.join(root, "brand-new-game")
    os.makedirs(new_folder)
    with open(os.path.join(new_folder, "game.exe"), "wb") as handle:
        handle.write(b"MZ")

    win.show_view(NAV_MANAGER)
    view = win.current_view()
    spin(qapp, 60)
    view.start_rescan()
    deadline = time.time() + 10.0
    while time.time() < deadline and view._busy:
        spin(qapp, 50)
    spin(qapp, 100)

    found = state.repo.get_game_by_folder(new_folder)
    assert found is not None, "the rescan must have catalogued the new folder"
    assert found.metadata_state == "none"


def test_manager_summary_counts(window, qapp):
    win, state, games = window
    win.show_view(NAV_MANAGER)
    view = win.current_view()
    spin(qapp, 60)
    assert "missing folders" in view.summary_label.text()


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
def test_settings_masks_credential_fields(window):
    win, state, games = window
    win.show_view(NAV_SETTINGS)
    view = win.current_view()
    from PyQt5.QtWidgets import QLineEdit

    for field in (view.igdb_id, view.igdb_secret, view.rawg_key):
        assert field.echoMode() == QLineEdit.Password


def test_settings_saves_credentials_to_the_secret_store(window, qapp):
    win, state, games = window
    win.show_view(NAV_SETTINGS)
    view = win.current_view()
    view.igdb_id.setText("client-id-value-0001")
    view.igdb_secret.setText("client-secret-value-0001")
    view.rawg_key.setText("0" * 32)
    view._on_save_credentials()
    spin(qapp, 30)

    assert state.secret_store.get("igdb_client_id") == "client-id-value-0001"
    assert state.secret_store.get("rawg_api_key") == "0" * 32
    # the service picked the values up immediately
    assert state.service.igdb.configured is True
    assert state.service.rawg.configured is True
    # and nothing landed in SQLite
    dumped = "\n".join(state.db.conn.iterdump())
    assert "client-secret-value-0001" not in dumped


def test_settings_describes_the_storage_backend_honestly(window):
    win, state, games = window
    win.show_view(NAV_SETTINGS)
    view = win.current_view()
    text = view.store_label.text()
    assert "Session only" in text or "local file" in text or "DPAPI" in text
    assert "never written to the collection database" in text


def test_settings_shows_the_database_location(window):
    win, state, games = window
    win.show_view(NAV_SETTINGS)
    view = win.current_view()
    assert "Database:" in view.db_label.text()
    assert "collection.db" in view.db_label.text()


def test_settings_lists_roots_with_enable_toggle(window, qapp):
    win, state, games = window
    win.show_view(NAV_SETTINGS)
    view = win.current_view()
    spin(qapp, 30)
    checkboxes = view.roots_list.findChildren(view.roots_list.__class__.__mro__[1]) if False else None
    from PyQt5.QtWidgets import QCheckBox

    boxes = view.roots_list.findChildren(QCheckBox)
    assert len(boxes) >= 1
    boxes[0].setChecked(False)
    spin(qapp, 30)
    assert state.root_paths() == []
    boxes[0].setChecked(True)
    spin(qapp, 30)
    assert len(state.root_paths()) == 1


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------
def test_diagnostics_renders_without_secrets(window, qapp):
    win, state, games = window
    state.save_credentials(
        igdb_client_id="diag-client-id-9999",
        igdb_client_secret="diag-client-secret-9999",
        rawg_api_key="f" * 32,
    )
    win.show_view(NAV_DIAGNOSTICS)
    view = win.current_view()
    spin(qapp, 80)
    text = diagnostics_text(view)
    assert "diag-client-secret-9999" not in text
    assert "diag-client-id-9999" not in text
    assert "f" * 32 not in text
    # no game titles either
    assert "Beta Quest" not in text
    assert "Alpha Game" not in text


def test_diagnostics_reports_real_measurements(window, qapp):
    win, state, games = window
    win.show_view(NAV_DIAGNOSTICS)
    view = win.current_view()
    spin(qapp, 80)
    text = diagnostics_text(view)
    assert "not measured" in text or "ms" in text
    assert "integrity_check" in text.lower() or "integrity" in text.lower()
    assert "schema version" in text.lower()


def test_diagnostics_local_export_warns_and_includes_paths(window, qapp, tmp_path):
    from cartridge import diagnostics

    win, state, games = window
    local = diagnostics.collect_local(state, state.redactor)
    assert local["report_kind"] == "local"
    assert "warning" in local
    assert local["paths"]["database"] == state.db_path
    titles = [row["title"] for row in local.get("games", [])]
    assert "Beta Quest" in titles
    # still no credentials
    text = repr(local)
    assert "client_secret=" not in text


def diagnostics_text(view) -> str:
    return view.text_view.toPlainText()


# --------------------------------------------------------------------------
# add-game dialog (manual path - no network)
# --------------------------------------------------------------------------
def test_add_game_dialog_scans_and_suggests(window, qapp, tmp_path):
    win, state, games = window
    from cartridge.ui.dialogs.add_game import AddGameDialog

    folder = tmp_path / "Gry" / "dialog-game"
    folder.mkdir(parents=True)
    (folder / "game.iso").write_bytes(b"\x00" * 128)
    (folder / "game.cue").write_text("FILE game.iso BINARY", encoding="utf-8")

    dialog = AddGameDialog(state, win, suggested_folder=str(folder))
    spin(qapp, 60)
    assert dialog.folder_edit.text() == str(folder)
    hint = dialog.types_hint.text()
    assert "ISO" in hint
    chips = [chip.text() for chip in dialog._type_chips if chip.isChecked()]
    assert "ISO" in chips
    dialog.reject()


def test_add_game_manual_import_creates_a_record(window, qapp, tmp_path):
    win, state, games = window
    from cartridge.ui.dialogs.add_game import AddGameDialog

    folder = tmp_path / "Gry" / "manual-game"
    folder.mkdir(parents=True)
    (folder / "run.exe").write_bytes(b"MZ")

    dialog = AddGameDialog(state, win, suggested_folder=str(folder))
    spin(qapp, 40)
    dialog._set_page("manual")
    dialog.manual_title.setText("Typed By Hand")
    dialog.manual_year.setValue(1997)
    dialog.manual_developer.setText("Handmade Studio")
    dialog.artwork_check.setChecked(False)
    dialog._do_import(selected=None, manual=True)

    deadline = time.time() + 10.0
    while time.time() < deadline and dialog._import_handle is not None:
        spin(qapp, 50)
    spin(qapp, 60)

    game = state.repo.get_game_by_folder(str(folder))
    assert game is not None
    assert game.title == "Typed By Hand"
    assert game.metadata_state == STATE_MANUAL
    assert game.release_year == 1997
    assert game.effective_developer() == "Handmade Studio"


def test_add_game_dialog_rejects_an_unusable_folder(window, qapp, tmp_path):
    win, state, games = window
    from cartridge.ui.dialogs.add_game import AddGameDialog

    missing = str(tmp_path / "Gry" / "not-there")
    dialog = AddGameDialog(state, win, suggested_folder=missing)
    spin(qapp, 40)
    assert "not usable" in dialog.folder_state_label.text()
    assert dialog.primary_button.isEnabled() is False
    dialog.reject()


# --------------------------------------------------------------------------
# edit dialog
# --------------------------------------------------------------------------
def test_edit_dialog_overrides_survive_a_provider_refresh(window, qapp):
    win, state, games = window
    from cartridge.ui.dialogs.edit_game import EditGameDialog

    target = [g for g in games if g.effective_title() == "Beta Quest"][0]
    target = state.repo.get_game(target.id)
    dialog = EditGameDialog(state, target, win)
    dialog.f_title.override_check.setChecked(True)
    dialog.f_title.editor.setText("My Beta")
    dialog.favourite_check.setChecked(True)
    dialog._on_save()
    spin(qapp, 60)

    after = state.repo.get_game(target.id)
    assert after.effective_title() == "My Beta"
    assert after.favourite is True

    # a provider refresh must not clobber the override
    state.repo.apply_provider_metadata(
        target.id, {"title": "Provider Says Otherwise", "provider": "igdb", "provider_id": "9001"}
    )
    after = state.repo.get_game(target.id)
    assert after.effective_title() == "My Beta"
    assert after.title == "Provider Says Otherwise"


def test_edit_dialog_clear_override_returns_to_provider(window, qapp):
    win, state, games = window
    from cartridge.ui.dialogs.edit_game import EditGameDialog

    target = state.repo.get_game(games[0].id)
    state.repo.update_user_fields(target.id, {"user_title": "Mine"})
    dialog = EditGameDialog(state, state.repo.get_game(target.id), win)
    dialog.f_title.override_check.setChecked(False)
    dialog._on_save()
    spin(qapp, 60)
    assert state.repo.get_game(target.id).effective_title() == "Alpha Game"


# --------------------------------------------------------------------------
# content types dialog
# --------------------------------------------------------------------------
def test_content_types_dialog_add_rename_delete(window, qapp):
    win, state, games = window
    from cartridge.ui.dialogs.content_types import ContentTypesDialog

    dialog = ContentTypesDialog(state, win)
    dialog.name_edit.setText("Modded")
    dialog._on_add()
    names = [ct.name for ct in state.repo.list_content_types(with_counts=False)]
    assert "Modded" in names

    dialog.table.selectRow(
        next(i for i, ct in enumerate(state.repo.list_content_types(with_counts=False))
             if ct.name == "Modded")
    )
    # rename through the repository directly: the dialog uses an input box
    target = [ct for ct in state.repo.list_content_types() if ct.name == "Modded"][0]
    state.repo.rename_content_type(target.id, "Modded v2")
    dialog.refresh()
    names = [ct.name for ct in state.repo.list_content_types(with_counts=False)]
    assert "Modded v2" in names

    target = [ct for ct in state.repo.list_content_types() if ct.name == "Modded v2"][0]
    state.repo.delete_content_type(target.id)
    dialog.refresh()
    names = [ct.name for ct in state.repo.list_content_types(with_counts=False)]
    assert "Modded v2" not in names
    dialog.accept()


# --------------------------------------------------------------------------
# image viewer
# --------------------------------------------------------------------------
def test_image_viewer_shows_a_local_file(window, qapp, tmp_path):
    from cartridge.ui.dialogs.image_viewer import ImageViewerDialog
    from tests._png import gradient_png_bytes

    path = tmp_path / "shot.png"
    path.write_bytes(gradient_png_bytes(200, 120))
    dialog = ImageViewerDialog(str(path))
    spin(qapp, 40)
    assert "200 × 120" in dialog.info.text()
    assert dialog._pixmap is not None
    dialog.accept()


def test_image_viewer_reports_an_unreadable_file(window, qapp, tmp_path):
    from cartridge.ui.dialogs.image_viewer import ImageViewerDialog

    path = tmp_path / "broken.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    dialog = ImageViewerDialog(str(path))
    spin(qapp, 40)
    assert "could not be displayed" in dialog.info.text()
    dialog.accept()


# --------------------------------------------------------------------------
# first-run dialog
# --------------------------------------------------------------------------
def test_first_run_dialog_explains_what_will_be_written(window, qapp, tmp_path):
    from cartridge.ui.dialogs.first_run import FirstRunDialog

    win, state, games = window
    target = tmp_path / "NewRoot"
    target.mkdir()
    dialog = FirstRunDialog(state, win)
    dialog._set_root_text(str(target))
    spin(qapp, 40)
    assert ".cartridge" in dialog.explain.text()
    assert "collection.db" in dialog.explain.text()
    assert dialog.continue_button.isEnabled() is True
    dialog.reject()


def test_first_run_dialog_refuses_a_missing_folder(window, qapp, tmp_path):
    from cartridge.ui.dialogs.first_run import FirstRunDialog

    win, state, games = window
    dialog = FirstRunDialog(state, win)
    dialog.show()
    spin(qapp, 40)
    # The field is read-only (folder choice goes through the browse button), so
    # QLineEdit.setText does not emit textChanged; drive the handler directly.
    dialog._on_root_changed(str(tmp_path / "does-not-exist"))
    spin(qapp, 40)
    assert not dialog.warning.isHidden()
    assert dialog.continue_button.isEnabled() is False

    # and a real folder clears the warning and enables continue
    good = tmp_path / "ok-root"
    good.mkdir()
    dialog._on_root_changed(str(good))
    spin(qapp, 40)
    assert dialog.warning.isHidden()
    assert dialog.continue_button.isEnabled() is True
    dialog.reject()


# --------------------------------------------------------------------------
# fresh-install regressions (reported from a real Windows 7 run, 2026-10-10)
# --------------------------------------------------------------------------
@pytest.fixture
def fresh_state(tmp_path, store_dir):
    """An AppState with NO database open - exactly what a first launch looks like."""
    state = AppState(secret_store=store_dir, windows=False)
    yield state
    state.shutdown()


def test_first_run_continue_creates_the_database_and_root(qapp, fresh_state, tmp_path):
    """The reported bug: 'Create collection' said 'No database path is configured'."""
    from cartridge.ui.dialogs.first_run import FirstRunDialog

    apply_theme(qapp)
    root = tmp_path / "Gry"
    root.mkdir()
    dialog = FirstRunDialog(fresh_state, None)
    dialog.show()
    spin(qapp, 30)
    dialog._set_root_text(str(root))
    spin(qapp, 30)
    assert dialog.continue_button.isEnabled() is True

    dialog._on_continue()
    spin(qapp, 60)

    assert fresh_state.ready, "the database must be open after Create collection"
    expected_db = os.path.join(str(root), ".cartridge", "collection.db")
    assert os.path.exists(expected_db), expected_db
    assert fresh_state.db_path == expected_db
    roots = fresh_state.root_paths()
    assert roots == [str(root)]
    # and the choice is remembered for the next launch
    from cartridge.app import save_last_session

    assert save_last_session(roots[0], fresh_state.db_path) is True


def test_first_run_continue_with_forward_slashes(qapp, fresh_state, tmp_path):
    """QFileDialog hands back 'E:/Gry'-style paths on Windows; both must work."""
    from cartridge.ui.dialogs.first_run import FirstRunDialog

    apply_theme(qapp)
    root = tmp_path / "Gry"
    root.mkdir()
    dialog = FirstRunDialog(fresh_state, None)
    dialog.show()
    spin(qapp, 30)
    slashed = str(root).replace(os.sep, "/")
    dialog._set_root_text(slashed)
    spin(qapp, 30)
    dialog._on_continue()
    spin(qapp, 60)
    assert fresh_state.ready
    assert len(fresh_state.root_paths()) == 1


def test_settings_add_root_works_without_any_database(qapp, fresh_state, tmp_path):
    """The reported bug: Add root folder in Settings did literally nothing."""
    from cartridge.ui.views.settings import SettingsView

    apply_theme(qapp)
    root = tmp_path / "Games"
    root.mkdir()
    view = SettingsView(fresh_state)
    view._add_root_chosen(str(root))
    spin(qapp, 60)

    assert fresh_state.ready, "Settings must create the database on a fresh install"
    assert fresh_state.root_paths() == [str(root)]
    assert os.path.exists(os.path.join(str(root), ".cartridge", "collection.db"))


def test_settings_add_root_surfaces_errors_instead_of_silence(qapp, fresh_state, tmp_path, monkeypatch):
    """A failure in the slot must be visible, not swallowed into stderr."""
    from cartridge.ui.views import settings as settings_mod
    from cartridge.ui.views.settings import SettingsView

    apply_theme(qapp)
    root = tmp_path / "Games2"
    root.mkdir()
    view = SettingsView(fresh_state)

    shown = []

    class FakeBox:
        @staticmethod
        def critical(parent, title, text):
            shown.append((title, text))

        @staticmethod
        def question(parent, title, text, *a, **k):
            shown.append((title, text))
            return FakeBox.Yes

        Yes = 1
        No = 0

    monkeypatch.setattr(settings_mod, "QMessageBox", FakeBox)
    monkeypatch.setattr(
        settings_mod.QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *a, **k: str(root)),
    )

    def explode(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(fresh_state, "open_database", explode)

    view._on_add_root()          # the guarded entry point the button calls
    spin(qapp, 30)

    assert shown, "the user must see a dialog when adding a root fails"
    assert any("disk on fire" in text for _title, text in shown)


# --------------------------------------------------------------------------
# field regressions from the Windows 7 run of 2026-10-10
# --------------------------------------------------------------------------
def test_content_type_picker_opens_and_saves(window, qapp):
    """Regression: ContentTypePicker crashed with NameError: QCheckBox."""
    from cartridge.ui.dialogs.content_types import ContentTypePicker

    win, state, games = window
    target = state.repo.get_game(games[0].id)
    picker = ContentTypePicker(state, target.id, win)
    picker.show()
    spin(qapp, 40)
    assert len(picker._chips) >= 1
    before = set(target.content_types)
    for chip in picker._chips:
        chip.setChecked(chip.text() == "GOG")
    picker._on_save()
    spin(qapp, 40)
    after = state.repo.get_game(target.id).content_types
    assert after != before or "GOG" in after


def test_manager_content_types_on_unmatched_folder_asks_to_add(window, qapp, monkeypatch, tmp_path):
    """Regression: the menu item appeared dead for folders without a record."""
    from cartridge.core.models import FolderEntry
    from cartridge.ui.views import manager as manager_mod

    win, state, games = window
    win.show_view(NAV_MANAGER)
    view = win.current_view()
    spin(qapp, 40)

    folder = tmp_path / "unmatched-thing"
    folder.mkdir()
    entry = FolderEntry(path=str(folder), name="unmatched-thing", association="unmatched")

    answers = iter([manager_mod.QMessageBox.No])
    seen = {}

    class FakeBox:
        Yes = 1
        No = 0

        @staticmethod
        def question(parent, title, text, *a, **k):
            seen["question"] = (title, text)
            return next(answers)

    monkeypatch.setattr(manager_mod, "QMessageBox", FakeBox)
    view._edit_content_types(entry)
    spin(qapp, 20)
    assert "question" in seen, "the user must get a real question, not silence"
    assert "not in the catalogue" in seen["question"][1].lower() or "no catalogue record" in seen["question"][1].lower()

    # answering Yes must open the import flow for that folder
    opened = {}

    class StubDialog:
        def __init__(self, state, parent=None, suggested_folder=None):
            opened["folder"] = suggested_folder

        def exec_(self):
            return False

    monkeypatch.setattr(
        "cartridge.ui.dialogs.add_game.AddGameDialog", StubDialog
    )
    answers = iter([FakeBox.Yes])
    view._edit_content_types(entry)
    spin(qapp, 20)
    assert opened.get("folder") == str(folder)


def test_add_game_dialog_has_an_open_folder_button(window, qapp, tmp_path):
    """Field request: verify a folder's contents without leaving the dialog."""
    from cartridge.ui.dialogs.add_game import AddGameDialog

    win, state, games = window
    folder = tmp_path / "Gry" / "check-me"
    folder.mkdir()
    (folder / "game.iso").write_bytes(b"\x00" * 64)

    dialog = AddGameDialog(state, win, suggested_folder=str(folder))
    spin(qapp, 40)
    assert hasattr(dialog, "open_folder_button")
    assert dialog.open_folder_button.isEnabled() is True

    opened = {}
    from cartridge.ui import actions

    def monkey_open(path, parent=None):
        opened["path"] = path
        return True, "Opened test"

    actions_open = actions.open_folder
    actions.open_folder = monkey_open
    try:
        dialog._on_open_folder()
    finally:
        actions.open_folder = actions_open
    assert opened.get("path") == str(folder)
    assert "Opened test" in dialog.status_label.text()
    dialog.reject()


def _mock_provider_state(state, fixture_payload, png_bytes):
    """Point the state's HTTP at a mock transport: IGDB + image CDN offline."""
    import httpx

    from cartridge.providers.base import HttpSettings, HttpClient
    from cartridge.providers.ratelimit import TEST_POLICY, RateLimiter
    from cartridge.providers.service import MetadataService

    def handler(request):
        url = str(request.url)
        if "id.twitch.tv" in url:
            return httpx.Response(
                200, json={"access_token": "tok0123456789abcdef", "expires_in": 3600}
            )
        if "images.igdb.com" in url or "media.rawg.io" in url:
            return httpx.Response(200, content=png_bytes)
        if "api.igdb.com" in url:
            return httpx.Response(200, json=fixture_payload)
        return httpx.Response(200, json={"results": []})

    client = HttpClient(
        settings=HttpSettings(max_retries=0),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        limiter=RateLimiter(TEST_POLICY, sleep=lambda _s: None),
    )
    service = MetadataService(client=client, limiters=state.limiters)
    service.set_credentials("fake-id-000000000000000000", "fake-secret-0000000000", "")
    state.http = client
    state.service = service
    return state


def test_search_results_stream_thumbnails_and_preview_cover(window, qapp, tmp_path):
    """Field request: every result's cover loads in the background, and the
    preview shows the selected one instead of a floating 'No cover' box."""
    import json
    import os

    from cartridge.ui.dialogs.add_game import AddGameDialog
    from tests._png import gradient_png_bytes

    win, state, games = window
    fixture = json.load(
        open(os.path.join(os.path.dirname(__file__), "fixtures", "igdb_search.json"),
             encoding="utf-8")
    )["payload"]
    _mock_provider_state(state, fixture, gradient_png_bytes(60, 80))

    folder = tmp_path / "Gry" / "stream-thumbs"
    folder.mkdir()
    dialog = AddGameDialog(state, win, suggested_folder=str(folder))
    spin(qapp, 40)
    dialog._set_page("search")
    dialog.search_edit.setText("The Witcher 2")
    dialog._on_search()

    deadline = time.time() + 10.0
    while time.time() < deadline and (dialog._search_handle is not None
                                      or len(dialog._rows) == 0):
        spin(qapp, 40)
    assert len(dialog._rows) >= 2

    # thumbnails stream in for every row, not just the selected one
    deadline = time.time() + 10.0
    while time.time() < deadline and len(dialog._thumb_paths) < 2:
        spin(qapp, 50)
    assert len(dialog._thumb_paths) >= 2, "covers must prefetch for all rows"
    for index, path in dialog._thumb_paths.items():
        assert os.path.exists(path)

    # the selected row's preview shows a real cover, not the placeholder
    selected = dialog.results_list.currentRow()
    assert selected in dialog._thumb_paths
    assert dialog.preview.title.text() != "No result selected"
    assert dialog.preview.cover._source is not None
    dialog.reject()


def test_preview_layout_keeps_cover_beside_the_title(window, qapp):
    """Regression: the centred placeholder overlapped the title column."""
    from cartridge.core.models import ProviderGame
    from cartridge.ui.dialogs.add_game import DetailPreview

    win, state, games = window
    preview = DetailPreview(state)
    game = ProviderGame(
        provider="igdb", provider_id="1", title="Some Game", year=2004,
        cover_url="https://images.igdb.com/x.jpg",
    )
    preview.set_provider_game(game)
    top_row = preview.layout().itemAt(0).widget()
    assert top_row is not None
    # cover and title live in the same horizontal row now
    inner = top_row.layout()
    widgets = [inner.itemAt(i).widget() for i in range(inner.count()) if inner.itemAt(i).widget()]
    assert preview.cover in widgets
    preview.set_cover_path(None)
    assert preview.cover._source is None


def test_dialogs_paint_the_dark_theme(window, qapp):
    """Regression: the generic QWidget rule once overrode QDialog's background,
    leaving every dialog transparent (white/black depending on compositor)."""
    from PyQt5.QtWidgets import QDialog, QLabel, QVBoxLayout

    apply_theme(qapp)
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("theme probe"))
    dialog.resize(300, 200)
    dialog.show()
    spin(qapp, 40)
    image = dialog.grab().toImage()
    dark = 0
    total = 0
    for y in range(0, image.height(), 7):
        for x in range(0, image.width(), 7):
            color = image.pixelColor(x, y)
            luminance = (color.red() * 299 + color.green() * 587 + color.blue() * 114) // 1000
            total += 1
            if luminance < 80:
                dark += 1
    dialog.close()
    assert dark / float(total) > 0.9, "dialogs must render the dark theme"
