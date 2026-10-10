"""SQLite schema, versioning and migrations.

Design notes (brief section 8):

* **Parameterized SQL only.** This module contains DDL; every DML statement in
  :mod:`cartridge.db.repository` uses bound parameters. No string is ever
  interpolated into SQL from provider data, folder names or user input.
* **Foreign keys are enforced** (``PRAGMA foreign_keys = ON`` per connection) and
  child rows cascade from ``games``.
* **Schema versioning** uses both ``PRAGMA user_version`` (fast, authoritative)
  and a ``migrations`` history table (auditable in Diagnostics).
* **Search** uses a denormalized ``search_blob`` column with ``LIKE`` rather than
  FTS5. FTS5 availability depends on how the target's ``sqlite3`` was compiled,
  and a game collection is thousands of rows, not millions: a folded ``LIKE``
  scan is comfortably fast and has no optional dependency. Measured, not assumed
  — see ``docs/PERFORMANCE_REPORT.md``.
* **Timestamps** are ISO-8601 UTC text (``cartridge.core.clock``).

Artwork is *not* stored as a blob here. ``assets`` holds a path relative to the
game folder plus a SHA-256, so the collection keeps working offline and stays
portable if the user moves the root.
"""

from __future__ import annotations

from typing import List, Tuple

from cartridge import SCHEMA_VERSION
from cartridge.core.clock import now_iso

