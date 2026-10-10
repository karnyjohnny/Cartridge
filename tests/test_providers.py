"""Tests for IGDB, RAWG, mapping, ranking and the fallback service.

All payloads come from ``tests/fixtures/`` or from inline dicts shaped like the
real APIs. Transport is mocked, so nothing here needs the network or credentials.
The opt-in live version of these checks is ``tests/test_live_providers.py``.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from cartridge.core.models import PROVIDER_IGDB, PROVIDER_RAWG
from cartridge.providers.base import (
    HttpSettings,
    HttpClient,
    ProviderAuthError,
    ProviderDataError,
    ProviderRateLimited,
    ProviderResponseError,
)
from cartridge.providers.igdb import (
    IgdbProvider,
    escape_apicalypse,
)
from cartridge.providers.mapping import (
    as_float,
    as_int,
    as_list,
    as_text,
    first_image,
    igdb_age_rating,
    igdb_image_url,
    map_igdb_game,
    map_igdb_games,
    map_rawg_game,
    map_rawg_games,
    name_list,
    normalize_http_url,
    provider_label,
    rawg_rating,
    sanitize_for_fixture,
    year_from_epoch,
)
from cartridge.providers.ranking import (
    AUTO_CONFIRM,
    confidence_label,
    dedupe,
    explain,
    is_confident,
    rank,
    score_game,
    summarize,
)
from cartridge.providers.rawg import RawgProvider
from cartridge.providers.ratelimit import TEST_POLICY, RateLimiter
from cartridge.providers.service import MetadataService, SearchOutcome

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

FAKE_IGDB_ID = "fakeigdbclientid000000000000"
FAKE_IGDB_SECRET = "fakeigdbclientsecret0000000"
FAKE_RAWG_KEY = "0123456789abcdef0123456789abcdef"


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)["payload"]


def mock_client(handler, **settings):
    """A client with mocked transport, mocked sleeps and unlimited rate policy."""
    return HttpClient(
        settings=HttpSettings(**settings),
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
        limiter=RateLimiter(TEST_POLICY, sleep=lambda _seconds: None),
    )


# --------------------------------------------------------------------------
# mapping: scalar coercion
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("  Hello   World ", "Hello World"),
        ("<b>Bold</b> text", "Bold text"),
        (12345, "12345"),
        (None, None),
        (True, None),
        ("", None),
        ("   ", None),
        ({"name": "from dict"}, "from dict"),
        (["list"], None),
        (b"bytes", None),
    ],
)
def test_as_text(value, expected):
    assert as_text(value) == expected


def test_as_text_truncates():
    assert as_text("x" * 500, limit=10) == "x" * 10


def test_as_int_and_float_bounds():
    assert as_int("42") == 42
    assert as_int(42.9) == 42
    assert as_int("nope") is None
    assert as_int(True) is None
    assert as_int(5, minimum=10) is None
    assert as_int(50, maximum=10) is None
    assert as_float("3.5") == 3.5
    assert as_float(float("nan")) is None
    assert as_float(150.0, maximum=100.0) is None
    assert as_float(None) is None


def test_as_list_tolerates_anything():
    assert as_list([1, 2]) == [1, 2]
    assert as_list(None) == []
    assert as_list("scalar") == [], "a stray string must not become a list item"
    assert as_list({"results": [1]}) == [1]
    assert as_list({"a": 1}) == [{"a": 1}]


def test_name_list_dedupes_and_skips_junk():
    items = [
        {"name": "RPG"}, {"name": "rpg"}, {"name": "Action"},
        {"name": None}, None, "Plain String", {"company": {"name": "CDPR"}},
    ]
    assert name_list(items) == ["RPG", "Action", "Plain String", "CDPR"]


def test_name_list_respects_limit():
    assert len(name_list([{"name": "g%d" % i} for i in range(200)], limit=5)) == 5


def test_normalize_http_url_rejects_schemes():
    assert normalize_http_url("//example.com/a.jpg") == "https://example.com/a.jpg"
    assert normalize_http_url("https://example.com/a.jpg") == "https://example.com/a.jpg"
    assert normalize_http_url("file:///etc/passwd") is None
    assert normalize_http_url("javascript:alert(1)") is None
    assert normalize_http_url("not a url") is None
    assert normalize_http_url("https://x/" + "a" * 3000) is None


def test_year_from_epoch():
    assert year_from_epoch(1305849600) == 2011
    assert year_from_epoch(None) is None
    assert year_from_epoch("junk") is None
    assert year_from_epoch(-1) is None
    assert year_from_epoch(10 ** 15) is None


# --------------------------------------------------------------------------
# mapping: IGDB
# --------------------------------------------------------------------------
def test_igdb_image_url_rewrites_size_token():
    raw = "//images.igdb.com/igdb/image/upload/t_thumb/co1wyy.jpg"
    assert igdb_image_url(raw, "cover") == (
        "https://images.igdb.com/igdb/image/upload/t_cover_big/co1wyy.jpg"
    )
    assert "t_screenshot_big" in igdb_image_url(raw, "screenshot")
    assert igdb_image_url(None) is None
    assert igdb_image_url("nope") is None


def test_first_image_and_unknown_kind():
    items = [{"url": "//images.igdb.com/igdb/image/upload/t_thumb/a.jpg"}]
    assert first_image(items, "cover").endswith("t_cover_big/a.jpg")
    assert first_image([], "cover") is None


def test_igdb_age_rating_prefers_known_enums():
    assert igdb_age_rating([{"category": 2, "rating": 5}]) == "PEGI 18"
    assert igdb_age_rating([{"category": 1, "rating": 6}]) == "ESRB: M"
    assert igdb_age_rating([{"category": 9, "rating": 99}]) is None
    assert igdb_age_rating(None) is None


def test_map_igdb_game_from_fixture():
    payload = load_fixture("igdb_search.json")[0]
    game = map_igdb_game(payload)
    assert game is not None
    assert game.provider == PROVIDER_IGDB
    assert game.provider_id == "17916"
    assert game.title.startswith("The Witcher 2")
    assert game.year == 2011
    assert game.release_date == "2011-05-20"
    assert game.rating == 82.31
    assert game.developer == "CD Projekt RED"
    assert game.publisher == "CD Projekt"
    assert game.franchise == "The Witcher"
    assert "Role-playing (RPG)" in game.genres
    assert "PC (Microsoft Windows)" in game.platforms
    assert "Wiedzmin 2" in game.alternative_names
    assert game.age_rating == "PEGI 18"
    assert "t_cover_big" in game.cover_url
    assert len(game.screenshot_urls) == 2
    assert game.url.startswith("https://www.igdb.com/")


def test_map_igdb_games_skips_unusable_entries():
    payload = load_fixture("igdb_search.json")
    games = map_igdb_games(payload)
    assert len(games) == 2, "the entry with id but no title must be dropped"
    assert all(g.title for g in games)


def test_map_igdb_game_rejects_missing_id_or_title():
    assert map_igdb_game({"name": "No Id"}) is None
    assert map_igdb_game({"id": 1}) is None
    assert map_igdb_game({"id": 1, "name": ""}) is None
    assert map_igdb_game(None) is None
    assert map_igdb_game("string") is None
    assert map_igdb_games(None) == []


def test_map_igdb_game_handles_wrong_types_without_raising():
    payload = load_fixture("igdb_search.json")[2]
    # id 999 has a null name, a numeric summary, a non-numeric rating and a
    # string where a list belongs: all must degrade, none may raise.
    game = map_igdb_game(dict(payload, name="Salvaged Title"))
    assert game is not None
    assert game.title == "Salvaged Title"
    assert game.rating is None
    assert game.genres == []
    assert game.cover_url is None
    assert game.platforms == ["42"]


def test_map_igdb_release_falls_back_to_release_dates():
    game = map_igdb_game(
        {
            "id": 5,
            "name": "No First Release Date",
            "release_dates": [{"date": 946684800}, {"date": 1305849600}],
        }
    )
    assert game.year == 2000, "earliest known release date wins"


def test_map_igdb_rating_fallbacks():
    assert map_igdb_game({"id": 1, "name": "A", "aggregated_rating": 70.0}).rating == 70.0
    assert map_igdb_game({"id": 1, "name": "A", "total_rating": 60.0}).rating == 60.0
    assert map_igdb_game({"id": 1, "name": "A"}).rating is None


def test_summary_is_truncated():
    game = map_igdb_game({"id": 1, "name": "A", "summary": "x" * 20000})
    assert len(game.summary) <= 4000


# --------------------------------------------------------------------------
# mapping: RAWG
# --------------------------------------------------------------------------
def test_map_rawg_games_from_fixture():
    payload = load_fixture("rawg_search.json")
    games = map_rawg_games(payload)
    assert len(games) == 1, "the entry with no id/name must be dropped"
    game = games[0]
    assert game.provider == PROVIDER_RAWG
    assert game.provider_id == "3272"
    assert game.title == "The Witcher 2: Assassins of Kings"
    assert game.year == 2011
    assert game.release_date == "2011-05-17"
    assert game.rating == 82.2, "0-5 scale normalized to 0-100"
    assert game.developer == "CD Projekt RED"
    assert game.publisher == "CD Projekt"
    assert game.age_rating == "ESRB: Mature"
    assert game.cover_url.startswith("https://media.rawg.io/")
    # the null-image entry in short_screenshots must be skipped
    assert len(game.screenshot_urls) == 2
    assert game.url == "https://rawg.io/games/the-witcher-2-assassins-of-kings"


def test_rawg_rating_normalization():
    assert rawg_rating(5.0) == 100.0
    assert rawg_rating(0.0) == 0.0
    assert rawg_rating(9.0) is None, "out of the 0-5 range"
    assert rawg_rating(None) is None


def test_map_rawg_detail_shape_with_nested_screenshots():
    game = map_rawg_game(
        {
            "id": 1,
            "slug": "some-game",
            "name": "Some Game",
            "description_raw": "A description.",
            "screenshots": {"results": [{"image": "https://media.rawg.io/s1.jpg"}]},
            "series": [{"name": "Some Series"}],
            "pegi": {"rating": "18"},
        }
    )
    assert game.summary == "A description."
    assert game.screenshot_urls == ["https://media.rawg.io/s1.jpg"]
    assert game.franchise == "Some Series"
    assert game.age_rating == "PEGI 18"


def test_map_rawg_rejects_junk():
    assert map_rawg_game(None) is None
    assert map_rawg_game({}) is None
    assert map_rawg_game({"name": "no id"}) is None
    assert map_rawg_games("nope") == []
    assert map_rawg_games({"count": 0, "results": []}) == []


def test_provider_label():
    assert provider_label(PROVIDER_IGDB) == "IGDB"
    assert provider_label(PROVIDER_RAWG) == "RAWG"
    assert provider_label("manual") == "Manual entry"
    assert provider_label(None) == "Unknown provider"
    assert provider_label("mystery") == "Unknown provider"


def test_sanitize_for_fixture_strips_credential_shaped_keys():
    dirty = {
        "access_token": "abcdef0123456789abcdef",
        "key": FAKE_RAWG_KEY,
        "nested": {"client_secret": FAKE_IGDB_SECRET, "safe": "value"},
        "list": [{"token": "xyz0123456789abcdef"}],
    }
    clean = sanitize_for_fixture(dirty)
    text = json.dumps(clean)
    assert FAKE_RAWG_KEY not in text
    assert FAKE_IGDB_SECRET not in text
    assert "abcdef0123456789abcdef" not in text
    assert clean["nested"]["safe"] == "value"


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------
def make_game(title, provider=PROVIDER_IGDB, year=None, aliases=None, rating=None):
    from cartridge.core.models import ProviderGame

    return ProviderGame(
        provider=provider,
        provider_id=str(abs(hash(title)) % 100000),
        title=title,
        year=year,
        alternative_names=aliases or [],
        rating=rating,
    )


def test_rank_orders_by_match_score():
    games = [
        make_game("The Witcher 3: Wild Hunt"),
        make_game("The Witcher 2: Assassins of Kings"),
        make_game("Diablo III"),
    ]
    ordered = rank("The Witcher 2", games)
    assert ordered[0].title.startswith("The Witcher 2")
    assert ordered[0].match_score >= ordered[1].match_score
    assert ordered[-1].title == "Diablo III"


def test_rank_does_not_mutate_input_order():
    games = [make_game("B"), make_game("A")]
    original = list(games)
    rank("A", games)
    assert games == original


def test_rank_prefers_igdb_on_a_tie():
    games = [
        make_game("Same Title", provider=PROVIDER_RAWG),
        make_game("Same Title", provider=PROVIDER_IGDB),
    ]
    ordered = rank("Same Title", games)
    assert ordered[0].provider == PROVIDER_IGDB


def test_rank_uses_rating_as_a_tiebreaker():
    games = [
        make_game("Same Title", provider=PROVIDER_RAWG, rating=10.0),
        make_game("Same Title", provider=PROVIDER_RAWG, rating=90.0),
    ]
    ordered = rank("Same Title", games)
    assert ordered[0].rating == 90.0


def test_confidence_labels():
    assert confidence_label(1.0) == "exact"
    assert confidence_label(0.85) == "strong"
    assert confidence_label(0.5) == "possible"
    assert confidence_label(0.1) == "weak"


def test_is_confident_requires_near_exact():
    exact = make_game("The Witcher 2")
    score_game(exact, "The Witcher 2")
    assert is_confident(exact) is True
    loose = make_game("The Witcher 2: Enhanced Edition")
    score_game(loose, "witcher")
    assert is_confident(loose) is False
    assert is_confident(None) is False


def test_explain_mentions_year_and_alias():
    game = make_game(
        "The Witcher 2: Enhanced Edition", year=2011, aliases=["Wiedzmin 2"]
    )
    score_game(game, "Wiedzmin 2", query_year=2011)
    text = explain(game.match_score, game, "Wiedzmin 2")
    assert "2011" in text
    assert "alternative title" in text


def test_explain_reports_a_year_disagreement():
    game = make_game("Some Game", year=2015)
    score_game(game, "Some Game", query_year=2001)
    text = explain(game.match_score, game, "Some Game 2001")
    assert "differs" in text


def test_dedupe_removes_cross_provider_duplicates():
    games = [
        make_game("The Witcher 2", provider=PROVIDER_IGDB, year=2011),
        make_game("the witcher 2", provider=PROVIDER_RAWG, year=2011),
    ]
    assert len(dedupe(games)) == 1


def test_dedupe_keeps_different_years():
    games = [
        make_game("Some Game", provider=PROVIDER_IGDB, year=2001),
        make_game("Some Game", provider=PROVIDER_RAWG, year=2020),
    ]
    assert len(dedupe(games)) == 2


def test_dedupe_keeps_entries_without_titles():
    from cartridge.core.models import ProviderGame

    odd = ProviderGame(provider=PROVIDER_IGDB, provider_id="1", title="!!!")
    assert len(dedupe([odd])) == 1


def test_summarize_counts_bands():
    games = rank(
        "The Witcher 2",
        [
            make_game("The Witcher 2"),
            make_game("The Witcher 2: Enhanced Edition"),
            make_game("Diablo"),
        ],
    )
    summary = summarize(games)
    assert sum(summary.values()) == 3
    assert summary["exact"] >= 1


# --------------------------------------------------------------------------
# IGDB provider
# --------------------------------------------------------------------------
def twitch_ok(request):
    return httpx.Response(
        200, json={"access_token": "faketoken0123456789abcdef", "expires_in": 3600,
                   "token_type": "bearer"}
    )


def test_igdb_requires_credentials():
    provider = IgdbProvider(mock_client(lambda r: twitch_ok(r)))
    assert provider.configured is False
    with pytest.raises(ProviderAuthError):
        provider.search("anything")
    assert "not set" in provider.last_auth_error


def test_igdb_authenticates_then_searches():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        return httpx.Response(200, json=load_fixture("igdb_search.json"))

    provider = IgdbProvider(
        mock_client(handler), FAKE_IGDB_ID, FAKE_IGDB_SECRET
    )
    games = provider.search("The Witcher 2")
    assert len(games) >= 2
    assert any("id.twitch.tv" in url for url in calls)
    assert any("api.igdb.com/v4/games" in url for url in calls)
    assert provider.has_token is True


def test_igdb_token_is_cached_across_searches():
    calls = {"token": 0, "search": 0}

    def handler(request):
        if "id.twitch.tv" in str(request.url):
            calls["token"] += 1
            return twitch_ok(request)
        calls["search"] += 1
        return httpx.Response(200, json=load_fixture("igdb_search.json"))

    provider = IgdbProvider(mock_client(handler), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    provider.search("A")
    provider.search("B")
    provider.search("C")
    assert calls["token"] == 1, "token must be reused until it expires"
    assert calls["search"] == 3


def test_igdb_token_is_refreshed_after_expiry():
    calls = {"token": 0}
    clock = {"t": 1000.0}

    def handler(request):
        if "id.twitch.tv" in str(request.url):
            calls["token"] += 1
            return twitch_ok(request)
        return httpx.Response(200, json=load_fixture("igdb_search.json"))

    provider = IgdbProvider(
        mock_client(handler), FAKE_IGDB_ID, FAKE_IGDB_SECRET,
        clock=lambda: clock["t"],
    )
    provider.search("A")
    clock["t"] += 7200  # well past the 1 h lifetime
    provider.search("B")
    assert calls["token"] == 2


def test_igdb_reauthenticates_once_on_401():
    state = {"n": 0}

    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(401, text="invalid token")
        return httpx.Response(200, json=load_fixture("igdb_search.json"))

    provider = IgdbProvider(mock_client(handler), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    games = provider.search("The Witcher 2")
    assert len(games) >= 1
    assert state["n"] == 2


def test_igdb_falls_back_when_the_version_filter_is_rejected():
    bodies = []

    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        bodies.append(request.content.decode("utf-8"))
        if "version_parent" in bodies[-1]:
            return httpx.Response(400, text="syntax error")
        return httpx.Response(200, json=load_fixture("igdb_search.json"))

    provider = IgdbProvider(mock_client(handler), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    games = provider.search("The Witcher 2")
    assert len(games) >= 1
    assert any("version_parent" in body for body in bodies)
    assert any("version_parent" not in body for body in bodies)


def test_igdb_falls_back_to_prefix_search_when_search_returns_nothing():
    bodies = []

    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        bodies.append(request.content.decode("utf-8"))
        if bodies[-1].lstrip().startswith("search"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=load_fixture("igdb_search.json"))

    provider = IgdbProvider(mock_client(handler), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    games = provider.search("Wiedzmin 2 Zabojcy Krolow PL")
    assert len(games) >= 1
    assert any("name ~" in body for body in bodies)


def test_igdb_rate_limit_surfaces_with_retry_after():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        return httpx.Response(429, text="rate limited", headers={"Retry-After": "1"})

    provider = IgdbProvider(
        mock_client(handler, max_retries=0), FAKE_IGDB_ID, FAKE_IGDB_SECRET
    )
    with pytest.raises(ProviderRateLimited) as info:
        provider.search("anything")
    assert info.value.retry_after == 1.0


def test_igdb_bad_credentials_raise_auth_error():
    """Twitch answers a bad secret with HTTP 400; that must become an auth error."""
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return httpx.Response(
                400, json={"status": 400, "message": "invalid client secret"}
            )
        return httpx.Response(200, json=[])

    provider = IgdbProvider(mock_client(handler), FAKE_IGDB_ID, "wrongsecret0000000000000")
    with pytest.raises(ProviderAuthError) as info:
        provider.search("anything")
    assert "client id" in str(info.value).lower() or "client secret" in str(info.value).lower()
    assert "invalid client secret" in str(info.value)
    assert provider.last_auth_error
    assert "wrongsecret0000000000000" not in str(info.value)
    assert "wrongsecret0000000000000" not in provider.last_auth_error


def test_igdb_auth_error_without_a_json_body_still_explains_itself():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return httpx.Response(401, text="Unauthorized")
        return httpx.Response(200, json=[])

    provider = IgdbProvider(mock_client(handler), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    with pytest.raises(ProviderAuthError) as info:
        provider.search("anything")
    assert "client id" in str(info.value).lower()


def test_igdb_token_response_without_token_raises_data_error():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return httpx.Response(200, json={"expires_in": 3600})
        return httpx.Response(200, json=[])

    provider = IgdbProvider(mock_client(handler), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    with pytest.raises(ProviderDataError):
        provider.search("anything")


def test_igdb_details_by_id():
    bodies = []

    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        bodies.append(request.content.decode("utf-8"))
        return httpx.Response(200, json=[load_fixture("igdb_search.json")[0]])

    provider = IgdbProvider(mock_client(handler), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    game = provider.details(17916)
    assert game is not None
    assert game.provider_id == "17916"
    assert "where id = 17916;" in bodies[-1]


def test_igdb_details_rejects_non_numeric_id():
    provider = IgdbProvider(mock_client(twitch_ok), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    with pytest.raises(ProviderDataError):
        provider.details("12; delete everything")


def test_igdb_search_escapes_user_input():
    bodies = []

    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        bodies.append(request.content.decode("utf-8"))
        return httpx.Response(200, json=[])

    provider = IgdbProvider(mock_client(handler), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    provider.search('Bad"; limit 500; search "injected')
    assert all("limit 500;" not in body for body in bodies)
    assert provider.client.request_count >= 1


def test_escape_apicalypse():
    assert escape_apicalypse('say "hi"') == 'say \\"hi\\"'
    assert escape_apicalypse("back\\slash") == "back\\\\slash"
    assert escape_apicalypse("semi;colon") == "semi colon"
    assert escape_apicalypse("line\nbreak") == "line break"
    assert escape_apicalypse(None) == ""


def test_igdb_empty_query_returns_no_results():
    provider = IgdbProvider(mock_client(twitch_ok), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    assert provider.search("   ") == []


def test_igdb_limit_is_clamped():
    bodies = []

    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        bodies.append(request.content.decode("utf-8"))
        return httpx.Response(200, json=[])

    provider = IgdbProvider(mock_client(handler), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    provider.search("x", limit=5000)
    assert "limit 50;" in bodies[0]


def test_igdb_token_info_never_contains_the_token():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        return httpx.Response(200, json=[])

    provider = IgdbProvider(mock_client(handler), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    provider.authenticate()
    info = provider.token_info()
    assert info["configured"] is True
    assert info["authenticated"] is True
    assert "faketoken0123456789abcdef" not in json.dumps(info)
    assert FAKE_IGDB_SECRET not in json.dumps(info)


def test_igdb_credentials_are_registered_with_the_redactor():
    provider = IgdbProvider(mock_client(twitch_ok), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    assert provider.redactor.text("leaked %s" % FAKE_IGDB_SECRET) .count(FAKE_IGDB_SECRET) == 0


def test_igdb_test_connection_reports_failure_without_secrets():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return httpx.Response(401, text="invalid client")
        return httpx.Response(200, json=[])

    provider = IgdbProvider(mock_client(handler, max_retries=0), FAKE_IGDB_ID, FAKE_IGDB_SECRET)
    result = provider.test_connection()
    assert result["ok"] is False
    assert result["configured"] is True
    assert FAKE_IGDB_SECRET not in json.dumps(result)


def test_igdb_test_connection_when_unconfigured():
    provider = IgdbProvider(mock_client(twitch_ok))
    result = provider.test_connection()
    assert result["ok"] is False
    assert result["configured"] is False
    assert "not set" in result["error"]


# --------------------------------------------------------------------------
# RAWG provider
# --------------------------------------------------------------------------
def test_rawg_search_maps_results():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json=load_fixture("rawg_search.json"))

    provider = RawgProvider(mock_client(handler), FAKE_RAWG_KEY)
    games = provider.search("The Witcher 2")
    assert len(games) == 1
    assert games[0].provider == PROVIDER_RAWG
    assert "search=The+Witcher+2" in urls[0] or "search=The%20Witcher%202" in urls[0]
    assert "exclude_addons=true" in urls[0]


def test_rawg_requires_key():
    provider = RawgProvider(mock_client(lambda r: httpx.Response(200, json={})))
    assert provider.configured is False
    with pytest.raises(ProviderAuthError):
        provider.search("x")


def test_rawg_invalid_key_raises_auth_error():
    def handler(request):
        return httpx.Response(401, json={"detail": "Unauthorized"})

    provider = RawgProvider(mock_client(handler, max_retries=0), FAKE_RAWG_KEY)
    with pytest.raises(ProviderAuthError):
        provider.search("x")


def test_rawg_details_by_slug():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json=load_fixture("rawg_search.json")["results"][0])

    provider = RawgProvider(mock_client(handler), FAKE_RAWG_KEY)
    game = provider.details("the-witcher-2-assassins-of-kings")
    assert game is not None
    assert "/games/the-witcher-2-assassins-of-kings" in urls[0]


def test_rawg_details_rejects_path_traversal():
    provider = RawgProvider(mock_client(lambda r: httpx.Response(200, json={})), FAKE_RAWG_KEY)
    for bad in ("../../etc/passwd", "slug/../secret", "a b", "x?y=1"):
        with pytest.raises(ProviderDataError):
            provider.details(bad)


def test_rawg_empty_query_returns_no_results():
    provider = RawgProvider(mock_client(lambda r: httpx.Response(200, json={})), FAKE_RAWG_KEY)
    assert provider.search("") == []


def test_rawg_page_size_is_clamped():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"results": []})

    provider = RawgProvider(mock_client(handler), FAKE_RAWG_KEY)
    provider.search("x", limit=999)
    assert "page_size=20" in urls[0]


def test_rawg_test_connection_ok():
    def handler(request):
        return httpx.Response(200, json=load_fixture("rawg_search.json"))

    provider = RawgProvider(mock_client(handler), FAKE_RAWG_KEY)
    result = provider.test_connection()
    assert result["ok"] is True
    assert result["sample_results"] == 1
    assert FAKE_RAWG_KEY not in json.dumps(result)


def test_rawg_key_is_masked_in_the_reported_url():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    from cartridge.providers.base import ProviderNetworkError

    provider = RawgProvider(mock_client(handler, max_retries=0), FAKE_RAWG_KEY)
    with pytest.raises(ProviderNetworkError) as info:
        provider.search("x")
    assert FAKE_RAWG_KEY not in str(info.value)


# --------------------------------------------------------------------------
# MetadataService: fallback orchestration
# --------------------------------------------------------------------------
def make_service(handler, igdb=True, rawg=True, **settings):
    service = MetadataService(client=mock_client(handler, **settings))
    service.set_credentials(
        FAKE_IGDB_ID if igdb else "",
        FAKE_IGDB_SECRET if igdb else "",
        FAKE_RAWG_KEY if rawg else "",
    )
    return service


def test_service_uses_igdb_when_available():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        if "rawg.io" in str(request.url):
            raise AssertionError("RAWG must not be called when IGDB answers")
        return httpx.Response(200, json=load_fixture("igdb_search.json"))

    service = make_service(handler)
    outcome = service.search("The Witcher 2")
    assert outcome.ok is True
    assert outcome.provider_used == PROVIDER_IGDB
    assert outcome.fell_back is False
    assert outcome.results[0].match_score >= outcome.results[-1].match_score
    assert "IGDB" in outcome.status_text()


def test_service_falls_back_to_rawg_on_igdb_failure():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return httpx.Response(500, text="twitch down")
        return httpx.Response(200, json=load_fixture("rawg_search.json"))

    service = make_service(handler, max_retries=0)
    outcome = service.search("The Witcher 2")
    assert outcome.ok is True
    assert outcome.provider_used == PROVIDER_RAWG
    assert outcome.fell_back is True
    assert any("IGDB" in error for error in outcome.errors)


def test_service_falls_back_when_igdb_has_no_match():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        if "igdb.com" in str(request.url):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=load_fixture("rawg_search.json"))

    service = make_service(handler)
    outcome = service.search("The Witcher 2")
    assert outcome.provider_used == PROVIDER_RAWG
    assert any("no matches" in error for error in outcome.errors)


def test_service_reports_both_providers_failing_without_raising():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return httpx.Response(503, text="down")
        return httpx.Response(503, text="down")

    service = make_service(handler, max_retries=0)
    outcome = service.search("The Witcher 2")
    assert outcome.ok is False
    assert outcome.results == []
    assert len(outcome.errors) == 2
    assert "No results" in outcome.status_text()


def test_service_with_no_credentials_explains_itself():
    service = make_service(lambda r: httpx.Response(200, json={}), igdb=False, rawg=False)
    outcome = service.search("anything")
    assert outcome.ok is False
    assert any("not configured" in error for error in outcome.errors)
    assert service.is_configured() is False


def test_service_empty_query_is_not_sent_to_a_provider():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, json={})

    service = make_service(handler)
    outcome = service.search("   ")
    assert outcome.ok is False
    assert called["n"] == 0
    assert outcome.errors == ["Enter a title to search for."]


def test_service_dedupes_merged_results():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        if "igdb.com" in str(request.url):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=load_fixture("rawg_search.json"))

    service = make_service(handler)
    outcome = service.search("The Witcher 2")
    titles = [game.title.lower() for game in outcome.results]
    assert len(titles) == len(set(titles))


def test_service_ranks_results_by_relevance_not_provider_order():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        return httpx.Response(200, json=load_fixture("igdb_search.json"))

    service = make_service(handler)
    outcome = service.search("The Witcher 3")
    assert outcome.results[0].title.startswith("The Witcher 3"), (
        "IGDB returned the Witcher 2 first; local ranking must correct the order"
    )


def test_service_details_delegates_to_the_right_provider():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        if "igdb.com" in str(request.url):
            return httpx.Response(200, json=[load_fixture("igdb_search.json")[0]])
        return httpx.Response(200, json=load_fixture("rawg_search.json")["results"][0])

    service = make_service(handler)
    igdb_game = service.details(PROVIDER_IGDB, 17916)
    assert igdb_game is not None and igdb_game.provider == PROVIDER_IGDB
    rawg_game = service.details(PROVIDER_RAWG, "the-witcher-2-assassins-of-kings")
    assert rawg_game is not None and rawg_game.provider == PROVIDER_RAWG
    assert service.details("mystery", 1) is None


def test_service_enrich_fills_gaps_from_the_detail_call():
    search_row = {
        "id": 17916,
        "name": "The Witcher 2",
        "cover": {"url": "//images.igdb.com/igdb/image/upload/t_thumb/a.jpg"},
    }
    detail_row = dict(
        search_row,
        summary="Full description.",
        involved_companies=[{"company": {"name": "CD Projekt RED"}, "developer": True}],
        screenshots=[{"url": "//images.igdb.com/igdb/image/upload/t_thumb/s.jpg"}],
    )

    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        body = request.content.decode("utf-8")
        if "where id =" in body:
            return httpx.Response(200, json=[detail_row])
        return httpx.Response(200, json=[search_row])

    service = make_service(handler)
    game = map_igdb_game(search_row)
    assert game.summary is None
    enriched = service.enrich(game)
    assert enriched.summary == "Full description."
    assert enriched.developer == "CD Projekt RED"
    assert len(enriched.screenshot_urls) == 1


def test_service_enrich_survives_a_failed_detail_call():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        body = request.content.decode("utf-8")
        if "where id =" in body:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=[load_fixture("igdb_search.json")[0]])

    service = make_service(handler, max_retries=0)
    game = map_igdb_game(load_fixture("igdb_search.json")[0])
    game.summary = None
    enriched = service.enrich(game)
    assert enriched is game


def test_service_enrich_skips_the_extra_request_when_complete():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if "id.twitch.tv" in str(request.url):
            return twitch_ok(request)
        return httpx.Response(200, json=[load_fixture("igdb_search.json")[0]])

    service = make_service(handler)
    game = map_igdb_game(load_fixture("igdb_search.json")[0])
    before = calls["n"]
    service.enrich(game)
    assert calls["n"] == before, "no extra request when nothing is missing"


def test_service_status_contains_no_credentials():
    service = make_service(lambda r: httpx.Response(200, json={}))
    status = service.status()
    text = json.dumps(status)
    assert FAKE_IGDB_ID not in text
    assert FAKE_IGDB_SECRET not in text
    assert FAKE_RAWG_KEY not in text
    assert status["configured"] == [PROVIDER_IGDB, PROVIDER_RAWG]
    assert status["http"]["max_retries"] >= 0


def test_search_outcome_status_text_variants():
    empty = SearchOutcome(query="x")
    assert "No results for" in empty.status_text()
    empty.errors = ["IGDB: boom"]
    assert "No results: IGDB: boom" in empty.status_text()
    filled = SearchOutcome(query="x", results=rank("x", [make_game("X")]))
    assert "1 results" in filled.status_text()
    assert filled.best().title == "X"


def test_service_close_is_idempotent():
    service = make_service(lambda r: httpx.Response(200, json={}))
    service.close()
    service.close()
