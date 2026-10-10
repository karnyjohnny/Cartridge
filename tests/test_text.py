"""Tests for cartridge.text: normalization, folding, ranking signals."""

from __future__ import annotations

import pytest

from cartridge import text


def test_strip_accents_polish_and_general():
    assert text.strip_accents("Wiedźmin") == "Wiedzmin"
    assert text.strip_accents("Łódź") == "Lodz"
    assert text.strip_accents("Crusader Königs") == "Crusader Konigs"
    assert text.strip_accents("") == ""
    assert text.strip_accents(None) == ""


def test_normalize_title_lowercases_and_drops_punctuation():
    # normalize_title stays readable: it folds case/accents and drops
    # punctuation and edition markers, but keeps ordinary words ("the", "of").
    # Noise-word removal is search_key's job.
    assert text.normalize_title("The Witcher 2: Assassins of Kings") == (
        "the witcher 2 assassins of kings"
    )
    assert text.normalize_title("  Spaced   Out  ") == "spaced out"
    assert text.search_key("The Witcher 2: Assassins of Kings") == "witcher2assassinskings"


def test_normalize_title_drops_edition_noise():
    assert text.normalize_title("Wiedzmin 2 Enhanced Edition") == "wiedzmin 2"
    assert text.normalize_title("Game GOTY Deluxe") == "game"


def test_search_key_is_alnum_only_and_removes_noise_words():
    assert text.search_key("The Witcher 2: Enhanced Edition") == "witcher2"
    assert text.search_key("Wiedźmin 2") == "wiedzmin2"


def test_search_key_falls_back_when_everything_is_noise():
    # "The Game: Deluxe Edition" -> all words are noise; must stay searchable.
    assert text.search_key("The Deluxe Edition") != ""


def test_tokenize():
    assert text.tokenize("Wiedźmin 2: Zabójcy Królów") == ["wiedzmin", "2", "zabojcy", "krolow"]
    assert text.tokenize("") == []


@pytest.mark.parametrize(
    "haystack,needle",
    [
        ("The Witcher 2: Assassins of Kings", "witcher 2"),
        ("Wiedźmin 2", "wiedzmin 2"),
        ("Wiedźmin 2", "WIEDZMIN2"),
        ("Baldur's Gate II", "baldur gate"),
    ],
)
def test_contains_folded_is_tolerant(haystack, needle):
    assert text.contains_folded(haystack, needle)


def test_contains_folded_rejects_absent_needle():
    assert not text.contains_folded("The Witcher 2", "diablo")
    assert text.contains_folded("anything", "")


def test_similarity_bounds():
    assert text.similarity("Wiedzmin 2", "Wiedzmin 2") == 1.0
    assert text.similarity("Wiedzmin 2", "Wiedzmin 3") > 0.5
    assert text.similarity("Wiedzmin", "Starcraft") < 0.5
    assert text.similarity("", "x") == 0.0


def test_extract_year():
    assert text.extract_year("Some Game (1998)") == 1998
    assert text.extract_year("Release 2011-05-17") == 2011
    assert text.extract_year("no year here") is None
    assert text.extract_year(None) is None
    # Future-dated titles are real (Cyberpunk 2077 was sold under that year for
    # a decade), so the default window accepts them; callers can bound it.
    assert text.extract_year("Cyberpunk 2077") == 2077
    assert text.extract_year("Cyberpunk 2077", future_limit=2030) is None
    assert text.extract_year("1899") is None  # below the floor


def test_decade_label():
    assert text.decade_label(1998) == "1990s"
    assert text.decade_label(2011) == "2010s"
    assert text.decade_label(None) is None


def test_truncate_word_boundary():
    assert text.truncate("short", 20) == "short"
    out = text.truncate("A very long description that must be cut", 24)
    assert len(out) <= 24
    assert out.endswith("...")
    assert text.truncate("", 5) == ""


# --------------------------------------------------------------------------
# Title match scoring - the local re-ranking signal
# --------------------------------------------------------------------------
def test_exact_match_scores_highest():
    assert text.title_match_score("The Witcher 2", "The Witcher 2") == 1.0


