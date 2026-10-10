"""Tests for the httpx transport layer: timeouts, retries, status handling,
downloads and credential redaction.

Every test uses ``httpx.MockTransport``, so the suite is deterministic and needs
neither the network nor real credentials (brief section 16).
"""

from __future__ import annotations

import os

import httpx
import pytest

from cartridge.providers.base import (
    DEFAULT_MAX_BYTES,
    HttpSettings,
    HttpClient,
    ProviderAuthError,
    ProviderDataError,
    ProviderNetworkError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeout,
    describe_error,
)
from cartridge.providers.ratelimit import TEST_POLICY, RateLimiter
from tests._png import png_bytes


def make_client(handler, **settings) -> HttpClient:
    """A client wired to a mock transport, with sleeps replaced by a counter."""
    sleeps = []
    client = HttpClient(
        settings=HttpSettings(**settings),
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        limiter=RateLimiter(TEST_POLICY, sleep=lambda _seconds: None),
    )
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client


def json_response(payload, status=200, headers=None):
    return httpx.Response(status, json=payload, headers=headers or {})


# --------------------------------------------------------------------------
# success paths
# --------------------------------------------------------------------------
def test_json_request_returns_payload_and_latency():
    def handler(request):
        return json_response({"ok": True, "n": 3})

    client = make_client(handler)
    info = client.request("GET", "https://api.example.test/things", operation="test")
    assert info.status_code == 200
    assert info.payload == {"ok": True, "n": 3}
    assert info.latency_ms >= 0.0
    assert info.attempts == 1
    assert client.request_count == 1


def test_explicit_timeouts_are_always_set():
    settings = HttpSettings()
    timeout = settings.timeout()
    assert timeout.connect == 5.0
    assert timeout.read == 15.0
    assert timeout.write == 10.0
    assert timeout.pool == 5.0
    assert settings.download_timeout().read == 30.0


def test_user_agent_and_custom_headers_are_sent():
    seen = {}

    def handler(request):
        seen["user_agent"] = request.headers.get("User-Agent")
        seen["client_id"] = request.headers.get("Client-ID")
        return json_response({})

    client = make_client(handler)
    client.request("GET", "https://api.example.test/x", headers={"Client-ID": "abc"})
    assert seen["user_agent"].startswith("Cartridge/")
    assert seen["client_id"] == "abc"


def test_query_params_and_json_body_are_passed_through():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        return json_response({})

    client = make_client(handler)
    client.request(
        "POST",
        "https://api.example.test/x",
        params={"search": "witcher"},
        json_body={"a": 1},
    )
    assert "search=witcher" in captured["url"]
    assert '"a"' in captured["body"]


def test_raw_expect_returns_bytes():
    def handler(request):
        return httpx.Response(200, content=b"\x89PNG raw")

    client = make_client(handler)
    info = client.request("GET", "https://img.example.test/a.png", expect="raw")
    assert info.payload == b"\x89PNG raw"


def test_latency_hook_receives_operation_and_duration():
    calls = []

    def handler(request):
        return json_response({})

    client = make_client(handler)
    client.latency_hook = lambda op, ms, attempts: calls.append((op, ms, attempts))
    client.request("GET", "https://api.example.test/x", operation="search")
    assert calls and calls[0][0] == "search"
    assert calls[0][1] >= 0.0


def test_latency_hook_failure_does_not_break_the_request():
    def handler(request):
        return json_response({"ok": True})

    def bad_hook(op, ms, attempts):
        raise RuntimeError("instrumentation exploded")

    client = make_client(handler)
    client.latency_hook = bad_hook
    info = client.request("GET", "https://api.example.test/x")
    assert info.payload == {"ok": True}


# --------------------------------------------------------------------------
# status handling
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,expected",
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (404, ProviderResponseError),
        (400, ProviderResponseError),
        (422, ProviderResponseError),
    ],
)
def test_client_errors_raise_typed_exceptions(status, expected):
    def handler(request):
        return httpx.Response(status, text="nope")

    client = make_client(handler)
    with pytest.raises(expected) as info:
        client.request("GET", "https://api.example.test/x")
    assert info.value.transient is False
    # A permanent error must not be retried.
    assert client.request_count == 1


def test_429_raises_rate_limited_with_retry_after():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, text="slow down", headers={"Retry-After": "3"})

    client = make_client(handler, max_retries=1)
    with pytest.raises(ProviderRateLimited) as info:
        client.request("GET", "https://api.example.test/x")
    assert info.value.retry_after == 3.0
    assert calls["n"] == 2, "429 is transient and should have been retried once"