# --------------------------------------------------------------------------
# DDL for schema version 1
# --------------------------------------------------------------------------
SCHEMA_V1: List[str] = [
    # -- application metadata -------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS meta (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS migrations (
        version    INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL,
        note       TEXT NOT NULL DEFAULT ''
    )
    """,

    # -- roots ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS roots (
        id              INTEGER PRIMARY KEY,
        path            TEXT NOT NULL,
        path_key        TEXT NOT NULL UNIQUE,
        label           TEXT,
        enabled         INTEGER NOT NULL DEFAULT 1,
        added_at        TEXT NOT NULL,
        last_scanned_at TEXT
    )
    """,

    # -- content types (user-defined data, never a hard-coded enum) -------
    """
    CREATE TABLE IF NOT EXISTS content_types (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL,
        name_key   TEXT NOT NULL UNIQUE,
        color      TEXT,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,

    # -- games ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS games (
        id            INTEGER PRIMARY KEY,
        folder_path   TEXT NOT NULL,
        folder_key    TEXT NOT NULL UNIQUE,
        root_id       INTEGER REFERENCES roots(id) ON DELETE SET NULL,

        title         TEXT NOT NULL DEFAULT '',
        sort_title    TEXT NOT NULL DEFAULT '',
        summary       TEXT,
        release_date  TEXT,
        release_year  INTEGER,
        rating        REAL,
        age_rating    TEXT,
        franchise     TEXT,
        developer     TEXT,
        publisher     TEXT,

        user_title         TEXT,
        user_summary       TEXT,
        user_release_year  INTEGER,
        user_rating        REAL,
        user_developer     TEXT,
        user_publisher     TEXT,
        user_franchise     TEXT,
        user_age_rating    TEXT,

        favourite     INTEGER NOT NULL DEFAULT 0,
        notes         TEXT,
        search_blob   TEXT NOT NULL DEFAULT '',

        provider        TEXT,
        provider_id     TEXT,
        provider_url    TEXT,
        metadata_state  TEXT NOT NULL DEFAULT 'none',
        metadata_error  TEXT,

        folder_state            TEXT NOT NULL DEFAULT 'unknown',
        folder_state_checked_at TEXT,
        folder_size_bytes       INTEGER,
        folder_size_measured_at TEXT,

        added_at         TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        last_verified_at TEXT,
        last_refreshed_at TEXT
    )
    """,
    # A provider identity may only be attached to one folder; NULLs are allowed
    # to repeat (SQLite treats NULLs as distinct), which is what manual entries need.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_games_provider_identity
        ON games(provider, provider_id)
        WHERE provider IS NOT NULL AND provider_id IS NOT NULL
    """,
    "CREATE INDEX IF NOT EXISTS idx_games_sort_title ON games(sort_title)",
    "CREATE INDEX IF NOT EXISTS idx_games_release_year ON games(release_year)",
    "CREATE INDEX IF NOT EXISTS idx_games_favourite ON games(favourite)",
    "CREATE INDEX IF NOT EXISTS idx_games_metadata_state ON games(metadata_state)",
    "CREATE INDEX IF NOT EXISTS idx_games_folder_state ON games(folder_state)",
    "CREATE INDEX IF NOT EXISTS idx_games_added_at ON games(added_at)",
    "CREATE INDEX IF NOT EXISTS idx_games_root ON games(root_id)",
    "CREATE INDEX IF NOT EXISTS idx_games_rating ON games(rating)",

    # -- alternative titles ----------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS game_aliases (
        id       INTEGER PRIMARY KEY,
        game_id  INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        name     TEXT NOT NULL,
        name_key TEXT NOT NULL DEFAULT '',
        source   TEXT,
        UNIQUE (game_id, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_aliases_game ON game_aliases(game_id)",
    "CREATE INDEX IF NOT EXISTS idx_aliases_key ON game_aliases(name_key)",

    # -- genres -----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS genres (
        id       INTEGER PRIMARY KEY,
        name     TEXT NOT NULL UNIQUE,
        name_key TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS game_genres (
        game_id  INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
        PRIMARY KEY (game_id, genre_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_game_genres_genre ON game_genres(genre_id)",

    # -- platforms --------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS platforms (
        id       INTEGER PRIMARY KEY,
        name     TEXT NOT NULL UNIQUE,
        name_key TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS game_platforms (
        game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        platform_id INTEGER NOT NULL REFERENCES platforms(id) ON DELETE CASCADE,
        PRIMARY KEY (game_id, platform_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_game_platforms_platform ON game_platforms(platform_id)",

    # -- user genres (overrides): free-text rows, deliberately not in `genres`
    """
    CREATE TABLE IF NOT EXISTS game_user_genres (
        game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        name    TEXT NOT NULL,
        PRIMARY KEY (game_id, name)
    )
    """,

    # -- game <-> content type -------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS game_content_types (
        game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        content_type_id INTEGER NOT NULL REFERENCES content_types(id) ON DELETE CASCADE,
        source         TEXT NOT NULL DEFAULT 'user',   -- user | suggested
        PRIMARY KEY (game_id, content_type_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gct_type ON game_content_types(content_type_id)",

    # -- artwork ----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS assets (
        id            INTEGER PRIMARY KEY,
        game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL,
        rel_path      TEXT NOT NULL,
        source_url    TEXT,
        provider      TEXT,
        width         INTEGER,
        height        INTEGER,
        size_bytes    INTEGER,
        sha256        TEXT,
        sort_order    INTEGER NOT NULL DEFAULT 0,
        state         TEXT NOT NULL DEFAULT 'ok',
        downloaded_at TEXT,
        UNIQUE (game_id, kind, rel_path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_assets_game ON assets(game_id, kind)",

    # -- scan history -----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS scan_runs (
        id                 INTEGER PRIMARY KEY,
        root_id            INTEGER REFERENCES roots(id) ON DELETE SET NULL,
        root_path          TEXT NOT NULL DEFAULT '',
        started_at         TEXT NOT NULL,
        finished_at        TEXT,
        folders_seen       INTEGER NOT NULL DEFAULT 0,
        new_folders        INTEGER NOT NULL DEFAULT 0,
        matched            INTEGER NOT NULL DEFAULT 0,
        unmatched          INTEGER NOT NULL DEFAULT 0,
        missing_folders    INTEGER NOT NULL DEFAULT 0,
        unavailable_drives INTEGER NOT NULL DEFAULT 0,
        status             TEXT NOT NULL DEFAULT 'running',
        message            TEXT NOT NULL DEFAULT '',
        duration_ms        REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scan_runs_started ON scan_runs(started_at)",

    # -- settings (never secrets: see DECISIONS D-005) -------------------
    """
    CREATE TABLE IF NOT EXISTS settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,

    # -- latency samples for Diagnostics ---------------------------------
    """
    CREATE TABLE IF NOT EXISTS op_samples (
        id         INTEGER PRIMARY KEY,
        operation  TEXT NOT NULL,
        duration_ms REAL NOT NULL,
        detail     TEXT NOT NULL DEFAULT '',
        recorded_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_op_samples_op ON op_samples(operation, id)",
]

# Migration registry: version -> (statements, note).
# Version 1 is the baseline; later versions append here and are applied in order.
MIGRATIONS: List[Tuple[int, List[str], str]] = [
    (1, SCHEMA_V1, "baseline schema"),
]

# Settings keys that must never be written to the database. The repository
# refuses them outright; this list exists so the refusal is explicit and testable.
SECRET_SETTING_KEYS = frozenset(
    (
        "igdb_client_id",
        "igdb_client_secret",
        "twitch_access_token",
        "twitch_refresh_token",
        "rawg_api_key",
        "api_key",
        "client_secret",
        "password",
        "token",
    )
)

DEFAULT_CONTENT_TYPES = (
    "GOG", "EXE", "ISO", "Installer", "Portable",
    "Emulator", "Patch", "Backup", "Manual", "Launcher",
)


def statements_for_version(version: int) -> List[str]:
    for ver, stmts, _note in MIGRATIONS:
        if ver == version:
            return list(stmts)
    raise ValueError("unknown schema version: %r" % (version,))


def migration_note(version: int) -> str:
    for ver, _stmts, note in MIGRATIONS:
        if ver == version:
            return note
    return ""


def latest_version() -> int:
    return max(ver for ver, _stmts, _note in MIGRATIONS)


def bootstrap_sql() -> List[str]:
    """Statements needed to create a brand-new database at the latest version."""
    out: List[str] = []
    for ver, stmts, _note in MIGRATIONS:
        out.extend(stmts)
    out.append(
        "INSERT OR REPLACE INTO meta (key, value, updated_at) VALUES (?, ?, ?)"
    )
    return out


def current_schema_version() -> int:
    return SCHEMA_VERSION


def migration_plan(from_version: int) -> List[Tuple[int, List[str], str]]:
    """Ordered migrations needed to move ``from_version`` -> latest."""
    target = latest_version()
    if from_version > target:
        raise ValueError(
            "database schema version %d is newer than this build supports (%d). "
            "Refusing to open it rather than risk a destructive downgrade."
            % (from_version, target)
        )
    return [
        (ver, stmts, note)
        for ver, stmts, note in MIGRATIONS
        if ver > from_version
    ]


def seed_content_types_sql() -> str:
    return (
        "INSERT OR IGNORE INTO content_types (name, name_key, sort_order, created_at) "
        "VALUES (?, ?, ?, ?)"
    )


def initial_timestamp() -> str:
    return now_iso()
