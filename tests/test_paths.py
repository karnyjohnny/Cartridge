"""Tests for cartridge.paths: normalization, identity, exclusion, folder state."""

from __future__ import annotations

import os

import pytest

from cartridge import paths
from cartridge.paths import FolderState


# --------------------------------------------------------------------------
# Windows normalization and identity
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "spellings",
    [
        [r"E:\Gry\Wiedzmin 2", r"e:\gry\wiedzmin 2", "E:/Gry/Wiedzmin 2",
         r"E:\Gry\Wiedzmin 2\\", r"E:\Gry\.\Wiedzmin 2"],
        [r"E:\Gry\Some Game", r"E:\GRY\SOME GAME", r"e:/gry/some game/"],
    ],
)
def test_windows_spellings_share_one_identity(spellings):
    keys = {paths.path_key(s, windows=True) for s in spellings}
    assert len(keys) == 1, keys


def test_windows_key_is_upper_and_backslashed():
    assert paths.path_key("e:/gry/wiedzmin 2", windows=True) == r"E:\GRY\WIEDZMIN 2"


def test_normalize_preserves_case_for_display():
    assert paths.normalize_path("e:/gry/Wiedzmin 2/", windows=True) == r"e:\gry\Wiedzmin 2"


def test_dotdot_segments_collapse():
    assert paths.normalize_path(r"E:\Gry\Nested\..\Wiedzmin 2", windows=True) == r"E:\Gry\Wiedzmin 2"


def test_posix_paths_stay_case_sensitive():
    a = paths.path_key("/mnt/gry/Wiedzmin", windows=False)
    b = paths.path_key("/mnt/gry/wiedzmin", windows=False)
    assert a != b


def test_root_paths_keep_trailing_separator():
    assert paths.is_root_path(r"E:\\", windows=True)
    assert paths.is_root_path(r"\\nas\share", windows=True)
    assert not paths.is_root_path(r"E:\Gry", windows=True)
    assert paths.is_root_path("/", windows=False)


def test_unc_root_detection():
    assert paths.is_root_path(r"\\nas\share", windows=True)
    assert not paths.is_root_path(r"\\nas\share\gry", windows=True)


def test_long_path_prefix_survives_normalization():
    long = "\\\\?\\E:\\Gry\\Wiedzmin 2"
    assert paths.normalize_path(long, windows=True) == long
    assert paths.drive_letter(long) == "E"


def test_empty_and_none_inputs_are_safe():
    assert paths.normalize_path("") == ""
    assert paths.normalize_path(None) == ""
    assert paths.path_key("") == ""
    assert paths.folder_name("") == ""
    assert paths.classify_folder_state("") == FolderState.UNKNOWN


# --------------------------------------------------------------------------
# Drive handling and root containment
# --------------------------------------------------------------------------
def test_drive_letter_and_root():
    assert paths.drive_letter(r"E:\Gry") == "E"
    assert paths.drive_letter("e:/gry") == "E"
    assert paths.drive_root(r"E:\Gry\Game") == "E:\\"
    assert paths.drive_letter("/mnt/gry") is None
    assert paths.drive_root(r"\\nas\share\gry") is None


def test_is_under_root():
    root = r"E:\Gry"
    assert paths.is_under_root(r"E:\Gry\Wiedzmin 2", root, windows=True)
    assert paths.is_under_root(r"E:\GRY\WIEDZMIN 2", root, windows=True)
    assert paths.is_under_root(r"E:\Gry", root, windows=True)
    assert not paths.is_under_root(r"D:\Other\Wiedzmin 2", root, windows=True)
    assert not paths.is_under_root(r"E:\GryBackup\Wiedzmin 2", root, windows=True)
    assert not paths.is_under_root("", root, windows=True)


def test_is_under_root_posix():
    assert paths.is_under_root("/mnt/gry/wiedzmin", "/mnt/gry", windows=False)
    assert not paths.is_under_root("/mnt/gry2/wiedzmin", "/mnt/gry", windows=False)


# --------------------------------------------------------------------------
# Application-managed directories must never be catalogued
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "candidate",
    [
        r"E:\Gry\.cartridge",
        r"E:\Gry\.cartridge\collection.db",
        r"E:\Gry\Wiedzmin 2\_cartridge",
        r"E:\Gry\Wiedzmin 2\_cartridge\assets\cover.jpg",
        "E:/Gry/_cartridge/assets/cover.jpg",
    ],
)
def test_app_managed_paths_are_detected(candidate):
    assert paths.is_app_managed(candidate, windows=True), candidate


@pytest.mark.parametrize(
    "candidate",
    [
        r"E:\Gry\Wiedzmin 2",
        r"E:\Gry\Cartridge The Game",
        r"E:\Gry\_cartridge_wannabe",
        r"E:\Gry\my.cartridge.collection",
    ],
)
def test_game_folders_are_not_flagged_as_managed(candidate):
    assert not paths.is_app_managed(candidate, windows=True), candidate


