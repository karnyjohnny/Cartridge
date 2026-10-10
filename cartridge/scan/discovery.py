r"""Root scanning: discover game folders and reconcile them with the catalogue.

Scan policy (brief section 7 and DECISIONS D-007):

* **One level deep.** Only the immediate children of each configured root are
  candidates. A collection root like ``E:\Gry`` holds one folder per game;
  recursing into every file on every launch would take minutes on a mechanical
  disk and buy nothing.
* **Application-managed directories are excluded** (``.cartridge``,
  ``_cartridge``), as are Windows system folders and dot-folders, so the app
  never catalogues itself.
* **Nothing is executed, moved, renamed or deleted.** Discovery reads names and
  states only. Content-type hints come from
  :mod:`cartridge.scan.content_types`, which also only reads names.
* **A per-folder listing is bounded** (``max_entries_per_folder``) because a
  single game folder can hold tens of thousands of files.
* **Errors are collected, not raised.** One unreadable folder must not abort a
  scan of 2,000 others.

Reconciliation never deletes a record: a folder that vanished is marked
``missing``, and a folder on a drive that is not mounted is marked
``unavailable_drive`` — different situations, different remedies (section 13).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from cartridge.core.clock import monotonic, now_iso
from cartridge.core.models import ScanSummary
from cartridge.paths import (
    APP_MANAGED_NAMES,
    FolderState,
    classify_folder_state,
    folder_name,
    is_app_managed,
    path_key,
)
from cartridge.scan.content_types import (
    DEFAULT_MAX_ENTRIES,
    TypeSuggestion,
    has_executable,
    suggest_content_types,
)

# Windows system folders that are never games. Compared case-insensitively on
# Windows, exactly on POSIX.
SYSTEM_FOLDER_NAMES = frozenset(
    name.lower()
    for name in (
        "$RECYCLE.BIN", "System Volume Information", "found.000", "found.001",
        "Config.Msi", "RECYCLER", "lost+found", ".Trashes", ".Spotlight-V100",
        ".fseventsd", "desktop.ini",
    )
)


@dataclass
class DiscoveredFolder(object):
    """One candidate game folder found under a root."""

    path: str
    name: str
    key: str = ""
    state: str = FolderState.PRESENT.value
    suggestions: List[TypeSuggestion] = field(default_factory=list)
    entry_count: int = 0
    listing_truncated: bool = False
    has_executable: bool = False
    is_hidden: bool = False
    error: Optional[str] = None

    @property
    def suggested_names(self) -> List[str]:
        return [suggestion.name for suggestion in self.suggestions]


@dataclass
class DiscoveryResult(object):
    """Everything one root scan produced."""

    root: str = ""
    root_state: str = FolderState.UNKNOWN.value
    folders: List[DiscoveredFolder] = field(default_factory=list)
    skipped_app_managed: int = 0
    skipped_system: int = 0
    skipped_hidden: int = 0
    errors: List[str] = field(default_factory=list)
    entries_inspected: int = 0
    duration_ms: float = 0.0
    listing_limit: int = DEFAULT_MAX_ENTRIES

    @property
    def ok(self) -> bool:
        return self.root_state == FolderState.PRESENT.value


def _should_skip(name: str, windows: bool) -> str:
    """Return a skip reason, or "" when the entry is a candidate.

    The comparison is case-insensitive on every platform: these are Windows
    system folder names, and matching them exactly on POSIX would silently let
    ``System Volume Information`` into the catalogue when a Windows collection is
    mounted or copied.
    """
    if not name:
        return "empty"
    compare = name.lower()
    if compare in _APP_MANAGED_LOWER:
        return "app-managed"
    if compare in SYSTEM_FOLDER_NAMES:
        return "system"
    if name.startswith("."):
        return "hidden"
    return ""


_APP_MANAGED_LOWER = frozenset(name.lower() for name in APP_MANAGED_NAMES)


def discover_root(
    root: str,
    windows: Optional[bool] = None,
    max_entries_per_folder: int = DEFAULT_MAX_ENTRIES,
    detect_types: bool = True,
    include_hidden: bool = False,
    cancel: Optional[Callable[[], bool]] = None,
) -> DiscoveryResult:
    """Scan one root directory (one level) and describe what was found."""
    from cartridge.paths import is_windows as _is_windows
    from cartridge.paths import normalize_path

    started = monotonic()
    normalized = normalize_path(root, windows)
    win = _is_windows(windows, normalized)
    result = DiscoveryResult(
        root=normalized, listing_limit=int(max_entries_per_folder)
    )
    if not normalized:
        result.root_state = FolderState.UNKNOWN.value
        result.errors.append("No root path was given.")
        result.duration_ms = round((monotonic() - started) * 1000.0, 1)
        return result

    state = classify_folder_state(normalized, win)
    result.root_state = state.value
    if state != FolderState.PRESENT:
        result.errors.append(
            {
                FolderState.MISSING.value: "The root folder does not exist: %s",
                FolderState.UNAVAILABLE_DRIVE.value:
                    "The drive for this root is not available: %s",
                FolderState.ACCESS_DENIED.value:
                    "Access to this root was denied: %s",
                FolderState.NOT_A_DIRECTORY.value:
                    "The root path is not a folder: %s",
            }.get(state.value, "The root folder cannot be used: %s") % normalized
        )
        result.duration_ms = round((monotonic() - started) * 1000.0, 1)
        return result

    try:
        entries = list(os.scandir(normalized))
    except PermissionError:
        result.root_state = FolderState.ACCESS_DENIED.value
        result.errors.append("Access to %s was denied." % normalized)
        result.duration_ms = round((monotonic() - started) * 1000.0, 1)
        return result
    except OSError as exc:
        result.errors.append("Could not list %s: %s" % (normalized, exc))
        result.duration_ms = round((monotonic() - started) * 1000.0, 1)
        return result

    for entry in entries:
        if cancel is not None and cancel():
            result.errors.append("Scan cancelled by the user.")
            break
        try:
            name = entry.name
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError as exc:
            result.errors.append("Could not inspect an entry in %s: %s" % (normalized, exc))
            continue

        result.entries_inspected += 1
        if not is_dir:
            continue

        reason = _should_skip(name, win)
        if reason == "app-managed":
            result.skipped_app_managed += 1
            continue
        if reason == "system":
            result.skipped_system += 1
            continue
        if reason == "hidden":
            result.skipped_hidden += 1
            if not include_hidden:
                continue
        elif reason == "empty":
            continue

        full_path = os.path.join(normalized, name)
        if is_app_managed(full_path, win):
            result.skipped_app_managed += 1
            continue

        discovered = DiscoveredFolder(
            path=full_path,
            name=name,
            key=path_key(full_path, win),
            state=classify_folder_state(full_path, win).value,
            is_hidden=name.startswith("."),
        )
        if detect_types:
            _inspect_shallow(discovered, max_entries_per_folder, result)
        result.folders.append(discovered)

    result.duration_ms = round((monotonic() - started) * 1000.0, 1)
    return result


def _inspect_shallow(
    discovered: DiscoveredFolder, limit: int, result: DiscoveryResult
) -> None:
    """Read up to ``limit`` names from one game folder to hint at content types."""
    names: List[str] = []
    try:
        with os.scandir(discovered.path) as iterator:
            for entry in iterator:
                if len(names) >= limit:
                    discovered.listing_truncated = True
                    break
                try:
                    names.append(entry.name)
                except OSError:
                    continue
    except PermissionError:
        discovered.error = "access denied while listing the folder"
        discovered.state = FolderState.ACCESS_DENIED.value
        result.errors.append(
            "Access denied while listing %s" % discovered.path
        )
        return
    except OSError as exc:
        discovered.error = "could not list the folder: %s" % exc
        result.errors.append("Could not list %s: %s" % (discovered.path, exc))
        return

    discovered.entry_count = len(names)
    discovered.has_executable = has_executable(names)
    discovered.suggestions = suggest_content_types(names, discovered.name)


def reconcile(
    repo: Any,
    result: DiscoveryResult,
    root_id: Optional[int],
    windows: Optional[bool] = None,
    apply_suggestions: bool = True,
) -> ScanSummary:
    """Write a discovery result into the catalogue. Returns a summary.

    Rules:
      * a known folder updates its stored spelling and state (never its metadata);
      * an unknown folder becomes a new record with a folder-derived title;
      * a record whose folder was not seen in this scan is re-probed and marked,
        never deleted;
      * suggested content types are attached only when the user has not already
        chosen types for that game.
    """
    summary = ScanSummary(root_path=result.root)
    started = monotonic()
    summary.started_at = now_iso()

    seen_keys: Set[str] = set()
    for discovered in result.folders:
        seen_keys.add(discovered.key)
        state = discovered.state or FolderState.PRESENT.value
        game, created = repo.upsert_folder(
            discovered.path, root_id=root_id, state=state
        )
        if game is None:
            summary.errors.append("Could not store %s" % discovered.path)
            continue
        if created:
            summary.new_folders += 1
        else:
            summary.matched += 1
        if apply_suggestions and discovered.suggested_names:
            existing = set(game.content_types)
            user_types = repo.db.query(
                "SELECT 1 FROM game_content_types gct"
                " JOIN content_types ct ON ct.id = gct.content_type_id"
                " WHERE gct.game_id = ? AND gct.source = 'user' LIMIT 1",
                (game.id,),
            )
            if not user_types and not existing:
                repo.set_game_content_types(
                    game.id, discovered.suggested_names, source="suggested"
                )

    # Records under this root that were not seen: figure out why, and say so.
    if root_id is None:
        rows = repo.db.query(
            "SELECT id, folder_path, folder_key FROM games WHERE root_id IS NULL"
        )
    else:
        rows = repo.db.query(
            "SELECT id, folder_path, folder_key FROM games WHERE root_id = ?",
            (root_id,),
        )
    for row in rows:
        if row["folder_key"] in seen_keys:
            continue
        state = classify_folder_state(row["folder_path"], windows)
        repo.set_folder_state(row["id"], state.value)
        if state == FolderState.MISSING:
            summary.missing_folders += 1
        elif state == FolderState.UNAVAILABLE_DRIVE:
            summary.unavailable_drives += 1

    summary.folders_seen = result.entries_inspected
    summary.unmatched = len(
        [
            discovered
            for discovered in result.folders
            if _is_unmatched(repo, discovered)
        ]
    )
    summary.app_managed_skipped = result.skipped_app_managed
    summary.errors.extend(result.errors)
    summary.duration_ms = round((monotonic() - started) * 1000.0, 1)
    summary.finished_at = now_iso()
    return summary


def _is_unmatched(repo: Any, discovered: DiscoveredFolder) -> bool:
    game = repo.get_game_by_folder(discovered.path)
    if game is None:
        return True
    return game.metadata_state in ("none", "error")


def discover_all_roots(
    roots: Sequence[str],
    windows: Optional[bool] = None,
    max_entries_per_folder: int = DEFAULT_MAX_ENTRIES,
    cancel: Optional[Callable[[], bool]] = None,
) -> List[DiscoveryResult]:
    """Scan several roots. Each root is independent: one failure does not stop
    the others."""
    out = []
    for root in roots or ():
        out.append(
            discover_root(
                root,
                windows=windows,
                max_entries_per_folder=max_entries_per_folder,
                cancel=cancel,
            )
        )
    return out


def folder_display_name(discovered: DiscoveredFolder) -> str:
    """Title suggestion for a brand-new record: the folder name, tidied."""
    name = discovered.name or folder_name(discovered.path)
    # Scene-style dots and underscores read badly as a title.
    cleaned = name.replace("_", " ").replace(".", " ")
    cleaned = " ".join(cleaned.split())
    return cleaned or name
