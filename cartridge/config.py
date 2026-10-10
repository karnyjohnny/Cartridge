"""Application configuration and credential resolution.

Credential precedence (highest first):

1. **Environment variables** — ``CARTRIDGE_IGDB_CLIENT_ID``,
   ``CARTRIDGE_IGDB_CLIENT_SECRET``, ``CARTRIDGE_RAWG_API_KEY``. Session-only,
   never persisted. This is what the opt-in live tests and a power user's
   launcher script use.
2. **The secret store** (:mod:`cartridge.core.secret_store`) — DPAPI-encrypted on
   Windows, a 0600 local file elsewhere, memory-only if neither is available.
   This is what the Settings dialog writes to.
3. Nothing — the app runs fully offline and says so in Settings.

Values are registered with the redactor as soon as they are loaded, so any later
error message, log line or diagnostics export that accidentally interpolates one
is scrubbed. Credentials are never written to SQLite (the repository refuses
those keys) and never appear in :meth:`Credentials.masked`.

Non-secret preferences (roots, grid columns, view mode, artwork options) live in
the SQLite ``settings`` table next to the collection they describe.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cartridge import __app_name__, __version__
from cartridge.core.redaction import Redactor, default_redactor
from cartridge.core.secret_store import SecretStore

ENV_IGDB_CLIENT_ID = "CARTRIDGE_IGDB_CLIENT_ID"
ENV_IGDB_CLIENT_SECRET = "CARTRIDGE_IGDB_CLIENT_SECRET"
ENV_RAWG_API_KEY = "CARTRIDGE_RAWG_API_KEY"
ENV_LIVE_TESTS = "CARTRIDGE_LIVE"
ENV_ROOT = "CARTRIDGE_ROOT"
ENV_DB = "CARTRIDGE_DB"
ENV_CONFIG_DIR = "CARTRIDGE_CONFIG_DIR"
ENV_DEBUG = "CARTRIDGE_DEBUG"

STORE_KEY_IGDB_CLIENT_ID = "igdb_client_id"
STORE_KEY_IGDB_CLIENT_SECRET = "igdb_client_secret"
STORE_KEY_RAWG_API_KEY = "rawg_api_key"


@dataclass
class Credentials(object):
    """API credentials plus where each one came from."""

    igdb_client_id: str = ""
    igdb_client_secret: str = ""
    rawg_api_key: str = ""
    sources: Dict[str, str] = field(default_factory=dict)

    @property
    def igdb_configured(self) -> bool:
        return bool(self.igdb_client_id and self.igdb_client_secret)

    @property
    def rawg_configured(self) -> bool:
        return bool(self.rawg_api_key)

    @property
    def any_configured(self) -> bool:
        return self.igdb_configured or self.rawg_configured

    def values(self) -> List[str]:
        return [
            value for value in (
                self.igdb_client_id, self.igdb_client_secret, self.rawg_api_key
            ) if value
        ]

    def masked(self) -> Dict[str, Any]:
        """Safe-to-display/serialize view. Never contains a usable secret."""
        return {
            "igdb_client_id": _mask(self.igdb_client_id),
            "igdb_client_secret": _mask(self.igdb_client_secret),
            "rawg_api_key": _mask(self.rawg_api_key),
            "igdb_configured": self.igdb_configured,
            "rawg_configured": self.rawg_configured,
            "sources": dict(self.sources),
        }

    def register_with(self, redactor: Redactor) -> None:
        for value in self.values():
            redactor.register(value)


def _mask(value: str) -> str:
    """``"abcd1234..."`` for a set value, ``""`` when unset. Never reversible."""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:4] + "*" * min(8, len(value) - 4)


def load_credentials(
    store: Optional[SecretStore] = None,
    environ: Optional[Dict[str, str]] = None,
    redactor: Optional[Redactor] = None,
) -> Credentials:
    """Resolve credentials from the environment first, then the secret store."""
    env = os.environ if environ is None else environ
    red = redactor or default_redactor()
    credentials = Credentials()

    client_id = (env.get(ENV_IGDB_CLIENT_ID) or "").strip()
    client_secret = (env.get(ENV_IGDB_CLIENT_SECRET) or "").strip()
    rawg_key = (env.get(ENV_RAWG_API_KEY) or "").strip()

    if client_id:
        credentials.igdb_client_id = client_id
        credentials.sources[STORE_KEY_IGDB_CLIENT_ID] = "environment"
    if client_secret:
        credentials.igdb_client_secret = client_secret
        credentials.sources[STORE_KEY_IGDB_CLIENT_SECRET] = "environment"
    if rawg_key:
        credentials.rawg_api_key = rawg_key
        credentials.sources[STORE_KEY_RAWG_API_KEY] = "environment"

    if store is not None:
        if not credentials.igdb_client_id:
            value = store.get(STORE_KEY_IGDB_CLIENT_ID)
            if value:
                credentials.igdb_client_id = value.strip()
                credentials.sources[STORE_KEY_IGDB_CLIENT_ID] = "secret store (%s)" % store.backend
        if not credentials.igdb_client_secret:
            value = store.get(STORE_KEY_IGDB_CLIENT_SECRET)
            if value:
                credentials.igdb_client_secret = value.strip()
                credentials.sources[STORE_KEY_IGDB_CLIENT_SECRET] = "secret store (%s)" % store.backend
        if not credentials.rawg_api_key:
            value = store.get(STORE_KEY_RAWG_API_KEY)
            if value:
                credentials.rawg_api_key = value.strip()
                credentials.sources[STORE_KEY_RAWG_API_KEY] = "secret store (%s)" % store.backend

    credentials.register_with(red)
    return credentials


def save_credentials(store: SecretStore, credentials: Credentials) -> Dict[str, str]:
    """Persist credentials through the secret store (never SQLite)."""
    written = {}
    if credentials.igdb_client_id:
        store.set(STORE_KEY_IGDB_CLIENT_ID, credentials.igdb_client_id)
        written[STORE_KEY_IGDB_CLIENT_ID] = store.backend
    if credentials.igdb_client_secret:
        store.set(STORE_KEY_IGDB_CLIENT_SECRET, credentials.igdb_client_secret)
        written[STORE_KEY_IGDB_CLIENT_SECRET] = store.backend
    if credentials.rawg_api_key:
        store.set(STORE_KEY_RAWG_API_KEY, credentials.rawg_api_key)
        written[STORE_KEY_RAWG_API_KEY] = store.backend
    return written


def clear_credentials(store: SecretStore) -> None:
    for key in (STORE_KEY_IGDB_CLIENT_ID, STORE_KEY_IGDB_CLIENT_SECRET, STORE_KEY_RAWG_API_KEY):
        store.delete(key)


def config_dir(environ: Optional[Dict[str, str]] = None) -> str:
    """Cartridge's per-user config directory (overridable for tests)."""
    from cartridge.core.secret_store import user_config_dir

    env = os.environ if environ is None else environ
    override = (env.get(ENV_CONFIG_DIR) or "").strip()
    if override:
        return override
    return user_config_dir(__app_name__)


