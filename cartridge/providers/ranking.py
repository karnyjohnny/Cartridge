"""Local re-ranking and match-confidence labelling of provider results.

Brief section 10 is explicit: provider ordering may not match the user's intended
title, so ranking happens locally; and a weak match must never be
auto-associated. This module turns a list of :class:`ProviderGame` into an ordered
list plus an honest confidence label for each one.

Thresholds are policy, kept here as named constants so the UI, the import dialog
and the tests all agree on what "confident" means.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from cartridge.core.models import PROVIDER_IGDB, PROVIDER_RAWG, ProviderGame
from cartridge.text import extract_year, title_match_score

# Policy thresholds.
AUTO_CONFIRM = 0.97   # essentially the same title; still needs a user click
STRONG = 0.80         # very likely correct; preselect it
POSSIBLE = 0.45       # plausible; show it, do not favour it
WEAK = 0.0            # anything below POSSIBLE is "probably not this game"

# Provider preference when scores tie: IGDB is the primary source of truth.
PROVIDER_PRIORITY = {PROVIDER_IGDB: 0, PROVIDER_RAWG: 1}


@dataclass
class Confidence(object):
    """A score plus the words a human can act on."""

    label: str
    score: float
    explanation: str

    @property
    def is_auto_confirmable(self) -> bool:
        return self.score >= AUTO_CONFIRM

    @property
    def needs_confirmation(self) -> bool:
        return True  # always: the brief forbids silent association


def confidence_label(score: float) -> str:
    if score >= AUTO_CONFIRM:
        return "exact"
    if score >= STRONG:
        return "strong"
    if score >= POSSIBLE:
        return "possible"
    return "weak"


def explain(score: float, game: ProviderGame, query: str) -> str:
    """Short human reason for the score, shown as a tooltip in the dialog."""
    label = confidence_label(score)
    query_year = extract_year(query)
    bits = []
    if label == "exact":
        bits.append("title matches your query")
    elif label == "strong":
        bits.append("title closely matches your query")
    elif label == "possible":
        bits.append("partially matches your query")
    else:
        bits.append("does not match your query well")

    if game.alternative_names:
        # An alias match is the common case for a localized folder name.
        from cartridge.text import search_key

        query_key = search_key(query)
        for alias in game.alternative_names:
            if query_key and query_key == search_key(alias):
                bits.append("matched an alternative title")
                break

    if query_year and game.year:
        if query_year == game.year:
            bits.append("release year agrees (%d)" % game.year)
        else:
            bits.append("release year differs (query %d vs %d)" % (query_year, game.year))
    elif game.year:
        bits.append("released %d" % game.year)

    if game.platforms:
        bits.append(", ".join(game.platforms[:2]))
    return "; ".join(bits)


def score_game(game: ProviderGame, query: str, query_year: Optional[int] = None) -> float:
    """Compute and store ``match_score`` for one provider result."""
    if query_year is None:
        query_year = extract_year(query)
    score = title_match_score(
        query,
        game.title,
        alternative_names=game.alternative_names,
        query_year=query_year,
        candidate_year=game.year,
    )
    game.match_score = round(float(score), 4)
    return game.match_score


def rank(
    query: str,
    games: Sequence[ProviderGame],
    query_year: Optional[int] = None,
) -> List[ProviderGame]:
    """Score and sort results. Returns a new list; inputs are not reordered."""
    if query_year is None:
        query_year = extract_year(query)
    scored = list(games)
    for game in scored:
        score_game(game, query, query_year)

    def sort_key(game: ProviderGame):
        return (
            -game.match_score,
            PROVIDER_PRIORITY.get(game.provider, 9),
            -(game.rating or 0.0),
            game.title.lower(),
        )

    scored.sort(key=sort_key)
    return scored


def best(games: Sequence[ProviderGame]) -> Optional[ProviderGame]:
    return games[0] if games else None


def is_confident(game: Optional[ProviderGame]) -> bool:
    """True only for a near-exact title match. Still never auto-imported."""
    return bool(game) and game.match_score >= AUTO_CONFIRM


def dedupe(games: Sequence[ProviderGame]) -> List[ProviderGame]:
    """Drop cross-provider duplicates of the same game.

    Two results are the same game when their normalized titles match and their
    release years are equal or one of them is unknown. The higher-ranked entry
    wins, which keeps IGDB ahead of RAWG when both answered.
    """
    from cartridge.text import search_key

    seen = {}
    out: List[ProviderGame] = []
    for game in games:
        key = search_key(game.title)
        if not key:
            out.append(game)
            continue
        previous = seen.get(key)
        if previous is None:
            seen[key] = game
            out.append(game)
            continue
        previous_year = previous.year
        if game.year and previous_year and game.year != previous_year:
            # Different year, same title: a remake or a different entry. Keep both.
            out.append(game)
            seen[key + ":%d" % game.year] = game
        # otherwise it is a duplicate of an already-kept, better-ranked result
    return out


def summarize(games: Sequence[ProviderGame]) -> dict:
    """Counts per confidence band, for the import dialog's status line."""
    counts = {"exact": 0, "strong": 0, "possible": 0, "weak": 0}
    for game in games:
        counts[confidence_label(game.match_score)] += 1
    return counts