def test_rate_limit_longer_than_the_cap_is_not_waited_out():
    def handler(request):
        return httpx.Response(429, text="slow down", headers={"Retry-After": "600"})

    client = make_client(handler, max_retries=2, rate_limit_wait_cap=15.0)
    with pytest.raises(ProviderRateLimited):
        client.request("GET", "https://api.example.test/x")
    assert client.request_count == 1, "must not sleep 10 minutes on the GUI's behalf"
    assert client.sleeps == []


def test_5xx_is_retried_then_surfaced():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    client = make_client(handler, max_retries=2)
    with pytest.raises(ProviderServerError):
        client.request("GET", "https://api.example.test/x")
    assert calls["n"] == 3  # initial + 2 retries
    assert len(client.sleeps) == 2
    assert client.sleeps[1] > client.sleeps[0], "backoff must grow"


def test_recovery_after_a_transient_server_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502, text="bad gateway")
        return json_response({"recovered": True})

    client = make_client(handler, max_retries=3)
    info = client.request("GET", "https://api.example.test/x")
    assert info.payload == {"recovered": True}
    assert info.attempts == 3


# --------------------------------------------------------------------------
# transport failures
# --------------------------------------------------------------------------
def test_connect_error_becomes_network_error():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(handler, max_retries=1)
    with pytest.raises(ProviderNetworkError) as info:
        client.request("GET", "https://api.example.test/x")
    assert info.value.transient is True
    assert client.request_count == 2


def test_dns_failure_becomes_network_error():
    def handler(request):
        raise httpx.ConnectError(
            "All connection attempts failed", request=request
        )

    client = make_client(handler, max_retries=0)
    with pytest.raises(ProviderNetworkError):
        client.request("GET", "https://api.invalid.test/x")
    assert client.request_count == 1


def test_tls_error_becomes_network_error():
    def handler(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]", request=request)

    client = make_client(handler, max_retries=0)
    with pytest.raises(ProviderNetworkError) as info:
        client.request("GET", "https://api.example.test/x")
    assert "reach" in str(info.value).lower()


def test_timeout_becomes_typed_timeout():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    client = make_client(handler, max_retries=1)
    with pytest.raises(ProviderTimeout):
        client.request("GET", "https://api.example.test/x")
    assert client.request_count == 2


def test_error_messages_never_include_the_full_url_query_string():
    """A RAWG-style key lives in the query string; it must not reach the UI."""
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = make_client(handler, max_retries=0)
    url = "https://api.rawg.io/api/games?key=SUPERSECRETKEYVALUE1234567890"
    client.redactor.register("SUPERSECRETKEYVALUE1234567890")
    with pytest.raises(ProviderNetworkError) as info:
        client.request("GET", url)
    assert "SUPERSECRETKEYVALUE1234567890" not in str(info.value)
    assert "SUPERSECRETKEYVALUE1234567890" not in describe_error(info.value, client.redactor)


# --------------------------------------------------------------------------
# payload validation
# --------------------------------------------------------------------------
def test_malformed_json_raises_data_error():
    def handler(request):
        return httpx.Response(200, text="{not json at all", headers={"Content-Type": "application/json"})

    client = make_client(handler)
    with pytest.raises(ProviderDataError):
        client.request("GET", "https://api.example.test/x")


def test_empty_body_raises_data_error():
    def handler(request):
        return httpx.Response(200, content=b"")

    client = make_client(handler)
    with pytest.raises(ProviderDataError):
        client.request("GET", "https://api.example.test/x")


def test_html_instead_of_json_raises_data_error():
    def handler(request):
        return httpx.Response(
            200, text="<html><body>captive portal</body></html>",
            headers={"Content-Type": "text/html"},
        )

    client = make_client(handler)
    with pytest.raises(ProviderDataError):
        client.request("GET", "https://api.example.test/x")


def test_error_detail_excerpt_is_truncated_and_redacted():
    secret = "SECRETTOKENVALUEabcdefghij1234567890"

    def handler(request):
        return httpx.Response(400, text=(secret + "x" * 5000))

    client = make_client(handler)
    client.redactor.register(secret)
    with pytest.raises(ProviderResponseError) as info:
        client.request("GET", "https://api.example.test/x")
    assert secret not in info.value.detail
    assert len(info.value.detail) < 300


# --------------------------------------------------------------------------
# downloads
# --------------------------------------------------------------------------
def test_download_writes_the_file(tmp_path):
    data = png_bytes(8, 8)

    def handler(request):
        return httpx.Response(200, content=data)

    client = make_client(handler)
    destination = str(tmp_path / "cover.part")
    info = client.download("https://img.example.test/cover.jpg", destination)
    assert info.payload["bytes"] == len(data)
    with open(destination, "rb") as handle:
        assert handle.read() == data


