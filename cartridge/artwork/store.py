"""Local artwork store: where assets live and how they are named.

Layout, per game folder (brief section 11):

    <game folder>\\_cartridge\\
        assets\\
            cover.jpg
            background.jpg
            screenshot-01.jpg
            screenshot-02.jpg
            manifest.json
            thumbs\\
                cover.jpg
                screenshot-01.jpg

Rules this module exists to enforce:

* the app writes **only** inside ``_cartridge``; every path it hands back is
  checked against the game folder before it is used, so a hostile provider URL
  or a manipulated manifest cannot make it write elsewhere;
* nothing is stored by URL alone — the local file is the reference, the URL is
  attribution;
* a manifest is written next to the assets and contains **no credentials**
  (verified by ``tests/test_artwork.py``);
* thumbnails live in their own subdirectory and are disposable: deleting
  ``thumbs`` never loses artwork.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cartridge.core.clock import now_iso
from cartridge.core.models import ASSET_COVER, ASSET_SCREENSHOT, Asset
from cartridge.paths import (
    APP_ASSET_DIR,
    ASSET_MANIFEST_NAME,
    ASSET_SUBDIR,
    asset_dir_for_game,
    is_app_managed,
    is_under_root,
    normalize_path,
)

THUMB_SUBDIR = "thumbs"

# Maximum screenshots kept per game. A 2 GB machine with a mechanical disk does
# not want 40 full-size JPEGs per title; the import dialog offers a small set.
MAX_SCREENSHOTS = 4


@dataclass
class StorePaths(object):
    """Resolved, validated locations for one game's artwork."""

    game_folder: str
    asset_dir: str
    thumb_dir: str
    manifest_path: str

    def asset_path(self, filename: str) -> str:
        return os.path.join(self.asset_dir, filename)

    def thumb_path(self, filename: str) -> str:
        return os.path.join(self.thumb_dir, filename)


def paths_for_game(game_folder: str, windows: Optional[bool] = None) -> StorePaths:
    """Build (but do not create) the artwork paths for a game folder."""
    normalized = normalize_path(game_folder, windows)
    asset_dir = asset_dir_for_game(normalized, windows)
    return StorePaths(
        game_folder=normalized,
        asset_dir=asset_dir,
        thumb_dir=os.path.join(asset_dir, THUMB_SUBDIR),
        manifest_path=os.path.join(asset_dir, ASSET_MANIFEST_NAME),
    )


def filename_for(kind: str, index: int = 0, extension: str = ".jpg") -> str:
    """Deterministic asset naming: ``cover.jpg``, ``screenshot-02.jpg``."""
    ext = extension if extension.startswith(".") else "." + extension
    ext = ext.lower()
    if kind == ASSET_COVER:
        return "cover" + ext
    if kind == ASSET_SCREENSHOT:
        return "screenshot-%02d%s" % (max(1, int(index)), ext)
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in str(kind))
    return "%s%s" % (safe or "asset", ext)


