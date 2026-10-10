"""Tests for credential redaction (brief section 15).

All values below are fake. The suite must never contain a real credential.
"""

from __future__ import annotations

import pytest

from cartridge.core.redaction import REDACTED, Redactor, redact, register_secret

FAKE_SECRET = "FAKEclientsecretVALUE0001"
FAKE_KEY = "0123456789abcdef0123456789abcdef"  # 32 hex, RAWG-shaped
FAKE_TOKEN = "ya29.fakeaccesstokenABCDEFG"


@pytest.fixture
def r():
    return Redactor([FAKE_SECRET, FAKE_KEY, FAKE_TOKEN])


def test_known_values_are_scrubbed_from_plain_text(r):
    out = r.text("failed with secret=%s while calling api" % FAKE_SECRET)
    assert FAKE_SECRET not in out
    assert REDACTED in out


def test_multiple_occurrences_are_all_scrubbed(r):
    out = r.text("%s and again %s" % (FAKE_KEY, FAKE_KEY))
    assert FAKE_KEY not in out
    assert out.count(REDACTED) >= 2


def test_authorization_header_pattern_is_masked():
    out = Redactor().text("Authorization: Bearer abcdef0123456789abcdef")
    assert "abcdef0123456789abcdef" not in out
    assert "authorization" in out.lower()
    assert REDACTED in out


def test_bearer_token_without_header_name_is_masked():
    out = Redactor().text("request used Bearer zz99887766554433221100aabbccddeeff")
    assert "zz99887766554433221100aabbccddeeff" not in out


def test_query_string_credentials_are_masked():
    raw = "https://api.rawg.io/api/games?key=%s&search=diablo" % FAKE_KEY
    out = Redactor().text(raw)
    assert FAKE_KEY not in out
    assert "search=diablo" in out


def test_client_secret_assignment_is_masked():
    out = Redactor().text("client_secret=zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz&grant_type=client_credentials")
    assert "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz" not in out
    assert "grant_type=client_credentials" in out


def test_long_high_entropy_blob_is_masked_even_when_unknown():
    blob = "aB3xK9mQ2wZ7rT5yU8iO1pL4vN6cX0sD9fG2hJ5k"
    out = Redactor().text("token leaked: %s" % blob)
    assert blob not in out


def test_32_hex_key_is_masked_by_pattern():
    out = Redactor().text("api key was %s" % FAKE_KEY)
    assert FAKE_KEY not in out


def test_normal_prose_is_untouched():
    sample = "IGDB returned 20 results in 412 ms for query 'The Witcher 2'"
    assert Redactor().text(sample) == sample


def test_numbers_and_versions_are_untouched():
    sample = "PyQt5 5.15.11 / Qt 5.15.2 / httpx 0.28.1 / Python 3.8.20"
    out = Redactor().text(sample)
    assert "5.15.11" in out and "0.28.1" in out


def test_mapping_drops_sensitive_headers(r):
    headers = {
        "Client-ID": "someclientid",
        "Authorization": "Bearer %s" % FAKE_TOKEN,
        "Accept": "application/json",
        "User-Agent": "cartridge/0.1",
    }
    clean = r.mapping(headers)
    assert clean["Client-ID"] == REDACTED
    assert FAKE_TOKEN not in str(clean["Authorization"])
    assert clean["Accept"] == "application/json"
    assert clean["User-Agent"] == "cartridge/0.1"


def test_mapping_handles_empty_and_none(r):
    assert r.mapping(None) == {}
    assert r.mapping({}) == {}


def test_url_scrubbing_keeps_host_and_path(r):
    url = "https://api.rawg.io/api/games?key=%s&page_size=10" % FAKE_KEY
    out = r.url(url)
    assert FAKE_KEY not in out
    assert out.startswith("https://api.rawg.io/api/games?")
    assert "page_size=10" in out


def test_url_scrubbing_masks_known_secret_anywhere(r):
    out = r.url("https://example.com/x/%s" % FAKE_SECRET)
    assert FAKE_SECRET not in out


def test_exception_description_is_single_line_and_scrubbed(r):
    exc = RuntimeError("connect failed with key=%s" % FAKE_KEY)
    out = r.exception(exc)
    assert FAKE_KEY not in out
    assert out.startswith("RuntimeError:")
    assert "\n" not in out


def test_non_string_input_is_coerced(r):
    assert r.text(None) == ""
    assert r.text(404) == "404"
    assert REDACTED in r.text({"k": FAKE_SECRET})


def test_register_ignores_short_and_empty_values():
    red = Redactor()
    red.register("")
    red.register(None)
    red.register("abc")  # below the minimum length floor
    assert red.known_count == 0
    red.register(FAKE_SECRET)
    assert red.known_count == 1
    red.forget(FAKE_SECRET)
    assert red.known_count == 0


def test_default_redactor_is_process_wide():
    register_secret(FAKE_TOKEN)
    out = redact("refresh_token=%s" % FAKE_TOKEN)
    assert FAKE_TOKEN not in out


def test_redaction_is_lossy_in_one_direction(r):
    out = r.text(FAKE_SECRET)
    assert out == REDACTED or REDACTED in out
    assert FAKE_SECRET not in out
