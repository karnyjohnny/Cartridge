"""SQLite connection lifecycle, migrations and transactions.

Thread model
------------
A :class:`Database` is safe to hand to worker threads. ``sqlite3`` connections are
*not* shareable across threads by default, so each thread gets its own connection
through ``threading.local``; all of them point at the same file. This is what lets
the bounded worker pool (DECISIONS D-006) run scans, downloads and metadata
refreshes without touching the GUI thread and without ``check_same_thread`` errors.

Journal mode
------------
WAL is preferred (readers do not block the writer, and a crash mid-write leaves the
database consistent). WAL can fail on some network shares and on filesystems
without proper locking, so the code falls back to ``DELETE`` and records which mode
is actually active — Diagnostics shows the real value rather than the wish.

Durability
----------
``synchronous=NORMAL`` under WAL: a power cut can lose the last few committed
transactions but cannot corrupt the file. ``FULL`` is available through
``Database(durability="full")`` for users who prefer the slower guarantee on a
mechanical disk. Foreign keys are enabled per connection because the pragma is
not persistent.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from cartridge import SCHEMA_VERSION
from cartridge.core.clock import now_iso
from cartridge.db import schema as schema_mod
from cartridge.paths import APP_DATA_DIR


class DatabaseError(RuntimeError):
    """Raised for unrecoverable database problems (never for "row not found")."""


class SchemaTooNewError(DatabaseError):
    """The file was written by a newer build; opening it could destroy data."""


class Database(object):
    """Owns a SQLite file: connections, schema version, transactions."""

    def __init__(
        self,
        path: str,
        durability: str = "normal",
        wal: bool = True,
        timeout: float = 15.0,
        windows: Optional[bool] = None,
    ):
        if durability not in ("normal", "full"):
            raise ValueError("durability must be 'normal' or 'full'")
        self.path = str(path)
        self.durability = durability
        self.want_wal = bool(wal)
        self.timeout = float(timeout)
        self.windows = windows
        self._local = threading.local()
        self._lock = threading.RLock()
        self._closed = False
        self.journal_mode = "unknown"
        self.schema_version = 0
        self.created_new = False
        self.pragma_notes: List[str] = []

    # ------------------------------------------------------------------
    # connection handling
    # ------------------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's connection, created on first use."""
        if self._closed:
            raise DatabaseError("database is closed: %s" % self.path)
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            return existing
        connection = self._connect()
        self._local.conn = connection
        return connection

    def _connect(self) -> sqlite3.Connection:
        self._ensure_parent_dir()
        try:
            connection = sqlite3.connect(
                self.path, timeout=self.timeout, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise DatabaseError(
                "cannot open database at %s: %s" % (self.path, exc)
            )
        connection.row_factory = sqlite3.Row
        connection.text_factory = str
        self._apply_pragmas(connection)
        return connection

    def _ensure_parent_dir(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent and not os.path.isdir(parent):
            ok, message = is_writable_location(parent, create=True)
            if not ok:
                raise DatabaseError(
                    "cannot create database directory %s: %s. Choose a writable "
                    "location in Settings." % (parent, message)
                )

    def _apply_pragmas(self, connection: sqlite3.Connection) -> None:
        """Apply per-connection pragmas, degrading gracefully where needed."""
        notes = self.pragma_notes

        def pragma(sql: str) -> Optional[Any]:
            try:
                return connection.execute(sql).fetchone()
            except sqlite3.Error as exc:
                notes.append("%s -> failed (%s)" % (sql, exc))
                return None

        pragma("PRAGMA foreign_keys = ON")
        pragma("PRAGMA busy_timeout = %d" % int(self.timeout * 1000))

        if self.want_wal:
            row = pragma("PRAGMA journal_mode = WAL")
            mode = str(row[0]).lower() if row else "unknown"
            if mode != "wal":
                # Network shares and some removable media cannot do WAL.
                notes.append(
                    "WAL unavailable (got %r); falling back to DELETE journal" % mode
                )
                pragma("PRAGMA journal_mode = DELETE")
                row = pragma("PRAGMA journal_mode")
                mode = str(row[0]).lower() if row else "delete"
            self.journal_mode = mode
        else:
            row = pragma("PRAGMA journal_mode = DELETE")
            self.journal_mode = "delete"

        sync = "FULL" if self.durability == "full" else "NORMAL"
        pragma("PRAGMA synchronous = %s" % sync)
        pragma("PRAGMA temp_store = FILE")
        pragma("PRAGMA cache_size = -4000")  # ~4 MB page cache: 2 GB RAM budget
        pragma("PRAGMA mmap_size = 0")      # avoid mmap on a mechanical disk

    # ------------------------------------------------------------------
    # schema lifecycle
    # ------------------------------------------------------------------
    def open(self) -> "Database":
        """Open (creating/migrating if needed) and verify the schema."""
        existed = os.path.exists(self.path) and os.path.getsize(self.path) > 0
        self.created_new = not existed
        connection = self.conn
        self.schema_version = self._read_version(connection)

        if self.schema_version == 0:
            self._apply_migrations(connection, from_version=0)
        elif self.schema_version > schema_mod.latest_version():
            raise SchemaTooNewError(
                "database at %s has schema version %d but this build supports up "
                "to %d. Refusing to open it: writing with an older schema map could "
                "destroy data. Update Cartridge or restore a backup."
                % (self.path, self.schema_version, schema_mod.latest_version())
            )
        else:
            self._apply_migrations(connection, from_version=self.schema_version)

        self.schema_version = self._read_version(connection)
        return self

    def _read_version(self, connection: sqlite3.Connection) -> int:
        try:
            row = connection.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError("cannot read schema version: %s" % exc)
        value = int(row[0]) if row and row[0] is not None else 0
        if value:
            return value
        # A file may exist with tables but no user_version (hand-made or restored
        # from an old copy). Infer from the migrations table before assuming new.
        try:
            row = connection.execute(
                "SELECT MAX(version) FROM migrations"
            ).fetchone()
            if row and row[0] is not None:
                inferred = int(row[0])
                connection.execute("PRAGMA user_version = %d" % inferred)
                self.pragma_notes.append(
                    "user_version was 0 but migrations table said %d; adopted it"
                    % inferred
                )
                return inferred
        except sqlite3.Error:
            pass
        return 0

    def _apply_migrations(
        self, connection: sqlite3.Connection, from_version: int
    ) -> None:
        plan = schema_mod.migration_plan(from_version)
        if not plan:
            return
        for version, statements, note in plan:
            # One transaction per migration: a half-applied migration would leave
            # the database in a state no version describes.
            try:
                connection.execute("BEGIN IMMEDIATE")
                for statement in statements:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = %d" % version)
                connection.execute(
                    "INSERT OR REPLACE INTO migrations (version, applied_at, note) "
                    "VALUES (?, ?, ?)",
                    (version, now_iso(), note),
                )
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise DatabaseError(
                    "migration to schema version %d failed: %s" % (version, exc)
                )
            self.pragma_notes.append("applied migration -> v%d (%s)" % (version, note))
        self._seed_defaults(connection)

    def _seed_defaults(self, connection: sqlite3.Connection) -> None:
        """Insert the starting content types once. User data is never touched."""
        stamp = now_iso()
        sql = schema_mod.seed_content_types_sql()
        rows = [
            (name, name.strip().lower(), index, stamp)
            for index, name in enumerate(schema_mod.DEFAULT_CONTENT_TYPES)
        ]
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(sql, rows)
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self.pragma_notes.append("content-type seeding skipped: %s" % exc)

    # ------------------------------------------------------------------
    # transactions
    # ------------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction boundary. Rolls back on any exception.

        Imports and multi-row edits must run inside this so a failure cannot leave
        a game row that looks complete while its assets or genres are missing.
        """
        connection = self.conn
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            try:
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise DatabaseError("commit failed: %s" % exc)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None or row[0] is None:
            return default
        return row[0]

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------
    def integrity_check(self) -> Tuple[bool, str]:
        try:
            row = self.conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            return False, "integrity_check failed: %s" % exc
        result = str(row[0]) if row else "no result"
        return result == "ok", result

    def foreign_key_check(self) -> List[Dict[str, Any]]:
        try:
            rows = self.conn.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.Error as exc:
            return [{"error": str(exc)}]
        return [dict(row) for row in rows]

    def vacuum(self) -> None:
        with self._lock:
            self.conn.execute("VACUUM")

    def backup(self, destination: str) -> str:
        """Online backup through the sqlite3 backup API (safe while in use)."""
        parent = os.path.dirname(os.path.abspath(destination))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        target = sqlite3.connect(destination)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        return destination

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for table in ("games", "roots", "assets", "content_types", "scan_runs"):
            try:
                out[table] = int(self.scalar("SELECT COUNT(*) FROM %s" % table, default=0))
            except sqlite3.Error:
                out[table] = -1
        return out

    def migration_history(self) -> List[Dict[str, Any]]:
        try:
            rows = self.query("SELECT version, applied_at, note FROM migrations ORDER BY version")
        except sqlite3.Error:
            return []
        return [dict(row) for row in rows]

    def info(self) -> Dict[str, Any]:
        size = None
        try:
            size = os.path.getsize(self.path) if os.path.exists(self.path) else None
        except OSError:
            size = None
        ok, integrity = self.integrity_check()
        return {
            "path": self.path,
            "exists": os.path.exists(self.path),
            "size_bytes": size,
            "schema_version": self.schema_version,
            "latest_supported_version": schema_mod.latest_version(),
            "journal_mode": self.journal_mode,
            "synchronous": self.durability,
            "foreign_keys": self.scalar("PRAGMA foreign_keys", default=0),
            "integrity_ok": ok,
            "integrity": integrity,
            "counts": self.counts(),
            "migrations": self.migration_history(),
            "notes": list(self.pragma_notes),
        }

    def close(self) -> None:
        """Close this thread's connection. Other threads close their own."""
        connection = getattr(self._local, "conn", None)
        if connection is not None:
            try:
                connection.close()
            finally:
                self._local.conn = None

    def close_all(self) -> None:
        """Mark closed and drop this thread's connection.

        Connections created by worker threads are closed when those threads exit
        (see :meth:`cartridge.core.workers.WorkerPool.shutdown`).
        """
        self._closed = True
        self.close()

    def __enter__(self) -> "Database":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def open_database(
    path: str, durability: str = "normal", wal: bool = True
) -> Database:
    """Convenience: open (and migrate) a database file."""
    return Database(path, durability=durability, wal=wal).open()


def default_db_path(root: str, windows: Optional[bool] = None) -> str:
    """``<root>\\.cartridge\\collection.db``."""
    from cartridge.paths import db_path_for_root

    return db_path_for_root(root, windows=windows)


def is_writable_location(directory: str, create: bool = False) -> Tuple[bool, str]:
    """Check that a directory can host the database, with a usable message.

    By default this is a pure probe: it never creates anything. That matters
    because the check runs while the user is still typing a path - creating
    folders as a side effect of a *question* would litter the disk with
    directories nobody confirmed. Pass ``create=True`` only from code that has
    already decided to write there (``Database._ensure_parent_dir``).
    """
    if not directory:
        return False, "no directory given"
    if os.path.exists(directory) and not os.path.isdir(directory):
        return False, "%s exists but is not a directory" % directory
    try:
        if not os.path.isdir(directory):
            if not create:
                return False, "the directory does not exist yet"
            os.makedirs(directory)
        probe = os.path.join(directory, ".cartridge-write-probe.tmp")
        with open(probe, "wb") as handle:
            handle.write(b"probe")
        os.remove(probe)
        return True, "writable"
    except OSError as exc:
        return False, "not writable: %s" % exc


def probe_writable_for_creation(directory: str) -> Tuple[bool, str]:
    """Can something be created inside ``directory``, even if it does not exist?

    Answers by probing the nearest *existing* ancestor: if ``E:\\Gry`` is
    writable then ``E:\\Gry\\.cartridge`` can be created, and a fresh root must
    not be reported as unwritable just because Cartridge has not written to it
    yet. Probing never creates anything.
    """
    current = os.path.abspath(directory or ".")
    while current and not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    if not current or not os.path.isdir(current):
        return False, "no existing parent directory could be found"
    return is_writable_location(current, create=False)


def suggest_fallback_db_path(root: str) -> str:
    """A user-visible alternative when the games root cannot be written.

    Never silently used: the caller must show this to the user and ask
    (brief section 7).
    """
    base = os.path.join(os.path.expanduser("~"), APP_DATA_DIR.lstrip("."), "collections")
    safe = "".join(ch if ch.isalnum() else "_" for ch in os.path.abspath(root))
    return os.path.join(base, safe, "collection.db")
