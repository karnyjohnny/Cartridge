"""Image validation, hashing and thumbnailing.

Brief section 11 requires that a download is validated *before* it becomes part
of the collection: status, size, format and decodability. Everything here is
defensive, because artwork bytes come from the internet and a single corrupt file
must never take the browser down.

Qt is used for decoding (it is already a hard dependency and supports the formats
the providers serve). Pillow is deliberately not required.

Thumbnails are produced at display size and cached on disk, so scrolling a grid
of 1,000 covers never decodes full-resolution JPEGs — that is the difference
between a usable browser and a slideshow on a GMA 4500MHD.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Optional, Tuple

# Magic bytes for the formats IGDB/RAWG actually serve. Sniffing beats trusting a
# Content-Type header or a file extension.
MAGIC = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"RIFF", "webp"),      # RIFF....WEBP; refined below
    (b"\x00\x00\x01\x00", "ico"),
)

MIN_VALID_BYTES = 64
HASH_CHUNK = 256 * 1024

# Display sizes. Covers are shown at roughly 180x250 in the grid; thumbnails are
# generated at 2x that so HiDPI/zoom does not blur, but never at source size.
COVER_THUMB = (240, 336)
SCREENSHOT_THUMB = (320, 180)
DETAIL_COVER = (300, 420)


def sniff_format(data: bytes) -> Optional[str]:
    """Identify an image format from its magic bytes, or None.

    Works on a prefix, so it can be used on the first few bytes of a stream
    before the whole file is on disk.
    """
    if not data:
        return None
    for magic, name in MAGIC:
        if data.startswith(magic):
            if name == "webp":
                # RIFF is a container: the real marker sits at offset 8.
                if len(data) >= 12 and data[8:12] == b"WEBP":
                    return "webp"
                return None
            return name
    return None


def has_terminator(fmt: Optional[str], tail: bytes) -> bool:
    """Whether the end of a file still carries its format's terminator.

    A truncated download keeps valid magic bytes and decodes "partially", which
    is exactly how a half-finished cover ends up in a collection. Checking the
    tail catches it before the file is accepted. Formats without a cheap
    terminator (BMP, WebP) are left to the decode step.
    """
    if not tail:
        return False
    if fmt == "jpeg":
        return b"\xff\xd9" in tail[-32:]
    if fmt == "png":
        return b"IEND" in tail[-32:]
    if fmt == "gif":
        return tail.rstrip(b"\x00").endswith(b"\x3b")
    return True


def is_supported_format(name: Optional[str]) -> bool:
    return name in ("jpeg", "png", "gif", "bmp", "webp")


def sha256_file(path: str) -> Optional[str]:
    """Hash a file in chunks (``hashlib.file_digest`` is 3.11+)."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def validate_file(path: str, min_bytes: int = MIN_VALID_BYTES) -> Dict[str, Any]:
    """Validate an image file on disk without loading it into a QPixmap.

    Returns a dict with ``ok``, ``format``, ``bytes``, ``width``, ``height`` and
    a ``reason`` when not ok. Qt is used only for the final decode check, and a
    missing/broken Qt is reported rather than raised.
    """
    result: Dict[str, Any] = {
        "path": path,
        "ok": False,
        "format": None,
        "bytes": 0,
        "width": 0,
        "height": 0,
        "reason": "",
    }
    if not path or not os.path.exists(path):
        result["reason"] = "file does not exist"
        return result
    try:
        result["bytes"] = os.path.getsize(path)
    except OSError as exc:
        result["reason"] = "cannot stat file: %s" % exc
        return result
    if result["bytes"] < min_bytes:
        result["reason"] = "file is only %d bytes" % result["bytes"]
        return result

    try:
        with open(path, "rb") as handle:
            head = handle.read(32)
            handle.seek(max(0, result["bytes"] - 32))
            tail = handle.read(32)
    except OSError as exc:
        result["reason"] = "cannot read file: %s" % exc
        return result

    # Magic bytes come from the head; the terminator check needs the real tail.
    # (An earlier version passed the head to both checks, which flagged every
    # valid JPEG as truncated - caught by the live IGDB/RAWG image test.)
    sniffed = sniff_format(head)
    if not is_supported_format(sniffed):
        result["reason"] = "unrecognized image format"
        return result
    result["format"] = sniffed
    if not has_terminator(sniffed, tail):
        result["reason"] = "%s appears truncated (missing end-of-file marker)" % sniffed
        return result

    dimensions = read_dimensions(path)
    if dimensions is None:
        result["reason"] = "image could not be decoded"
        return result
    result["width"], result["height"] = dimensions
    if result["width"] <= 0 or result["height"] <= 0:
        result["reason"] = "image has no usable dimensions"
        return result
    result["ok"] = True
    return result


