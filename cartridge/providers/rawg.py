"""RAWG provider (fallback).

RAWG is a plain REST API authenticated by an API key. The key is passed as a
query parameter, which means it can end up in a URL string — so the client's
redactor registers it and :meth:`HttpClient.url` masks ``key=`` parameters before
anything is logged, stored or exported.

RAWG ratings are 0-5 while IGDB ratings are 0-100. :mod:`cartridge.providers.mapping`
normalizes RAWG to the 0-100 scale so the catalogue has one rating scale and the
UI does not need to know which provider answered.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from cartridge.core.models import PROVIDER_RAWG, ProviderGame
from cartridge.core.redaction import Redactor, default_redactor
from cartridge.providers.base import (
    HttpClient,
    ProviderAuthError,
    ProviderError,
)
from cartridge.providers.mapping import map_rawg_game, map_rawg_games
from cartridge.providers.ratelimit import RAWG_POLICY, RateLimiter

RAWG_BASE = "https://api.rawg.io/api"
DEFAULT_LIMIT = 12


class RawgProvider(object):
    """RAWG search/detail over an injected :class:`HttpClient`."""

    name = PROVIDER_RAWG
    label = "RAWG"

    def __init__(
        self,
        client: HttpClient,
        api_key: str = "",
        redactor: Optional[Redactor] = None,
        limiter: Optional[RateLimiter] = None,
    ):
        self.client = client
        self.redactor = redactor or default_redactor()
        self.limiter = limiter or client.limiter or RateLimiter(RAWG_POLICY)
        self._last_auth_error = ""
        self.set_credentials(api_key)

    # ------------------------------------------------------------------
    def set_credentials(self, api_key: str = "") -> None:
        self._api_key = (api_key or "").strip()
        self.redactor.register(self._api_key)

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def last_auth_error(self) -> str:
        return self._last_auth_error

    def token_info(self) -> Dict[str, Any]:
        """RAWG has no token; kept for a uniform provider interface."""
        return {
            "configured": self.configured,
            "authenticated": self.configured,
            "seconds_until_expiry": None,
            "last_auth_error": self._last_auth_error,
        }

    def _params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"key": self._api_key}
        if extra:
            params.update(extra)
        return params

    # ------------------------------------------------------------------
    def search(self, query: str, limit: int = DEFAULT_LIMIT) -> List[ProviderGame]:
        text = (query or "").strip()
        if not text:
            return []
        if not self.configured:
            self._last_auth_error = "RAWG API key is not set."
            raise ProviderAuthError(self._last_auth_error)

        limit = max(1, min(int(limit or DEFAULT_LIMIT), 20))
        params = self._params(
            {
                "search": text,
                "page_size": limit,
                "search_precise": "false",
                # exclude_addons keeps DLC rows out of a collection import
                "exclude_addons": "true",
            }
        )
        info = self.client.request(
            "GET",
            RAWG_BASE + "/games",
            params=params,
            operation="rawg_search",
            expect="json",
            limiter=self.limiter,
        )
        return map_rawg_games(info.payload)

    def details(self, provider_id: Any) -> Optional[ProviderGame]:
        """Fetch one game by id or slug. The detail call adds description/people."""
        if not self.configured:
            self._last_auth_error = "RAWG API key is not set."
            raise ProviderAuthError(self._last_auth_error)
        identifier = str(provider_id or "").strip()
        if not identifier:
            return None
        # Only alphanumerics, dashes and underscores are valid in a RAWG
        # id/slug; anything else would be a path-traversal attempt on the URL.
        if not all(ch.isalnum() or ch in "-_" for ch in identifier):
            from cartridge.providers.base import ProviderDataError

            raise ProviderDataError(
                "RAWG id/slug %r contains characters that are not allowed." % identifier
            )
        info = self.client.request(
            "GET",
            "%s/games/%s" % (RAWG_BASE, identifier),
            params=self._params(),
            operation="rawg_details",
            expect="json",
            limiter=self.limiter,
        )
        return map_rawg_game(info.payload)

    def test_connection(self) -> Dict[str, Any]:
        """Connectivity check for Settings. Never returns credential material."""
        started = time.monotonic()
        result: Dict[str, Any] = {
            "provider": self.name,
            "configured": self.configured,
            "ok": False,
            "stage": "credentials",
            "latency_ms": 0.0,
            "error": "",
        }
        if not self.configured:
            result["error"] = self._last_auth_error or "API key is not set"
            return result
        try:
            result["stage"] = "query"
            games = self.search("the witcher", limit=1)
            result["ok"] = True
            result["sample_results"] = len(games)
        except ProviderError as exc:
            result["error"] = self.redactor.text(exc.user_text())
            result["reason"] = exc.reason
        except Exception as exc:  # pragma: no cover - defensive
            result["error"] = self.redactor.exception(exc)
        result["latency_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        result["rate_limit"] = self.limiter.budget()
        return result
