"""Tests for filesystem discovery, content-type hints and size measurement.

Two properties are asserted relentlessly here because they are the safety
contract of the whole product:

* discovery **never writes, moves, renames or deletes** anything it finds;
* a vanished folder or an unmounted drive **never deletes catalogue metadata**.
"""

from __future__ import annotations

import os
import time

import pytest

from cartridge.core.models import STATE_NONE
from cartridge.db.connection import Database
from cartridge.db.repository import Repository
from cartridge.paths import APP_ASSET_DIR, APP_DATA_DIR, FolderState
from cartridge.scan import discovery
from cartridge.scan.content_types import (
    EXECUTABLE_PATTERN,
    has_executable,
    names_from_listing,
    suggest_content_types,
)
from cartridge.scan.folder_size import (
    measure_folder_size,
    measure_many,
    quick_entry_count,
)


@pytest.fixture
def repo(tmp_path):
    database = Database(str(tmp_path / "collection.db")).open()
    yield Repository(database, windows=False)
    database.close_all()


def snapshot_tree(root):
    """Every path + size + mtime under ``root``, for before/after comparisons."""
    out = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in sorted(dirnames) + sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                out.append((full, os.path.getsize(full) if os.path.isfile(full) else -1))
            except OSError:
                out.append((full, None))
    return sorted(out)


# --------------------------------------------------------------------------
# content-type detection (advisory, name-based)
# --------------------------------------------------------------------------
def test_gog_install_is_detected():
    names = ["witcher2.exe", "goggame-1234.info", "support", "README.txt", "webcache"]
    suggestions = [s.name for s in suggest_content_types(names)]
    assert "GOG" in suggestions
    assert suggestions[0] == "GOG"


def test_iso_dump_is_detected():
    names = ["game.iso", "game.cue", "game.bin"]
    assert "ISO" in [s.name for s in suggest_content_types(names)]


def test_dosbox_setup_is_detected_as_emulator():
    names = ["dosbox.exe", "dosboxW2.conf", "game.bat", "MANUAL.PDF"]
    assert "Emulator" in [s.name for s in suggest_content_types(names)]


def test_installer_is_detected():
    names = ["setup.exe", "unins000.exe", "data1.cab"]
    assert "Installer" in [s.name for s in suggest_content_types(names)]


def test_archive_only_folder_is_a_backup():
    names = ["game.zip", "game.part1.rar", "game.part2.rar"]
    suggestions = [s.name for s in suggest_content_types(names)]
    assert "Backup" in suggestions


def test_portable_layout_needs_two_hits():
    # One stray file called "portable" is not evidence; the PortableApps layout is.
    weak = [s.name for s in suggest_content_types(["portable"])]
    strong = [
        s.name for s in suggest_content_types(["GameLauncher.exe", "app.ini", "Data", "Other"])
    ]
    assert "Portable" in strong
    assert "Portable" not in weak


def test_bare_exe_falls_back_to_EXE():
    suggestions = [s.name for s in suggest_content_types(["somegame.exe", "data"])]
    assert suggestions == ["EXE"]


def test_stronger_label_suppresses_the_EXE_fallback():
    names = ["game.exe", "goggame-1.info"]
    suggestions = [s.name for s in suggest_content_types(names)]
    assert "GOG" in suggestions
    assert "EXE" not in suggestions


def test_EXE_can_be_requested_explicitly():
    names = ["game.exe", "goggame-1.info"]
    suggestions = [s.name for s in suggest_content_types(names, include_noisy=True)]
    assert "EXE" in suggestions


def test_empty_folder_suggests_nothing():
    assert suggest_content_types([]) == []
    assert suggest_content_types(None) == []


def test_suggestions_carry_examples_for_the_ui():
    suggestions = suggest_content_types(["game.iso", "game.cue"])
    iso = [s for s in suggestions if s.name == "ISO"][0]
    assert iso.hits >= 2
    assert "game.iso" in iso.examples
    assert "ISO" in iso.reason()


def test_folder_name_participates_in_detection():
    assert "Portable" in [
        s.name for s in suggest_content_types(["game.exe"], folder_name="Game Portable")
    ]


def test_detection_is_case_insensitive():
    upper = [s.name for s in suggest_content_types(["GAME.ISO", "GOGGAME-1.INFO"])]
    assert "ISO" in upper and "GOG" in upper


