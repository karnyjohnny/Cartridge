"""Defensive mapping from provider JSON to internal models.

Every value coming from IGDB or RAWG is treated as untrusted (brief section 1):
wrong types, missing keys, ``null`` where a list belongs, absurdly long strings
and unexpected shapes must degrade to "field unknown" rather than raise, and
certainly never reach the database as something that breaks a constraint.

Only two things are mandatory for a usable search result: a provider id and a
title. Everything else is optional. If a payload lacks even those, mapping
returns ``None`` and the caller skips the entry — a provider returning junk must
not break the whole result list.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from cartridge.core.models import (
    PROVIDER_IGDB,
    PROVIDER_RAWG,
    ProviderGame,
)
from cartridge.text import extract_year

MAX_SUMMARY_CHARS = 4000
MAX_TITLE_CHARS = 300
MAX_NAME_LIST = 40
MAX_SCREENSHOTS = 12

# IGDB image size tokens. "t_cover_big" is 264x374 - plenty for a card cover and
# small enough for a 2 GB machine; full-size art is never fetched for a grid.
IGDB_IMAGE_SIZES = {
    "cover": "t_cover_big",
    "cover_small": "t_cover_small",
    "screenshot": "t_screenshot_big",
    "screenshot_hd": "t_screenshot_huge",
    "background": "t_screenshot_huge",
    "logo": "t_logo_med",
    "thumb": "t_thumb",
}

# IGDB age-rating enums (category 1 = ESRB, 2 = PEGI).
IGDB_ESRB = {
    1: "ESRB: RP", 2: "ESRB: EC", 3: "ESRB: E", 4: "ESRB: E10+",
    5: "ESRB: T", 6: "ESRB: M", 7: "ESRB: AO",
}
IGDB_PEGI = {
    1: "PEGI 3", 2: "PEGI 7", 3: "PEGI 12", 4: "PEGI 16",
    5: "PEGI 18", 6: "PEGI 3", 7: "PEGI 7", 8: "PEGI 12",
}

_WS_RE = re.compile(r"\s+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------
# scalar coercion helpers
# --------------------------------------------------------------------------
def as_text(value: Any, limit: Optional[int] = None) -> Optional[str]:
    """Best-effort string, or None. Strips HTML tags and collapses whitespace."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value
    elif isinstance(value, bytes):
        # Never decode provider bytes as text blindly; ignore instead.
        return None
    elif isinstance(value, dict):
        text = str(value.get("name") or value.get("title") or "")
    else:
        return None
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return None
    if limit and len(text) > limit:
        text = text[:limit].rstrip()
    return text


def as_int(value: Any, minimum: Optional[int] = None, maximum: Optional[int] = None) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def as_float(value: Any, minimum: Optional[float] = None, maximum: Optional[float] = None) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return round(number, 2)