def live_tests_enabled(environ: Optional[Dict[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(ENV_LIVE_TESTS, "")).strip().lower() in ("1", "true", "yes", "on")


def debug_enabled(environ: Optional[Dict[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(ENV_DEBUG, "")).strip().lower() in ("1", "true", "yes", "on")


def resolve_db_path(
    root: Optional[str], override: Optional[str] = None, windows: Optional[bool] = None
) -> Dict[str, Any]:
    """Decide where the database goes, and whether that place is writable.

    Returns a dict rather than raising, because the caller (first-run dialog or
    Settings) must *show* the situation to the user and offer the alternative —
    brief section 7 forbids silently writing somewhere unexpected.
    """
    from cartridge.db.connection import (
        probe_writable_for_creation,
        suggest_fallback_db_path,
    )
    from cartridge.paths import db_path_for_root, normalize_path

    result: Dict[str, Any] = {
        "root": root or "",
        "db_path": "",
        "writable": False,
        "reason": "",
        "fallback": "",
        "used_fallback": False,
    }
    if override:
        candidate = normalize_path(override, windows)
        result["db_path"] = candidate
        parent = os.path.dirname(candidate) or "."
        ok, message = probe_writable_for_creation(parent)
        result["writable"] = ok
        result["reason"] = message
        return result

    if not root:
        result["reason"] = "No games root folder is configured yet."
        return result

    normalized_root = normalize_path(root, windows)
    result["root"] = normalized_root
    candidate = db_path_for_root(normalized_root, windows)
    result["db_path"] = candidate
    ok, message = probe_writable_for_creation(os.path.dirname(candidate))
    result["writable"] = ok
    result["reason"] = message
    if not ok:
        result["fallback"] = suggest_fallback_db_path(normalized_root)
    return result


def default_roots(environ: Optional[Dict[str, str]] = None) -> List[str]:
    env = os.environ if environ is None else environ
    value = (env.get(ENV_ROOT) or "").strip()
    return [value] if value else []


def environment_info() -> Dict[str, Any]:
    """Versions and platform facts for Diagnostics. Contains no secrets."""
    info: Dict[str, Any] = {
        "app_version": __version__,
        "python_version": sys.version.split()[0],
        "python_implementation": sys.implementation.name,
        "platform": sys.platform,
        "os_release": "",
        "machine": "",
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
    }
    try:
        import platform as _platform

        info["os_release"] = _platform.platform()
        info["machine"] = _platform.machine()
    except Exception:
        pass
    try:
        import sqlite3

        info["sqlite_library"] = sqlite3.sqlite_version
        info["sqlite_module"] = getattr(sqlite3, "version", "unknown")
    except Exception:
        pass
    try:
        import httpx

        info["httpx_version"] = httpx.__version__
    except Exception:
        info["httpx_version"] = "not installed"
    try:
        from PyQt5.QtCore import PYQT_VERSION_STR, qVersion

        info["pyqt5_version"] = PYQT_VERSION_STR
        info["qt_runtime_version"] = qVersion()
    except Exception:
        info["pyqt5_version"] = "not available"
        info["qt_runtime_version"] = "not available"
    return info


def is_windows_target() -> bool:
    return sys.platform.startswith("win")
