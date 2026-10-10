"""Sanitized diagnostics collection and export.

Brief section 6.7 and 15 define what this may contain: real, currently available
measurements, and **no** credentials, tokens, full private paths, game titles or
other user-specific data unless the user explicitly chooses the local export that
warns about exactly that.

So there are two report shapes, built by two different functions:

``collect()``
    the shareable report. Versions, database health, cache and worker counters,
    latency samples and API connectivity state. Paths are reduced to their last
    component, collection counts stay numeric, and every string passes through the
    redactor. This is what Diagnostics shows and what "Copy report" copies.

``collect_local()``
    everything above plus the real absolute paths and the game list. Returned only
    to the caller that asked for it, and the UI states in plain words what the file
    will contain before writing it.

Nothing here fabricates a number: a measurement that was never taken is reported
as ``None`` and rendered as "not measured".
"""

from __future__ import annotations

import os
import platform
import sys
from typing import Any, Dict, List, Optional

from cartridge import __version__
from cartridge.app_state import AppState
from cartridge.core.clock import now_iso
from cartridge.core.redaction import Redactor, default_redactor

NOT_MEASURED = None


_WIN_MEMORY_ERROR = ["not attempted"]


def _windows_memory_error() -> str:
    return _WIN_MEMORY_ERROR[0]


def _windows_process_memory() -> Optional[Dict[str, Any]]:
    """Working-set figures through GetProcessMemoryInfo, the Win7-safe way.

    The naive version failed on a real Windows 7 x64 frozen build
    (``GetProcessMemoryInfo failed``, reported by the user 2026-10-10). Three
    classic ctypes traps, all handled here:

    * an untyped ``GetCurrentProcess()`` return value can lose the 64-bit
      pseudo-handle, so argtypes/restype are declared explicitly;
    * ``psapi.dll`` is not the only entry point - Windows 7 also exports the
      same function from ``kernel32`` as ``K32GetProcessMemoryInfo``;
    * a bare "failed" tells nobody anything, so the Win32 error code of every
      attempt is recorded and shown in Diagnostics.
    """
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetLastError.restype = wintypes.DWORD

    candidates = []
    try:
        candidates.append(("psapi.GetProcessMemoryInfo", ctypes.windll.psapi.GetProcessMemoryInfo))
    except Exception as exc:  # pragma: no cover - loader problem
        candidates.append(("psapi load failed: %s" % type(exc).__name__, None))
    for name in ("K32GetProcessMemoryInfo", "GetProcessMemoryInfo"):
        fn = getattr(kernel32, name, None)
        if fn is not None:
            candidates.append(("kernel32.%s" % name, fn))

    handle = kernel32.GetCurrentProcess()
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)

    tried = []
    for label, fn in candidates:
        if fn is None:
            tried.append(label)
            continue
        try:
            fn.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD,
            ]
            fn.restype = wintypes.BOOL
        except Exception:
            pass
        try:
            ok = fn(handle, ctypes.byref(counters), counters.cb)
        except Exception as exc:  # pragma: no cover - loader/ABI problem
            tried.append("%s raised %s" % (label, type(exc).__name__))
            continue
        if ok:
            return {
                "working_set_bytes": int(counters.WorkingSetSize),
                "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
                "private_bytes": int(counters.PagefileUsage),
                "source": label,
            }
        tried.append("%s -> Win32 error %d" % (label, kernel32.GetLastError()))

    _WIN_MEMORY_ERROR[0] = "; ".join(tried) or "no entry point available"
    return None


def _basename(path: Optional[str]) -> Optional[str]:
    """Last component only: enough to be useful, not enough to reveal a layout."""
    if not path:
        return None
    return os.path.basename(str(path).rstrip("\\/")) or str(path)


