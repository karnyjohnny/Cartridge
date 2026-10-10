"""Tests for the artwork store and download pipeline (brief section 11).

The security-relevant assertions here are the path checks: artwork is written
*inside the user's game folders*, so a hostile URL, a manipulated manifest or a
path with ``..`` in it must never be able to escape ``_cartridge\\assets``.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from cartridge.artwork.cache import (
    DEFAULT_MAX_BYTES,
    ImageCache,
    ThumbCache,
    estimate_bytes,
    make_key,
)
from cartridge.artwork.downloader import (
    ArtworkDownloader,
    BatchResult,
    DownloadRequest,
    _base_name,
    _extension_from_url,
    _provider_from_url,
)
from cartridge.artwork.store import (
    MAX_SCREENSHOTS,
    ArtworkStore,
    filename_for,
    paths_for_game,
)
from cartridge.core.models import ASSET_COVER, ASSET_SCREENSHOT, Asset
from cartridge.providers.base import HttpSettings, HttpClient
from cartridge.providers.ratelimit import TEST_POLICY, RateLimiter
from tests._png import gradient_png_bytes, png_bytes


@pytest.fixture
def game(tmp_path):
    folder = tmp_path / "Gry" / "Some Game"
    folder.mkdir(parents=True)
    (folder / "game.exe").write_bytes(b"MZfake")
    return str(folder)


@pytest.fixture
def store():
    return ArtworkStore(windows=False)


def make_client(handler, max_bytes=None):
    settings = HttpSettings(max_retries=0)
    if max_bytes is not None:
        settings.max_bytes = max_bytes
    return HttpClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        limiter=RateLimiter(TEST_POLICY, sleep=lambda _s: None),
    )


def downloader(handler, **kwargs):
    return ArtworkDownloader(
        client=make_client(handler, kwargs.pop("max_bytes", None)),
        limiter=RateLimiter(TEST_POLICY, sleep=lambda _s: None),
        windows=False,
        **kwargs
    )


# --------------------------------------------------------------------------
# paths and naming
# --------------------------------------------------------------------------
def test_asset_paths_live_inside_the_game_folder(game, store):
    paths = store.paths_for(game)
    assert paths.asset_dir == os.path.join(game, "_cartridge", "assets")
    assert paths.thumb_dir == os.path.join(paths.asset_dir, "thumbs")
    assert paths.manifest_path == os.path.join(paths.asset_dir, "manifest.json")
    assert paths.game_folder == game


def test_paths_for_game_does_not_create_anything(game):
    before = sorted(os.listdir(game))
    paths_for_game(game, windows=False)
    assert sorted(os.listdir(game)) == before


def test_windows_asset_paths_use_backslashes():
    paths = paths_for_game(r"E:\Gry\Wiedzmin 2", windows=True)
    assert paths.asset_dir == r"E:\Gry\Wiedzmin 2\_cartridge\assets"


def test_ensure_dirs_creates_only_app_owned_directories(game, store):
    paths = store.ensure_dirs(game)
    assert os.path.isdir(paths.asset_dir)
    assert os.path.isdir(paths.thumb_dir)
    assert sorted(os.listdir(game)) == ["_cartridge", "game.exe"]


def test_filename_for_is_deterministic():
    assert filename_for(ASSET_COVER) == "cover.jpg"
    assert filename_for(ASSET_SCREENSHOT, index=2) == "screenshot-02.jpg"
    assert filename_for(ASSET_SCREENSHOT, index=0) == "screenshot-01.jpg"
    assert filename_for("background", extension="png") == "background.png"
    assert filename_for("weird/kind", extension=".jpg") == "weird-kind.jpg"


def test_extension_from_url():
    assert _extension_from_url("https://cdn/x/cover.jpg") == ".jpg"
    assert _extension_from_url("https://cdn/x/cover.png?v=2") == ".png"
    assert _extension_from_url("https://cdn/x/image") == ".img"
    assert _extension_from_url("https://cdn/x/file.exe") == ".img"


def test_base_name():
    assert _base_name(ASSET_COVER, 0, ".jpg") == "cover.jpg"
    assert _base_name(ASSET_SCREENSHOT, 3, ".jpg") == "screenshot-03.jpg"


def test_provider_attribution_from_url():
    assert _provider_from_url("https://images.igdb.com/igdb/image/upload/x.jpg") == "igdb"
    assert _provider_from_url("https://media.rawg.io/media/games/x.jpg") == "rawg"
    assert _provider_from_url("https://example.com/x.jpg") is None


# --------------------------------------------------------------------------
# path safety - the important part
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rel_path",
    [
        "../../game.exe",
        "../../../etc/passwd",
        "_cartridge/assets/../../../../outside.txt",
        "/etc/passwd",
        "C:\\Windows\\system32\\cmd.exe",
        "..\\..\\other-game\\_cartridge\\assets\\cover.jpg",
    ],
)
def test_resolve_refuses_to_escape_the_asset_dir(game, store, rel_path):
    store.ensure_dirs(game)
    assert store.resolve(game, rel_path) is None, rel_path


def test_resolve_accepts_legitimate_relative_paths(game, store):
    store.ensure_dirs(game)
    resolved = store.resolve(game, "_cartridge/assets/cover.jpg")
    assert resolved == os.path.join(game, "_cartridge", "assets", "cover.jpg")
    assert store.is_safe_path(game, resolved)


def test_resolve_refuses_absolute_paths(game, store):
    store.ensure_dirs(game)
    absolute = os.path.join(store.paths_for(game).asset_dir, "cover.jpg")
    assert store.resolve(game, absolute) is None


def test_resolve_handles_empty_input(game, store):
    assert store.resolve(game, "") is None
    assert store.resolve(game, None) is None


def test_is_safe_path_rejects_the_game_folder_itself(game, store):
    assert store.is_safe_path(game, game) is False
    assert store.is_safe_path(game, os.path.join(game, "game.exe")) is False


# --------------------------------------------------------------------------
# finalize / cleanup
# --------------------------------------------------------------------------
def test_finalize_moves_a_validated_file_into_place(game, store, tmp_path):
    store.ensure_dirs(game)
    temp = store.temp_path(game, "cover.jpg")
    with open(temp, "wb") as handle:
        handle.write(png_bytes(20, 30))
    rel = store.finalize(game, temp, ASSET_COVER, extension=".png")
    assert rel == "_cartridge/assets/cover.png"
    assert os.path.exists(os.path.join(game, "_cartridge", "assets", "cover.png"))
    assert not os.path.exists(temp), "the temp file must be gone"


def test_finalize_overwrites_an_existing_cover_atomically(game, store):
    store.ensure_dirs(game)
    first = store.temp_path(game, "a.jpg")
    with open(first, "wb") as handle:
        handle.write(png_bytes(10, 10, (1, 2, 3)))
    store.finalize(game, first, ASSET_COVER, extension=".png")

    second = store.temp_path(game, "b.jpg")
    with open(second, "wb") as handle:
        handle.write(png_bytes(40, 40, (9, 9, 9)))
    rel = store.finalize(game, second, ASSET_COVER, extension=".png")
    assert rel == "_cartridge/assets/cover.png"
    assert os.path.getsize(os.path.join(game, "_cartridge", "assets", "cover.png")) > 0


def test_finalize_returns_none_for_a_missing_temp_file(game, store):
    store.ensure_dirs(game)
    assert store.finalize(game, str(os.path.join(game, "nope.part")), ASSET_COVER) is None


def test_finalize_creates_directories_when_needed(game, store):
    temp = os.path.join(game, "loose.part")
    with open(temp, "wb") as handle:
        handle.write(png_bytes(8, 8))
    assert store.finalize(game, temp, ASSET_COVER, extension=".png") is not None


def test_cleanup_removes_only_part_files(game, store):
    store.ensure_dirs(game)
    paths = store.paths_for(game)
    leftovers = [os.path.join(paths.asset_dir, name) for name in ("a.jpg.part", "b.png.part")]
    for path in leftovers:
        with open(path, "wb") as handle:
            handle.write(b"partial")
    good = os.path.join(paths.asset_dir, "cover.jpg")
    with open(good, "wb") as handle:
        handle.write(png_bytes(8, 8))

    assert len(store.orphaned_part_files(game)) == 2
    assert store.cleanup(game) == 2
    assert os.path.exists(good), "validated artwork must survive a cleanup"
    assert store.orphaned_part_files(game) == []


def test_existing_files_lists_assets(game, store):
    store.ensure_dirs(game)
    paths = store.paths_for(game)
    for name in ("cover.jpg", "screenshot-01.jpg", "leftover.part"):
        with open(os.path.join(paths.asset_dir, name), "wb") as handle:
            handle.write(png_bytes(8, 8))
    listed = store.existing_files(game)
    assert "cover.jpg" in listed and "screenshot-01.jpg" in listed
    assert "leftover.part" not in listed


def test_remove_asset_deletes_file_and_thumbnail(game, store):
    store.ensure_dirs(game)
    paths = store.paths_for(game)
    asset_file = os.path.join(paths.asset_dir, "cover.jpg")
    with open(asset_file, "wb") as handle:
        handle.write(png_bytes(8, 8))
    os.makedirs(paths.thumb_dir, exist_ok=True)
    thumb = os.path.join(paths.thumb_dir, "cover.jpg")
    with open(thumb, "wb") as handle:
        handle.write(png_bytes(4, 4))

    assert store.remove_asset(game, "_cartridge/assets/cover.jpg") is True
    assert not os.path.exists(asset_file)
    assert not os.path.exists(thumb)
    assert store.remove_asset(game, "_cartridge/assets/cover.jpg") is False


def test_remove_all_deletes_only_the_app_directory(game, store):
    store.ensure_dirs(game)
    with open(os.path.join(store.paths_for(game).asset_dir, "cover.jpg"), "wb") as handle:
        handle.write(png_bytes(8, 8))
    assert store.remove_all(game) == 1
    assert not os.path.exists(os.path.join(game, "_cartridge"))
    assert os.path.exists(os.path.join(game, "game.exe")), "game files must be untouched"
    assert store.remove_all(game) == 0


def test_remove_all_refuses_a_non_app_directory(game, store):
    assert store.remove_all(game) == 0
    assert os.path.exists(game)


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------
def test_manifest_roundtrip(game, store):
    assets = [
        Asset(kind=ASSET_COVER, rel_path="_cartridge/assets/cover.jpg", sha256="a" * 64,
              width=264, height=352, size_bytes=1234, provider="igdb",
              source_url="https://images.igdb.com/x.jpg"),
    ]
    path = store.write_manifest(game, assets, extra={"imported_by": "test"})
    assert path and os.path.exists(path)
    data = store.read_manifest(game)
    assert data["app"] == "Cartridge"
    assert data["manifest_version"] == 1
    assert data["assets"][0]["kind"] == "cover"
    assert data["assets"][0]["sha256"] == "a" * 64
    assert data["extra"]["imported_by"] == "test"


def test_manifest_contains_no_credential_shaped_fields(game, store):
    secret = "SUPERSECRETVALUE1234567890"
    assets = [
        Asset(kind=ASSET_COVER, rel_path="_cartridge/assets/cover.jpg",
              source_url="https://media.rawg.io/x.jpg?key=%s" % secret),
    ]
    store.write_manifest(game, assets)
    text = open(store.paths_for(game).manifest_path, encoding="utf-8").read()
    for forbidden in ("client_secret", "access_token", "authorization", "api_key"):
        assert forbidden not in text.lower()


def test_read_manifest_handles_a_missing_or_corrupt_file(game, store):
    assert store.read_manifest(game) is None
    store.ensure_dirs(game)
    with open(store.paths_for(game).manifest_path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert store.read_manifest(game) is None


def test_write_manifest_leaves_no_temp_file_on_failure(game, store, monkeypatch):
    store.ensure_dirs(game)

    def deny(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store.__class__, "paths_for", lambda self, g: paths_for_game(g))
    monkeypatch.setattr("builtins.open", deny)
    assert store.write_manifest(game, []) is None
    monkeypatch.undo()
    assert not os.path.exists(store.paths_for(game).manifest_path + ".tmp")


# --------------------------------------------------------------------------
# downloads
# --------------------------------------------------------------------------
def test_download_cover_end_to_end(game, qapp):
    data = gradient_png_bytes(300, 400)

    def handler(request):
        return httpx.Response(200, content=data)

    result = downloader(handler).download_one(
        game, DownloadRequest(url="https://images.igdb.com/x/cover.png", kind=ASSET_COVER)
    )
    assert result.ok is True, result.error
    assert result.bytes_downloaded == len(data)
    assert result.asset.kind == ASSET_COVER
    assert result.asset.rel_path == "_cartridge/assets/cover.png"
    assert result.asset.width == 300 and result.asset.height == 400
    assert result.asset.sha256 and len(result.asset.sha256) == 64
    assert result.asset.provider == "igdb"
    assert result.asset.state == "ok"
    assert os.path.exists(os.path.join(game, "_cartridge", "assets", "cover.png"))
    # no .part leftovers
    assert sorted(os.listdir(os.path.join(game, "_cartridge", "assets"))) == [
        "cover.png", "thumbs"
    ]


def test_download_creates_a_thumbnail(game, qapp):
    def handler(request):
        return httpx.Response(200, content=gradient_png_bytes(800, 1100))

    result = downloader(handler).download_one(
        game, DownloadRequest(url="https://images.igdb.com/cover.png", kind=ASSET_COVER)
    )
    assert result.ok and result.thumbnailed is True
    thumb_dir = os.path.join(game, "_cartridge", "assets", "thumbs")
    assert os.listdir(thumb_dir) == ["cover.jpg"]
    assert os.path.getsize(os.path.join(thumb_dir, "cover.jpg")) > 0


def test_download_rejects_a_non_image_payload(game):
    """An HTML captive-portal page must not become cover art."""
    def handler(request):
        return httpx.Response(
            200, content=b"<html><body>Login</body></html>" * 40,
            headers={"Content-Type": "image/jpeg"},
        )

    result = downloader(handler).download_one(
        game, DownloadRequest(url="https://images.igdb.com/cover.jpg", kind=ASSET_COVER)
    )
    assert result.ok is False
    assert result.reason == "invalid_image"
    assert "not a usable image" in result.error
    asset_dir = os.path.join(game, "_cartridge", "assets")
    assert os.listdir(asset_dir) == ["thumbs"], "a rejected download must leave nothing behind"


def test_download_rejects_a_truncated_image(game):
    full = png_bytes(200, 200)

    def handler(request):
        return httpx.Response(200, content=full[: len(full) // 2])

    result = downloader(handler).download_one(
        game, DownloadRequest(url="https://images.igdb.com/cover.png", kind=ASSET_COVER)
    )
    assert result.ok is False
    assert "truncated" in result.error or "not a usable image" in result.error


def test_download_rejects_an_exe_renamed_as_an_image(game):
    def handler(request):
        return httpx.Response(200, content=b"MZ\x90\x00" + os.urandom(4096))

    result = downloader(handler).download_one(
        game, DownloadRequest(url="https://cdn.example/cover.jpg", kind=ASSET_COVER)
    )
    assert result.ok is False
    assert result.asset is None


def test_download_reports_a_network_failure_cleanly(game):
    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    result = downloader(handler).download_one(
        game, DownloadRequest(url="https://images.igdb.com/cover.png", kind=ASSET_COVER)
    )
    assert result.ok is False
    assert result.reason == "network"
    assert "reach" in result.error.lower()


def test_download_reports_a_404_cleanly(game):
    def handler(request):
        return httpx.Response(404, text="not found")

    result = downloader(handler).download_one(
        game, DownloadRequest(url="https://images.igdb.com/gone.png", kind=ASSET_COVER)
    )
    assert result.ok is False
    assert "404" in result.error


def test_download_rejects_an_empty_url(game):
    result = downloader(lambda r: httpx.Response(200, content=b"")).download_one(
        game, DownloadRequest(url="", kind=ASSET_COVER)
    )
    assert result.ok is False
    assert result.reason == "no URL"


def test_download_respects_the_byte_cap(game):
    def handler(request):
        return httpx.Response(200, content=os.urandom(64 * 1024))

    result = downloader(handler, max_bytes=1024).download_one(
        game, DownloadRequest(url="https://cdn/huge.png", kind=ASSET_COVER)
    )
    assert result.ok is False
    assert "limit" in result.error.lower()


def test_download_handles_an_unwritable_game_folder(tmp_path, qapp):
    """A read-only or vanished game folder must produce a message, not a crash."""
    missing = str(tmp_path / "no-such-game")
    client = make_client(lambda r: httpx.Response(200, content=png_bytes(8, 8)))
    store = ArtworkStore(windows=False)

    def deny(_folder):
        raise OSError(13, "Access is denied")

    store.ensure_dirs = deny  # type: ignore[assignment]
    result = ArtworkDownloader(client=client, store=store, windows=False).download_one(
        missing, DownloadRequest(url="https://cdn/x.png", kind=ASSET_COVER)
    )
    assert result.ok is False
    assert "artwork folder" in result.error


def test_batch_downloads_cover_and_bounded_screenshots(game, qapp):
    def handler(request):
        return httpx.Response(200, content=gradient_png_bytes(120, 160))

    urls = ["https://images.igdb.com/s%d.png" % index for index in range(10)]
    batch = downloader(handler).download_for_game(
        game,
        cover_url="https://images.igdb.com/cover.png",
        screenshot_urls=urls,
    )
    assert len(batch.results) == 1 + MAX_SCREENSHOTS
    assert batch.all_ok is True
    assert len(batch.succeeded) == 1 + MAX_SCREENSHOTS
    files = sorted(os.listdir(os.path.join(game, "_cartridge", "assets")))
    assert "cover.png" in files
    assert "screenshot-01.png" in files
    assert "screenshot-%02d.png" % MAX_SCREENSHOTS in files
    assert "screenshot-%02d.png" % (MAX_SCREENSHOTS + 1) not in files


def test_batch_reports_partial_success(game, qapp):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if "cover" in str(request.url):
            return httpx.Response(200, content=gradient_png_bytes(100, 100))
        return httpx.Response(500, text="boom")

    batch = downloader(handler).download_for_game(
        game,
        cover_url="https://images.igdb.com/cover.png",
        screenshot_urls=["https://images.igdb.com/s1.png"],
    )
    assert len(batch.succeeded) == 1
    assert len(batch.failed) == 1
    assert batch.all_ok is False
    assert "1 of 2" in batch.summary()


def test_batch_is_cancellable(game, qapp):
    def handler(request):
        return httpx.Response(200, content=png_bytes(50, 50))

    state = {"n": 0}

    def cancel_after_one():
        state["n"] += 1
        return state["n"] > 1

    batch = downloader(handler).download_for_game(
        game,
        cover_url="https://images.igdb.com/cover.png",
        screenshot_urls=["https://images.igdb.com/s1.png", "https://images.igdb.com/s2.png"],
        cancel=cancel_after_one,
    )
    assert len(batch.results) == 1


def test_batch_with_no_urls_is_a_no_op(game):
    batch = downloader(lambda r: httpx.Response(200)).download_for_game(game, None)
    assert batch.results == []
    assert batch.all_ok is False


def test_batch_progress_callback_failure_does_not_lose_artwork(game, qapp):
    def handler(request):
        return httpx.Response(200, content=png_bytes(60, 60))

    def bad_progress(kind, result):
        raise RuntimeError("ui exploded")

    batch = downloader(handler).download_for_game(
        game, cover_url="https://images.igdb.com/cover.png", progress=bad_progress
    )
    assert batch.all_ok is True


def test_reconcile_with_disk_flags_vanished_files(game, store, qapp):
    def handler(request):
        return httpx.Response(200, content=png_bytes(50, 50))

    down = downloader(handler)
    result = down.download_one(
        game, DownloadRequest(url="https://images.igdb.com/cover.png", kind=ASSET_COVER)
    )
    assets = [result.asset]
    assert down.reconcile_with_disk(game, assets) == []

    os.remove(os.path.join(game, "_cartridge", "assets", "cover.png"))
    changed = down.reconcile_with_disk(game, assets)
    assert changed == ["_cartridge/assets/cover.png"]


def test_cleanup_after_an_interrupted_download(game, store):
    store.ensure_dirs(game)
    temp = store.temp_path(game, "cover.png")
    with open(temp, "wb") as handle:
        handle.write(b"half a file")
    assert downloader(lambda r: httpx.Response(200)).cleanup(game) == 1
    assert not os.path.exists(temp)


# --------------------------------------------------------------------------
# memory cache
# --------------------------------------------------------------------------
def test_cache_key_includes_the_requested_size():
    assert make_key("a.jpg") == ("a.jpg", 0, 0)
    assert make_key("a.jpg", 100, 100) != make_key("a.jpg", 200, 200)


def test_cache_stores_and_returns(qapp):
    from PyQt5.QtGui import QPixmap

    cache = ImageCache()
    pixmap = QPixmap(10, 10)
    cache.put(make_key("a.jpg"), pixmap)
    assert cache.get(make_key("a.jpg")) is pixmap
    assert cache.get(make_key("b.jpg")) is None
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1


def test_cache_evicts_by_entry_count(qapp):
    from PyQt5.QtGui import QPixmap

    cache = ImageCache(max_entries=3, max_bytes=DEFAULT_MAX_BYTES)
    for index in range(6):
        cache.put(make_key("img%d.jpg" % index), QPixmap(20, 20))
    assert len(cache) == 3
    assert cache.stats()["evictions"] == 3
    # the oldest entries are gone, the newest survive
    assert cache.get(make_key("img0.jpg")) is None
    assert cache.get(make_key("img5.jpg")) is not None


def test_cache_evicts_by_byte_ceiling(qapp):
    from PyQt5.QtGui import QPixmap

    # 100x100 ARGB32 = 40,000 bytes; a 100 KB ceiling holds two of them.
    cache = ImageCache(max_entries=100, max_bytes=100_000)
    for index in range(5):
        cache.put(make_key("big%d.jpg" % index), QPixmap(100, 100))
    assert len(cache) <= 3
    assert cache.current_bytes <= 100_000


def test_cache_lru_order_is_maintained(qapp):
    from PyQt5.QtGui import QPixmap

    cache = ImageCache(max_entries=2, max_bytes=DEFAULT_MAX_BYTES)
    cache.put(make_key("a"), QPixmap(10, 10))
    cache.put(make_key("b"), QPixmap(10, 10))
    cache.get(make_key("a"))            # touch "a" so "b" becomes the LRU
    cache.put(make_key("c"), QPixmap(10, 10))
    assert cache.get(make_key("b")) is None
    assert cache.get(make_key("a")) is not None
    assert cache.get(make_key("c")) is not None


def test_cache_rejects_null_pixmaps(qapp):
    from PyQt5.QtGui import QPixmap

    cache = ImageCache()
    cache.put(make_key("null"), QPixmap())
    assert len(cache) == 0
    cache.put(make_key("none"), None)
    assert len(cache) == 0


def test_cache_invalidate_drops_every_size_of_one_path(qapp):
    from PyQt5.QtGui import QPixmap

    cache = ImageCache()
    cache.put(make_key("a.jpg", 100, 100), QPixmap(100, 100))
    cache.put(make_key("a.jpg", 200, 200), QPixmap(200, 200))
    cache.put(make_key("b.jpg"), QPixmap(10, 10))
    assert cache.invalidate("a.jpg") == 2
    assert len(cache) == 1


def test_cache_clear_and_stats(qapp):
    from PyQt5.QtGui import QPixmap

    cache = ImageCache()
    cache.put(make_key("a"), QPixmap(10, 10))
    cache.clear()
    assert len(cache) == 0
    stats = cache.stats()
    assert stats["entries"] == 0 and stats["bytes"] == 0


def test_cache_is_thread_safe(qapp):
    import threading

    from PyQt5.QtGui import QPixmap

    cache = ImageCache(max_entries=50)
    errors = []

    def churn(seed):
        try:
            for index in range(200):
                cache.put(make_key("t%d-%d" % (seed, index % 20)), QPixmap(8, 8))
                cache.get(make_key("t%d-%d" % (seed, index % 20)))
        except Exception as exc:  # pragma: no cover - would be a real bug
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(seed,)) for seed in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(cache) <= 50


def test_estimate_bytes(qapp):
    from PyQt5.QtGui import QPixmap

    assert estimate_bytes(QPixmap(10, 10)) >= 400
    assert estimate_bytes(QPixmap()) == 0
    assert estimate_bytes(None) == 0


def test_thumb_cache_detects_a_stale_thumbnail(tmp_path, qapp):
    source = tmp_path / "src.png"
    source.write_bytes(png_bytes(20, 20))
    destination = str(tmp_path / "thumb.jpg")
    cache = ThumbCache()
    assert cache.needs_build(str(source), destination) is True

    from cartridge.artwork import images as im

    im.make_thumbnail(str(source), destination, (10, 10))
    assert cache.needs_build(str(source), destination) is False

    os.utime(str(source), (source.stat().st_atime, source.stat().st_mtime + 100))
    assert cache.needs_build(str(source), destination) is True


def test_thumb_cache_counts(qapp):
    cache = ThumbCache()
    cache.note(True)
    cache.note(False)
    stats = cache.stats()
    assert stats["thumbnails_built"] == 1
    assert stats["thumbnails_reused"] == 1
    assert "memory" in stats


def test_batch_result_helpers():
    batch = BatchResult(game_folder="/x")
    assert batch.all_ok is False
    assert batch.succeeded == [] and batch.failed == []
    assert "0 of 0" in batch.summary()


def test_download_result_describe():
    ok = downloader(lambda r: httpx.Response(200, content=png_bytes(8, 8)))
    result = ok.download_one(
        os.path.dirname(__file__), DownloadRequest(url="https://x/y.png", kind=ASSET_COVER)
    )
    assert "cover" in result.describe()