class ArtworkStore(object):
    """Reads and writes artwork for one collection."""

    def __init__(self, windows: Optional[bool] = None):
        self.windows = windows

    # ------------------------------------------------------------------
    def paths_for(self, game_folder: str) -> StorePaths:
        """Resolved artwork paths for a game folder (no filesystem writes)."""
        return paths_for_game(game_folder, self.windows)

    def ensure_dirs(self, game_folder: str) -> StorePaths:
        """Create the application-owned artwork directories for a game."""
        paths = paths_for_game(game_folder, self.windows)
        for directory in (paths.asset_dir, paths.thumb_dir):
            if not os.path.isdir(directory):
                os.makedirs(directory)
        return paths

    def is_safe_path(self, game_folder: str, candidate: str) -> bool:
        """True only when ``candidate`` is inside the game's own asset folder."""
        paths = paths_for_game(game_folder, self.windows)
        normalized = normalize_path(candidate, self.windows)
        if not normalized:
            return False
        if not is_under_root(normalized, paths.asset_dir, self.windows):
            return False
        return is_app_managed(normalized, self.windows)

    def resolve(self, game_folder: str, rel_path: str) -> Optional[str]:
        """Turn a stored relative path into an absolute one, or None if unsafe.

        ``rel_path`` is stored **relative to the game folder**
        (``_cartridge/assets/cover.jpg``) so a collection stays portable when the
        user moves or renames the root. This method is the single place those
        paths become absolute, and it refuses anything that does not land inside
        that game's own ``_cartridge/assets`` directory: ``..`` segments, absolute
        paths, drive letters and UNC names all resolve to ``None``.
        """
        if not rel_path:
            return None
        text = str(rel_path).replace("\\", os.sep).replace("/", os.sep)
        if os.path.isabs(text):
            return None
        paths = paths_for_game(game_folder, self.windows)
        candidate = os.path.normpath(os.path.join(paths.game_folder, text))
        if not self.is_safe_path(game_folder, candidate):
            return None
        return candidate

    def temp_path(self, game_folder: str, filename: str) -> str:
        """Where an in-flight download is written before validation."""
        paths = paths_for_game(game_folder, self.windows)
        return os.path.join(paths.asset_dir, filename + ".part")

    def existing_files(self, game_folder: str) -> List[str]:
        """Relative paths of the asset files currently on disk."""
        paths = paths_for_game(game_folder, self.windows)
        if not os.path.isdir(paths.asset_dir):
            return []
        out: List[str] = []
        for name in sorted(os.listdir(paths.asset_dir)):
            full = os.path.join(paths.asset_dir, name)
            if not os.path.isfile(full):
                continue
            if name.endswith(".part"):
                continue
            out.append(name)
        return out

    def orphaned_part_files(self, game_folder: str) -> List[str]:
        """Leftovers from interrupted downloads, safe to delete."""
        paths = paths_for_game(game_folder, self.windows)
        if not os.path.isdir(paths.asset_dir):
            return []
        return [
            os.path.join(paths.asset_dir, name)
            for name in os.listdir(paths.asset_dir)
            if name.endswith(".part")
        ]

    def cleanup(self, game_folder: str) -> int:
        """Remove ``.part`` leftovers. Never touches validated artwork."""
        removed = 0
        for path in self.orphaned_part_files(game_folder):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                continue
        return removed

    # ------------------------------------------------------------------
    def finalize(
        self,
        game_folder: str,
        temp_file: str,
        kind: str,
        index: int = 0,
        extension: str = ".jpg",
    ) -> Optional[str]:
        """Move a validated temp file into place. Returns the relative path.

        ``os.replace`` is atomic on NTFS, so an interrupted import leaves either
        the previous asset or the new one - never a half-written cover that the
        browser would then try to decode.
        """
        if not os.path.exists(temp_file):
            return None
        paths = paths_for_game(game_folder, self.windows)
        if not os.path.isdir(paths.asset_dir):
            self.ensure_dirs(game_folder)
        filename = filename_for(kind, index, extension)
        destination = paths.asset_path(filename)
        try:
            os.replace(temp_file, destination)
        except OSError:
            return None
        rel = os.path.join(APP_ASSET_DIR, ASSET_SUBDIR, filename)
        self._drop_thumb(game_folder, filename)
        return rel.replace("\\", "/")

    def _drop_thumb(self, game_folder: str, filename: str) -> None:
        """Invalidate a stale thumbnail after its source changed."""
        paths = paths_for_game(game_folder, self.windows)
        for candidate in (
            os.path.splitext(filename)[0] + ".jpg",
            filename,
        ):
            thumb = os.path.join(paths.thumb_dir, candidate)
            if os.path.exists(thumb):
                try:
                    os.remove(thumb)
                except OSError:
                    pass

    def remove_asset(self, game_folder: str, rel_path: str) -> bool:
        """Delete one asset file (and its thumbnail) if it belongs to us."""
        absolute = self.resolve(game_folder, rel_path)
        if not absolute or not os.path.exists(absolute):
            return False
        try:
            os.remove(absolute)
        except OSError:
            return False
        self._drop_thumb(game_folder, os.path.basename(absolute))
        return True

    def remove_all(self, game_folder: str) -> int:
        """Delete the whole ``_cartridge`` folder for one game. Explicit only."""
        paths = paths_for_game(game_folder, self.windows)
        base = os.path.join(paths.game_folder, APP_ASSET_DIR)
        if not os.path.isdir(base):
            return 0
        if not is_app_managed(base, self.windows):
            return 0
        try:
            shutil.rmtree(base)
            return 1
        except OSError:
            return 0

    # ------------------------------------------------------------------
    def write_manifest(
        self, game_folder: str, assets: List[Asset], extra: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Write ``manifest.json`` next to the artwork.

        The manifest is a convenience for the user (and for a future re-import),
        not the source of truth - SQLite is. It must never contain a credential,
        so only whitelisted fields are written.
        """
        paths = self.ensure_dirs(game_folder)
        payload: Dict[str, Any] = {
            "app": "Cartridge",
            "manifest_version": 1,
            "written_at": now_iso(),
            "game_folder": os.path.basename(paths.game_folder),
            "assets": [
                {
                    "kind": asset.kind,
                    "rel_path": asset.rel_path,
                    "sha256": asset.sha256,
                    "width": asset.width,
                    "height": asset.height,
                    "bytes": asset.size_bytes,
                    "provider": asset.provider,
                    # The source URL is attribution, and it is public by design
                    # (it is an image CDN link). No key or token is ever present.
                    "source_url": asset.source_url,
                    "downloaded_at": asset.downloaded_at,
                }
                for asset in assets
            ],
        }
        if extra:
            payload["extra"] = extra
        temporary = paths.manifest_path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.replace(temporary, paths.manifest_path)
        except (OSError, TypeError, ValueError):
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass
            return None
        return paths.manifest_path

    def read_manifest(self, game_folder: str) -> Optional[Dict[str, Any]]:
        paths = paths_for_game(game_folder, self.windows)
        if not os.path.exists(paths.manifest_path):
            return None
        try:
            with open(paths.manifest_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