def test_download_rejects_oversize_payload(tmp_path):
    big = b"x" * (2048 + 1)

    def handler(request):
        return httpx.Response(200, content=big)

    client = make_client(handler)
    destination = str(tmp_path / "cover.part")
    with pytest.raises(ProviderDataError):
        client.download(
            "https://img.example.test/cover.jpg", destination, max_bytes=2048
        )
    # A rejected download must not leave a complete-looking file behind.
    assert not os.path.exists(destination) or os.path.getsize(destination) <= 2048


def test_download_default_cap_is_bounded():
    assert DEFAULT_MAX_BYTES == 12 * 1024 * 1024


def test_download_reports_progress(tmp_path):
    data = png_bytes(16, 16)
    seen = []

    def handler(request):
        return httpx.Response(
            200, content=data, headers={"Content-Length": str(len(data))}
        )

    client = make_client(handler)
    client.download(
        "https://img.example.test/cover.jpg",
        str(tmp_path / "x.part"),
        progress=lambda received, expected: seen.append((received, expected)),
    )
    assert seen
    assert seen[-1][0] == len(data)
    assert seen[-1][1] == len(data)


def test_download_404_raises_response_error(tmp_path):
    def handler(request):
        return httpx.Response(404, text="gone")

    client = make_client(handler)
    with pytest.raises(ProviderResponseError) as info:
        client.download("https://img.example.test/x.jpg", str(tmp_path / "x.part"))
    assert info.value.status_code == 404


def test_download_403_raises_auth_error(tmp_path):
    def handler(request):
        return httpx.Response(403, text="forbidden")

    client = make_client(handler)
    with pytest.raises(ProviderAuthError):
        client.download("https://img.example.test/x.jpg", str(tmp_path / "x.part"))


def test_download_retries_server_errors(tmp_path):
    calls = {"n": 0}
    data = png_bytes(4, 4)

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, content=data)

    client = make_client(handler, max_retries=2)
    info = client.download("https://img.example.test/x.jpg", str(tmp_path / "x.part"))
    assert info.payload["bytes"] == len(data)
    assert calls["n"] == 2


def test_download_timeout_is_typed(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("stalled", request=request)

    client = make_client(handler, max_retries=0)
    with pytest.raises(ProviderTimeout):
        client.download("https://img.example.test/x.jpg", str(tmp_path / "x.part"))


def test_download_unwritable_destination_is_a_data_error(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"12345")

    client = make_client(handler)
    # A path whose parent does not exist cannot be opened.
    missing = str(tmp_path / "no-such-dir" / "x.part")
    with pytest.raises(ProviderDataError) as info:
        client.download("https://img.example.test/x.jpg", missing)
    assert "write" in str(info.value).lower()


def test_truncated_stream_leaves_a_partial_file_the_caller_can_discard(tmp_path):
    """An interrupted transfer must be detectable, never silently accepted."""
    def handler(request):
        def chunks():
            yield b"first-part"
            raise httpx.ReadError("connection reset")

        return httpx.Response(200, content=chunks())

    client = make_client(handler, max_retries=0)
    destination = str(tmp_path / "x.part")
    with pytest.raises(ProviderNetworkError):
        client.download("https://img.example.test/x.jpg", destination)
    # The caller (artwork.downloader) treats the .part file as garbage and
    # deletes it; the important part is that no exception was swallowed.
    assert not os.path.exists(destination) or os.path.getsize(destination) < 100


# --------------------------------------------------------------------------
# client lifecycle
# --------------------------------------------------------------------------
def test_client_reuses_one_connection_and_can_close():
    def handler(request):
        return json_response({})

    client = make_client(handler)
    client.request("GET", "https://api.example.test/a")
    first = client._client
    client.request("GET", "https://api.example.test/b")
    assert client._client is first
    client.close()
    assert client._client is None
    # usable again after close (a new client is built lazily)
    client.request("GET", "https://api.example.test/c")
    assert client._client is not None
    client.close()


def test_context_manager_closes():
    def handler(request):
        return json_response({})

    with make_client(handler) as client:
        client.request("GET", "https://api.example.test/a")
    assert client._client is None


def test_request_and_retry_counters():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="x")
        return json_response({})

    client = make_client(handler, max_retries=2)
    client.request("GET", "https://api.example.test/a")
    assert client.request_count == 2
    assert client.retry_count == 1