def as_list(value: Any) -> List[Any]:
    """Coerce to a list.

    A ``{"results": [...]}`` envelope is unwrapped and a single dict becomes a
    one-item list, but a bare scalar (a string where an array belongs) becomes
    an empty list: inventing a one-element list from a stray string would put
    junk into genres and screenshots.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        # {"results": [...]} is a common envelope
        for key in ("results", "items", "data"):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    return []


def name_list(items: Any, key: str = "name", limit: int = MAX_NAME_LIST) -> List[str]:
    """Extract a de-duplicated list of names from a list of dicts/strings."""
    out: List[str] = []
    seen = set()
    for item in as_list(items)[: limit * 2]:
        if isinstance(item, str):
            name = as_text(item, MAX_TITLE_CHARS)
        elif isinstance(item, dict):
            inner = item.get(key)
            if isinstance(inner, dict):
                inner = inner.get("name")
            name = as_text(inner, MAX_TITLE_CHARS)
            if name is None:
                # involved_companies uses {"company": {"name": ...}}
                company = item.get("company")
                if isinstance(company, dict):
                    name = as_text(company.get("name"), MAX_TITLE_CHARS)
        else:
            name = None
        if not name:
            continue
        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(name)
        if len(out) >= limit:
            break
    return out


def year_from_epoch(seconds: Any) -> Optional[int]:
    """IGDB stores release timestamps as Unix epoch seconds."""
    value = as_int(seconds, minimum=0, maximum=4102444800)  # <= 2100-01-01
    if value is None:
        return None
    import datetime

    try:
        return datetime.datetime.utcfromtimestamp(value).year
    except (OverflowError, OSError, ValueError):
        return None


def date_from_epoch(seconds: Any) -> Optional[str]:
    value = as_int(seconds, minimum=0, maximum=4102444800)
    if value is None:
        return None
    import datetime

    try:
        return datetime.datetime.utcfromtimestamp(value).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def normalize_http_url(value: Any) -> Optional[str]:
    """Accept protocol-relative and http(s) URLs only."""
    text = as_text(value)
    if not text:
        return None
    if text.startswith("//"):
        text = "https:" + text
    if not text.lower().startswith(("http://", "https://")):
        return None
    if len(text) > 2000:
        return None
    return text


def igdb_image_url(value: Any, kind: str = "cover") -> Optional[str]:
    """Rewrite an IGDB image URL to the requested size token."""
    url = normalize_http_url(value)
    if not url:
        return None
    token = IGDB_IMAGE_SIZES.get(kind, IGDB_IMAGE_SIZES["thumb"])
    return re.sub(r"/t_[a-z0-9_]+/", "/%s/" % token, url, count=1)


def first_image(items: Any, kind: str = "cover", provider: str = PROVIDER_IGDB) -> Optional[str]:
    for item in as_list(items):
        if isinstance(item, dict):
            url = item.get("url") or item.get("image")
        else:
            url = item
        if provider == PROVIDER_IGDB:
            converted = igdb_image_url(url, kind)
        else:
            converted = normalize_http_url(url)
        if converted:
            return converted
    return None


def image_urls(items: Any, kind: str = "screenshot", provider: str = PROVIDER_IGDB,
               limit: int = MAX_SCREENSHOTS) -> List[str]:
    out: List[str] = []
    for item in as_list(items):
        if isinstance(item, dict):
            url = item.get("url") or item.get("image")
        else:
            url = item
        converted = (
            igdb_image_url(url, kind) if provider == PROVIDER_IGDB
            else normalize_http_url(url)
        )
        if converted and converted not in out:
            out.append(converted)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# IGDB
# --------------------------------------------------------------------------
def igdb_age_rating(payload: Any) -> Optional[str]:
    for entry in as_list(payload):
        if not isinstance(entry, dict):
            continue
        category = as_int(entry.get("category"))
        rating = as_int(entry.get("rating"))
        if rating is None:
            continue
        if category == 1 and rating in IGDB_ESRB:
            return IGDB_ESRB[rating]
        if category == 2 and rating in IGDB_PEGI:
            return IGDB_PEGI[rating]
    return None


def igdb_companies(payload: Any) -> Dict[str, Optional[str]]:
    """First developer and first publisher from ``involved_companies``."""
    developer = None
    publisher = None
    for entry in as_list(payload):
        if not isinstance(entry, dict):
            continue
        company = entry.get("company")
        name = as_text(company.get("name") if isinstance(company, dict) else None, MAX_TITLE_CHARS)
        if not name:
            continue
        if developer is None and entry.get("developer"):
            developer = name
        if publisher is None and entry.get("publisher"):
            publisher = name
    return {"developer": developer, "publisher": publisher}


def igdb_release(payload: Dict[str, Any]) -> Dict[str, Optional[Any]]:
    """Pick the most useful release date: first_release_date, else the earliest."""
    date = date_from_epoch(payload.get("first_release_date"))
    year = year_from_epoch(payload.get("first_release_date"))
    if date is None:
        candidates = []
        for entry in as_list(payload.get("release_dates")):
            if isinstance(entry, dict):
                stamp = as_int(entry.get("date"), minimum=0)
                if stamp:
                    candidates.append(stamp)
        if candidates:
            earliest = min(candidates)
            date = date_from_epoch(earliest)
            year = year_from_epoch(earliest)
    return {"release_date": date, "release_year": year}


def map_igdb_game(payload: Any, provider: str = PROVIDER_IGDB) -> Optional[ProviderGame]:
    """Map one IGDB game object. Returns None when it is unusable."""
    if not isinstance(payload, dict):
        return None
    provider_id = as_text(payload.get("id"))
    title = as_text(payload.get("name"), MAX_TITLE_CHARS)
    if not provider_id or not title:
        return None

    companies = igdb_companies(payload.get("involved_companies"))
    release = igdb_release(payload)
    rating = as_float(payload.get("rating"), 0.0, 100.0)
    if rating is None:
        rating = as_float(payload.get("aggregated_rating"), 0.0, 100.0)
    if rating is None:
        rating = as_float(payload.get("total_rating"), 0.0, 100.0)

    collection = payload.get("collection")
    franchise = as_text(
        collection.get("name") if isinstance(collection, dict) else None, MAX_TITLE_CHARS
    )
    if franchise is None:
        franchise = as_text(payload.get("franchise") if not isinstance(
            payload.get("franchise"), dict) else payload["franchise"].get("name"),
            MAX_TITLE_CHARS)

    summary = as_text(payload.get("summary") or payload.get("storyline"), MAX_SUMMARY_CHARS)

    return ProviderGame(
        provider=provider,
        provider_id=provider_id,
        title=title,
        year=release["release_year"],
        release_date=release["release_date"],
        summary=summary,
        cover_url=first_image(payload.get("cover"), "cover"),
        screenshot_urls=image_urls(payload.get("screenshots"), "screenshot"),
        background_url=first_image(payload.get("screenshots"), "background"),
        genres=name_list(payload.get("genres")),
        platforms=name_list(payload.get("platforms")),
        developer=companies["developer"],
        publisher=companies["publisher"],
        franchise=franchise,
        alternative_names=name_list(payload.get("alternative_names")),
        rating=rating,
        age_rating=igdb_age_rating(payload.get("age_ratings")),
        url=normalize_http_url(payload.get("url")),
    )


def map_igdb_games(payload: Any) -> List[ProviderGame]:
    out: List[ProviderGame] = []
    for item in as_list(payload):
        mapped = map_igdb_game(item)
        if mapped is not None:
            out.append(mapped)
    return out


# --------------------------------------------------------------------------
# RAWG
# --------------------------------------------------------------------------
def rawg_rating(value: Any) -> Optional[float]:
    """RAWG rates 0-5; the catalogue stores 0-100 to match IGDB."""
    score = as_float(value, 0.0, 5.0)
    if score is None:
        return None
    return round(score * 20.0, 2)


def rawg_platform_names(payload: Any) -> List[str]:
    out: List[str] = []
    seen = set()
    for entry in as_list(payload):
        if not isinstance(entry, dict):
            continue
        platform = entry.get("platform")
        name = as_text(
            platform.get("name") if isinstance(platform, dict) else entry.get("name"),
            MAX_TITLE_CHARS,
        )
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def map_rawg_game(payload: Any, provider: str = PROVIDER_RAWG) -> Optional[ProviderGame]:
    if not isinstance(payload, dict):
        return None
    provider_id = as_text(payload.get("id")) or as_text(payload.get("slug"))
    title = as_text(payload.get("name"), MAX_TITLE_CHARS)
    if not provider_id or not title:
        return None

    released = as_text(payload.get("released") or payload.get("released_raw"), 40)
    year = extract_year(released) if released else None

    screenshots = image_urls(
        payload.get("short_screenshots") or payload.get("screenshots"),
        "screenshot",
        provider=PROVIDER_RAWG,
    )
    if not screenshots:
        nested = payload.get("screenshots")
        if isinstance(nested, dict):
            screenshots = image_urls(nested.get("results"), "screenshot", provider=PROVIDER_RAWG)

    esrb = payload.get("esrb_rating")
    age = as_text(esrb.get("name") if isinstance(esrb, dict) else esrb, 40)
    if age:
        age = "ESRB: %s" % age if not age.upper().startswith("ESRB") else age

    pegi = payload.get("pegi")
    if not age and isinstance(pegi, dict):
        rating_text = as_text(pegi.get("rating"), 20)
        if rating_text:
            age = "PEGI %s" % rating_text

    return ProviderGame(
        provider=provider,
        provider_id=provider_id,
        title=title,
        year=year,
        release_date=released if (released and re.match(r"^\d{4}-\d{2}-\d{2}", released)) else None,
        summary=as_text(payload.get("description_raw") or payload.get("description"), MAX_SUMMARY_CHARS),
        cover_url=normalize_http_url(payload.get("background_image")),
        screenshot_urls=screenshots,
        background_url=normalize_http_url(
            payload.get("background_image_additional") or payload.get("background_image")
        ),
        genres=name_list(payload.get("genres")),
        platforms=rawg_platform_names(payload.get("platforms") or payload.get("parent_platforms")),
        developer=first_name(payload.get("developers")),
        publisher=first_name(payload.get("publishers")),
        franchise=_rawg_series(payload),
        alternative_names=name_list(payload.get("alternative_names")),
        rating=rawg_rating(payload.get("rating")),
        age_rating=age,
        url=normalize_http_url(payload.get("website")) or _rawg_site_url(payload),
    )


def first_name(items: Any) -> Optional[str]:
    """First name out of a provider's people list, or None."""
    names = name_list(items, limit=1)
    return names[0] if names else None