def test_app_managed_is_case_insensitive_on_windows():
    assert paths.is_app_managed(r"E:\Gry\_CARTRIDGE\assets", windows=True)
    assert paths.is_app_managed(r"E:\Gry\.Cartridge\collection.db", windows=True)


def test_db_and_asset_locations():
    assert paths.db_path_for_root(r"E:\Gry", windows=True) == r"E:\Gry\.cartridge\collection.db"
    assert paths.app_data_dir(r"E:\Gry", windows=True) == r"E:\Gry\.cartridge"
    assert paths.asset_dir_for_game(r"E:\Gry\Wiedzmin 2", windows=True) == (
        r"E:\Gry\Wiedzmin 2\_cartridge\assets"
    )


def test_asset_dir_is_inside_its_own_game_folder():
    game = r"E:\Gry\Wiedzmin 2"
    assets = paths.asset_dir_for_game(game, windows=True)
    assert paths.is_under_root(assets, game, windows=True)
    assert paths.is_app_managed(assets, windows=True)


# --------------------------------------------------------------------------
# Folder state classification
# --------------------------------------------------------------------------
def test_classify_present_folder(tmp_path):
    folder = tmp_path / "Game"
    folder.mkdir()
    assert paths.classify_folder_state(str(folder), windows=False) == FolderState.PRESENT


def test_classify_missing_folder(tmp_path):
    missing = str(tmp_path / "Gone")
    assert paths.classify_folder_state(missing, windows=False) == FolderState.MISSING


def test_classify_file_is_not_a_directory(tmp_path):
    # A regular file where a folder was expected must not read as PRESENT,
    # otherwise the UI would offer "Open folder" for something that is not one.
    f = tmp_path / "notafolder"
    f.write_text("x", encoding="utf-8")
    state = paths.classify_folder_state(str(f), windows=False)
    assert state == FolderState.MISSING


def test_classify_access_denied(monkeypatch):
    monkeypatch.setattr(paths, "_probe", lambda p: None)
    assert paths.classify_folder_state(r"C:\Locked\Game", windows=True) == (
        FolderState.ACCESS_DENIED
    )


def test_classify_unavailable_drive(monkeypatch):
    """A vanished drive letter must not be reported as a deleted folder."""
    def fake_probe(path):
        if path.upper().startswith("E:\\") and path in ("E:\\", "e:\\"):
            return False
        if path == "E:\\":
            return False
        return False

    monkeypatch.setattr(paths, "_probe", fake_probe)
    state = paths.classify_folder_state(r"E:\Gry\Wiedzmin 2", windows=True)
    assert state == FolderState.UNAVAILABLE_DRIVE


def test_classify_present_on_windows_drive(monkeypatch):
    monkeypatch.setattr(paths, "_probe", lambda p: True)
    assert paths.classify_folder_state(r"E:\Gry\Wiedzmin 2", windows=True) == (
        FolderState.PRESENT
    )


def test_real_probe_helper_reports_directory(tmp_path):
    folder = tmp_path / "x"
    folder.mkdir()
    assert paths._probe(str(folder)) is True
    assert paths._probe(str(tmp_path / "nope")) is False


# --------------------------------------------------------------------------
# Display helpers
# --------------------------------------------------------------------------
def test_folder_name():
    assert paths.folder_name(r"E:\Gry\Wiedzmin 2\\", windows=True) == "Wiedzmin 2"
    assert paths.folder_name("/mnt/gry/wiedzmin/", windows=False) == "wiedzmin"


def test_shorten_path_keeps_the_tail():
    long = r"E:\Gry\Bardzo Dlugi Katalog\Jeszcze Dluzej\Wiedzmin 2"
    out = paths.shorten_path(long, 30)
    assert len(out) == 30
    assert out.startswith("...")
    assert out.endswith("Wiedzmin 2")
    assert paths.shorten_path("short", 60) == "short"
    assert paths.shorten_path("", 60) == ""


def test_split_path_parts():
    assert paths.split_path_parts(r"E:\Gry\Wiedzmin 2", windows=True) == [
        "E:", "Gry", "Wiedzmin 2",
    ]
    assert paths.split_path_parts("/mnt/gry/wiedzmin", windows=False) == [
        "mnt", "gry", "wiedzmin",
    ]


def test_windows_detection_heuristics():
    assert paths.looks_like_windows_path(r"E:\Gry")
    assert paths.looks_like_windows_path(r"\\nas\share")
    assert not paths.looks_like_windows_path("/mnt/gry")
    assert paths.is_windows(None, r"E:\Gry") is True


def test_module_never_writes(fake_root):
    """Guard: the paths module must not create anything as a side effect."""
    before = sorted(os.listdir(str(fake_root)))
    paths.normalize_path(str(fake_root / "Wiedzmin 2"), windows=False)
    paths.path_key(str(fake_root), windows=False)
    paths.classify_folder_state(str(fake_root), windows=False)
    paths.db_path_for_root(str(fake_root), windows=False)
    paths.asset_dir_for_game(str(fake_root / "Wiedzmin 2"), windows=False)
    assert sorted(os.listdir(str(fake_root))) == before
