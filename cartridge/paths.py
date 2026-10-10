"""Path handling: normalization, identity keys and folder state classification.

Windows semantics matter here even when the code runs elsewhere, because the
product target is Windows 7 and the tests must be able to exercise Windows path
rules on a Linux CI box. Every function therefore takes an explicit ``windows``
flag that defaults to "detect from the path, then from the OS".

Hard rules enforced by this module (brief sections 7 and 9):

* a game record's identity is the *normalized folder path*, never the title;
* Windows paths are case-insensitive and separator-agnostic;
* application-managed directories (``.cartridge``, ``_cartridge``) are excluded
  from discovery so the app never catalogues its own database or artwork;
* a missing folder is distinguished from an unavailable drive and from a
  permission error - they are different problems with different remedies.

This module performs no writes and never executes anything it finds.
"""

from __future__ import annotations

import os
import re
from enum import Enum
from typing import List, Optional

# Directory names owned by the application. The database lives in
# <root>\.cartridge\ and downloaded artwork lives in <game>\_cartridge\assets\.
APP_DATA_DIR = ".cartridge"
APP_ASSET_DIR = "_cartridge"
APP_MANAGED_NAMES = (APP_DATA_DIR, APP_ASSET_DIR)

DB_FILE_NAME = "collection.db"
ASSET_SUBDIR = "assets"
ASSET_MANIFEST_NAME = "manifest.json"

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]?$")
_DRIVE_PREFIX_RE = re.compile(r"^([A-Za-z]):")
_UNC_PREFIXES = ("\\\\", "//")
_WIN_LONG_PREFIX = "\\\\?\\"


class FolderState(str, Enum):
    """Filesystem state of a catalogue entry's folder."""

    PRESENT = "present"
    MISSING = "missing"
    UNAVAILABLE_DRIVE = "unavailable_drive"
    ACCESS_DENIED = "access_denied"
    NOT_A_DIRECTORY = "not_a_directory"
    UNKNOWN = "unknown"


def looks_like_windows_path(path: str) -> bool:
    """Heuristic: does this string use Windows path syntax?"""
    if not path:
        return False
    if _DRIVE_PREFIX_RE.match(path):
        return True
    if path.startswith(_UNC_PREFIXES):
        return True
    return "\\" in path


def is_windows(windows: Optional[bool] = None, path: str = "") -> bool:
    """Resolve the effective platform for a path operation."""
    if windows is not None:
        return bool(windows)
    if path and looks_like_windows_path(path):
        return True
    return os.name == "nt"


def separators(path: str, windows: Optional[bool] = None) -> str:
    """Unify separators to the platform-native one."""
    win = is_windows(windows, path)
    if win:
        return path.replace("/", "\\")
    return path.replace("\\", "/")


def normalize_path(path: str, windows: Optional[bool] = None) -> str:
    """Return a canonical, case-preserving form of ``path``.

    Expands ``~`` and environment variables, unifies separators, collapses
    ``.``/``..`` and duplicate separators, and removes a trailing separator
    (except for a drive or UNC root, where it is significant).
    """
    if path is None:
        return ""
    text = str(path).strip()
    if not text:
        return ""

    win = is_windows(windows, text)
    text = os.path.expanduser(text)
    # expandvars is safe: it only substitutes ${VAR}/%VAR% and leaves unknown
    # names untouched. Never applied to data coming from a provider response.
    text = os.path.expandvars(text)
    text = separators(text, win)

    long_prefix = ""
    if win and text.startswith(_WIN_LONG_PREFIX):
        long_prefix = _WIN_LONG_PREFIX
        text = text[len(_WIN_LONG_PREFIX):]

    if win:
        import ntpath

        drive_match = _DRIVE_RE.match(text)
        if drive_match:
            # A bare drive root is already canonical; normpath would strip the
            # trailing separator, and "E:" (cwd-relative) != "E:\\" (drive root).
            text = text[:2] + "\\"
        else:
            text = ntpath.normpath(text)
    else:
        import posixpath

        text = posixpath.normpath(text)

    text = long_prefix + text

    # normpath keeps a trailing slash on roots; strip it elsewhere so that
    # "E:\\Gry\\Wiedmin" and "E:\\Gry\\Wiedmin\\" share one identity.
    if not is_root_path(text, win):
        text = text.rstrip("\\/" if win else "/")
        if win:
            text = text.rstrip("\\")
    return text


