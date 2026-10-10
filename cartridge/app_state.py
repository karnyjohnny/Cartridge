"""Application state: the object graph the UI talks to.

Everything expensive or stateful lives here — the database, the worker pool, the
image cache, the provider service, the secret store — so views stay thin and
testable, and so there is exactly one owner for each resource's lifetime.

Thread rules enforced by this module:

* the database is opened once and hands each thread its own connection
  (:class:`cartridge.db.connection.Database`);
* all blocking work goes through :attr:`pool`;
* the image cache is only touched on the GUI thread.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from cartridge import __version__
from cartridge.artwork.cache import ImageCache, ThumbCache
from cartridge.artwork.downloader import ArtworkDownloader
from cartridge.artwork.store import ArtworkStore
from cartridge.config import (
    Credentials,
    config_dir,
    load_credentials,
    save_credentials,
)
from cartridge.core.clock import monotonic
from cartridge.core.redaction import Redactor, default_redactor
from cartridge.core.secret_store import SecretStore
from cartridge.core.workers import WorkerPool
from cartridge.db.connection import Database, DatabaseError
from cartridge.db.repository import Repository
from cartridge.providers.base import HttpSettings, HttpClient
from cartridge.providers.ratelimit import LimiterSet
from cartridge.providers.service import MetadataService


class AppState(object):
    """Owns every long-lived resource the UI needs."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        secret_store: Optional[SecretStore] = None,
        redactor: Optional[Redactor] = None,
        max_workers: int = 3,
        windows: Optional[bool] = None,
        offline: bool = False,
    ):
        self.started_at = monotonic()
        self.version = __version__
        self.redactor = redactor or default_redactor()
        self.windows = windows
        self.offline = offline

        self.secret_store = secret_store or SecretStore(directory=config_dir())
        self.credentials: Credentials = load_credentials(
            store=self.secret_store, redactor=self.redactor
        )

        self.pool = WorkerPool(max_threads=max_workers)
        self.image_cache = ImageCache()
        self.thumb_cache = ThumbCache(self.image_cache)
        self.asset_store = ArtworkStore(windows=windows)
        self.limiters = LimiterSet()

        self.http = HttpClient(
            settings=HttpSettings(),
            redactor=self.redactor,
            latency_hook=self._record_latency,
        )
        self.service = MetadataService(
            client=self.http, redactor=self.redactor, limiters=self.limiters
        )
        self.downloader = ArtworkDownloader(
            client=self.http,
            store=self.asset_store,
            limiter=self.limiters.images,
            windows=windows,
        )

        self.db: Optional[Database] = None
        self.repo: Optional[Repository] = None
        self.db_path: Optional[str] = db_path
        self.open_errors: List[str] = []
        self._latency_sink: Optional[Callable[[str, float], None]] = None

        self.apply_credentials_to_service()

    # ------------------------------------------------------------------
    # database
    # ------------------------------------------------------------------
    def open_database(self, db_path: Optional[str] = None) -> bool:
        """Open (creating/migrating as needed) the collection database."""
        target = db_path or self.db_path
        if not target:
            self.open_errors.append("No database path is configured.")
            return False
        self.close_database()
        try:
            self.db = Database(target).open()
        except DatabaseError as exc:
            self.open_errors.append(str(exc))
            self.db = None
            self.repo = None
            return False
        except Exception as exc:  # pragma: no cover - unexpected driver failure
            self.open_errors.append(
                "Unexpected error opening %s: %s" % (target, self.redactor.text(str(exc)))
            )
            self.db = None
            self.repo = None
            return False

        self.db_path = target
        self.repo = Repository(self.db, windows=self.windows)
        self._latency_sink = self._record_db_latency
        return True

    def close_database(self) -> None:
        self._latency_sink = None
        if self.db is not None:
            try:
                self.db.close_all()
            except Exception:
                pass
        self.db = None
        self.repo = None

    @property
    def ready(self) -> bool:
        return self.repo is not None

    def db_info(self) -> Dict[str, Any]:
        if self.db is None:
            return {"open": False, "path": self.db_path, "errors": list(self.open_errors)}
        info = self.db.info()
        info["open"] = True
        info["errors"] = list(self.open_errors)
        return info

    # ------------------------------------------------------------------
    # settings (non-secret, stored next to the collection)
    # ------------------------------------------------------------------
    def setting(self, key: str, default: Any = None) -> Any:
        if self.repo is None:
            return default
        return self.repo.get_setting(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        if self.repo is not None:
            self.repo.set_setting(key, value)

    def roots(self) -> List[Any]:
        if self.repo is None:
            return []
        return self.repo.list_roots()

    def root_paths(self) -> List[str]:
        return [root.path for root in self.roots() if root.enabled]

    # ------------------------------------------------------------------
    # credentials
    # ------------------------------------------------------------------
    def apply_credentials_to_service(self) -> None:
        self.service.set_credentials(
            self.credentials.igdb_client_id,
            self.credentials.igdb_client_secret,
            self.credentials.rawg_api_key,
        )

    def save_credentials(
        self,
        igdb_client_id: Optional[str] = None,
        igdb_client_secret: Optional[str] = None,
        rawg_api_key: Optional[str] = None,
    ) -> Dict[str, str]:
        """Persist credentials to the secret store and apply them immediately."""
        if igdb_client_id is not None:
            self.credentials.igdb_client_id = igdb_client_id.strip()
        if igdb_client_secret is not None:
            self.credentials.igdb_client_secret = igdb_client_secret.strip()
        if rawg_api_key is not None:
            self.credentials.rawg_api_key = rawg_api_key.strip()
        self.credentials.register_with(self.redactor)
        written = save_credentials(self.secret_store, self.credentials)
        self.apply_credentials_to_service()
        return written

    def clear_saved_credentials(self) -> None:
        from cartridge.config import clear_credentials

        clear_credentials(self.secret_store)
        self.credentials = load_credentials(store=self.secret_store, redactor=self.redactor)
        self.apply_credentials_to_service()

    def provider_status(self) -> Dict[str, Any]:
        status = self.service.status()
        status["secret_store"] = self.secret_store.describe()
        status["credentials"] = self.credentials.masked()
        return status

    # ------------------------------------------------------------------
    # latency instrumentation (feeds Diagnostics with real numbers)
    # ------------------------------------------------------------------
    def _record_latency(self, operation: str, latency_ms: float, attempts: int) -> None:
        detail = "attempts=%d" % attempts if attempts > 1 else ""
        self._store_sample(operation, latency_ms, detail)

    def _record_db_latency(self, operation: str, latency_ms: float) -> None:
        self._store_sample(operation, latency_ms, "")

    def _store_sample(self, operation: str, latency_ms: float, detail: str) -> None:
        if self.repo is None:
            return
        try:
            self.repo.record_sample(operation, float(latency_ms), detail)
        except Exception:
            # Instrumentation must never break the operation it measures.
            pass

    def time(self, operation: str) -> "_Timer":
        """Context manager that records how long a block took."""
        return _Timer(self, operation)

    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "version": self.version,
            "db_open": self.ready,
            "workers": self.pool.stats(),
            "image_cache": self.image_cache.stats(),
            "thumbnails": self.thumb_cache.stats(),
            "http": {
                "requests": self.http.request_count,
                "retries": self.http.retry_count,
                "throttled": self.http.throttled_count,
            },
            "rate_limits": self.limiters.summary(),
            "providers": self.service.status(),
            "secret_store": self.secret_store.describe(),
        }
        if self.repo is not None:
            out["collection"] = self.repo.stats()
            out["latency"] = self.repo.sample_stats()
        return out

    def shutdown(self) -> None:
        """Release everything, in dependency order."""
        self.pool.shutdown(4000)
        self.service.close()
        try:
            self.http.close()
        except Exception:
            pass
        self.close_database()


class _Timer(object):
    """``with state.time("search"): ...`` records a real latency sample."""

    def __init__(self, state: AppState, operation: str):
        self.state = state
        self.operation = operation
        self.elapsed_ms = 0.0

    def __enter__(self) -> "_Timer":
        self._started = monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.elapsed_ms = (monotonic() - self._started) * 1000.0
        self.state._store_sample(self.operation, self.elapsed_ms, "")
        return None
