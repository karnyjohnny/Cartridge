"""Artwork download pipeline: fetch, validate, finalize, thumbnail.

The sequence for one asset is deliberately paranoid, because the bytes come from
the internet and the result is written inside the user's game folder:

1. ask the rate limiter for permission (image CDNs get their own policy);
2. stream to ``<name>.part`` with a hard byte cap;
3. validate the finished file — magic bytes, terminator (truncation), size, and a
   real decode through ``QImage``;
4. only then ``os.replace`` it into place and invalidate any stale thumbnail;
5. record the asset in SQLite with its hash and dimensions.

A failure at any step leaves no asset row and no half-written file that the
browser could try to render: the ``.part`` is deleted and the caller gets a typed
error. An interrupted run is recoverable — :meth:`ArtworkStore.cleanup` sweeps
leftovers on the next scan.

Thumbnails are generated at display size so scrolling a 1,000-game grid never
decodes full-resolution artwork (brief section 11, DECISIONS on memory).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from cartridge.artwork import images as image_tools
from cartridge.artwork.store import ArtworkStore, MAX_SCREENSHOTS
from cartridge.core.clock import now_iso
from cartridge.core.models import ASSET_COVER, ASSET_SCREENSHOT, Asset
from cartridge.providers.base import HttpClient, ProviderError, describe_error


@dataclass
class DownloadRequest(object):
    """One artwork item the caller wants."""

    url: str
    kind: str = ASSET_COVER
    index: int = 0


@dataclass
class DownloadResult(object):
    """What happened to one :class:`DownloadRequest`."""

    request: DownloadRequest
    ok: bool = False
    asset: Optional[Asset] = None
    error: str = ""
    reason: str = ""
    bytes_downloaded: int = 0
    latency_ms: float = 0.0
    thumbnailed: bool = False

    def describe(self) -> str:
        if self.ok:
            return "%s ok (%d bytes)" % (self.request.kind, self.bytes_downloaded)
        return "%s failed: %s" % (self.request.kind, self.error or self.reason)


@dataclass
class BatchResult(object):
    """Outcome of one game's artwork import."""

    game_folder: str = ""
    results: List[DownloadResult] = field(default_factory=list)

    @property
    def succeeded(self) -> List[DownloadResult]:
        return [item for item in self.results if item.ok]

    @property
    def failed(self) -> List[DownloadResult]:
        return [item for item in self.results if not item.ok]

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and not self.failed

    def summary(self) -> str:
        return "%d of %d artwork files downloaded" % (
            len(self.succeeded), len(self.results)
        )


