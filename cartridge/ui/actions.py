"""OS actions: opening a folder, revealing a file, launching a game.

Safety rules from brief sections 1 and 7, implemented here once so every caller
inherits them:

* the target must **exist** and be a directory before anything is opened;
* paths are passed as an argument list, never through a shell, so a folder named
  ``foo & del /f`` cannot become a command;
* Cartridge **never executes a discovered file automatically**. ``launch_game``
  requires an explicit, user-selected executable path and is only ever called from
  a deliberate UI action;
* failures return a message the status bar can show instead of raising into the
  event loop.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional, Tuple

from cartridge.paths import FolderState, classify_folder_state, normalize_path

# Windows system executables used to open a folder. Resolved through the
# environment rather than hard-coded to C:\Windows so a relocated system root
# still works.
_EXPLORER_NAMES = ("explorer.exe",)


def _explorer_path() -> str:
    windir = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.join(windir, "explorer.exe")


def open_folder(path: str, parent=None) -> Tuple[bool, str]:
    """Open a folder in the platform file manager.

    Returns ``(ok, message)``; the message is safe to show to the user and never
    contains anything but the path itself.
    """
    normalized = normalize_path(path)
    if not normalized:
        return False, "No folder to open."

    state = classify_folder_state(normalized)
    if state == FolderState.UNAVAILABLE_DRIVE:
        return False, (
            "The drive holding this folder is not available. Reconnect it and try "
            "again — nothing was changed in the catalogue."
        )
    if state == FolderState.ACCESS_DENIED:
        return False, "Access to that folder was denied by Windows."
    if state != FolderState.PRESENT:
        return False, "That folder no longer exists on disk."

    try:
        if sys.platform.startswith("win"):
            # `explorer.exe <dir>` — argument list, no shell, no quoting bugs.
            subprocess.Popen([_explorer_path(), normalized])
            return True, "Opened %s" % _basename(normalized)
        if sys.platform == "darwin":
            subprocess.Popen(["open", normalized])
            return True, "Opened %s" % _basename(normalized)
        # Linux/BSD development hosts
        opener = _first_available(("xdg-open", "gio", "nautilus", "thunar"))
        if opener is None:
            return False, "No file manager launcher is available on this system."
        args = [opener, "open", normalized] if os.path.basename(opener) == "gio" else [opener, normalized]
        subprocess.Popen(args)
        return True, "Opened %s" % _basename(normalized)
    except OSError as exc:
        return False, "Could not open the folder: %s" % exc


def reveal_file(path: str, parent=None) -> Tuple[bool, str]:
    """Open the containing folder, selecting the file where the OS supports it."""
    normalized = normalize_path(path)
    if not normalized:
        return False, "No file to reveal."
    directory = os.path.dirname(normalized) or normalized
    if sys.platform.startswith("win") and os.path.isfile(normalized):
        try:
            subprocess.Popen([_explorer_path(), "/select,", normalized])
            return True, "Revealed %s" % os.path.basename(normalized)
        except OSError as exc:
            return False, "Could not reveal the file: %s" % exc
    return open_folder(directory, parent)


def launch_game(executable: str, working_directory: Optional[str] = None) -> Tuple[bool, str]:
    """Run a game executable — only ever from an explicit user action.

    The path must have been chosen by the user and must exist. Nothing in this
    application calls this function as a result of scanning: discovering a file
    is never a reason to execute it.
    """
    normalized = normalize_path(executable)
    if not normalized:
        return False, "No executable was selected."
    if not os.path.isfile(normalized):
        return False, "That executable does not exist."
    if not normalized.lower().endswith((".exe", ".bat", ".cmd", ".com")):
        return False, "Only Windows executables can be launched."

    cwd = working_directory or os.path.dirname(normalized)
    if cwd and not os.path.isdir(cwd):
        cwd = None
    try:
        # cwd is passed explicitly and no shell is involved, so a path containing
        # spaces or shell metacharacters cannot be misinterpreted.
        subprocess.Popen([normalized], cwd=cwd, shell=False)
        return True, "Launched %s" % os.path.basename(normalized)
    except OSError as exc:
        return False, "Could not start the game: %s" % exc


def pick_executable_in(folder: str) -> Optional[str]:
    """Suggest the most likely game executable in a folder.

    Advisory only: the result is shown to the user for confirmation and is never
    executed automatically. Preference goes to a launcher-ish name, then to the
    largest .exe in the top level, which is usually the game rather than an
    uninstaller or a redistributable installer.
    """
    if not folder or not os.path.isdir(folder):
        return None
    preferred = ("launch", "start", "play", "run", "game")
    avoid = ("unins", "uninstall", "setup", "install", "redist", "vcredist",
             "directx", "dxsetup", "crash", "report", "updater", "patch")
    candidates = []
    try:
        with os.scandir(folder) as iterator:
            for entry in iterator:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                name = entry.name
                if not name.lower().endswith(".exe"):
                    continue
                lowered = name.lower()
                if any(token in lowered for token in avoid):
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                score = 2 if any(token in lowered for token in preferred) else 1
                stem_matches_folder = _stem_matches(lowered, folder)
                if stem_matches_folder:
                    score += 3
                candidates.append((score, size, entry.path))
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], -item[1]))
    return candidates[0][2]


def _stem_matches(executable_name: str, folder: str) -> bool:
    stem = os.path.splitext(executable_name)[0]
    base = os.path.basename(folder.rstrip("\\/")).lower().replace(" ", "")
    stem = stem.lower().replace(" ", "")
    if not stem or not base:
        return False
    return stem in base or base in stem


def _basename(path: str) -> str:
    return os.path.basename(path.rstrip("\\/")) or path


def _first_available(names) -> Optional[str]:
    from shutil import which

    for name in names:
        found = which(name)
        if found:
            return found
    return None
