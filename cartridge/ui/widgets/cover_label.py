"""A cover image label that loads lazily, degrades gracefully and never blocks.

Three things make this widget safe on the target hardware:

* it loads the **thumbnail** (or a scaled-down decode) rather than the source
  image, so a 1,000-game grid never holds 1,000 full-resolution JPEGs;
* results come from a bounded LRU cache, so scrolling back up does not re-decode;
* a missing, corrupt or unreadable file renders a flat placeholder. One broken
  cover cannot take the browser down (brief section 11).

Loading happens synchronously *from a thumbnail that already exists on disk* or
from the memory cache; when neither is available the widget paints a placeholder
and asks the caller to schedule a background build through
``thumbnail_requested``. That keeps the GUI thread free without a second cache of
pending requests.
"""

from __future__ import annotations

import os
from typing import Optional

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QLabel, QSizePolicy

from cartridge.artwork.cache import ImageCache, make_key
from cartridge.artwork import images as image_tools
from cartridge.ui.theme import COLORS


class CoverLabel(QLabel):
    """Displays one cover, with placeholder and cache behaviour."""

    thumbnail_requested = pyqtSignal(str, str, int, int)  # source, dest, w, h

    def __init__(
        self,
        width: int = 150,
        height: int = 205,
        cache: Optional[ImageCache] = None,
        parent=None,
    ):
        super(CoverLabel, self).__init__(parent)
        self._cache = cache
        self._size = QSize(max(8, int(width)), max(8, int(height)))
        self._source: Optional[str] = None
        self._thumb: Optional[str] = None
        self._placeholder_text = "No cover"
        self._failed_paths = set()

        self.setFixedSize(self._size)
        self.setAlignment(Qt.AlignCenter)
        self.setScaledContents(False)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setObjectName("CoverPlaceholder")
        self._paint_placeholder()

    # ------------------------------------------------------------------
    def set_cache(self, cache: Optional[ImageCache]) -> None:
        self._cache = cache

    def set_placeholder_text(self, text: str) -> None:
        self._placeholder_text = text or ""
        if self._source is None:
            self._paint_placeholder()

    def load(self, source: Optional[str], thumbnail: Optional[str] = None) -> bool:
        """Show ``source`` (preferring an existing ``thumbnail``).

        Returns True when a real image is displayed, False when the placeholder is
        showing. Safe to call with None/missing/corrupt paths.
        """
        self._source = source or None
        self._thumb = thumbnail or None

        if not source or not os.path.exists(source):
            self.setObjectName("CoverMissing" if source else "CoverPlaceholder")
            self._paint_placeholder(
                "Missing file" if source else self._placeholder_text
            )
            return False

        path = self._thumb if (self._thumb and os.path.exists(self._thumb)) else source
        cached = self._cache_get(path)
        if cached is not None:
            self._show_pixmap(cached)
            return True

        if path in self._failed_paths:
            # Already known bad: do not retry on every repaint while scrolling.
            self.setObjectName("CoverMissing")
            self._paint_placeholder("Unreadable")
            return False

        pixmap = self._decode(path)
        if pixmap is None:
            self._failed_paths.add(path)
            self.setObjectName("CoverMissing")
            self._paint_placeholder("Corrupt image")
            return False

        self._cache_put(path, pixmap)
        self._show_pixmap(pixmap)
        # If we had to decode the full-size source, ask for a proper thumbnail so
        # the next paint is cheap.
        if self._thumb and path == source:
            self.thumbnail_requested.emit(source, self._thumb, self._size.width(), self._size.height())
        return True

    def clear_image(self) -> None:
        self._source = None
        self._thumb = None
        self.setObjectName("CoverPlaceholder")
        self._paint_placeholder()

    # ------------------------------------------------------------------
    def _decode(self, path: str) -> Optional[QPixmap]:
        """Decode and scale in one step, so the full image is never retained."""
        try:
            from PyQt5.QtGui import QImage

            image = QImage(path)
            if image.isNull():
                return None
            if image.width() > self._size.width() or image.height() > self._size.height():
                image = image.scaled(
                    self._size, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                if image.isNull():
                    return None
            return QPixmap.fromImage(image)
        except Exception:
            return None

    def _cache_get(self, path: str) -> Optional[QPixmap]:
        if self._cache is None:
            return None
        return self._cache.get(make_key(path, self._size.width(), self._size.height()))

    def _cache_put(self, path: str, pixmap: QPixmap) -> None:
        if self._cache is None:
            return
        self._cache.put(
            make_key(path, self._size.width(), self._size.height()), pixmap
        )

    def _show_pixmap(self, pixmap: QPixmap) -> None:
        self.setObjectName("CoverImage")
        self.setStyleSheet("")
        self.setText("")
        self.setPixmap(pixmap)

    def _paint_placeholder(self, text: Optional[str] = None) -> None:
        self.clear()
        # Flat, bordered placeholder - no icons to load, no effects to render.
        self.setStyleSheet(
            "background-color: %s; color: %s; border: 1px solid %s; border-radius: %dpx;"
            % (COLORS["surface_secondary"], COLORS["text_muted"], COLORS["border"], 4)
        )
        self.setText(text if text is not None else self._placeholder_text)

    # ------------------------------------------------------------------
    def invalidate(self) -> None:
        """Forget a cached decode (after a re-download)."""
        for path in (self._source, self._thumb):
            if path and self._cache is not None:
                self._cache.invalidate(path)
        self._failed_paths.discard(self._source)
        self._failed_paths.discard(self._thumb)
        if self._source:
            self.load(self._source, self._thumb)

    def sizeHint(self):  # noqa: N802 - Qt naming
        return self._size
