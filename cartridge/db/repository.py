"""Repository: every SQL statement in the application lives here.

Rules this module enforces (brief section 8):

* **Parameterized SQL only** — no value from a folder name, provider response or
  settings field is ever interpolated into SQL text. The only interpolated
  fragments are ``?`` placeholders and identifiers from closed registries in
  :mod:`cartridge.core.filtering`.
* **Foreign keys on**, children cascade from ``games``.
* **Explicit transactions** for imports and multi-row edits, so a failed import
  cannot leave a game that looks complete while its assets are missing.
* **User edits are untouchable by provider refreshes.** ``apply_provider_metadata``
  writes only provider columns; the ``user_*`` columns are written solely by
  ``update_user_fields``.
* **No silent deletes.** A vanished folder updates ``folder_state``; deleting a
  record requires an explicit ``delete_game`` call from a confirmed UI action.

Reads are batched: listing N games costs 5 queries, not 1 + 5N.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cartridge.core import filtering
from cartridge.core.clock import now_iso
from cartridge.core.models import (
    STATE_COMPLETE,
    STATE_MANUAL,
    STATE_NONE,
    STATE_PARTIAL,
    Asset,
    ContentType,
    Game,
    Root,
)
from cartridge.db.connection import Database, DatabaseError
from cartridge.db.schema import SECRET_SETTING_KEYS
from cartridge.paths import FolderState, folder_name, path_key
from cartridge.text import normalize_title, search_key


class SecretInDatabaseError(DatabaseError):
    """Raised when something tries to persist a credential in SQLite."""


# Columns a provider refresh may write. user_* columns are conspicuously absent.
PROVIDER_COLUMNS = (
    "title", "sort_title", "summary", "release_date", "release_year", "rating",
    "age_rating", "franchise", "developer", "publisher", "provider",
    "provider_id", "provider_url", "metadata_state", "metadata_error",
    "last_refreshed_at",
)

USER_COLUMNS = (
    "user_title", "user_summary", "user_release_year", "user_rating",
    "user_developer", "user_publisher", "user_franchise", "user_age_rating",
)

GAME_COLUMNS = (
    "id, folder_path, folder_key, root_id, title, sort_title, summary, "
    "release_date, release_year, rating, age_rating, franchise, developer, "
    "publisher, user_title, user_summary, user_release_year, user_rating, "
    "user_developer, user_publisher, user_franchise, user_age_rating, favourite, "
    "notes, search_blob, provider, provider_id, provider_url, metadata_state, "
    "metadata_error, folder_state, folder_state_checked_at, folder_size_bytes, "
    "folder_size_measured_at, added_at, updated_at, last_verified_at, "
    "last_refreshed_at"
)


def _sort_title(title: str) -> str:
    """Move a leading article to the end so "The Witcher" sorts under W."""
    text = (title or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    for article in ("the ", "a ", "an ", "le ", "la ", "les ", "der ", "die ", "das "):
        if lowered.startswith(article) and len(text) > len(article):
            return "%s, %s" % (text[len(article):], article.strip())
    return text


class Repository(object):
    """Data access for the whole application."""

    def __init__(self, db: Database, windows: Optional[bool] = None):
        self.db = db
        self.windows = windows

    # ==================================================================
    # roots
    # ==================================================================
    def add_root(self, path: str, label: Optional[str] = None) -> Root:
        from cartridge.paths import normalize_path

        normalized = normalize_path(path, self.windows)
        key = path_key(normalized, self.windows)
        stamp = now_iso()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM roots WHERE path_key = ?", (key,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE roots SET path = ?, label = COALESCE(?, label), enabled = 1 "
                    "WHERE id = ?",
                    (normalized, label, existing["id"]),
                )
                root_id = existing["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO roots (path, path_key, label, enabled, added_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (normalized, key, label or folder_name(normalized, self.windows), stamp),
                )
                root_id = cursor.lastrowid
        return self.get_root(root_id)

    def list_roots(self, enabled_only: bool = False) -> List[Root]:
        sql = "SELECT * FROM roots"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY path ASC"
        return [self._row_to_root(row) for row in self.db.query(sql)]

    def get_root(self, root_id: int) -> Optional[Root]:
        row = self.db.query_one("SELECT * FROM roots WHERE id = ?", (root_id,))
        return self._row_to_root(row) if row else None

    def remove_root(self, root_id: int, delete_games: bool = False) -> int:
        """Detach a root. Games are kept (marked root_id NULL) unless asked."""
        with self.db.transaction() as conn:
            if delete_games:
                conn.execute(
                    "DELETE FROM games WHERE root_id = ?", (root_id,)
                )
            else:
                conn.execute(
                    "UPDATE games SET root_id = NULL WHERE root_id = ?", (root_id,)
                )
            conn.execute("DELETE FROM roots WHERE id = ?", (root_id,))
        return 0

    def mark_root_scanned(self, root_id: int) -> None:
        self.db.execute(
            "UPDATE roots SET last_scanned_at = ? WHERE id = ?", (now_iso(), root_id)
        )

    def _row_to_root(self, row: sqlite3.Row) -> Root:
        return Root(
            id=row["id"],
            path=row["path"],
            path_key=row["path_key"],
            label=row["label"],
            enabled=bool(row["enabled"]),
            added_at=row["added_at"],
            last_scanned_at=row["last_scanned_at"],
        )

    # ==================================================================
    # content types (user-defined data - add/rename/remove without code changes)
    # ==================================================================
    def list_content_types(self, with_counts: bool = True) -> List[ContentType]:
        sql = (
            "SELECT ct.*, (SELECT COUNT(*) FROM game_content_types gct"
            " WHERE gct.content_type_id = ct.id) AS game_count"
            " FROM content_types ct ORDER BY ct.sort_order ASC, ct.name ASC"
            if with_counts
            else "SELECT *, 0 AS game_count FROM content_types ct"
                 " ORDER BY sort_order ASC, name ASC"
        )
        out = []
        for row in self.db.query(sql):
            out.append(
                ContentType(
                    id=row["id"],
                    name=row["name"],
                    name_key=row["name_key"],
                    color=row["color"],
                    sort_order=row["sort_order"],
                    created_at=row["created_at"],
                    game_count=int(row["game_count"] or 0),
                )
            )
        return out

    def add_content_type(self, name: str, color: Optional[str] = None) -> ContentType:
        clean = (name or "").strip()
        if not clean:
            raise ValueError("content type name must not be empty")
        key = clean.lower()
        stamp = now_iso()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM content_types WHERE name_key = ?", (key,)
            ).fetchone()
            if row:
                # Whitespace/case-only duplicates collapse onto the existing type
                # instead of creating a near-identical second one (§9).
                conn.execute(
                    "UPDATE content_types SET name = ?, color = COALESCE(?, color) WHERE id = ?",
                    (clean, color, row["id"]),
                )
                type_id = row["id"]
            else:
                order = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM content_types"
                ).fetchone()[0]
                cursor = conn.execute(
                    "INSERT INTO content_types (name, name_key, color, sort_order, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (clean, key, color, order, stamp),
                )
                type_id = cursor.lastrowid
        found = self.db.query_one("SELECT * FROM content_types WHERE id = ?", (type_id,))
        return ContentType(
            id=found["id"], name=found["name"], name_key=found["name_key"],
            color=found["color"], sort_order=found["sort_order"],
            created_at=found["created_at"],
        )

    def rename_content_type(self, type_id: int, new_name: str) -> bool:
        clean = (new_name or "").strip()
        if not clean:
            raise ValueError("content type name must not be empty")
        key = clean.lower()
        with self.db.transaction() as conn:
            clash = conn.execute(
                "SELECT id FROM content_types WHERE name_key = ? AND id != ?",
                (key, type_id),
            ).fetchone()
            if clash:
                raise ValueError(
                    "another content type already uses the name %r" % clean
                )
            conn.execute(
                "UPDATE content_types SET name = ?, name_key = ? WHERE id = ?",
                (clean, key, type_id),
            )
        return True

    def delete_content_type(self, type_id: int) -> None:
        """Removes the type and its links. Games themselves are untouched."""
        self.db.execute("DELETE FROM content_types WHERE id = ?", (type_id,))

    def set_game_content_types(
        self, game_id: int, names: Iterable[str], source: str = "user"
    ) -> List[str]:
        """Replace a game's content types, creating any that do not exist yet."""
        wanted: List[str] = []
        for raw in names or ():
            clean = (raw or "").strip()
            if clean and clean not in wanted:
                wanted.append(clean)
        stamp = now_iso()
        with self.db.transaction() as conn:
            if source == "user":
                # The user has now made an explicit choice, so advisory
                # suggestions from the folder scan are dropped instead of
                # lingering next to it and contradicting the UI.
                conn.execute(
                    "DELETE FROM game_content_types WHERE game_id = ?", (game_id,)
                )
            else:
                conn.execute(
                    "DELETE FROM game_content_types WHERE game_id = ? AND source = ?",
                    (game_id, source),
                )
            for name in wanted:
                key = name.lower()
                row = conn.execute(
                    "SELECT id FROM content_types WHERE name_key = ?", (key,)
                ).fetchone()
                if row:
                    type_id = row["id"]
                else:
                    order = conn.execute(
                        "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM content_types"
                    ).fetchone()[0]
                    type_id = conn.execute(
                        "INSERT INTO content_types (name, name_key, sort_order, created_at)"
                        " VALUES (?, ?, ?, ?)",
                        (name, key, order, stamp),
                    ).lastrowid
                conn.execute(
                    "INSERT OR IGNORE INTO game_content_types"
                    " (game_id, content_type_id, source) VALUES (?, ?, ?)",
                    (game_id, type_id, source),
                )
            conn.execute(
                "UPDATE games SET updated_at = ? WHERE id = ?", (stamp, game_id)
            )
        game = self.get_game(game_id)
        self._refresh_search_blob(game_id, game)
        return wanted

    # ==================================================================
    # games - creation from the filesystem
    # ==================================================================
    def upsert_folder(
        self,
        folder_path: str,
        root_id: Optional[int] = None,
        state: str = FolderState.PRESENT.value,
        title_hint: Optional[str] = None,
    ) -> Tuple[Optional[Game], bool]:
        """Create or update the record for a folder. Returns ``(game, created)``.

        Identity is the normalized folder key, so a rescan of the same tree can
        never produce duplicates, and case/separator differences on Windows are
        absorbed.
        """
        from cartridge.paths import normalize_path

        normalized = normalize_path(folder_path, self.windows)
        key = path_key(normalized, self.windows)
        if not key:
            return None, False
        stamp = now_iso()
        created = False
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id, folder_path, title FROM games WHERE folder_key = ?", (key,)
            ).fetchone()
            if row is None:
                display_title = title_hint or folder_name(normalized, self.windows)
                cursor = conn.execute(
                    "INSERT INTO games (folder_path, folder_key, root_id, title,"
                    " sort_title, metadata_state, folder_state, folder_state_checked_at,"
                    " added_at, updated_at, last_verified_at, search_blob)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        normalized, key, root_id, display_title,
                        _sort_title(display_title), STATE_NONE, state, stamp,
                        stamp, stamp, stamp, "",
                    ),
                )
                game_id = cursor.lastrowid
                created = True
            else:
                game_id = row["id"]
                # Keep the stored spelling in step with the filesystem, but never
                # touch titles/metadata: the folder may simply have been renamed.
                conn.execute(
                    "UPDATE games SET folder_path = ?, folder_state = ?,"
                    " folder_state_checked_at = ?, last_verified_at = ?,"
                    " root_id = COALESCE(?, root_id), updated_at = ? WHERE id = ?",
                    (normalized, state, stamp, stamp, root_id, stamp, game_id),
                )
        game = self.get_game(game_id)
        if created and game is not None:
            self._refresh_search_blob(game_id, game)
        return game, created

    def reassociate_folder(self, game_id: int, new_path: str) -> bool:
        """Point an existing record at a different folder (user-confirmed move).

        Refuses to collide with another record's identity.
        """
        from cartridge.paths import normalize_path

        normalized = normalize_path(new_path, self.windows)
        key = path_key(normalized, self.windows)
        if not key:
            return False
        stamp = now_iso()
        with self.db.transaction() as conn:
            clash = conn.execute(
                "SELECT id FROM games WHERE folder_key = ? AND id != ?", (key, game_id)
            ).fetchone()
            if clash:
                raise DatabaseError(
                    "another catalogue entry already owns that folder"
                )
            conn.execute(
                "UPDATE games SET folder_path = ?, folder_key = ?, folder_state = ?,"
                " folder_state_checked_at = ?, folder_size_bytes = NULL,"
                " folder_size_measured_at = NULL, updated_at = ? WHERE id = ?",
                (normalized, key, FolderState.PRESENT.value, stamp, stamp, game_id),
            )
        game = self.get_game(game_id)
        self._refresh_search_blob(game_id, game)
        return True

    # ==================================================================
    # games - reads
    # ==================================================================
    def get_game(self, game_id: int) -> Optional[Game]:
        row = self.db.query_one(
            "SELECT %s FROM games g WHERE g.id = ?" % GAME_COLUMNS, (game_id,)
        )
        if row is None:
            return None
        games = self._hydrate([row])
        return games[0] if games else None

    def get_game_by_folder(self, folder_path: str) -> Optional[Game]:
        key = path_key(folder_path, self.windows)
        row = self.db.query_one(
            "SELECT %s FROM games g WHERE g.folder_key = ?" % GAME_COLUMNS, (key,)
        )
        if row is None:
            return None
        games = self._hydrate([row])
        return games[0] if games else None

    def list_games(
        self, spec: Optional[filtering.FilterSpec] = None, with_assets: bool = True
    ) -> List[Game]:
        spec = spec or filtering.FilterSpec()
        where, params = filtering.build_where(spec)
        sql = "SELECT %s FROM games g" % GAME_COLUMNS
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY " + filtering.build_order(spec)
        limit_sql, limit_params = filtering.build_limit_offset(spec)
        sql += limit_sql
        rows = self.db.query(sql, list(params) + list(limit_params))
        return self._hydrate(rows, with_assets=with_assets)

    def count_games(self, spec: Optional[filtering.FilterSpec] = None) -> int:
        spec = spec or filtering.FilterSpec()
        where, params = filtering.build_where(spec)
        sql = "SELECT COUNT(*) FROM games g"
        if where:
            sql += " WHERE " + where
        return int(self.db.scalar(sql, params, default=0) or 0)

    def all_games(self) -> List[Game]:
        return self.list_games(filtering.FilterSpec(sort="title"))

    def facets(self) -> Dict[str, List[Tuple[str, int]]]:
        """Sidebar facet values with counts."""
        out: Dict[str, List[Tuple[str, int]]] = {}
        for name, sql in filtering.facet_rows_sql().items():
            try:
                rows = self.db.query(sql)
            except sqlite3.Error:
                out[name] = []
                continue
            out[name] = [(str(row["label"]), int(row["n"])) for row in rows if row["label"]]
        return out

    def distinct_years(self) -> List[int]:
        rows = self.db.query(
            "SELECT DISTINCT %s AS y FROM games WHERE y IS NOT NULL ORDER BY y DESC"
            % filtering.EFFECTIVE_YEAR
        )
        return [int(row["y"]) for row in rows]

    def _hydrate(
        self, rows: Sequence[sqlite3.Row], with_assets: bool = True
    ) -> List[Game]:
        """Turn game rows into fully populated models using batched queries."""
        if not rows:
            return []
        games: List[Game] = []
        by_id: Dict[int, Game] = {}
        for row in rows:
            game = Game(
                id=row["id"],
                folder_path=row["folder_path"],
                folder_key=row["folder_key"],
                root_id=row["root_id"],
                title=row["title"] or "",
                sort_title=row["sort_title"] or "",
                summary=row["summary"],
                release_date=row["release_date"],
                release_year=row["release_year"],
                rating=row["rating"],
                age_rating=row["age_rating"],
                franchise=row["franchise"],
                developer=row["developer"],
                publisher=row["publisher"],
                user_title=row["user_title"],
                user_summary=row["user_summary"],
                user_release_year=row["user_release_year"],
                user_rating=row["user_rating"],
                user_developer=row["user_developer"],
                user_publisher=row["user_publisher"],
                user_franchise=row["user_franchise"],
                user_age_rating=row["user_age_rating"],
                favourite=bool(row["favourite"]),
                notes=row["notes"],
                provider=row["provider"],
                provider_id=row["provider_id"],
                provider_url=row["provider_url"],
                metadata_state=row["metadata_state"] or STATE_NONE,
                metadata_error=row["metadata_error"],
                folder_state=row["folder_state"] or FolderState.UNKNOWN.value,
                folder_state_checked_at=row["folder_state_checked_at"],
                folder_size_bytes=row["folder_size_bytes"],
                folder_size_measured_at=row["folder_size_measured_at"],
                added_at=row["added_at"],
                updated_at=row["updated_at"],
                last_verified_at=row["last_verified_at"],
                last_refreshed_at=row["last_refreshed_at"],
            )
            games.append(game)
            by_id[game.id] = game

        ids = list(by_id.keys())
        placeholders = ",".join("?" * len(ids))

        for row in self.db.query(
            "SELECT game_id, name FROM game_aliases WHERE game_id IN (%s) ORDER BY id"
            % placeholders, ids
        ):
            by_id[row["game_id"]].alternative_names.append(row["name"])

        for row in self.db.query(
            "SELECT gg.game_id AS gid, gen.name AS name FROM game_genres gg"
            " JOIN genres gen ON gen.id = gg.genre_id"
            " WHERE gg.game_id IN (%s) ORDER BY gen.name" % placeholders, ids
        ):
            by_id[row["gid"]].genres.append(row["name"])

        for row in self.db.query(
            "SELECT gp.game_id AS gid, pf.name AS name FROM game_platforms gp"
            " JOIN platforms pf ON pf.id = gp.platform_id"
            " WHERE gp.game_id IN (%s) ORDER BY pf.name" % placeholders, ids
        ):
            by_id[row["gid"]].platforms.append(row["name"])

        for row in self.db.query(
            "SELECT game_id, name FROM game_user_genres WHERE game_id IN (%s)"
            " ORDER BY name" % placeholders, ids
        ):
            by_id[row["game_id"]].user_genres.append(row["name"])

        for row in self.db.query(
            "SELECT gct.game_id AS gid, ct.name AS name FROM game_content_types gct"
            " JOIN content_types ct ON ct.id = gct.content_type_id"
            " WHERE gct.game_id IN (%s) ORDER BY ct.sort_order, ct.name" % placeholders, ids
        ):
            game = by_id[row["gid"]]
            if row["name"] not in game.content_types:
                game.content_types.append(row["name"])

        if with_assets:
            for row in self.db.query(
                "SELECT * FROM assets WHERE game_id IN (%s)"
                " ORDER BY kind DESC, sort_order ASC, id ASC" % placeholders, ids
            ):
                by_id[row["game_id"]].assets.append(self._row_to_asset(row))

        return games

    @staticmethod
    def _row_to_asset(row: sqlite3.Row) -> Asset:
        return Asset(
            id=row["id"],
            game_id=row["game_id"],
            kind=row["kind"],
            rel_path=row["rel_path"],
            source_url=row["source_url"],
            provider=row["provider"],
            width=row["width"],
            height=row["height"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            sort_order=row["sort_order"] or 0,
            state=row["state"] or "ok",
            downloaded_at=row["downloaded_at"],
        )

    # ==================================================================
    # games - writes
    # ==================================================================
    def apply_provider_metadata(
        self, game_id: int, data: Dict[str, Any], mark_complete: bool = True
    ) -> bool:
        """Write provider-derived fields. **Never** touches ``user_*`` columns."""
        allowed = {key: value for key, value in data.items() if key in PROVIDER_COLUMNS}
        genres = list(data.get("genres") or [])
        platforms = list(data.get("platforms") or [])
        aliases = list(data.get("alternative_names") or [])

        title = str(allowed.get("title") or "").strip()
        allowed["title"] = title
        # Derive the sort key from the effective title so a user override of
        # "The Witcher" -> "Wiedzmin" also changes where it sorts.
        existing_user_title = self.db.scalar(
            "SELECT user_title FROM games WHERE id = ?", (game_id,), default=None
        )
        allowed["sort_title"] = _sort_title(existing_user_title or title)
        if allowed.get("release_date") and not allowed.get("release_year"):
            from cartridge.text import extract_year

            allowed["release_year"] = extract_year(str(allowed["release_date"]))
        if mark_complete:
            allowed.setdefault("metadata_state", STATE_COMPLETE)
        else:
            # A partially applied import must be visibly incomplete rather than
            # looking like a fresh, never-touched record (brief section 8).
            allowed.setdefault("metadata_state", STATE_PARTIAL)
        allowed["last_refreshed_at"] = now_iso()
        allowed["updated_at"] = now_iso()

        sets = ", ".join("%s = ?" % column for column in allowed)
        values = [allowed[column] for column in allowed]
        values.append(game_id)
        with self.db.transaction() as conn:
            conn.execute("UPDATE games SET %s WHERE id = ?" % sets, tuple(values))
            self._set_links(conn, game_id, "genres", genres)
            self._set_links(conn, game_id, "platforms", platforms)
            conn.execute("DELETE FROM game_aliases WHERE game_id = ?", (game_id,))
            for alias in aliases:
                clean = str(alias or "").strip()
                if not clean:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO game_aliases (game_id, name, name_key, source)"
                    " VALUES (?, ?, ?, ?)",
                    (game_id, clean, search_key(clean), allowed.get("provider")),
                )
        game = self.get_game(game_id)
        self._refresh_search_blob(game_id, game)
        return True

    def set_manual_metadata(self, game_id: int, data: Dict[str, Any]) -> bool:
        """Apply metadata the user typed by hand (import dialog "Enter manually").

        Manual entry writes the *provider* columns (there is no provider), because
        these values are the record of truth for that game; a later provider
        refresh would overwrite them, which is why manual records are marked
        ``manual`` and the UI warns before refreshing one.
        """
        payload = dict(data)
        payload["provider"] = None
        payload["provider_id"] = None
        payload["metadata_state"] = STATE_MANUAL
        return self.apply_provider_metadata(game_id, payload)

    def update_user_fields(self, game_id: int, fields: Dict[str, Any]) -> List[str]:
        """Set/clear user overrides. A ``None`` value clears the override.

        ``user_genres`` is stored in a child table rather than a column, but is
        accepted here so the Edit dialog has one call for "save my changes".
        """
        provided = dict(fields or {})
        genre_override = "user_genres" in provided
        new_genres = provided.pop("user_genres", None)

        updates: Dict[str, Any] = {}
        for key, value in provided.items():
            if key not in USER_COLUMNS:
                raise ValueError("not a user-editable column: %r" % key)
            updates[key] = value

        if "user_title" in updates:
            # Keep the ordering column in step with the new effective title.
            provider_title = self.db.scalar(
                "SELECT title FROM games WHERE id = ?", (game_id,), default=""
            )
            updates["sort_title"] = _sort_title(updates["user_title"] or provider_title or "")

        if updates:
            sets = ", ".join("%s = ?" % column for column in updates)
            params = list(updates.values()) + [now_iso(), game_id]
            self.db.execute(
                "UPDATE games SET %s, updated_at = ? WHERE id = ?" % sets, tuple(params)
            )
        if genre_override:
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM game_user_genres WHERE game_id = ?", (game_id,))
                for name in new_genres or ():
                    clean = str(name or "").strip()
                    if clean:
                        conn.execute(
                            "INSERT OR IGNORE INTO game_user_genres (game_id, name)"
                            " VALUES (?, ?)",
                            (game_id, clean),
                        )
        game = self.get_game(game_id)
        self._refresh_search_blob(game_id, game)
        return sorted(updates.keys())

    def set_title(self, game_id: int, title: str, as_override: bool = False) -> None:
        clean = (title or "").strip()
        if as_override:
            self.update_user_fields(game_id, {"user_title": clean or None})
        else:
            user_title = self.db.scalar(
                "SELECT user_title FROM games WHERE id = ?", (game_id,), default=None
            )
            self.db.execute(
                "UPDATE games SET title = ?, sort_title = ?, updated_at = ? WHERE id = ?",
                (clean, _sort_title(user_title or clean), now_iso(), game_id),
            )
            self._refresh_search_blob(game_id, self.get_game(game_id))

    def set_favourite(self, game_id: int, favourite: bool) -> None:
        self.db.execute(
            "UPDATE games SET favourite = ?, updated_at = ? WHERE id = ?",
            (1 if favourite else 0, now_iso(), game_id),
        )

    def toggle_favourite(self, game_id: int) -> bool:
        current = self.db.scalar(
            "SELECT favourite FROM games WHERE id = ?", (game_id,), default=0
        )
        new_value = not bool(current)
        self.set_favourite(game_id, new_value)
        return new_value

    def set_notes(self, game_id: int, notes: Optional[str]) -> None:
        self.db.execute(
            "UPDATE games SET notes = ?, updated_at = ? WHERE id = ?",
            (notes, now_iso(), game_id),
        )

    def set_folder_state(
        self, game_id: int, state: str, checked_at: Optional[str] = None
    ) -> None:
        self.db.execute(
            "UPDATE games SET folder_state = ?, folder_state_checked_at = ? WHERE id = ?",
            (state, checked_at or now_iso(), game_id),
        )

    def set_folder_size(
        self, game_id: int, size_bytes: Optional[int], measured_at: Optional[str] = None
    ) -> None:
        """Store a *measured* size. ``None`` means "not measured"."""
        self.db.execute(
            "UPDATE games SET folder_size_bytes = ?, folder_size_measured_at = ?,"
            " updated_at = ? WHERE id = ?",
            (
                int(size_bytes) if size_bytes is not None else None,
                measured_at or (now_iso() if size_bytes is not None else None),
                now_iso(),
                game_id,
            ),
        )

    def mark_verified(self, game_id: int) -> None:
        self.db.execute(
            "UPDATE games SET last_verified_at = ?, folder_state = ?,"
            " folder_state_checked_at = ? WHERE id = ?",
            (now_iso(), FolderState.PRESENT.value, now_iso(), game_id),
        )

    def set_metadata_error(self, game_id: int, message: str) -> None:
        """Record a failed refresh without pretending the record is complete."""
        self.db.execute(
            "UPDATE games SET metadata_state = 'error', metadata_error = ?,"
            " updated_at = ? WHERE id = ?",
            (message[:500], now_iso(), game_id),
        )

    def delete_game(self, game_id: int) -> None:
        """Explicit deletion only. Children cascade."""
        self.db.execute("DELETE FROM games WHERE id = ?", (game_id,))

    def game_id_for_folder(self, folder_path: str) -> Optional[int]:
        return self.db.scalar(
            "SELECT id FROM games WHERE folder_key = ?",
            (path_key(folder_path, self.windows),),
            default=None,
        )

    def missing_folder_games(self) -> List[Game]:
        spec = filtering.FilterSpec(view=filtering.VIEW_MISSING_FOLDER, sort="title")
        return self.list_games(spec)

    # ==================================================================
    # taxonomy links (genres / platforms)
    # ==================================================================
    def _set_links(
        self, conn: sqlite3.Connection, game_id: int, kind: str, names: Iterable[str]
    ) -> None:
        if kind == "genres":
            table, link, name_col = "genres", "game_genres", "genre_id"
        elif kind == "platforms":
            table, link, name_col = "platforms", "game_platforms", "platform_id"
        else:
            raise ValueError("unknown link kind: %r" % kind)

        conn.execute("DELETE FROM %s WHERE game_id = ?" % link, (game_id,))
        seen = set()
        for raw in names or ():
            clean = str(raw or "").strip()
            if not clean:
                continue
            key = search_key(clean)
            if not key or key in seen:
                continue
            seen.add(key)
            row = conn.execute(
                "SELECT id FROM %s WHERE name_key = ?" % table, (key,)
            ).fetchone()
            if row:
                target_id = row["id"]
            else:
                target_id = conn.execute(
                    "INSERT INTO %s (name, name_key) VALUES (?, ?)" % table,
                    (clean, key),
                ).lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO %s (game_id, %s) VALUES (?, ?)" % (link, name_col),
                (game_id, target_id),
            )

    def set_genres(self, game_id: int, names: Iterable[str]) -> None:
        with self.db.transaction() as conn:
            self._set_links(conn, game_id, "genres", names)
        self._refresh_search_blob(game_id, self.get_game(game_id))

    def set_platforms(self, game_id: int, names: Iterable[str]) -> None:
        with self.db.transaction() as conn:
            self._set_links(conn, game_id, "platforms", names)
        self._refresh_search_blob(game_id, self.get_game(game_id))

    def set_aliases(
        self, game_id: int, names: Iterable[str], source: Optional[str] = None
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM game_aliases WHERE game_id = ?", (game_id,))
            for raw in names or ():
                clean = str(raw or "").strip()
                if clean:
                    conn.execute(
                        "INSERT OR IGNORE INTO game_aliases (game_id, name, name_key, source)"
                        " VALUES (?, ?, ?, ?)",
                        (game_id, clean, search_key(clean), source),
                    )
        self._refresh_search_blob(game_id, self.get_game(game_id))

    # ==================================================================
    # search blob
    # ==================================================================
    def build_search_blob(self, game: Game) -> str:
        """Folded, de-duplicated text covering the useful search fields (§12)."""
        if game is None:
            return ""
        parts: List[str] = [
            game.effective_title(), game.title, game.folder_basename(),
            game.effective_summary() or "", game.effective_developer() or "",
            game.effective_publisher() or "", game.effective_franchise() or "",
        ]
        parts.extend(game.alternative_names)
        parts.extend(game.effective_genres())
        parts.extend(game.platforms)
        parts.extend(game.content_types)
        folded = []
        seen = set()
        for part in parts:
            key = search_key(part or "")
            if key and key not in seen:
                seen.add(key)
                folded.append(key)
            normalized = normalize_title(part or "")
            if normalized and normalized not in seen:
                seen.add(normalized)
                folded.append(normalized)
        return " ".join(folded)

    def _refresh_search_blob(self, game_id: Optional[int], game: Optional[Game]) -> None:
        if game_id is None or game is None:
            return
        blob = self.build_search_blob(game)
        self.db.execute(
            "UPDATE games SET search_blob = ? WHERE id = ?", (blob, game_id)
        )

    def rebuild_all_search_blobs(self) -> int:
        count = 0
        for game in self.all_games():
            self._refresh_search_blob(game.id, game)
            count += 1
        return count

    # ==================================================================
    # assets
    # ==================================================================
    def add_asset(self, game_id: int, asset: Asset) -> int:
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM assets WHERE game_id = ? AND kind = ? AND rel_path = ?",
                (game_id, asset.kind, asset.rel_path),
            )
            cursor = conn.execute(
                "INSERT INTO assets (game_id, kind, rel_path, source_url, provider,"
                " width, height, size_bytes, sha256, sort_order, state, downloaded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    game_id, asset.kind, asset.rel_path, asset.source_url,
                    asset.provider, asset.width, asset.height, asset.size_bytes,
                    asset.sha256, asset.sort_order, asset.state or "ok",
                    asset.downloaded_at or now_iso(),
                ),
            )
            asset_id = cursor.lastrowid
            conn.execute(
                "UPDATE games SET updated_at = ? WHERE id = ?", (now_iso(), game_id)
            )
        return asset_id

    def list_assets(self, game_id: int, kind: Optional[str] = None) -> List[Asset]:
        if kind:
            rows = self.db.query(
                "SELECT * FROM assets WHERE game_id = ? AND kind = ?"
                " ORDER BY sort_order, id", (game_id, kind),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM assets WHERE game_id = ? ORDER BY kind DESC, sort_order, id",
                (game_id,),
            )
        return [self._row_to_asset(row) for row in rows]

    def set_asset_state(self, asset_id: int, state: str) -> None:
        self.db.execute(
            "UPDATE assets SET state = ? WHERE id = ?", (state, asset_id)
        )

    def mark_missing_assets(self, game_id: int, existing_rel_paths: Iterable[str]) -> int:
        """Flag assets whose files are gone, without deleting their records."""
        present = set(existing_rel_paths or ())
        changed = 0
        for asset in self.list_assets(game_id):
            wanted = "ok" if asset.rel_path in present else "missing"
            if asset.state != wanted:
                self.set_asset_state(asset.id, wanted)
                changed += 1
        return changed

    def delete_asset(self, asset_id: int) -> None:
        self.db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))

    def clear_assets(self, game_id: int, kind: Optional[str] = None) -> None:
        if kind:
            self.db.execute(
                "DELETE FROM assets WHERE game_id = ? AND kind = ?", (game_id, kind)
            )
        else:
            self.db.execute("DELETE FROM assets WHERE game_id = ?", (game_id,))

    # ==================================================================
    # settings (never secrets)
    # ==================================================================
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        value = self.db.scalar(
            "SELECT value FROM settings WHERE key = ?", (key,), default=None
        )
        return default if value is None else str(value)

    def set_setting(self, key: str, value: Any) -> None:
        clean_key = str(key).strip().lower()
        if clean_key in SECRET_SETTING_KEYS:
            raise SecretInDatabaseError(
                "refusing to store %r in SQLite: credentials live in the secret "
                "store (DECISIONS D-005), never in the database" % clean_key
            )
        text = "" if value is None else str(value)
        self.db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (clean_key, text, now_iso()),
        )

    def all_settings(self) -> Dict[str, str]:
        rows = self.db.query("SELECT key, value FROM settings ORDER BY key")
        return {str(row["key"]): str(row["value"]) for row in rows}

    def get_int(self, key: str, default: int) -> int:
        raw = self.get_setting(key)
        try:
            return int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool) -> bool:
        raw = self.get_setting(key)
        if raw is None:
            return default
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    # ==================================================================
    # scan runs + latency samples (Diagnostics)
    # ==================================================================
    def start_scan_run(self, root_id: Optional[int], root_path: str) -> int:
        cursor = self.db.execute(
            "INSERT INTO scan_runs (root_id, root_path, started_at, status)"
            " VALUES (?, ?, ?, 'running')",
            (root_id, root_path, now_iso()),
        )
        return int(cursor.lastrowid)

    def finish_scan_run(
        self, run_id: int, summary: Dict[str, Any], status: str = "done", message: str = ""
    ) -> None:
        self.db.execute(
            "UPDATE scan_runs SET finished_at = ?, folders_seen = ?, new_folders = ?,"
            " matched = ?, unmatched = ?, missing_folders = ?, unavailable_drives = ?,"
            " status = ?, message = ?, duration_ms = ? WHERE id = ?",
            (
                now_iso(),
                int(summary.get("folders_seen", 0)),
                int(summary.get("new_folders", 0)),
                int(summary.get("matched", 0)),
                int(summary.get("unmatched", 0)),
                int(summary.get("missing_folders", 0)),
                int(summary.get("unavailable_drives", 0)),
                status,
                (message or "")[:500],
                float(summary.get("duration_ms", 0.0)),
                run_id,
            ),
        )

    def recent_scan_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (int(limit),)
        )
        return [dict(row) for row in rows]

    def record_sample(self, operation: str, duration_ms: float, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO op_samples (operation, duration_ms, detail, recorded_at)"
            " VALUES (?, ?, ?, ?)",
            (operation, float(duration_ms), (detail or "")[:200], now_iso()),
        )
        # Keep the table bounded: 2 GB of RAM and a mechanical disk are not the
        # place for an ever-growing telemetry table.
        self.db.execute(
            "DELETE FROM op_samples WHERE id NOT IN"
            " (SELECT id FROM op_samples ORDER BY id DESC LIMIT 500)"
        )

    def sample_stats(self) -> Dict[str, Dict[str, float]]:
        rows = self.db.query(
            "SELECT operation, COUNT(*) AS n, AVG(duration_ms) AS avg_ms,"
            " MAX(duration_ms) AS max_ms FROM op_samples GROUP BY operation"
        )
        return {
            str(row["operation"]): {
                "n": int(row["n"]),
                "avg_ms": round(float(row["avg_ms"] or 0.0), 2),
                "max_ms": round(float(row["max_ms"] or 0.0), 2),
            }
            for row in rows
        }

    # ==================================================================
    # maintenance / export
    # ==================================================================
    def stats(self) -> Dict[str, Any]:
        games = int(self.db.scalar("SELECT COUNT(*) FROM games", default=0) or 0)
        favourites = int(
            self.db.scalar("SELECT COUNT(*) FROM games WHERE favourite = 1", default=0) or 0
        )
        incomplete = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM games WHERE metadata_state IN ('none','partial','error')",
                default=0,
            ) or 0
        )
        missing_folder = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM games WHERE folder_state != 'present'", default=0
            ) or 0
        )
        with_cover = int(
            self.db.scalar(
                "SELECT COUNT(DISTINCT game_id) FROM assets WHERE kind = 'cover'"
                " AND state = 'ok'", default=0,
            ) or 0
        )
        assets = int(self.db.scalar("SELECT COUNT(*) FROM assets", default=0) or 0)
        return {
            "games": games,
            "favourites": favourites,
            "incomplete": incomplete,
            "missing_folder": missing_folder,
            "with_cover": with_cover,
            "without_cover": max(0, games - with_cover),
            "assets": assets,
            "roots": len(self.list_roots()),
            "content_types": len(self.list_content_types(with_counts=False)),
        }

    def export_collection(self) -> Dict[str, Any]:
        """Full export for backup. Contains no credentials by construction."""
        games = []
        for game in self.all_games():
            payload = {
                "folder_path": game.folder_path,
                "title": game.title,
                "user_title": game.user_title,
                "summary": game.summary,
                "release_year": game.release_year,
                "rating": game.rating,
                "developer": game.developer,
                "publisher": game.publisher,
                "franchise": game.franchise,
                "genres": game.genres,
                "platforms": game.platforms,
                "alternative_names": game.alternative_names,
                "content_types": game.content_types,
                "favourite": game.favourite,
                "notes": game.notes,
                "provider": game.provider,
                "provider_id": game.provider_id,
                "metadata_state": game.metadata_state,
                "folder_state": game.folder_state,
                "folder_size_bytes": game.folder_size_bytes,
                "assets": [
                    {
                        "kind": a.kind, "rel_path": a.rel_path, "sha256": a.sha256,
                        "state": a.state, "provider": a.provider,
                    }
                    for a in game.assets
                ],
            }
            games.append(payload)
        return {
            "app": "Cartridge",
            "exported_at": now_iso(),
            "content_types": [ct.name for ct in self.list_content_types(with_counts=False)],
            "roots": [root.path for root in self.list_roots()],
            "games": games,
        }
