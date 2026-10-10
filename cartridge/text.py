"""Text normalization, search keys and title-match scoring.

Pure functions, no I/O, no Qt: everything here is unit-testable on any platform
and is used by search, filtering, provider ranking and folder-name suggestions.

The brief asks for search that is "case-insensitive and tolerant of punctuation
and diacritics when practical", and for local re-ranking of provider results
because "provider ordering may not match the user's intended title". Both need a
single, predictable normalization definition, which lives here.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Sequence, Tuple

# Punctuation that carries no meaning in a game title match. Kept explicit so a
# future tweak ("keep ':' for subtitles") is a one-line change.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]", re.UNICODE)
_YEAR_RE = re.compile(r"(19|20)\d{2}")

# Words that inflate similarity without identifying the game. They are dropped
# from the *search key* but never from the displayed title.
NOISE_WORDS = frozenset(
    """
    the a an of and or plus edition version game goty deluxe definitive
    complete enhanced remastered remake repack crack fix patch update dlc
    """
    .split()
)

# Common release-region / edition suffixes stripped before comparison.
_EDITION_RE = re.compile(
    r"\b(goty|game of the year|deluxe|definitive|complete|remastered|remake|"
    r"enhanced edition|enhanced|anniversary edition|anniversary|gold edition|"
    r"ultimate edition|complete edition|digital deluxe|platinum|"
    r"director'?s? cut|special edition| collectors edition|collectors edition|"
    r"standard edition|ultimate|gold)\b",
    re.IGNORECASE,
)

# Two-letter language/region codes appended by scene-style folder names
# ("... Enhanced Edition PL"). They are noise for title matching but must not
# eat real two-letter words, hence the anchored, end-of-string position.
_LANG_SUFFIX_RE = re.compile(
    r"(?:^|\s)(?:pl|en|eng|de|ger|fr|fre|es|spa|it|ita|ru|rus|cz|cs|multi\d*)$",
    re.IGNORECASE,
)

# Roman numerals, folded to arabic so "Baldur's Gate II" matches "Baldur's Gate 2".
_ROMAN_RE = re.compile(r"\b([ivxlcdm]{2,7})\b", re.IGNORECASE)
_ROMAN_VALUES = {
    "i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000,
}


# NFKD decomposition does not cover every Latin letter a Polish/Czech collection
# will contain - notably the barred L ("Łódź"), which has no combining form.
_EXTRA_FOLD = {
    "\u0141": "L",  # LATIN CAPITAL LETTER L WITH STROKE
    "\u0142": "l",  # LATIN SMALL LETTER L WITH STROKE
    "\u0110": "D", "\u0111": "d",  # D WITH STROKE
    "\u0126": "H", "\u0127": "h",  # H WITH STROKE
    "\u0130": "I", "\u0131": "i",  # dotted / dotless I
    "\u00d8": "O", "\u00f8": "o",  # O WITH STROKE
    "\u0166": "T", "\u0167": "t",  # T WITH STROKE
    "\u00c6": "AE", "\u00e6": "ae",
    "\u0152": "OE", "\u0153": "oe",
    "\u00df": "ss",  # sharp s
}


def strip_accents(text: str) -> str:
    """``"Wiedźmin"`` -> ``"Wiedzmin"``, ``"Łódź"`` -> ``"Lodz"``.

    NFKD decomposition plus an explicit table for letters that have no
    combining-mark form.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(_EXTRA_FOLD.get(ch, ch) for ch in without_marks)


def roman_to_int(word: str) -> Optional[int]:
    """Convert a roman numeral word to an int, or None if it is not one."""
    if not word:
        return None
    lowered = word.lower()
    if lowered not in _ROMAN_VALUES and not all(ch in _ROMAN_VALUES for ch in lowered):
        return None
    total = 0
    previous = 0
    for ch in reversed(lowered):
        value = _ROMAN_VALUES.get(ch, 0)
        if value < previous:
            total -= value
        else:
            total += value
            previous = value
    return total if total > 0 else None


