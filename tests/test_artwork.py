"""Tests for image validation, hashing and thumbnailing (brief section 11).

Uses stdlib-generated PNGs plus Qt-generated JPEGs so both a lossless and a
lossy format are covered. Qt is required for decoding; the tests skip cleanly if
PyQt5 is somehow unavailable.
"""

from __future__ import annotations

import os

import pytest

from cartridge.artwork import images as im
from tests._png import gradient_png_bytes, png_bytes


@pytest.fixture
def jpeg_factory(tmp_path, qapp):
    """Write a real JPEG using Qt, so terminator/truncation checks are genuine."""
    from PyQt5.QtGui import QImage

    def make(name="cover.jpg", width=200, height=280):
        path = str(tmp_path / name)
        image = QImage(width, height, QImage.Format_RGB32)
        image.fill(0xFF2C6FBF)
        assert image.save(path, "JPEG", 90)
        return path

    return make


# --------------------------------------------------------------------------
# format sniffing
# --------------------------------------------------------------------------
def test_sniff_png():
    assert im.sniff_format(png_bytes(4, 4)) == "png"


def test_sniff_jpeg(jpeg_factory):
    path = jpeg_factory()
    with open(path, "rb") as handle:
        assert im.sniff_format(handle.read(32)) == "jpeg"


def test_sniff_from_a_short_prefix():
    """Magic bytes must be identifiable from the first bytes of a stream."""
    data = png_bytes(8, 8)
    assert im.sniff_format(data[:12]) == "png"


def test_sniff_webp_requires_the_riff_marker():
    assert im.sniff_format(b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64) == "webp"
    assert im.sniff_format(b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 64) is None


def test_sniff_rejects_non_images():
    assert im.sniff_format(b"MZ\x90\x00" + b"\x00" * 64) is None      # an .exe
    assert im.sniff_format(b"<!DOCTYPE html><html>" + b" " * 64) is None
    assert im.sniff_format(b"") is None
    assert im.sniff_format(None) is None


def test_is_supported_format():
    assert im.is_supported_format("jpeg")
    assert im.is_supported_format("png")
    assert not im.is_supported_format(None)
    assert not im.is_supported_format("tiff")


# --------------------------------------------------------------------------
# truncation detection (the bug the live IGDB test caught)
# --------------------------------------------------------------------------
def test_complete_jpeg_has_its_terminator(jpeg_factory):
    path = jpeg_factory()
    with open(path, "rb") as handle:
        data = handle.read()
    assert im.has_terminator("jpeg", data[-32:]) is True