def test_has_executable():
    assert has_executable(["game.exe"])
    assert has_executable(["run.BAT"])
    assert not has_executable(["game.iso", "readme.txt"])
    assert not has_executable([])


def test_executable_pattern_does_not_match_a_document():
    assert not EXECUTABLE_PATTERN.search("notes.txt")
    assert not EXECUTABLE_PATTERN.search("game.iso")


def test_names_from_listing_accepts_strings_and_entries(tmp_path):
    assert names_from_listing(["a", "b"]) == ["a", "b"]
    with os.scandir(str(tmp_path)) as iterator:
        assert isinstance(names_from_listing(iterator), list)
    assert names_from_listing([str(x) for x in range(50)], limit=10) == [str(i) for i in range(10)]


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------
def test_discover_finds_immediate_children(fake_root):
    result = discovery.discover_root(str(fake_root))
    assert result.ok is True
    names = sorted(folder.name for folder in result.folders)
    assert names == ["Nested", "Some ISO Game", "Wiedzmin 2", "empty folder"]


def test_discover_excludes_app_managed_directories(fake_root):
    result = discovery.discover_root(str(fake_root))
    names = [folder.name for folder in result.folders]
    assert APP_ASSET_DIR not in names
    assert result.skipped_app_managed >= 1


def test_discover_excludes_system_folders(tmp_path):
    for name in ("$RECYCLE.BIN", "System Volume Information", "Real Game"):
        (tmp_path / name).mkdir()
    result = discovery.discover_root(str(tmp_path))
    names = [folder.name for folder in result.folders]
    assert names == ["Real Game"]
    assert result.skipped_system == 2


def test_discover_skips_hidden_folders_unless_asked(tmp_path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "Visible").mkdir()
    default = discovery.discover_root(str(tmp_path))
    assert [f.name for f in default.folders] == ["Visible"]
    assert default.skipped_hidden == 1

    with_hidden = discovery.discover_root(str(tmp_path), include_hidden=True)
    assert sorted(f.name for f in with_hidden.folders) == [".hidden", "Visible"]


def test_discover_is_one_level_deep_only(fake_root):
    result = discovery.discover_root(str(fake_root))
    names = [folder.name for folder in result.folders]
    assert "Inner Game" not in names, "nested folders must not be catalogued"
    assert "Nested" in names


def test_discover_produces_content_type_hints(fake_root):
    result = discovery.discover_root(str(fake_root))
    by_name = {folder.name: folder for folder in result.folders}
    assert "GOG" in by_name["Wiedzmin 2"].suggested_names
    assert "ISO" in by_name["Some ISO Game"].suggested_names
    assert by_name["Wiedzmin 2"].has_executable is True
    assert by_name["empty folder"].suggested_names == []


def test_discover_can_skip_detection(fake_root):
    result = discovery.discover_root(str(fake_root), detect_types=False)
    assert all(folder.suggestions == [] for folder in result.folders)
    assert all(folder.entry_count == 0 for folder in result.folders)


def test_discover_bounds_the_per_folder_listing(tmp_path):
    big = tmp_path / "Big Game"
    big.mkdir()
    for index in range(60):
        (big / ("file%03d.dat" % index)).write_bytes(b"x")
    result = discovery.discover_root(str(tmp_path), max_entries_per_folder=25)
    folder = result.folders[0]
    assert folder.entry_count == 25
    assert folder.listing_truncated is True


def test_discover_reports_a_missing_root(tmp_path):
    result = discovery.discover_root(str(tmp_path / "nope"))
    assert result.ok is False
    assert result.root_state == FolderState.MISSING.value
    assert result.folders == []
    assert any("does not exist" in error for error in result.errors)


def test_discover_reports_an_empty_path():
    result = discovery.discover_root("")
    assert result.ok is False
    assert result.errors