class ArtworkDownloader(object):
    """Downloads and validates artwork for the collection."""

    def __init__(
        self,
        client: HttpClient,
        store: Optional[ArtworkStore] = None,
        limiter: Any = None,
        windows: Optional[bool] = None,
        max_screenshots: int = MAX_SCREENSHOTS,
    ):
        self.client = client
        self.store = store or ArtworkStore(windows=windows)
        self.limiter = limiter
        self.windows = windows
        self.max_screenshots = max(0, int(max_screenshots))

    # ------------------------------------------------------------------
    def download_one(
        self,
        game_folder: str,
        request: DownloadRequest,
        progress: Optional[Callable[[int, Optional[int]], None]] = None,
    ) -> DownloadResult:
        """Fetch, validate and finalize a single asset."""
        result = DownloadResult(request=request)
        if not request.url:
            result.reason = "no URL"
            result.error = "This result has no artwork URL."
            return result

        try:
            paths = self.store.ensure_dirs(game_folder)
        except OSError as exc:
            result.error = (
                "Could not create the artwork folder inside %s: %s. Check that the "
                "drive is writable." % (os.path.basename(game_folder) or game_folder, exc)
            )
            return result

        extension = _extension_from_url(request.url)
        base_name = _base_name(request.kind, request.index, extension)
        temp_path = self.store.temp_path(game_folder, base_name)

        try:
            info = self.client.download(
                request.url,
                temp_path,
                operation="artwork_download",
                progress=progress,
                limiter=self.limiter,
            )
            result.bytes_downloaded = int(info.payload.get("bytes", 0))
            result.latency_ms = info.latency_ms
        except ProviderError as exc:
            result.error = describe_error(exc, self.client.redactor)
            result.reason = exc.reason
            _remove_quietly(temp_path)
            return result
        except OSError as exc:
            result.error = "Could not write the artwork to disk: %s" % exc
            _remove_quietly(temp_path)
            return result

        validation = image_tools.validate_file(temp_path)
        if not validation["ok"]:
            result.error = "Downloaded file is not a usable image (%s)." % (
                validation.get("reason") or "unknown reason"
            )
            result.reason = "invalid_image"
            _remove_quietly(temp_path)
            return result

        rel_path = self.store.finalize(
            game_folder,
            temp_path,
            request.kind,
            index=request.index,
            extension=image_tools.extension_for_format(validation["format"]),
        )
        if not rel_path:
            result.error = "Could not move the validated artwork into place."
            _remove_quietly(temp_path)
            return result

        absolute = self.store.resolve(game_folder, rel_path)
        asset = Asset(
            kind=request.kind,
            rel_path=rel_path,
            source_url=request.url,
            provider=_provider_from_url(request.url),
            width=validation.get("width"),
            height=validation.get("height"),
            size_bytes=validation.get("bytes"),
            sha256=image_tools.sha256_file(absolute) if absolute else None,
            sort_order=request.index,
            state="ok",
            downloaded_at=now_iso(),
        )
        result.asset = asset
        result.ok = True
        result.thumbnailed = self.build_thumbnail(game_folder, asset)
        return result

    # ------------------------------------------------------------------
    def download_for_game(
        self,
        game_folder: str,
        cover_url: Optional[str],
        screenshot_urls: Optional[List[str]] = None,
        background_url: Optional[str] = None,
        progress: Optional[Callable[[str, DownloadResult], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> BatchResult:
        """Download a game's cover plus a bounded set of screenshots."""
        batch = BatchResult(game_folder=game_folder)
        requests: List[DownloadRequest] = []
        if cover_url:
            requests.append(DownloadRequest(url=cover_url, kind=ASSET_COVER))
        if background_url:
            requests.append(DownloadRequest(url=background_url, kind="background"))
        for index, url in enumerate(list(screenshot_urls or [])[: self.max_screenshots], start=1):
            requests.append(
                DownloadRequest(url=url, kind=ASSET_SCREENSHOT, index=index)
            )

        for request in requests:
            if cancel is not None and cancel():
                break
            result = self.download_one(game_folder, request)
            batch.results.append(result)
            if progress is not None:
                try:
                    progress(request.kind, result)
                except Exception:
                    # A broken UI callback must not lose the downloaded artwork.
                    pass
        return batch

    # ------------------------------------------------------------------
    def build_thumbnail(
        self, game_folder: str, asset: Asset, size: Optional[Any] = None
    ) -> bool:
        """Generate (or refresh) the display-sized thumbnail for one asset."""
        absolute = self.store.resolve(game_folder, asset.rel_path)
        if not absolute or not os.path.exists(absolute):
            return False
        paths = self.store.paths_for(game_folder)
        target_size = tuple(size) if size else (
            image_tools.SCREENSHOT_THUMB
            if asset.kind == ASSET_SCREENSHOT
            else image_tools.COVER_THUMB
        )
        destination = os.path.join(
            paths.thumb_dir, os.path.splitext(os.path.basename(absolute))[0] + ".jpg"
        )
        if os.path.exists(destination):
            try:
                if os.path.getmtime(destination) >= os.path.getmtime(absolute):
                    return True
            except OSError:
                pass
        result = image_tools.make_thumbnail(absolute, destination, target_size)
        return bool(result.get("ok"))

    def thumbnail_path(self, game_folder: str, asset: Asset) -> Optional[str]:
        """Path to an asset's thumbnail if one exists, else None."""
        absolute = self.store.resolve(game_folder, asset.rel_path)
        if not absolute:
            return None
        paths = self.store.paths_for(game_folder)
        candidate = os.path.join(
            paths.thumb_dir, os.path.splitext(os.path.basename(absolute))[0] + ".jpg"
        )
        return candidate if os.path.exists(candidate) else None

    # ------------------------------------------------------------------
    def reconcile_with_disk(self, game_folder: str, assets: List[Asset]) -> List[str]:
        """Flag assets whose files disappeared. Returns the changed rel_paths."""
        present = set(self.store.existing_files(game_folder))
        changed: List[str] = []
        for asset in assets:
            name = os.path.basename(asset.rel_path.replace("/", os.sep))
            should_be = "ok" if name in present else "missing"
            if asset.state != should_be:
                changed.append(asset.rel_path)
        return changed

    def cleanup(self, game_folder: str) -> int:
        return self.store.cleanup(game_folder)


def _remove_quietly(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _extension_from_url(url: str) -> str:
    """Best-effort extension from a URL; validation decides the real format."""
    tail = str(url).split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    if "." in tail:
        candidate = "." + tail.rsplit(".", 1)[-1].lower()
        if candidate in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
            return candidate
    return ".img"


def _base_name(kind: str, index: int, extension: str) -> str:
    if kind == ASSET_COVER:
        return "cover" + extension
    if kind == ASSET_SCREENSHOT:
        return "screenshot-%02d%s" % (max(1, int(index)), extension)
    return "%s%s" % (kind or "asset", extension)


def _provider_from_url(url: str) -> Optional[str]:
    """Attribute an asset to a provider from its CDN host."""
    text = str(url).lower()
    if "igdb.com" in text:
        return "igdb"
    if "rawg.io" in text:
        return "rawg"
    return None