def _rawg_series(payload: Dict[str, Any]) -> Optional[str]:
    series = payload.get("series")
    if isinstance(series, list) and series:
        first = series[0]
        if isinstance(first, dict):
            return as_text(first.get("name"), MAX_TITLE_CHARS)
        return as_text(first, MAX_TITLE_CHARS)
    if isinstance(series, dict):
        return as_text(series.get("name"), MAX_TITLE_CHARS)
    return None


def _rawg_site_url(payload: Dict[str, Any]) -> Optional[str]:
    slug = as_text(payload.get("slug"))
    if slug:
        return "https://rawg.io/games/%s" % slug
    return None


def map_rawg_games(payload: Any) -> List[ProviderGame]:
    """RAWG list responses are ``{"count": n, "results": [...]}``."""
    if isinstance(payload, dict):
        items = payload.get("results")
        if isinstance(items, list):
            payload = items
    out: List[ProviderGame] = []
    for item in as_list(payload):
        mapped = map_rawg_game(item)
        if mapped is not None:
            out.append(mapped)
    return out


def safe_result_count(payload: Any) -> Optional[int]:
    if isinstance(payload, dict):
        return as_int(payload.get("count"))
    return None


def provider_label(provider: Optional[str]) -> str:
    """Human attribution shown next to every result (brief section 6.5)."""
    return {
        PROVIDER_IGDB: "IGDB",
        PROVIDER_RAWG: "RAWG",
        "manual": "Manual entry",
    }.get(provider or "", "Unknown provider")


def sanitize_for_fixture(payload: Any) -> Any:
    """Strip anything credential-like before a payload is saved as a test fixture."""
    from cartridge.core.redaction import default_redactor

    redactor = default_redactor()

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ("***REDACTED***" if str(key).lower() in
                      ("authorization", "client_id", "client_secret", "key", "api_key",
                       "access_token", "token") else walk(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, str):
            return redactor.text(value)
        return value

    return walk(payload)


def unused(_items: Iterable[Any]) -> None:  # pragma: no cover - typing aid
    return None
