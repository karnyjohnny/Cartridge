"""Bounded in-memory image cache.

On a 2 GB machine with software rendering, an unbounded ``QPixmap`` cache is how
an application dies: scrolling a 1,000-cover grid decodes a new image per card and
nothing is ever released. This cache is therefore hard-bounded on **both** entry
count and estimated bytes, evicts least-recently-used, and degrades to "decode
again" rather than growing.

Sizes are estimated from the pixel geometry (width * height * 4 bytes for ARGB32)
because that is what actually consumes RAM — the JPEG file size on disk is
irrelevant once decoded.

Thread rule: Qt paint devices belong to the GUI thread. Workers decode into
``QImage`` (thread-safe, no paint device) and hand that back; the cache stores the
``QPixmap`` conversion, which only ever happens on the GUI thread. The cache
itself is guarded by a lock so its bookkeeping stays correct either way.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Dict, Hashable, Optional, Tuple

DEFAULT_MAX_ENTRIES = 96
DEFAULT_MAX_BYTES = 48 * 1024 * 1024      # 48 MB of decoded pixels
BYTES_PER_PIXEL = 4                        # ARGB32

# Cache keys must never be a raw path only: the same file can be requested at
# several display sizes, and reusing the wrong size makes text blurry.
Key = Tuple[str, int, int]


def make_key(path: str, width: int = 0, height: int = 0) -> Key:
    """Cache key: path + requested size (0,0 means "as decoded")."""
    return (str(path or ""), int(width), int(height))


def estimate_bytes(pixmap: Any) -> int:
    """Estimated RAM cost of a decoded pixmap."""
    try:
        width = int(pixmap.width())
        height = int(pixmap.height())
    except Exception:
        return 0
    if width <= 0 or height <= 0:
        return 0
    depth = 0
    try:
        depth = int(pixmap.depth())
    except Exception:
        depth = 0
    per_pixel = max(1, depth // 8) if depth else BYTES_PER_PIXEL
    return width * height * per_pixel


class ImageCache(object):
    """LRU cache of decoded pixmaps with a byte ceiling."""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1024, int(max_bytes))
        self._entries: "OrderedDict[Key, Any]" = OrderedDict()
        self._costs: Dict[Key, int] = {}
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.decoded = 0

    # ------------------------------------------------------------------
    def get(self, key: Key) -> Optional[Any]:
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                self.hits += 1
                return self._entries[key]
            self.misses += 1
            return None

    def put(self, key: Key, pixmap: Any) -> None:
        if pixmap is None:
            return
        try:
            if pixmap.isNull():
                return
        except Exception:
            return
        cost = estimate_bytes(pixmap)
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                self._entries[key] = pixmap
                self._costs[key] = cost
            else:
                self._entries[key] = pixmap
                self._costs[key] = cost
                self.decoded += 1
            self._evict()

    def _evict(self) -> None:
        """Drop least-recently-used entries until both ceilings are met."""
        while self._entries and (
            len(self._entries) > self.max_entries or self.current_bytes > self.max_bytes
        ):
            key, _value = self._entries.popitem(last=False)
            self._costs.pop(key, None)
            self.evictions += 1

    # ------------------------------------------------------------------
    @property
    def current_bytes(self) -> int:
        return sum(self._costs.values())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._costs.clear()

    def invalidate(self, path: str) -> int:
        """Drop every cached size of one file (after a re-download)."""
        removed = 0
        with self._lock:
            for key in [k for k in self._entries if k[0] == str(path or "")]:
                self._entries.pop(key, None)
                self._costs.pop(key, None)
                removed += 1
        return removed

    def contains(self, key: Key) -> bool:
        with self._lock:
            return key in self._entries

    def stats(self) -> Dict[str, Any]:
        """Numbers for the Diagnostics view. Real counts, never estimates."""
        with self._lock:
            lookups = self.hits + self.misses
            return {
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "bytes": self.current_bytes,
                "max_bytes": self.max_bytes,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / float(lookups), 3) if lookups else 0.0,
                "evictions": self.evictions,
                "decoded": self.decoded,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class ThumbCache(object):
    """Disk-thumbnail helper: build once, reuse forever.

    Thumbnails are cached on disk next to the artwork
    (``_cartridge/assets/thumbs``), so a second launch does not re-decode
    anything. This class only decides *whether* work is needed; the actual
    scaling lives in :mod:`cartridge.artwork.images`.
    """

    def __init__(self, memory: Optional[ImageCache] = None):
        self.memory = memory if memory is not None else ImageCache()
        self.built = 0
        self.reused = 0

    def needs_build(self, source: str, destination: str) -> bool:
        import os

        if not os.path.exists(destination):
            return True
        try:
            return os.path.getmtime(destination) < os.path.getmtime(source)
        except OSError:
            return True

    def note(self, built: bool) -> None:
        if built:
            self.built += 1
        else:
            self.reused += 1

    def stats(self) -> Dict[str, Any]:
        return {
            "thumbnails_built": self.built,
            "thumbnails_reused": self.reused,
            "memory": self.memory.stats(),
        }
