"""httpx transport layer shared by every provider and by artwork downloads.

Responsibilities (brief section 10):

* one place that builds an ``httpx.Client`` with explicit connect/read/write/pool
  timeouts — never a bare default;
* a typed error taxonomy so the UI can say *why* something failed ("rate limited,
  retry in 40 s" vs "no network" vs "credentials rejected") instead of showing a
  traceback;
* finite retries for transient failures only, with backoff and ``Retry-After``;
* credential redaction on every error path and every log line;
* latency measurement that callers can persist for Diagnostics.

Nothing here touches Qt. The worker pool
(:mod:`cartridge.core.workers`) is what keeps these blocking calls off the GUI
thread, which makes this module testable without a QApplication.

Tests inject ``httpx.MockTransport`` so the suite never needs the network.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx

from cartridge import __version__
from cartridge.core.clock import monotonic
from cartridge.core.redaction import Redactor, default_redactor
from cartridge.providers.ratelimit import AcquireResult, RateLimiter

USER_AGENT = "Cartridge/%s (local desktop game collection manager)" % __version__

DEFAULT_MAX_BYTES = 12 * 1024 * 1024      # 12 MB per artwork file
DEFAULT_CHUNK = 64 * 1024


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
class ProviderError(Exception):
    """Base class for every provider failure. Always safe to show to the user."""

    #: short, stable, machine-readable reason code
    reason = "provider_error"
    #: can the caller sensibly retry later?
    transient = False

    def __init__(self, message: str, detail: str = "", retry_after: Optional[float] = None):
        super(ProviderError, self).__init__(message)
        self.message = message
        self.detail = detail
        self.retry_after = retry_after

    def user_text(self) -> str:
        if self.retry_after:
            return "%s (retry in %d s)" % (self.message, int(self.retry_after))
        return self.message


class ProviderAuthError(ProviderError):
    reason = "auth_failed"
    transient = False


class ProviderRateLimited(ProviderError):
    reason = "rate_limited"
    transient = True


class ProviderNetworkError(ProviderError):
    reason = "network"
    transient = True


class ProviderTimeout(ProviderError):
    reason = "timeout"
    transient = True


class ProviderResponseError(ProviderError):
    reason = "bad_response"
    transient = False

    def __init__(self, message: str, status_code: int = 0, detail: str = "",
                 retry_after: Optional[float] = None):
        super(ProviderResponseError, self).__init__(message, detail, retry_after)
        self.status_code = status_code


class ProviderServerError(ProviderResponseError):
    reason = "server_error"
    transient = True


class ProviderDataError(ProviderError):
    """The provider answered, but the payload is not what we expected."""

    reason = "bad_payload"
    transient = False


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
@dataclass
class HttpSettings(object):
    """Explicit timeouts and retry policy. No default httpx timeouts anywhere."""

    connect_timeout: float = 5.0
    read_timeout: float = 15.0
    write_timeout: float = 10.0
    pool_timeout: float = 5.0
    download_read_timeout: float = 30.0
    max_retries: int = 2          # finite, per brief section 10
    backoff_base: float = 0.6     # seconds; exponential
    backoff_cap: float = 8.0
    user_agent: str = USER_AGENT
    max_bytes: int = DEFAULT_MAX_BYTES
    follow_redirects: bool = True
    # Never sleep longer than this for a Retry-After; report the limit instead.
    rate_limit_wait_cap: float = 15.0

    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.write_timeout,
            pool=self.pool_timeout,
        )

    def download_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=self.download_read_timeout,
            write=self.write_timeout,
            pool=self.pool_timeout,
        )


@dataclass
class ResponseInfo(object):
    """What a call cost and what it returned (already decoded)."""

    payload: Any = None
    status_code: int = 0
    url: str = ""
    latency_ms: float = 0.0
    attempts: int = 1
    from_cache: bool = False
    headers: Dict[str, str] = field(default_factory=dict)


LatencyHook = Callable[[str, float, int], None]


class HttpClient(object):
    """Thin, retrying, redacting wrapper around ``httpx.Client``."""

    def __init__(
        self,
        settings: Optional[HttpSettings] = None,
        transport: Optional[httpx.BaseTransport] = None,
        redactor: Optional[Redactor] = None,
        latency_hook: Optional[LatencyHook] = None,
        sleep: Callable[[float], None] = time.sleep,
        limiter: Optional[RateLimiter] = None,
    ):
        self.settings = settings or HttpSettings()
        self.redactor = redactor or default_redactor()
        self.latency_hook = latency_hook
        self._sleep = sleep
        self._transport = transport
        self._client: Optional[httpx.Client] = None
        #: default limiter; callers may override per request (a provider needs a
        #: different policy from its auth endpoint or from an image CDN).
        self.limiter = limiter
        self.request_count = 0
        self.retry_count = 0
        self.throttled_count = 0

    # ------------------------------------------------------------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.settings.timeout(),
                transport=self._transport,
                follow_redirects=self.settings.follow_redirects,
                headers={"User-Agent": self.settings.user_agent},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        content: Optional[str] = None,
        json_body: Optional[Any] = None,
        operation: str = "request",
        expect: str = "json",
        limiter: Optional[RateLimiter] = None,
    ) -> ResponseInfo:
        """Perform a request with finite retries for transient failures.

        ``expect`` is ``"json"`` (decode and validate) or ``"raw"`` (return bytes
        in ``payload``). Retries happen only for connect/read timeouts, transport
        errors, 429 and 5xx — never for 4xx, which are permanent.
        """
        attempt = 0
        started = monotonic()
        last_error: Optional[ProviderError] = None

        active_limiter = limiter if limiter is not None else self.limiter

        while True:
            attempt += 1
            self.request_count += 1
            self._gate(active_limiter, operation)
            try:
                response = self.client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    content=content,
                    json=json_body,
                    timeout=self.settings.timeout(),
                )
                info = self._handle_response(response, url, operation, expect, attempt, started)
                if active_limiter is not None:
                    active_limiter.note_headers(info.headers)
            except httpx.TimeoutException as exc:
                last_error = ProviderTimeout(
                    "The request to %s timed out." % _host_of(url),
                    detail=self.redactor.exception(exc),
                )
            except httpx.TransportError as exc:
                # DNS failure, connection refused, TLS error, unplugged cable.
                last_error = ProviderNetworkError(
                    "Could not reach %s." % _host_of(url),
                    detail=self.redactor.exception(exc),
                )
            except ProviderError as exc:
                last_error = exc
                if exc.retry_after and active_limiter is not None:
                    # Let the provider's own instruction drive the cooldown
                    # instead of guessing from our backoff table.
                    active_limiter.note_retry_after(exc.retry_after)
                if not exc.transient:
                    break
                if attempt > self.settings.max_retries:
                    break
                delay = self._backoff(attempt, exc.retry_after)
                if delay > self.settings.rate_limit_wait_cap:
                    # Waiting longer than the cap would freeze the UI for no
                    # benefit; surface it so the user can retry later.
                    break
                self.retry_count += 1
                self._sleep(delay)
                continue
            else:
                self._record_latency(operation, info)
                return info

            # Transient transport failure: retry with exponential backoff.
            if attempt > self.settings.max_retries:
                break
            self.retry_count += 1
            self._sleep(self._backoff(attempt, None))

        self._notify_latency(operation, (monotonic() - started) * 1000.0, attempt)
        raise last_error or ProviderNetworkError(
            "Request to %s failed." % _host_of(url)
        )

    def _handle_response(
        self,
        response: httpx.Response,
        url: str,
        operation: str,
        expect: str,
        attempt: int,
        started: float,
    ) -> ResponseInfo:
        """Decode a successful response, or raise the matching ProviderError."""
        status = response.status_code

        if status in (401, 403):
            raise ProviderAuthError(
                "The provider rejected the credentials (HTTP %d)." % status,
                detail=self._safe_excerpt(response),
            )
        if status == 429:
            retry_after = _retry_after_seconds(response)
            raise ProviderRateLimited(
                "The provider is rate limiting requests.",
                detail=self._safe_excerpt(response),
                retry_after=retry_after,
            )
        if status in (404,):
            raise ProviderResponseError(
                "The provider has no record for that request (HTTP 404).",
                status_code=status,
                detail=self._safe_excerpt(response),
            )
        if status >= 500:
            # Server-side trouble is transient: the caller's retry loop handles it.
            raise ProviderServerError(
                "The provider returned an error (HTTP %d)." % status,
                status_code=status,
                detail=self._safe_excerpt(response),
            )
        if status >= 400:
            raise ProviderResponseError(
                "The provider rejected the request (HTTP %d)." % status,
                status_code=status,
                detail=self._safe_excerpt(response),
            )

        elapsed = (monotonic() - started) * 1000.0
        if expect == "raw":
            payload: Any = response.content
        else:
            payload = self._decode_json(response)
        return ResponseInfo(
            payload=payload,
            status_code=status,
            url=self.redactor.url(str(response.url or url)),
            latency_ms=round(elapsed, 2),
            attempts=attempt,
            headers={k: self.redactor.text(v) for k, v in response.headers.items()},
        )

    def _decode_json(self, response: httpx.Response) -> Any:
        if not response.content:
            raise ProviderDataError(
                "The provider returned an empty response body."
            )
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderDataError(
                "The provider returned data that is not valid JSON.",
                detail=self.redactor.text(str(exc)),
            )

    def _safe_excerpt(self, response: httpx.Response, limit: int = 240) -> str:
        """A short, redacted body excerpt for diagnostics. Never the whole body."""
        try:
            text = response.text or ""
        except Exception:  # pragma: no cover - unreadable body
            return ""
        return self.redactor.text(text[:limit]).strip()

    # ------------------------------------------------------------------
    def download(
        self,
        url: str,
        destination: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        max_bytes: Optional[int] = None,
        operation: str = "download",
        progress: Optional[Callable[[int, Optional[int]], None]] = None,
        limiter: Optional[RateLimiter] = None,
    ) -> ResponseInfo:
        """Stream a file to ``destination`` (the caller writes to a temp path).

        Enforces a byte cap so a hostile or misconfigured URL cannot fill the
        disk, and reports the true received size even when ``Content-Length`` is
        missing or wrong.
        """
        limit = int(max_bytes or self.settings.max_bytes)
        started = monotonic()
        attempt = 0
        last_error: Optional[ProviderError] = None
        active_limiter = limiter if limiter is not None else self.limiter

        while True:
            attempt += 1
            self.request_count += 1
            self._gate(active_limiter, operation)
            received = 0
            status = 0
            retryable = False
            try:
                with self.client.stream(
                    "GET",
                    url,
                    headers=headers,
                    timeout=self.settings.download_timeout(),
                ) as response:
                    status = response.status_code
                    if status in (401, 403):
                        raise ProviderAuthError(
                            "Artwork host rejected the request (HTTP %d)." % status
                        )
                    if status == 429:
                        raise ProviderRateLimited(
                            "Artwork host is rate limiting requests.",
                            retry_after=_retry_after_seconds(response),
                        )
                    if status >= 500:
                        raise ProviderServerError(
                            "Artwork host error (HTTP %d)." % status, status_code=status
                        )
                    if status >= 400:
                        raise ProviderResponseError(
                            "Artwork could not be downloaded (HTTP %d)." % status,
                            status_code=status,
                        )

                    expected = _content_length(response)
                    with open(destination, "wb") as handle:
                        for chunk in response.iter_bytes(chunk_size=DEFAULT_CHUNK):
                            if not chunk:
                                continue
                            received += len(chunk)
                            if received > limit:
                                raise ProviderDataError(
                                    "Artwork exceeded the %d byte limit and was "
                                    "rejected." % limit
                                )
                            handle.write(chunk)
                            if progress is not None:
                                progress(received, expected)

                elapsed = (monotonic() - started) * 1000.0
                info = ResponseInfo(
                    payload={"bytes": received, "path": destination},
                    status_code=status,
                    url=self.redactor.url(url),
                    latency_ms=round(elapsed, 2),
                    attempts=attempt,
                )
                self._notify_latency(operation, elapsed, attempt)
                return info

            except httpx.TimeoutException as exc:
                last_error = ProviderTimeout(
                    "Artwork download timed out.", detail=self.redactor.exception(exc)
                )
                retryable = True
            except httpx.TransportError as exc:
                last_error = ProviderNetworkError(
                    "Could not reach the artwork host.",
                    detail=self.redactor.exception(exc),
                )
                retryable = True
            except ProviderError as exc:
                last_error = exc
                if exc.retry_after and active_limiter is not None:
                    active_limiter.note_retry_after(exc.retry_after)
                retryable = exc.transient
                if retryable and attempt <= self.settings.max_retries:
                    delay = self._backoff(attempt, exc.retry_after)
                    if delay > self.settings.rate_limit_wait_cap:
                        retryable = False
                    else:
                        self.retry_count += 1
                        self._sleep(delay)
            except OSError as exc:
                # Disk full / not writable: distinct from a network failure and
                # never worth retrying.
                raise ProviderDataError(
                    "Could not write the downloaded artwork to disk.",
                    detail=self.redactor.text(str(exc)),
                )

            if not retryable or attempt > self.settings.max_retries:
                break

        self._notify_latency(operation, (monotonic() - started) * 1000.0, attempt)
        raise last_error or ProviderNetworkError("Artwork download failed.")

    # ------------------------------------------------------------------
    def _gate(self, limiter: Optional[RateLimiter], operation: str) -> None:
        """Wait for (or refuse) permission to send one more request.

        A refusal is surfaced as a typed, user-presentable rate-limit error
        rather than a silent skip, so the UI can say *why* nothing happened.
        """
        if limiter is None:
            return
        result = limiter.acquire(1.0, block=True)
        if result.allowed:
            return
        self.throttled_count += 1
        raise ProviderRateLimited(
            result.reason or "%s requests are being throttled." % operation,
            retry_after=result.retry_after,
        )

    def _backoff(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after:
            return min(float(retry_after), self.settings.backoff_cap * 4)
        delay = self.settings.backoff_base * (2 ** (attempt - 1))
        return min(delay, self.settings.backoff_cap)

    def _record_latency(self, operation: str, info: ResponseInfo) -> None:
        self._notify_latency(operation, info.latency_ms, info.attempts)

    def _notify_latency(self, operation: str, latency_ms: float, attempts: int) -> None:
        if self.latency_hook is None:
            return
        try:
            self.latency_hook(operation, float(latency_ms), int(attempts))
        except Exception:
            # Instrumentation must never break a request.
            pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _host_of(url: str) -> str:
    """Host name only, so error text never leaks a credential in a query string."""
    try:
        parsed = httpx.URL(url)
        return parsed.host or url
    except Exception:
        return url.split("/")[0][:60]


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _content_length(response: httpx.Response) -> Optional[int]:
    raw = response.headers.get("Content-Length")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def describe_error(exc: BaseException, redactor: Optional[Redactor] = None) -> str:
    """One-line, redacted, user-presentable description of any failure."""
    red = redactor or default_redactor()
    if isinstance(exc, ProviderError):
        return red.text(exc.user_text())
    if isinstance(exc, httpx.HTTPError):
        return red.text("Network error: %s" % exc)
    return red.text("%s: %s" % (type(exc).__name__, exc))


def collect_errors(errors: Sequence[BaseException], redactor: Optional[Redactor] = None) -> List[str]:
    red = redactor or default_redactor()
    return [red.text(describe_error(exc, red)) for exc in errors]