def test_truncated_jpeg_is_detected(jpeg_factory):
    path = jpeg_factory()
    with open(path, "rb") as handle:
        data = handle.read()
    partial = data[: len(data) // 2]
    assert im.has_terminator("jpeg", partial[-32:]) is False


def test_truncated_png_is_detected():
    data = png_bytes(32, 32)
    assert im.has_terminator("png", data[-32:]) is True
    assert im.has_terminator("png", data[: len(data) // 3][-32:]) is False


def test_empty_tail_is_treated_as_incomplete():
    assert im.has_terminator("jpeg", b"") is False


def test_formats_without_a_cheap_terminator_pass_through():
    assert im.has_terminator("bmp", b"anything") is True
    assert im.has_terminator("webp", b"anything") is True


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------
def test_validate_accepts_a_real_png(tmp_path):
    path = tmp_path / "cover.png"
    path.write_bytes(gradient_png_bytes(64, 96))
    result = im.validate_file(str(path))
    assert result["ok"] is True
    assert result["format"] == "png"
    assert result["width"] == 64
    assert result["height"] == 96
    assert result["bytes"] == os.path.getsize(str(path))


def test_validate_accepts_a_real_jpeg(jpeg_factory):
    result = im.validate_file(jpeg_factory(width=120, height=160))
    assert result["ok"] is True
    assert result["format"] == "jpeg"
    assert result["width"] == 120
    assert result["height"] == 160


def test_validate_rejects_a_missing_file(tmp_path):
    result = im.validate_file(str(tmp_path / "nope.jpg"))
    assert result["ok"] is False
    assert "does not exist" in result["reason"]


def test_validate_rejects_a_truncated_jpeg(jpeg_factory, tmp_path):
    source = jpeg_factory()
    with open(source, "rb") as handle:
        data = handle.read()
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(data[: len(data) * 2 // 3])
    result = im.validate_file(str(broken))
    assert result["ok"] is False
    assert "truncated" in result["reason"]
    assert result["format"] == "jpeg"


def test_validate_rejects_an_exe_renamed_to_jpg(tmp_path):
    fake = tmp_path / "not-an-image.jpg"
    fake.write_bytes(b"MZ\x90\x00" + os.urandom(4096))
    result = im.validate_file(str(fake))
    assert result["ok"] is False
    assert "unrecognized image format" in result["reason"]


def test_validate_rejects_html_from_a_captive_portal(tmp_path):
    fake = tmp_path / "cover.jpg"
    fake.write_text("<html><body>Please log in to wifi</body></html>" * 20, encoding="utf-8")
    result = im.validate_file(str(fake))
    assert result["ok"] is False


def test_validate_rejects_a_too_small_file(tmp_path):
    tiny = tmp_path / "tiny.png"
    tiny.write_bytes(png_bytes(1, 1)[:40])
    result = im.validate_file(str(tiny))
    assert result["ok"] is False
    assert "bytes" in result["reason"]


def test_validate_rejects_corrupt_png_bytes(tmp_path):
    data = bytearray(png_bytes(32, 32))
    for index in range(40, min(len(data), 400)):
        data[index] = 0xFF
    broken = tmp_path / "corrupt.png"
    broken.write_bytes(bytes(data))
    result = im.validate_file(str(broken))
    assert result["ok"] is False


def test_validate_never_raises_on_a_directory(tmp_path):
    result = im.validate_file(str(tmp_path))
    assert result["ok"] is False


def test_validate_handles_an_empty_path():
    assert im.validate_file("")["ok"] is False


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------
def test_sha256_file_and_bytes_agree(tmp_path):
    data = png_bytes(8, 8)
    path = tmp_path / "a.png"
    path.write_bytes(data)
    assert im.sha256_file(str(path)) == im.sha256_bytes(data)
    assert len(im.sha256_file(str(path))) == 64


def test_sha256_file_missing_returns_none(tmp_path):
    assert im.sha256_file(str(tmp_path / "nope")) is None


def test_sha256_detects_a_changed_file(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(png_bytes(8, 8, (1, 2, 3)))
    first = im.sha256_file(str(path))
    path.write_bytes(png_bytes(8, 8, (4, 5, 6)))
    assert im.sha256_file(str(path)) != first


# --------------------------------------------------------------------------
# thumbnails
# --------------------------------------------------------------------------
def test_make_thumbnail_scales_down(tmp_path, qapp):
    source = tmp_path / "big.png"
    source.write_bytes(gradient_png_bytes(800, 1200))
    destination = str(tmp_path / "thumbs" / "cover.jpg")
    result = im.make_thumbnail(str(source), destination, im.COVER_THUMB)
    assert result["ok"] is True
    assert result["width"] <= im.COVER_THUMB[0]
    assert result["height"] <= im.COVER_THUMB[1]
    assert os.path.exists(destination)
    # aspect ratio is preserved (800x1200 -> 224x336 within a 240x336 box)
    assert abs(result["width"] / result["height"] - 800 / 1200.0) < 0.05
    # the thumbnail is much smaller than the source
    assert result["bytes"] < os.path.getsize(str(source))


def test_make_thumbnail_does_not_upscale(tmp_path, qapp):
    source = tmp_path / "small.png"
    source.write_bytes(png_bytes(40, 40))
    destination = str(tmp_path / "small_thumb.jpg")
    result = im.make_thumbnail(str(source), destination, im.COVER_THUMB)
    assert result["ok"] is True
    assert result["width"] <= 40 and result["height"] <= 40


def test_make_thumbnail_leaves_no_temp_file_behind(tmp_path, qapp):
    source = tmp_path / "ok.png"
    source.write_bytes(gradient_png_bytes(100, 100))
    destination = str(tmp_path / "thumb.jpg")
    im.make_thumbnail(str(source), destination)
    assert not os.path.exists(destination + ".tmp")
    assert sorted(os.listdir(str(tmp_path))) == ["ok.png", "thumb.jpg"]


def test_make_thumbnail_fails_cleanly_on_a_broken_source(tmp_path, qapp):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(200))
    result = im.make_thumbnail(str(broken), str(tmp_path / "out.jpg"))
    assert result["ok"] is False
    assert result["reason"]
    assert not os.path.exists(str(tmp_path / "out.jpg"))


def test_make_thumbnail_on_a_missing_source(tmp_path, qapp):
    result = im.make_thumbnail(str(tmp_path / "nope.png"), str(tmp_path / "out.jpg"))
    assert result["ok"] is False


def test_thumbnail_sizes_are_bounded_for_the_target_hardware():
    """Cards must never decode full-resolution artwork (brief section 11)."""
    assert im.COVER_THUMB[0] <= 320 and im.COVER_THUMB[1] <= 448
    assert im.SCREENSHOT_THUMB[0] <= 480
    assert im.DETAIL_COVER[0] <= 400


def test_read_dimensions(tmp_path, qapp):
    source = tmp_path / "d.png"
    source.write_bytes(png_bytes(50, 70))
    assert im.read_dimensions(str(source)) == (50, 70)
    assert im.read_dimensions(str(tmp_path / "missing.png")) is None


def test_extension_for_format():
    assert im.extension_for_format("jpeg") == ".jpg"
    assert im.extension_for_format("png") == ".png"
    assert im.extension_for_format(None) == ".img"
    assert im.extension_for_format("tiff") == ".img"


def test_placeholder_pixmap_renders(qapp):
    from cartridge.ui.theme import COLORS

    pixmap = im.placeholder_pixmap(120, 160, "No cover")
    assert not pixmap.isNull()
    assert pixmap.width() == 120 and pixmap.height() == 160
    image = pixmap.toImage()
    # Sample away from the centred label text and the 1 px border: the interior
    # must be the surface colour, not the white Qt default.
    interior = image.pixelColor(5, 5).name().lower()
    assert interior == COLORS["surface_secondary"].lower()
    # and the border must be visible as a distinct colour
    assert image.pixelColor(0, 0).name().lower() != interior


def test_placeholder_pixmap_handles_zero_size(qapp):
    pixmap = im.placeholder_pixmap(0, 0)
    assert not pixmap.isNull()
    assert pixmap.width() >= 1
