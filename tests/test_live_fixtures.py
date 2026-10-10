"""Offline tests against *captured live payloads*.

``tests/fixtures/live_*.json`` are sanitized captures produced by
``scripts/live_api_check.py --save-fixtures``. They pin the mapping code to the
shape the real APIs return today, so a provider field rename shows up here as a
test failure instead of as silently empty metadata on the user's machine.

These tests are offline: no network, no credentials. They skip quietly if the
captures have not been generated yet.
"""

from __future__ import annotations

import json
import os

import pytest

from cartridge.providers.mapping import (
    map_igdb_games,
    map_rawg_games,
    sanitize_for_fixture,
)
from cartridge.providers.ranking import confidence_label, rank
from cartridge.text import search_key

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def capture(name):
    path = os.path.join(FIXTURE_DIR, name)
    if not os.path.exists(path):
        pytest.skip("%s not captured yet (run scripts/live_api_check.py --save-fixtures)" % name)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def igdb_capture():
    return capture("live_igdb_search.json")


@pytest.fixture
def rawg_capture():
    return capture("live_rawg_search.json")


# --------------------------------------------------------------------------
# the captures themselves must be safe to commit
# --------------------------------------------------------------------------
def test_captures_contain_no_credentials(igdb_capture, rawg_capture):
    for name, data in (("igdb", igdb_capture), ("rawg", rawg_capture)):
        text = json.dumps(data).lower()
        for needle in ("client_secret", "access_token", "authorization", "bearer"):
            assert needle not in text, "%s capture mentions %s" % (name, needle)
        # a RAWG key would arrive as a `key` query param; it must not be embedded
        assert "api_key" not in text


def test_sanitizer_would_strip_a_key_if_one_appeared():
    dirty = {"key": "0123456789abcdef0123456789abcdef", "results": [{"name": "ok"}]}
    clean = sanitize_for_fixture(dirty)
    assert clean["key"] == "***REDACTED***"
    assert clean["results"][0]["name"] == "ok"


# --------------------------------------------------------------------------
# IGDB shape
# --------------------------------------------------------------------------
def test_igdb_capture_maps_without_errors(igdb_capture):
    games = map_igdb_games(igdb_capture["payload"])
    assert games, "no IGDB entries mapped from the live capture"
    for game in games:
        assert game.provider == "igdb"
        assert game.provider_id.isdigit()
        assert game.title
        assert game.rating is None or 0.0 <= game.rating <= 100.0
        if game.year is not None:
            assert 1970 <= game.year <= 2100


def test_igdb_capture_preserves_the_fields_the_ui_needs(igdb_capture):
    games = map_igdb_games(igdb_capture["payload"])
    with_cover = [g for g in games if g.cover_url]
    assert with_cover, "live capture produced no covers; the IGDB image field changed?"
    for game in with_cover:
        assert game.cover_url.startswith("https://")
        # covers must be fetched at a display size, never the source resolution
        assert "/t_cover_big/" in game.cover_url

    with_people = [g for g in games if g.developer or g.publisher]
    assert with_people, "live capture produced no developer/publisher; field renamed?"

    with_genres = [g for g in games if g.genres]
    assert with_genres, "live capture produced no genres; field renamed?"


def test_igdb_capture_screenshot_urls_are_rewritten(igdb_capture):
    games = map_igdb_games(igdb_capture["payload"])
    shots = [url for game in games for url in game.screenshot_urls]
    if shots:
        assert all("/t_screenshot_big/" in url for url in shots)
        assert all(url.startswith("https://") for url in shots)


def test_igdb_capture_alternative_names_survive(igdb_capture):
    games = map_igdb_games(igdb_capture["payload"])
    aliases = [name for game in games for name in game.alternative_names]
    assert aliases, (
        "live capture produced no alternative names; cross-language matching "
        "depends on this field"
    )


# --------------------------------------------------------------------------
# RAWG shape
# --------------------------------------------------------------------------
def test_rawg_capture_maps_without_errors(rawg_capture):
    games = map_rawg_games(rawg_capture["payload"])
    assert games, "no RAWG entries mapped from the live capture"
    for game in games:
        assert game.provider == "rawg"
        assert game.provider_id
        assert game.title
        # RAWG is 0-5 on the wire; the catalogue stores 0-100
        assert game.rating is None or 0.0 <= game.rating <= 100.0


def test_rawg_capture_images_are_absolute_https(rawg_capture):
    games = map_rawg_games(rawg_capture["payload"])
    covers = [g.cover_url for g in games if g.cover_url]
    assert covers, "live capture produced no covers; the RAWG image field changed?"
    for url in covers:
        assert url.startswith("https://")


def test_rawg_list_endpoint_has_genres_but_not_people(rawg_capture):
    """RAWG's /games *list* endpoint does not return developers or publishers.

    Verified against a live capture on 2026-10-09: the result objects contain
    genres, platforms, rating, images and esrb_rating, but no `developers` or
    `publishers` keys at all. Those only come from the detail endpoint
    (/games/{slug}), which is why the import flow calls
    :meth:`MetadataService.enrich` on the selected result before writing it to
    the database. If this assertion starts failing, RAWG added the fields to the
    list endpoint and the extra detail round trip can be skipped.
    """
    games = map_rawg_games(rawg_capture["payload"])
    assert any(g.genres for g in games), "genres disappeared from the list endpoint"
    assert all(not g.developer and not g.publisher for g in games), (
        "RAWG now returns people in list results - enrich() can skip the detail "
        "call and save a request"
    )
    raw_results = rawg_capture["payload"].get("results", [])
    assert raw_results
    assert "developers" not in raw_results[0]
    assert "publishers" not in raw_results[0]


def test_rawg_capture_rating_is_on_the_normalized_scale(rawg_capture):
    games = map_rawg_games(rawg_capture["payload"])
    rated = [g.rating for g in games if g.rating is not None]
    if rated:
        # RAWG never returns more than 5; if this ever trips, the normalization
        # or the API changed.
        assert all(value > 5.0 for value in rated), (
            "ratings look un-normalized (0-5 scale detected)"
        )
        assert all(value <= 100.0 for value in rated)


# --------------------------------------------------------------------------
# ranking against real data
# --------------------------------------------------------------------------
def test_ranking_a_real_igdb_capture_puts_the_best_match_first(igdb_capture):
    games = map_igdb_games(igdb_capture["payload"])
    query = igdb_capture.get("query") or games[0].title
    ordered = rank(query, games)
    assert ordered[0].match_score >= ordered[-1].match_score
    assert search_key(query) in search_key(ordered[0].title) or ordered[0].match_score > 0.5
    assert confidence_label(ordered[0].match_score) in ("exact", "strong", "possible")


def test_ranking_a_real_rawg_capture_is_stable(rawg_capture):
    games = map_rawg_games(rawg_capture["payload"])
    query = games[0].title
    first = rank(query, list(games))
    second = rank(query, list(games))
    assert [g.provider_id for g in first] == [g.provider_id for g in second]