def normalize_title(text: str) -> str:
    """Human-readable normalized form: lower case, no accents, no punctuation.

    Also folds roman numerals to arabic ("II" -> "2") and drops a trailing
    language code, because folder names carry both and provider titles do not.
    Displayed titles are never modified - only the comparison form is.
    """
    if not text:
        return ""
    lowered = strip_accents(str(text)).lower()
    lowered = _EDITION_RE.sub(" ", lowered)
    lowered = _LANG_SUFFIX_RE.sub(" ", lowered)
    lowered = _ROMAN_RE.sub(lambda m: str(roman_to_int(m.group(1)) or m.group(1)), lowered)
    lowered = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", lowered).strip()


def search_key(text: str) -> str:
    """Aggressive key for tolerant matching: alphanumerics only, noise removed."""
    return _key_from_normalized(normalize_title(text))


def _key_from_normalized(normalized: str) -> str:
    """Build the search key from an already-normalized title."""
    if not normalized:
        return ""
    words = [w for w in normalized.split(" ") if w and w not in NOISE_WORDS]
    if not words:
        # Everything was noise ("The Game: Deluxe Edition") - fall back to the
        # punctuation-free form so the entry stays searchable.
        words = [w for w in normalized.split(" ") if w]
    return _NON_ALNUM_RE.sub("", " ".join(words)).replace(" ", "")


def tokenize(text: str) -> List[str]:
    """Word tokens of the normalized form (used for facet filters)."""
    normalized = normalize_title(text)
    if not normalized:
        return []
    return [t for t in normalized.split(" ") if t]


def contains_folded(haystack: str, needle: str) -> bool:
    """Case/diacritic/punctuation-insensitive "are these words in that title".

    Token-sequence containment rather than blob containment, so that a needle
    written without punctuation ("baldur gate") still matches a title whose
    normalized form has an intervening word ("baldur s gate 2"). Noise words are
    ignored on both sides.
    """
    if not needle:
        return True
    hay = [t for t in _key_tokens(haystack) if t]
    needles = [t for t in _key_tokens(needle) if t]
    if not needles:
        return True
    if not hay:
        return False
    joined_hay = " ".join(hay)
    joined_needle = " ".join(needles)
    if joined_needle in joined_hay:
        return True
    # A needle written as one word ("WIEDZMIN2") has no token boundary, so also
    # compare the concatenated keys.
    if _NON_ALNUM_RE.sub("", joined_needle) in _NON_ALNUM_RE.sub("", joined_hay):
        return True
    # Fallback: every needle token present somewhere in the title.
    return all(token in hay for token in needles)


def _key_tokens(text: str) -> List[str]:
    """Noise-free alphanumeric tokens of ``text``."""
    normalized = normalize_title(text)
    if not normalized:
        return []
    words = [w for w in normalized.split(" ") if w and w not in NOISE_WORDS]
    if not words:
        words = [w for w in normalized.split(" ") if w]
    return [_NON_ALNUM_RE.sub("", w) for w in words]


def similarity(a: str, b: str) -> float:
    """0..1 similarity of two normalized strings."""
    ka = search_key(a)
    kb = search_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    return SequenceMatcher(None, ka, kb).ratio()


def extract_year(text: Optional[str], future_limit: int = 2099) -> Optional[int]:
    """First plausible release year found in a string, else None.

    The accepted window is 1900..``future_limit``. Future-dated titles are real
    (Cyberpunk 2077 was sold under that year for a decade), so the upper bound is
    deliberately generous; callers that want "not in the future" can pass
    ``future_limit=current_year + 5``.
    """
    if not text:
        return None
    for match in _YEAR_RE.finditer(str(text)):
        year = int(match.group(0))
        if 1900 <= year <= future_limit:
            return year
    return None