def is_root_path(path: str, windows: Optional[bool] = None) -> bool:
    """True for ``C:\\``, ``\\\\server\\share`` or ``/``.

    Collapses duplicate separators first, so ``"E:\\\\"`` is a root just like
    ``"E:\\"``. Does its own normalization rather than calling
    :func:`normalize_path`, because ``normalize_path`` consults this function.
    """
    if not path:
        return False
    win = is_windows(windows, path)
    text = separators(str(path).strip(), win)
    if not text:
        return False

    if win:
        import ntpath

        if text.startswith(_WIN_LONG_PREFIX):
            text = text[len(_WIN_LONG_PREFIX):]
        text = ntpath.normpath(text)
        if _DRIVE_RE.match(text):
            return True
        if text.startswith("\\\\"):
            # A UNC root is exactly \\server\share, with or without a
            # trailing backslash - anything deeper is a real directory.
            parts = [part for part in text.split("\\") if part]
            return len(parts) == 2
        return False

    import posixpath

    text = posixpath.normpath(text)
    return text == "/"


def join_path(*parts, **kwargs):
    """Platform-correct path join.

    ``os.path.join`` would emit forward slashes on Linux even for a Windows
    target path, which would corrupt the stored folder identity. Keyword-only
    ``windows`` keeps the signature Python 3.8 friendly.
    """
    windows = kwargs.pop("windows", None)
    if kwargs:
        raise TypeError("unexpected keyword arguments: %s" % sorted(kwargs))
    cleaned = [str(part) for part in parts if part not in (None, "")]
    if not cleaned:
        return ""
    win = is_windows(windows, cleaned[0])
    sep = "\\" if win else "/"
    out = cleaned[0].rstrip("\\/" if win else "/") if len(cleaned) > 1 else cleaned[0]
    if len(cleaned) > 1 and is_root_path(cleaned[0], win):
        out = cleaned[0]
    for part in cleaned[1:]:
        part = separators(part, win).strip("\\/" if win else "/")
        if not part:
            continue
        if not out.endswith(sep):
            out += sep
        out += part
    return out


def path_key(path: str, windows: Optional[bool] = None) -> str:
    """Identity key used for uniqueness constraints in SQLite.

    Windows: upper-cased, backslash separators. POSIX: case-sensitive, forward
    slashes. Two spellings of the same folder always produce the same key on
    Windows, which is what prevents duplicate records after a rescan.
    """
    normalized = normalize_path(path, windows)
    if not normalized:
        return ""
    win = is_windows(windows, normalized)
    if win:
        return normalized.upper().replace("/", "\\")
    return normalized


def drive_letter(path: str) -> Optional[str]:
    """Return the upper-case drive letter (``"E"``) or None for UNC/POSIX."""
    match = _DRIVE_PREFIX_RE.match(str(path).strip())
    if match:
        return match.group(1).upper()
    text = str(path).strip()
    if text.startswith(_WIN_LONG_PREFIX):
        match = _DRIVE_PREFIX_RE.match(text[len(_WIN_LONG_PREFIX):])
        if match:
            return match.group(1).upper()
    return None


def drive_root(path: str) -> Optional[str]:
    """Return ``E:\\`` for a drive path, else None."""
    letter = drive_letter(path)
    if letter:
        return letter + ":\\"
    return None


def is_under_root(path: str, root: str, windows: Optional[bool] = None) -> bool:
    """True when ``path`` is ``root`` itself or lives inside it."""
    key = path_key(path, windows)
    root_key = path_key(root, windows)
    if not key or not root_key:
        return False
    if key == root_key:
        return True
    prefix = root_key if root_key.endswith(("\\", "/")) else root_key + (
        "\\" if is_windows(windows, root_key) else "/"
    )
    return key.startswith(prefix)


