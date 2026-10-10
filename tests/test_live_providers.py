"""Opt-in live provider tests (brief section 10: live tests must be opt-in).

Skipped unless the environment says so explicitly:

    CARTRIDGE_LIVE=1
    CARTRIDGE_IGDB_CLIENT_ID=...
    CARTRIDGE_IGDB_CLIENT_SECRET=...
    CARTRIDGE_RAWG_API_KEY=...
    python -m pytest tests/test_live_providers.py -q

The default suite never reaches this file's assertions: no credentials and no
internet are required anywhere else, and nothing here is allowed to print a
secret. Total cost per run is about 8 HTTP requests, paced by the production
rate-limit policies.
"""

from __future__ import annotations

import os

import pytest

from cartridge import config
from cartridge.core.redaction import Redactor
from cartridge.providers.base import HttpSettings, HttpClient
from cartridge.providers.ratelimit import LimiterSet
from cartridge.providers.service import MetadataService

pytestmark = pytest.mark.live


def _credentials():
    if not config.live_tests_enabled():
        return None
    credentials = config.load_credentials(redactor=Redactor())
    return credentials if credentials.any_configured else None


CREDS = _credentials()

requires_live = pytest.mark.skipif(
    CREDS is None,
    reason="live provider tests are opt-in: set CARTRIDGE_LIVE=1 and export "
           "CARTRIDGE_IGDB_CLIENT_ID / CARTRIDGE_IGDB_CLIENT_SECRET / "
           "CARTRIDGE_RAWG_API_KEY",
)
requires_igdb = pytest.mark.skipif(
    CREDS is None or not CREDS.igdb_configured, reason="IGDB credentials not configured"
)
requires_rawg = pytest.mark.skipif(
    CREDS is None or not CREDS.rawg_configured, reason="RAWG credentials not configured"
)


@pytest.fixture(scope="module")
def service():
    if CREDS is None:
        pytest.skip("live tests disabled")
    limiters = LimiterSet()
    client = HttpClient(settings=HttpSettings(max_retries=1))
    svc = MetadataService(client=client, limiters=limiters)
    svc.set_credentials(
        CREDS.igdb_client_id, CREDS.igdb_client_secret, CREDS.rawg_api_key
    )
    yield svc
    svc.close()


@requires_live
@requires_igdb
def test_igdb_authenticates_through_twitch(service):
    service.igdb.authenticate(force=True)
    info = service.igdb.token_info()
    assert info["authenticated"] is True
    assert info["seconds_until_expiry"] > 60
    # The token must never be exposed through the status dict.
    text = repr(info)
    for value in CREDS.values():
        assert value not in text


@requires_live
@requires_igdb
def test_igdb_search_returns_usable_results(service):
    games = service.igdb.search("The Witcher 2", limit=5)
    assert games, "IGDB returned nothing for a well-known title"
    top = games[0]
    assert top.provider == "igdb"
    assert top.provider_id.isdigit()
    assert top.title
    assert top.year is None or 1980 <= top.year <= 2100
    assert top.rating is None or 0.0 <= top.rating <= 100.0


@requires_live
@requires_igdb
def test_igdb_details_by_id(service):
    games = service.igdb.search("Diablo II", limit=3)
    assert games
    detail = service.igdb.details(games[0].provider_id)
    assert detail is not None
    assert detail.provider_id == games[0].provider_id
    assert detail.title


@requires_live
@requires_rawg
def test_rawg_search_returns_usable_results(service):
    games = service.rawg.search("The Witcher 2", limit=5)
    assert games
    top = games[0]
    assert top.provider == "rawg"
    assert top.title
    # RAWG's 0-5 scale must be normalized to the catalogue's 0-100.
    assert top.rating is None or 0.0 <= top.rating <= 100.0


@requires_live
@requires_rawg
def test_rawg_details_includes_a_description(service):
    games = service.rawg.search("Diablo II", limit=3)
    assert games
    detail = service.rawg.details(games[0].provider_id)
    assert detail is not None
    assert detail.title


@requires_live
def test_service_search_ranks_and_attributes(service):
    outcome = service.search("Baldur's Gate 3", limit=6)
    assert outcome.ok, outcome.errors
    assert outcome.provider_used in ("igdb", "rawg")
    scores = [game.match_score for game in outcome.results]
    assert scores == sorted(scores, reverse=True)
    assert outcome.results[0].match_score > 0.5
    assert all(game.provider for game in outcome.results)


@requires_live
def test_bad_credentials_produce_a_clean_auth_error(service):
    """A wrong secret must be an error message, not a traceback or a leak."""
    from cartridge.providers.base import ProviderAuthError

    broken = MetadataService(client=HttpClient(settings=HttpSettings(max_retries=0)))
    broken.set_credentials(
        "definitely_not_a_real_client_id_00",
        "definitely_not_a_real_secret_00000",
        "",
    )
    try:
        # Twitch answers a bad secret with HTTP 400; the app must translate that
        # into an authentication error the user can act on.
        with pytest.raises(ProviderAuthError) as info:
            broken.igdb.search("anything", limit=1)
        message = str(info.value).lower()
        assert "client id" in message or "client secret" in message
        assert "settings" in message or "twitch said" in message
        assert broken.igdb.last_auth_error
        for value in CREDS.values():
            assert value not in str(info.value)
            assert value not in broken.igdb.last_auth_error
    finally:
        broken.close()


@requires_live
def test_live_run_stays_within_rate_budgets(service):
    summary = service.limiters.summary()
    for name, stats in summary.items():
        assert stats["used_today"] <= stats["max_per_day"], name
        assert stats["used_this_minute"] <= stats["max_per_minute"], name