def test_punctuation_and_case_do_not_reduce_the_score():
    score = text.title_match_score("witcher 2", "The Witcher 2: Assassins of Kings")
    assert score >= 0.8


def test_folder_style_query_matches_provider_title_via_alias():
    """A Polish folder name only matches an English title through provider aliases.

    This is a deliberate, documented limitation: no local scoring can know that
    "Wiedzmin" is "The Witcher". IGDB/RAWG return alternative_names, and those
    are what make the cross-language case work - which is exactly why the import
    dialog searches with the alias list included.
    """
    folder = "Wiedzmin 2 Zabojcy Krolow Enhanced Edition PL"
    provider = "The Witcher 2: Enhanced Edition"
    aliases = ["Wiedźmin 2: Zabójcy Królów", "Wiedzmin 2"]

    without = text.title_match_score(folder, provider)
    with_alias = text.title_match_score(folder, provider, alternative_names=aliases)

    assert without < 0.5, "cross-language match must not be trusted"
    assert with_alias > without
    assert with_alias > 0.6


def test_scene_style_suffixes_do_not_break_matching():
    assert text.title_match_score(
        "Some.Game.Title.MULTI6.PL", "Some Game Title"
    ) > 0.7


def test_series_siblings_are_not_treated_as_a_match():
    """Fallout 2 vs Fallout 3 are different games and must score low."""
    assert text.title_match_score("Fallout 2", "Fallout 3") < 0.4
    assert text.title_match_score("Fallout 2", "Fallout 2") == 1.0


def test_alternative_names_are_considered():
    without = text.title_match_score("Wiedzmin 2", "The Witcher 2: Enhanced Edition")
    with_alias = text.title_match_score(
        "Wiedzmin 2",
        "The Witcher 2: Enhanced Edition",
        alternative_names=["Wiedźmin 2: Zabójcy Królów"],
    )
    assert with_alias >= without


def test_different_games_score_low():
    assert text.title_match_score("Diablo II", "The Witcher 2") < 0.4


def test_year_agreement_helps_and_mismatch_hurts():
    # Use a pair that is close but not identical - an exact match is already 1.0
    # and the year tie-breaker is (correctly) not allowed to lower it.
    query, candidate = "Some Game", "Some Game Special"
    base = text.title_match_score(query, candidate)
    same_year = text.title_match_score(
        query, candidate, query_year=2001, candidate_year=2001
    )
    wrong_year = text.title_match_score(
        query, candidate, query_year=2001, candidate_year=2015
    )
    assert same_year >= base
    assert wrong_year < same_year


def test_year_cannot_promote_a_wrong_title():
    assert text.title_match_score(
        "Diablo II", "The Witcher 2", query_year=2000, candidate_year=2000
    ) < 0.5
    assert text.title_match_score("Diablo II", "The Witcher 2") < 0.4


def test_empty_inputs_score_zero():
    assert text.title_match_score("", "x") == 0.0
    assert text.title_match_score("x", "") == 0.0
    assert text.title_match_score("", "") == 0.0


def test_score_is_bounded():
    for query, cand in [("a", "a"), ("abc", "abcd"), ("x y", "y x"), ("", "")]:
        score = text.title_match_score(query, cand)
        assert 0.0 <= score <= 1.0


def test_ranked_is_descending_and_stable():
    items = [(0.2, "c"), (0.9, "a"), (0.9, "b"), (0.5, "d")]
    assert text.ranked(items) == ["a", "b", "d", "c"]


def test_human_size_never_fabricates_a_value():
    assert text.human_size(None) == "-"
    assert text.human_size(-5) == "-"
    assert text.human_size("nope") == "-"
    assert text.human_size(0) == "0 B"
    assert text.human_size(1023) == "1023 B"
    assert text.human_size(1024) == "1.0 KB"
    assert text.human_size(5 * 1024 * 1024) == "5.0 MB"
    assert text.human_size(int(2.5 * 1024 ** 3)) == "2.5 GB"