def _drive_letter(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    text = str(path)
    if len(text) >= 2 and text[1] == ":":
        return text[0].upper() + ":"
    return None


def process_memory() -> Dict[str, Any]:
    """Working-set figures, or ``None`` when they cannot be measured honestly.

    Windows uses ``GetProcessMemoryInfo`` through ctypes (no extra dependency and
    no Python 3.8 problem). Elsewhere ``/proc/self/status`` is used on Linux. If
    neither is available the result says so rather than guessing.
    """
    result: Dict[str, Any] = {
        "working_set_bytes": NOT_MEASURED,
        "peak_working_set_bytes": NOT_MEASURED,
        "private_bytes": NOT_MEASURED,
        "source": "unavailable",
    }
    try:
        if sys.platform.startswith("win"):
            measured = _windows_process_memory()
            if measured is not None:
                return measured
            result["source"] = _windows_memory_error()
            return result

        status_path = "/proc/self/status"
        if os.path.exists(status_path):
            values: Dict[str, int] = {}
            with open(status_path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if ":" not in line:
                        continue
                    key, _, raw = line.partition(":")
                    key = key.strip()
                    if key in ("VmRSS", "VmHWM", "VmSize"):
                        digits = "".join(ch for ch in raw if ch.isdigit())
                        if digits:
                            values[key] = int(digits) * 1024
            if values:
                result["working_set_bytes"] = values.get("VmRSS", NOT_MEASURED)
                result["peak_working_set_bytes"] = values.get("VmHWM", NOT_MEASURED)
                result["private_bytes"] = values.get("VmSize", NOT_MEASURED)
                result["source"] = "/proc/self/status"
            return result
    except Exception as exc:  # pragma: no cover - platform specific
        result["source"] = "error: %s" % type(exc).__name__
    return result


def collect(state: AppState, redactor: Optional[Redactor] = None) -> Dict[str, Any]:
    """Build the shareable diagnostics report. Contains no user-specific data."""
    red = redactor or default_redactor()
    report: Dict[str, Any] = {
        "report_kind": "shareable",
        "generated_at": now_iso(),
        "app": {
            "name": "Cartridge",
            "version": __version__,
            "frozen": bool(getattr(sys, "frozen", False)),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
        },
        "memory": process_memory(),
        "database": _database_section(state, red),
        "collection": _collection_section(state),
        "workers": state.pool.stats(),
        "image_cache": state.image_cache.stats(),
        "thumbnails": state.thumb_cache.stats(),
        "http": {
            "requests": state.http.request_count,
            "retries": state.http.retry_count,
            "throttled": state.http.throttled_count,
            "timeouts": {
                "connect": state.http.settings.connect_timeout,
                "read": state.http.settings.read_timeout,
                "write": state.http.settings.write_timeout,
                "pool": state.http.settings.pool_timeout,
            },
        },
        "rate_limits": state.limiters.summary(),
        "providers": _provider_section(state, red),
        "secret_store": _secret_store_section(state, red),
        "latency": _latency_section(state),
        "startup": _startup_section(state),
    }
    return _scrub(report, red)


def collect_local(state: AppState, redactor: Optional[Redactor] = None) -> Dict[str, Any]:
    """Shareable report plus real paths and titles. Explicit user choice only."""
    red = redactor or default_redactor()
    report = collect(state, red)
    report["report_kind"] = "local"
    report["warning"] = (
        "This file contains absolute folder paths and game titles from your "
        "collection. Do not share it publicly. It never contains API credentials."
    )
    report["paths"] = {
        "database": state.db_path,
        "config_dir": state.secret_store.directory,
        "roots": [root.path for root in state.roots()],
    }
    if state.repo is not None:
        try:
            report["games"] = [
                {
                    "title": game.effective_title(),
                    "folder": game.folder_path,
                    "folder_state": game.folder_state,
                    "metadata_state": game.metadata_state,
                    "artwork": game.artwork_state(),
                    "size_bytes": game.folder_size_bytes,
                }
                for game in state.repo.all_games()
            ]
        except Exception as exc:
            report["games_error"] = red.text(str(exc))
    # Even the local report must not leak a credential.
    return _scrub(report, red)


def _database_section(state: AppState, red: Redactor) -> Dict[str, Any]:
    info = state.db_info()
    return {
        "open": bool(info.get("open")),
        "filename": _basename(info.get("path")),
        "drive": _drive_letter(info.get("path")),
        "size_bytes": info.get("size_bytes"),
        "schema_version": info.get("schema_version"),
        "latest_supported_schema": info.get("latest_supported_version"),
        "journal_mode": info.get("journal_mode"),
        "synchronous": info.get("synchronous"),
        "foreign_keys_enabled": bool(info.get("foreign_keys")),
        "integrity_ok": info.get("integrity_ok"),
        "integrity": info.get("integrity"),
        "migrations": info.get("migrations"),
        "open_errors": [red.text(item) for item in (info.get("errors") or [])],
    }


def _collection_section(state: AppState) -> Dict[str, Any]:
    if state.repo is None:
        return {"available": False}
    try:
        stats = state.repo.stats()
    except Exception:
        return {"available": False}
    # Counts only: no titles, no paths.
    section: Dict[str, Any] = {"available": True}
    section.update(stats)
    return section


def _provider_section(state: AppState, red: Redactor) -> Dict[str, Any]:
    status = state.service.status()
    igdb = status.get("igdb", {})
    rawg = status.get("rawg", {})
    return {
        "configured": status.get("configured"),
        "order": status.get("order"),
        "igdb": {
            "configured": igdb.get("configured"),
            "authenticated": igdb.get("authenticated"),
            "token_seconds_until_expiry": igdb.get("seconds_until_expiry"),
            "last_error": red.text(igdb.get("last_auth_error") or ""),
        },
        "rawg": {
            "configured": rawg.get("configured"),
            "last_error": red.text(rawg.get("last_auth_error") or ""),
        },
        # A connectivity state, never a credential or a token.
        "connectivity": "unknown (use Settings → Test connection to measure)",
    }


def _secret_store_section(state: AppState, red: Redactor) -> Dict[str, Any]:
    describe = state.secret_store.describe()
    return {
        "backend": describe.get("backend"),
        "encrypted": describe.get("encrypted"),
        "filename": _basename(describe.get("path")),
        "keys_present": describe.get("keys_stored"),
        "warning": describe.get("warning"),
        "notes": [red.text(note) for note in (describe.get("notes") or [])],
    }


def _latency_section(state: AppState) -> Dict[str, Any]:
    """Real measured operation latencies, or an empty map."""
    if state.repo is None:
        return {}
    try:
        return state.repo.sample_stats()
    except Exception:
        return {}


def _startup_section(state: AppState) -> Dict[str, Any]:
    from cartridge.core.clock import monotonic

    started = getattr(state, "started_at", None)
    if started is None:
        return {"seconds_since_startup": NOT_MEASURED}
    return {"seconds_since_startup": round(monotonic() - started, 2)}


def _scrub(payload: Any, redactor: Redactor) -> Any:
    """Walk the report and redact every string. Belt and braces."""
    if isinstance(payload, dict):
        return {key: _scrub(value, redactor) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_scrub(item, redactor) for item in payload]
    if isinstance(payload, str):
        return redactor.text(payload)
    return payload


def render_text(report: Dict[str, Any]) -> str:
    """Human-readable report for the Diagnostics pane and the clipboard."""
    lines: List[str] = []

    def add(label: str, value: Any) -> None:
        if value is None:
            value = "not measured"
        elif value == "" or value == [] or value == {}:
            value = "none"
        lines.append("%-28s %s" % (label, value))

    app = report.get("app", {})
    lines.append("Cartridge diagnostics (%s report)" % report.get("report_kind", "shareable"))
    lines.append("generated %s" % report.get("generated_at"))
    lines.append("")
    lines.append("Application")
    add("version", app.get("version"))
    add("python", app.get("python"))
    add("platform", app.get("platform"))
    add("machine", app.get("machine"))
    add("cpu cores", app.get("cpu_count"))
    add("packaged build", app.get("frozen"))
    lines.append("")

    memory = report.get("memory", {})
    lines.append("Memory (this process)")
    add("working set", _mb(memory.get("working_set_bytes")))
    add("peak working set", _mb(memory.get("peak_working_set_bytes")))
    add("private bytes", _mb(memory.get("private_bytes")))
    add("source", memory.get("source"))
    lines.append("")

    database = report.get("database", {})
    lines.append("Database")
    add("open", database.get("open"))
    add("file", database.get("filename"))
    add("drive", database.get("drive"))
    add("size", _mb(database.get("size_bytes")))
    add("schema version", database.get("schema_version"))
    add("journal mode", database.get("journal_mode"))
    add("foreign keys", database.get("foreign_keys_enabled"))
    add("integrity_check", database.get("integrity"))
    for error in database.get("open_errors") or []:
        add("open error", error)
    lines.append("")

    collection = report.get("collection", {})
    if collection.get("available"):
        lines.append("Collection")
        for key in ("games", "favourites", "incomplete", "with_cover", "without_cover",
                    "assets", "roots", "content_types"):
            add(key, collection.get(key))
        lines.append("")

    workers = report.get("workers", {})
    lines.append("Background workers")
    for key in ("max_threads", "active_threads", "pending", "submitted", "completed",
                "failed", "cancelled"):
        add(key, workers.get(key))
    lines.append("")

    cache = report.get("image_cache", {})
    lines.append("Image cache")
    for key in ("entries", "max_entries", "bytes", "max_bytes", "hits", "misses",
                "hit_rate", "evictions", "decoded"):
        add(key, cache.get(key))
    lines.append("")

    limits = report.get("rate_limits", {})
    if limits:
        lines.append("API rate limits (used / allowed)")
        for name, stats in sorted(limits.items()):
            add(
                name,
                "minute %s/%s · today %s/%s · refused %s · cooldown %ss"
                % (
                    stats.get("used_this_minute"), stats.get("max_per_minute"),
                    stats.get("used_today"), stats.get("max_per_day"),
                    stats.get("refused"), stats.get("cooldown_seconds"),
                ),
            )
        lines.append("")

    providers = report.get("providers", {})
    lines.append("Providers")
    add("configured", providers.get("configured"))
    igdb = providers.get("igdb", {})
    add("igdb configured", igdb.get("configured"))
    add("igdb authenticated", igdb.get("authenticated"))
    add("igdb token expires in", igdb.get("token_seconds_until_expiry"))
    if igdb.get("last_error"):
        add("igdb last error", igdb.get("last_error"))
    rawg = providers.get("rawg", {})
    add("rawg configured", rawg.get("configured"))
    lines.append("")

    latency = report.get("latency", {})
    if latency:
        lines.append("Measured operation latency (samples from this session)")
        for operation, stats in sorted(latency.items()):
            add(
                operation,
                "n=%s avg=%s ms max=%s ms"
                % (stats.get("n"), stats.get("avg_ms"), stats.get("max_ms")),
            )
        lines.append("")

    startup = report.get("startup", {})
    lines.append("Session")
    add("seconds since startup", startup.get("seconds_since_startup"))
    lines.append("")

    store = report.get("secret_store", {})
    lines.append("Credential storage")
    add("backend", store.get("backend"))
    add("encrypted", store.get("encrypted"))
    add("keys present", store.get("keys_present"))
    if store.get("warning"):
        add("warning", store.get("warning"))
    lines.append("")
    lines.append("No API credentials, tokens, or secret values are included in this")
    lines.append("report. The shareable report also omits folder paths and titles.")
    return "\n".join(lines)


def _mb(value: Any) -> Optional[str]:
    if value is None:
        return NOT_MEASURED
    try:
        return "%.1f MB" % (float(value) / (1024.0 * 1024.0))
    except (TypeError, ValueError):
        return NOT_MEASURED