def decade_label(year: Optional[int]) -> Optional[str]:
    """``1998`` -> ``"1990s"``."""
    if not year:
        return None
    return "%ds" % ((int(year) // 10) * 10)


def truncate(text: str, limit: int, suffix: str = "...") -> str:
    """Trim to ``limit`` characters on a word boundary where possible."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(suffix):
        return text[:limit]
    cut = text[: limit - len(suffix)]
    space = cut.rfind(" ")
    if space > (limit - len(suffix)) // 2:
        cut = cut[:space]
    return cut.rstrip() + suffix


def title_match_score(
    query: str,
    candidate: str,
    alternative_names: Optional[Iterable[str]] = None,
    query_year: Optional[int] = None,
    candidate_year: Optional[int] = None,
) -> float:
    """Score how well ``candidate`` answers the search ``query`` (0.0 - 1.0).

    Combines several signals rather than trusting one, because folder names are
    messy ("Wiedzmin 2 Zabojcy Krolow Enhanced Edition PL") while provider titles
    are clean ("The Witcher 2: Enhanced Edition").

    Signals, in order of weight:
      * exact match on the normalized form
      * exact match on the aggressive search key
      * match against any alternative/alias title
      * token containment (all query tokens present in the candidate)
      * sequence similarity of the search keys
      * year agreement / disagreement as a tie-breaker
    """
    if not query or not candidate:
        return 0.0

    names: List[str] = [candidate]
    if alternative_names:
        names.extend([n for n in alternative_names if n])

    best = 0.0
    q_norm = normalize_title(query)
    q_key = search_key(query)
    q_tokens = {t for t in _key_tokens(query) if t}

    for name in names:
        n_norm = normalize_title(name)
        n_key = search_key(name)
        if not n_norm:
            continue

        score = 0.0
        if q_norm == n_norm:
            score = 1.0
        elif q_key and q_key == n_key:
            score = 0.97
        elif q_key and n_key and (q_key in n_key or n_key in q_key):
            score = 0.85
        else:
            n_tokens = set(_key_tokens(name))
            shared = q_tokens & n_tokens
            informative_shared = {
                t for t in shared if len(t) > 2 and not t.isdigit()
            }
            if q_tokens and q_tokens <= n_tokens:
                # Every query token appears in the candidate. Strong, but the
                # candidate may still be a sibling entry in the same series, so
                # this is a high-but-not-decisive score.
                score = 0.72
            elif informative_shared:
                # Partial overlap is only evidence when the shared token carries
                # meaning. "diablo 2" vs "the witcher 2" shares only the digit.
                coverage = len(informative_shared) / float(len(q_tokens))
                jaccard = len(shared) / float(len(q_tokens | n_tokens))
                score = 0.30 + 0.30 * coverage + 0.15 * jaccard
            elif shared:
                score = 0.12

            # Series-sibling guard: "Fallout 2" vs "Fallout 3" is a *different
            # game*, and sequence similarity alone would rate it highly because
            # the strings differ by one character. When both sides carry numeric
            # tokens and none of them coincide, cap the score well below the
            # auto-confirm threshold so the user is always asked.
            q_nums = {t for t in q_tokens if t.isdigit()}
            n_nums = {t for t in _key_tokens(name) if t.isdigit()}
            sibling_mismatch = bool(q_nums and n_nums and not (q_nums & n_nums))
            if sibling_mismatch:
                score = min(score, 0.32)

            # Sequence similarity alone is not trusted as a match: it would rank
            # "Diablo II" against "The Witcher 2" at 0.53 because of shared
            # letters. It only lifts a score that token evidence already
            # supports, and it can never carry an unrelated pair past the
            # confirmation threshold on its own.
            sim = similarity(query, name)
            if sibling_mismatch:
                score = max(score, round(0.25 * sim, 4))
                score = min(score, 0.32)
            elif score >= 0.30:
                score = max(score, round(sim, 4))
            else:
                score = max(score, round(0.35 * sim, 4))

        best = max(best, score)

    # Year tie-breaker: agreement nudges up, a clear mismatch pulls down. It must
    # never be able to promote a wrong title on its own.
    if query_year and candidate_year:
        if query_year == candidate_year:
            best = min(1.0, best + 0.05)
        elif abs(query_year - candidate_year) > 2 and best < 0.9:
            best = max(0.0, best - 0.18)
    return round(best, 4)


def ranked(
    scored: Sequence[Tuple[float, object]], stable_key=None
) -> List[object]:
    """Sort ``(score, item)`` pairs by descending score, keeping input order."""
    indexed = list(scored)
    if stable_key is None:
        return [item for _, item in sorted(indexed, key=lambda pair: -pair[0])]
    return [
        item
        for _, item in sorted(indexed, key=lambda pair: (-pair[0], stable_key(pair[1])))
    ]


def human_size(num_bytes: Optional[int]) -> str:
    """Format a *measured* byte count. Returns "-" when never measured.

    Deliberately never guesses: an unmeasured folder shows "-", not an estimate
    (brief section 9).
    """
    if num_bytes is None:
        return "-"
    try:
        value = float(num_bytes)
    except (TypeError, ValueError):
        return "-"
    if value < 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d B" % int(value)
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f TB" % value