def read_dimensions(path: str) -> Optional[Tuple[int, int]]:
    """Decode-only dimension probe (QImage does not need a QApplication)."""
    try:
        from PyQt5.QtGui import QImage
    except Exception:
        return None
    try:
        image = QImage(path)
    except Exception:
        return None
    if image.isNull():
        return None
    return (int(image.width()), int(image.height()))


def make_thumbnail(
    source: str,
    destination: str,
    size: Tuple[int, int] = COVER_THUMB,
) -> Dict[str, Any]:
    """Scale ``source`` down to fit ``size`` and write it to ``destination``.

    Written to a temp file and renamed into place, so an interrupted thumbnail
    build cannot leave a corrupt cached file that the browser would then trust.
    """
    result: Dict[str, Any] = {"ok": False, "path": destination, "reason": "",
                               "width": 0, "height": 0}
    try:
        from PyQt5.QtCore import QSize, Qt
        from PyQt5.QtGui import QImage
    except Exception as exc:
        result["reason"] = "Qt image support unavailable: %s" % exc
        return result

    image = QImage(source)
    if image.isNull():
        result["reason"] = "source image could not be decoded"
        return result

    # QImage.scaled() happily upscales, which turns a small provider thumbnail
    # into a blurry mess and wastes memory on a 2 GB machine. If the source
    # already fits the target box it is re-encoded as-is.
    if image.width() <= int(size[0]) and image.height() <= int(size[1]):
        scaled = image
    else:
        scaled = image.scaled(
            QSize(int(size[0]), int(size[1])),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
    if scaled.isNull():
        result["reason"] = "scaling produced an empty image"
        return result

    parent = os.path.dirname(os.path.abspath(destination))
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent)
        except OSError as exc:
            result["reason"] = "cannot create thumbnail directory: %s" % exc
            return result

    temporary = destination + ".tmp"
    quality = 88
    if not scaled.save(temporary, "JPEG", quality):
        result["reason"] = "could not write the thumbnail"
        return result
    try:
        os.replace(temporary, destination)
    except OSError as exc:
        result["reason"] = "could not finalize the thumbnail: %s" % exc
        return result

    result["ok"] = True
    result["width"] = int(scaled.width())
    result["height"] = int(scaled.height())
    result["bytes"] = os.path.getsize(destination) if os.path.exists(destination) else 0
    return result


def placeholder_pixmap(width: int, height: int, label: str = ""):
    """A flat placeholder for missing artwork (no icon files, no network)."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QColor, QPainter, QPixmap

    from cartridge.ui.theme import COLORS

    pixmap = QPixmap(max(1, int(width)), max(1, int(height)))
    pixmap.fill(QColor(COLORS["surface_secondary"]))
    painter = QPainter(pixmap)
    try:
        painter.setPen(QColor(COLORS["border_strong"]))
        painter.drawRect(0, 0, pixmap.width() - 1, pixmap.height() - 1)
        if label:
            painter.setPen(QColor(COLORS["text_muted"]))
            painter.drawText(pixmap.rect(), Qt.AlignCenter | Qt.TextWordWrap, label)
    finally:
        painter.end()
    return pixmap


def extension_for_format(name: Optional[str]) -> str:
    return {
        "jpeg": ".jpg",
        "png": ".png",
        "gif": ".gif",
        "bmp": ".bmp",
        "webp": ".webp",
    }.get(name or "", ".img")