def test_discover_reports_a_root_that_is_a_file(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    result = discovery.discover_root(str(f))
    assert result.ok is False
    assert result.root_state in (FolderState.MISSING.value, FolderState.NOT_A_DIRECTORY.value)


def test_discover_reports_an_unavailable_windows_drive(monkeypatch):
    import cartridge.paths as paths_mod

    monkeypatch.setattr(paths_mod, "_probe", lambda p: False)
    result = discovery.discover_root(r"E:\Gry", windows=True)
    assert result.ok is False
    assert result.root_state == FolderState.UNAVAILABLE_DRIVE.value
    assert any("not available" in error for error in result.errors)


def test_discover_survives_an_unreadable_subfolder(tmp_path, monkeypatch):
    good = tmp_path / "Good"
    bad = tmp_path / "Bad"
    good.mkdir()
    bad.mkdir()

    real_scandir = os.scandir

    def flaky(path, *args, **kwargs):
        if str(path).endswith("Bad"):
            raise PermissionError(13, "Access is denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(discovery.os, "scandir", flaky)
    result = discovery.discover_root(str(tmp_path))
    by_name = {folder.name: folder for folder in result.folders}
    assert by_name["Good"].state == FolderState.PRESENT.value
    assert by_name["Bad"].state == FolderState.ACCESS_DENIED.value
    assert by_name["Bad"].error
    assert any("Access denied" in error for error in result.errors)


def test_discover_is_cancellable(tmp_path):
    for index in range(30):
        (tmp_path / ("Game%02d" % index)).mkdir()
    calls = {"n": 0}

    def cancel_after_five():
        calls["n"] += 1
        return calls["n"] > 6

    result = discovery.discover_root(str(tmp_path), cancel=cancel_after_five)
    assert len(result.folders) <= 7
    assert any("cancelled" in error.lower() for error in result.errors)


def test_discover_all_roots_isolates_failures(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "Game A").mkdir()
    results = discovery.discover_all_roots([str(good), str(tmp_path / "missing")])
    assert results[0].ok is True
    assert results[1].ok is False
    assert len(results[0].folders) == 1


def test_discover_records_a_duration(fake_root):
    result = discovery.discover_root(str(fake_root))
    assert result.duration_ms >= 0.0


# --------------------------------------------------------------------------
# discovery must never modify the user's files
# --------------------------------------------------------------------------
def test_discovery_does_not_touch_anything(fake_root):
    before = snapshot_tree(fake_root)
    mtimes = {
        path: os.path.getmtime(path)
        for path, _ in before
        if os.path.exists(path) and os.path.isfile(path)
    }
    discovery.discover_root(str(fake_root))
    after = snapshot_tree(fake_root)
    assert before == after, "discovery changed the tree"
    for path, stamp in mtimes.items():
        assert os.path.getmtime(path) == stamp


def test_reconcile_does_not_touch_anything(fake_root, repo):
    before = snapshot_tree(fake_root)
    root = repo.add_root(str(fake_root))
    result = discovery.discover_root(str(fake_root))
    discovery.reconcile(repo, result, root.id)
    assert snapshot_tree(fake_root) == before


# --------------------------------------------------------------------------
# reconciliation against the catalogue
# --------------------------------------------------------------------------
def test_reconcile_creates_records_for_new_folders(fake_root, repo):
    root = repo.add_root(str(fake_root))
    result = discovery.discover_root(str(fake_root))
    summary = discovery.reconcile(repo, result, root.id)
    assert summary.new_folders == 4
    assert summary.matched == 0
    assert repo.count_games() == 4


def test_reconcile_is_idempotent(fake_root, repo):
    root = repo.add_root(str(fake_root))
    first = discovery.reconcile(repo, discovery.discover_root(str(fake_root)), root.id)
    second = discovery.reconcile(repo, discovery.discover_root(str(fake_root)), root.id)
    assert first.new_folders == 4
    assert second.new_folders == 0
    assert second.matched == 4
    assert repo.count_games() == 4


def test_reconcile_applies_suggested_types_only_once(fake_root, repo):
    root = repo.add_root(str(fake_root))
    discovery.reconcile(repo, discovery.discover_root(str(fake_root)), root.id)
    game = repo.get_game_by_folder(str(fake_root / "Wiedzmin 2"))
    assert "GOG" in game.content_types

    repo.set_game_content_types(game.id, ["Installer"], source="user")
    discovery.reconcile(repo, discovery.discover_root(str(fake_root)), root.id)
    after = repo.get_game(game.id)
    assert after.content_types == ["Installer"], "user choices must win over hints"


def test_reconcile_can_skip_suggestions(fake_root, repo):
    root = repo.add_root(str(fake_root))
    discovery.reconcile(
        repo, discovery.discover_root(str(fake_root)), root.id, apply_suggestions=False
    )
    game = repo.get_game_by_folder(str(fake_root / "Wiedzmin 2"))
    assert game.content_types == []


def test_reconcile_preserves_metadata_on_a_rescan(fake_root, repo):
    root = repo.add_root(str(fake_root))
    discovery.reconcile(repo, discovery.discover_root(str(fake_root)), root.id)
    game = repo.get_game_by_folder(str(fake_root / "Wiedzmin 2"))
    repo.apply_provider_metadata(game.id, {"title": "The Witcher 2", "release_year": 2011})
    repo.set_favourite(game.id, True)
    repo.update_user_fields(game.id, {"user_title": "Wiedźmin 2"})

    discovery.reconcile(repo, discovery.discover_root(str(fake_root)), root.id)
    after = repo.get_game(game.id)
    assert after.title == "The Witcher 2"
    assert after.user_title == "Wiedźmin 2"
    assert after.favourite is True
    assert after.release_year == 2011


def test_reconcile_marks_a_deleted_folder_without_removing_the_record(fake_root, repo):
    root = repo.add_root(str(fake_root))
    discovery.reconcile(repo, discovery.discover_root(str(fake_root)), root.id)
    game = repo.get_game_by_folder(str(fake_root / "Wiedzmin 2"))
    repo.apply_provider_metadata(game.id, {"title": "The Witcher 2"})

    for name in os.listdir(str(fake_root / "Wiedzmin 2")):
        full = os.path.join(str(fake_root / "Wiedzmin 2"), name)
        os.remove(full) if os.path.isfile(full) else os.rmdir(full)
    os.rmdir(str(fake_root / "Wiedzmin 2"))

    summary = discovery.reconcile(repo, discovery.discover_root(str(fake_root)), root.id)
    assert summary.missing_folders == 1
    after = repo.get_game(game.id)
    assert after is not None, "metadata must survive a deleted folder"
    assert after.title == "The Witcher 2"
    assert after.folder_state == FolderState.MISSING.value


def test_reconcile_distinguishes_an_unavailable_drive(fake_root, repo, monkeypatch):
    root = repo.add_root(str(fake_root))
    discovery.reconcile(repo, discovery.discover_root(str(fake_root)), root.id)
    game = repo.get_game_by_folder(str(fake_root / "Wiedzmin 2"))

    # Simulate the whole root vanishing (an unplugged USB disk, say).
    import cartridge.paths as paths_mod

    real_probe = paths_mod._probe

    def dead_drive(path):
        if os.path.abspath(path) == os.path.abspath(str(fake_root / "Wiedzmin 2")):
            return False
        return real_probe(path)

    monkeypatch.setattr(paths_mod, "_probe", dead_drive)
    discovery.reconcile(repo, discovery.discover_root(str(fake_root)), root.id,
                        windows=False)
    after = repo.get_game(game.id)
    assert after.folder_state in (
        FolderState.MISSING.value, FolderState.UNAVAILABLE_DRIVE.value
    )
    assert after is not None


def test_reconcile_survives_a_root_that_cannot_be_read(fake_root, repo, monkeypatch):
    root = repo.add_root(str(fake_root))
    discovery.reconcile(repo, discovery.discover_root(str(fake_root)), root.id)
    before = repo.count_games()

    def deny(path, *args, **kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(discovery.os, "scandir", deny)
    result = discovery.discover_root(str(fake_root))
    summary = discovery.reconcile(repo, result, root.id)
    # An unreadable root must not be interpreted as "every folder was deleted".
    assert repo.count_games() == before
    assert summary.errors


def test_summary_text_is_informative(fake_root, repo):
    root = repo.add_root(str(fake_root))
    summary = discovery.reconcile(repo, discovery.discover_root(str(fake_root)), root.id)
    text = summary.as_text()
    assert "folders seen" in text
    assert summary.duration_ms >= 0.0
    assert summary.started_at and summary.finished_at


def test_folder_display_name_tidies_scene_style_names():
    folder = discovery.DiscoveredFolder(path="/x", name="Some.Game.Title.MULTI6.PL")
    assert discovery.folder_display_name(folder) == "Some Game Title MULTI6 PL"


# --------------------------------------------------------------------------
# folder size measurement
# --------------------------------------------------------------------------
def test_measure_folder_size_totals_every_file(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    (game / "a.bin").write_bytes(b"x" * 1000)
    sub = game / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 500)

    result = measure_folder_size(str(game))
    assert result.size_bytes == 1500
    assert result.files == 2
    assert result.complete is True
    assert result.cancelled is False
    assert result.measured_at
    assert result.duration_ms >= 0.0
    assert "1.5 KB" in result.describe()


def test_measure_reports_progress(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    for index in range(50):
        (game / ("f%02d.bin" % index)).write_bytes(b"x" * 10)
    seen = []
    measure_folder_size(str(game), progress=lambda files, size: seen.append((files, size)),
                        progress_every=10)
    assert seen, "progress was never reported"
    assert seen[-1][0] == 50


def test_measure_is_cancellable(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    for index in range(200):
        (game / ("f%03d.bin" % index)).write_bytes(b"x" * 10)
    state = {"n": 0}

    def cancel_soon():
        state["n"] += 1
        return state["n"] > 25

    result = measure_folder_size(str(game), cancel=cancel_soon)
    assert result.cancelled is True
    assert result.complete is False
    assert result.files < 200
    assert "partial" in result.describe()


def test_measure_respects_a_file_cap(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    for index in range(100):
        (game / ("f%02d.bin" % index)).write_bytes(b"x")
    result = measure_folder_size(str(game), max_files=10)
    assert result.files == 10
    assert result.complete is False
    assert any("configured limit" in error for error in result.errors)


def test_measure_does_not_follow_symlinks(tmp_path):
    target = tmp_path / "Outside"
    target.mkdir()
    (target / "big.bin").write_bytes(b"z" * 5000)
    game = tmp_path / "Game"
    game.mkdir()
    (game / "small.bin").write_bytes(b"x" * 10)
    try:
        os.symlink(str(target), str(game / "link"))
        os.symlink(str(target / "big.bin"), str(game / "link.bin"))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks are not available on this platform")

    result = measure_folder_size(str(game))
    assert result.size_bytes == 10, "symlinked content must not be counted"


def test_measure_handles_a_missing_path(tmp_path):
    result = measure_folder_size(str(tmp_path / "nope"))
    assert result.complete is False
    assert result.size_bytes == 0
    assert result.errors


def test_measure_handles_a_single_file(tmp_path):
    f = tmp_path / "one.bin"
    f.write_bytes(b"x" * 123)
    result = measure_folder_size(str(f))
    assert result.size_bytes == 123
    assert result.files == 1


def test_measure_of_an_empty_folder(tmp_path):
    empty = tmp_path / "Empty"
    empty.mkdir()
    result = measure_folder_size(str(empty))
    assert result.size_bytes == 0
    assert result.files == 0
    assert result.ok is True


def test_measure_many_reports_each_folder(tmp_path):
    for name in ("A", "B"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "x.bin").write_bytes(b"x" * (10 if name == "A" else 20))
    seen = []
    results = measure_many(
        [str(tmp_path / "A"), str(tmp_path / "B")],
        progress=lambda path, result: seen.append((path, result.size_bytes)),
    )
    assert [r.size_bytes for r in results] == [10, 20]
    assert len(seen) == 2


def test_measure_many_is_cancellable(tmp_path):
    for name in ("A", "B", "C"):
        (tmp_path / name).mkdir()
    results = measure_many(
        [str(tmp_path / n) for n in ("A", "B", "C")], cancel=lambda: True
    )
    assert results == []


def test_a_broken_progress_callback_does_not_lose_the_result(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    (game / "x.bin").write_bytes(b"x" * 10)

    def boom(files, size):
        raise RuntimeError("ui exploded")

    result = measure_folder_size(str(game), progress=boom, progress_every=1)
    assert result.size_bytes == 10


def test_quick_entry_count_is_bounded(tmp_path):
    for index in range(30):
        (tmp_path / ("f%02d" % index)).write_bytes(b"x")
    assert quick_entry_count(str(tmp_path)) == 30
    assert quick_entry_count(str(tmp_path), limit=10) == 10
    assert quick_entry_count(str(tmp_path / "nope")) == 0


def test_size_is_stored_with_its_measurement_time(fake_root, repo):
    game, _ = repo.upsert_folder(str(fake_root / "Wiedzmin 2"))
    assert game.folder_size_bytes is None, "an unmeasured folder must not show a size"

    result = measure_folder_size(str(fake_root / "Wiedzmin 2"))
    repo.set_folder_size(game.id, result.size_bytes, result.measured_at)
    after = repo.get_game(game.id)
    assert after.folder_size_bytes == result.size_bytes
    assert after.folder_size_measured_at == result.measured_at
