"""Minimal PNG encoder built on zlib + struct.

Test artwork must not depend on Pillow or on Qt being importable, so fixtures
generate their own valid images here. Only 8-bit RGB is needed.
"""

from __future__ import annotations

import struct
import zlib
from typing import Sequence, Tuple


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def png_bytes(width: int, height: int, rgb: Sequence[int] = (78, 161, 255)) -> bytes:
    """Return the bytes of a solid-colour 8-bit RGB PNG."""
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    row = bytes(bytearray([int(rgb[0]), int(rgb[1]), int(rgb[2])]) * width)
    raw = b"".join(b"\x00" + row for _ in range(height))
    out = b"\x89PNG\r\n\x1a\n"
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    out += _chunk(b"IDAT", zlib.compress(raw, 6))
    out += _chunk(b"IEND", b"")
    return out


def gradient_png_bytes(width: int, height: int) -> bytes:
    """A non-uniform image, so scaling/format conversions are visibly exercised."""
    rows = []
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            row.append((x * 255) // max(1, width - 1))
            row.append((y * 255) // max(1, height - 1))
            row.append(128)
        rows.append(bytes(row))
    raw = b"".join(rows)
    out = b"\x89PNG\r\n\x1a\n"
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    out += _chunk(b"IDAT", zlib.compress(raw, 6))
    out += _chunk(b"IEND", b"")
    return out


def write_png(path, width: int, height: int, rgb: Tuple[int, int, int] = (78, 161, 255)) -> str:
    """Write a solid PNG to ``path`` and return the path as a string."""
    data = png_bytes(width, height, rgb)
    with open(str(path), "wb") as handle:
        handle.write(data)
    return str(path)