def is_app_managed(path: str, windows: Optional[bool] = None) -> bool:
    """True when the path is inside an application-owned directory.

    Used to keep ``.cartridge`` (database) and ``_cartridge`` (artwork) out of
    game discovery, and to guarantee the app only ever writes inside them.
    """
    normalized = normalize_path(path, windows)
    if not normalized:
        return False
    win = is_windows(windows, normalized)
    parts = [p for p in re.split(r"[\\/]+", normalized) if p]
    for part in parts:
        compare = part.upper() if win else part
        for managed in APP_MANAGED_NAMES:
            target = managed.upper() if win else managed
            if compare == target:
                return True
    return False


def folder_name(path: str, windows: Optional[bool] = None) -> str:
    """Last component of a path (the folder's own name)."""
    normalized = normalize_path(path, windows)
    if not normalized or is_root_path(normalized, windows):
        return normalized
    win = is_windows(windows, normalized)
    return re.split(r"[\\/]+", normalized.rstrip("\\/"))[-1]


def shorten_path(path: str, max_len: int = 60) -> str:
    """Abbreviate a long path for the UI, keeping the tail (the useful part)."""
    if not path:
        return ""
    if len(path) <= max_len:
        return path
    if max_len <= 4:
        return path[-max_len:]
    return "..." + path[-(max_len - 3):]


def classify_folder_state(
    path: str, windows: Optional[bool] = None, stat_result: Optional[bool] = None
) -> FolderState:
    """Classify a folder without raising and without touching its contents.

    ``stat_result`` exists for tests: passing True/False simulates the outcome of
    the directory probe so Windows-only branches can be covered on any platform.
    """
    normalized = normalize_path(path, windows)
    if not normalized:
        return FolderState.UNKNOWN

    win = is_windows(windows, normalized)

    # Distinguish "folder deleted/renamed" from "the whole drive is gone" -
    # the second one usually means an unplugged USB/eSATA disk or a mapped
    # network share, and deleting metadata for it would be a data-loss bug.
    if win:
        root = drive_root(normalized)
        if root:
            drive_ok = stat_result if stat_result is not None else _probe(root)
            if drive_ok is False:
                # Drive root missing: check whether the folder itself is visible
                folder_ok = _probe(normalized)
                if folder_ok is None:
                    return FolderState.ACCESS_DENIED
                if folder_ok is False:
                    return FolderState.UNAVAILABLE_DRIVE

    probe = stat_result if stat_result is not None else _probe(normalized)
    if probe is None:
        return FolderState.ACCESS_DENIED
    if probe is True:
        return FolderState.PRESENT
    return FolderState.MISSING


def _probe(path: str) -> Optional[bool]:
    """Return True (is a directory), False (absent), None (permission error)."""
    try:
        return os.path.isdir(path)
    except (PermissionError, OSError):
        try:
            os.stat(path)
            return None
        except PermissionError:
            return None
        except OSError:
            return False


def app_data_dir(root: str, windows: Optional[bool] = None) -> str:
    """``<root>\\.cartridge`` - where the collection database lives."""
    return join_path(normalize_path(root, windows), APP_DATA_DIR, windows=windows)


def db_path_for_root(root: str, windows: Optional[bool] = None) -> str:
    """``<root>\\.cartridge\\collection.db``."""
    return join_path(app_data_dir(root, windows), DB_FILE_NAME, windows=windows)


def asset_dir_for_game(game_folder: str, windows: Optional[bool] = None) -> str:
    """``<game folder>\\_cartridge\\assets`` - artwork lives with the game."""
    return join_path(
        normalize_path(game_folder, windows),
        APP_ASSET_DIR,
        ASSET_SUBDIR,
        windows=windows,
    )


def split_path_parts(path: str, windows: Optional[bool] = None) -> List[str]:
    """Split into components, keeping the drive/UNC root as the first item."""
    normalized = normalize_path(path, windows)
    if not normalized:
        return []
    win = is_windows(windows, normalized)
    sep = "\\" if win else "/"
    parts = [p for p in normalized.split(sep) if p]
    return parts
