"""Provider orchestration: search with fallback, details, connectivity status.

Policy implemented here (brief section 10):

* IGDB is primary, RAWG is the fallback. The fallback is consulted **only** when
  the primary raises or returns nothing — querying both every time would double
  the rate-limit cost for no user-visible gain.
* Results are re-ranked locally (:mod:`cartridge.providers.ranking`) and
  cross-provider duplicates are dropped.
* Manual entry is always available: a search that fails completely returns an
  empty list plus the redacted errors, never an exception that traps the dialog.
* Credentials are injected at runtime and registered with the redactor; nothing
  here persists them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from cartridge.core.models import PROVIDER_IGDB, PROVIDER_RAWG, ProviderGame
from cartridge.core.redaction import Redactor, default_redactor
from cartridge.providers import ranking
from cartridge.providers.base import HttpClient, ProviderError, describe_error
from cartridge.providers.mapping import provider_label
from cartridge.providers.igdb import IgdbProvider
from cartridge.providers.ratelimit import LimiterSet
from cartridge.providers.rawg import RawgProvider


@dataclass
class SearchOutcome(object):
    """Everything the import dialog needs to render a search honestly."""

    query: str = ""
    results: List[ProviderGame] = field(default_factory=list)
    provider_used: Optional[str] = None
    providers_tried: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    latency_ms: float = 0.0
    fell_back: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.results)

    def confidence_summary(self) -> Dict[str, int]:
        return ranking.summarize(self.results)

    def best(self) -> Optional[ProviderGame]:
        return ranking.best(self.results)

    def status_text(self) -> str:
        if self.results:
            summary = self.confidence_summary()
            bits = []
            for label in ("exact", "strong", "possible", "weak"):
                if summary.get(label):
                    bits.append("%d %s" % (summary[label], label))
            source = ", ".join(
                sorted({provider_label(r.provider) for r in self.results if r.provider})
            )
            return "%d results (%s) from %s" % (
                len(self.results), ", ".join(bits) or "unranked", source
            )
        if self.errors:
            return "No results: %s" % self.errors[0]
        return "No results for %r" % self.query


class MetadataService(object):
    """Facade over the configured providers."""

    def __init__(
        self,
        client: Optional[HttpClient] = None,
        igdb: Optional[IgdbProvider] = None,
        rawg: Optional[RawgProvider] = None,
        redactor: Optional[Redactor] = None,
        order: Sequence[str] = (PROVIDER_IGDB, PROVIDER_RAWG),
        limiters: Optional[LimiterSet] = None,
    ):
        self.redactor = redactor or default_redactor()
        #: one limiter set for the whole app, so the worker pool cannot exceed a
        #: provider budget by running three downloads in parallel.
        self.limiters = limiters or LimiterSet()
        self.client = client or HttpClient(redactor=self.redactor)
        self.igdb = igdb if igdb is not None else IgdbProvider(
            self.client,
            redactor=self.redactor,
            limiter=self.limiters.igdb,
            auth_limiter=self.limiters.twitch,
        )
        self.rawg = rawg if rawg is not None else RawgProvider(
            self.client, redactor=self.redactor, limiter=self.limiters.rawg
        )
        self.order = list(order)
        self.last_outcome: Optional[SearchOutcome] = None

    # ------------------------------------------------------------------
    def set_credentials(
        self,
        igdb_client_id: str = "",
        igdb_client_secret: str = "",
        rawg_api_key: str = "",
    ) -> None:
        """Apply credentials from Settings/the secret store. Never persists them."""
        self.igdb.set_credentials(igdb_client_id, igdb_client_secret)
        self.rawg.set_credentials(rawg_api_key)

    def provider(self, name: Optional[str]) -> Optional[Any]:
        if name == PROVIDER_IGDB:
            return self.igdb
        if name == PROVIDER_RAWG:
            return self.rawg
        return None

    def configured_providers(self) -> List[str]:
        out = []
        if self.igdb.configured:
            out.append(PROVIDER_IGDB)
        if self.rawg.configured:
            out.append(PROVIDER_RAWG)
        return out

    def is_configured(self) -> bool:
        return bool(self.configured_providers())

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        limit: int = 12,
        providers: Optional[Sequence[str]] = None,
    ) -> SearchOutcome:
        """Search configured providers in order, falling back on failure/empty."""
        text = (query or "").strip()
        outcome = SearchOutcome(query=text)
        started = time.monotonic()
        if not text:
            outcome.errors.append("Enter a title to search for.")
            self.last_outcome = outcome
            return outcome

        wanted = [p for p in (providers or self.order)]
        if not wanted:
            wanted = list(self.order)

        for name in wanted:
            provider = self.provider(name)
            if provider is None:
                continue
            if not provider.configured:
                outcome.errors.append(
                    "%s is not configured (add credentials in Settings)." % provider.label
                )
                continue
            outcome.providers_tried.append(name)
            try:
                results = provider.search(text, limit=limit)
            except ProviderError as exc:
                outcome.errors.append(
                    "%s: %s" % (provider.label, self.redactor.text(describe_error(exc, self.redactor)))
                )
                continue
            except Exception as exc:  # pragma: no cover - defensive
                outcome.errors.append(
                    "%s: %s" % (provider.label, self.redactor.exception(exc))
                )
                continue

            if results:
                outcome.provider_used = name
                outcome.results = results
                break
            # An empty answer from the primary is a legitimate reason to try the
            # fallback: IGDB and RAWG do not have identical catalogues.
            outcome.errors.append("%s returned no matches." % provider.label)

        if outcome.results:
            outcome.results = ranking.dedupe(ranking.rank(text, outcome.results))
            outcome.fell_back = bool(
                outcome.provider_used and outcome.provider_used != wanted[0]
            )
        outcome.latency_ms = round((time.monotonic() - started) * 1000.0, 1)
        self.last_outcome = outcome
        return outcome

    def details(
        self, provider_name: Optional[str], provider_id: Any
    ) -> Optional[ProviderGame]:
        """Fetch full metadata for one result (used before import)."""
        provider = self.provider(provider_name)
        if provider is None or not provider.configured:
            return None
        return provider.details(provider_id)

    def enrich(self, game: ProviderGame) -> ProviderGame:
        """Fill in gaps from the detail endpoint without losing what we have.

        Search endpoints often omit the description; a detail call is worth one
        extra request before an import, but a failure here must not abort it.
        """
        if game.summary and game.developer and game.screenshot_urls:
            return game
        try:
            detail = self.details(game.provider, game.provider_id)
        except ProviderError:
            return game
        except Exception:  # pragma: no cover - defensive
            return game
        if detail is None:
            return game
        for attribute in (
            "summary", "developer", "publisher", "franchise", "age_rating",
            "release_date", "year", "rating", "url",
        ):
            if not getattr(game, attribute, None) and getattr(detail, attribute, None):
                setattr(game, attribute, getattr(detail, attribute))
        for list_attribute in ("genres", "platforms", "alternative_names", "screenshot_urls"):
            if not getattr(game, list_attribute) and getattr(detail, list_attribute):
                setattr(game, list_attribute, list(getattr(detail, list_attribute)))
        if not game.cover_url and detail.cover_url:
            game.cover_url = detail.cover_url
        return game

    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        """Non-secret provider status for Settings/Diagnostics."""
        return {
            "configured": self.configured_providers(),
            "order": list(self.order),
            "igdb": self.igdb.token_info(),
            "rawg": self.rawg.token_info(),
            "http": {
                "connect_timeout": self.client.settings.connect_timeout,
                "read_timeout": self.client.settings.read_timeout,
                "max_retries": self.client.settings.max_retries,
                "requests_made": self.client.request_count,
                "retries": self.client.retry_count,
                "throttled": self.client.throttled_count,
            },
            "rate_limits": self.limiters.summary(),
        }

    def test_connections(self) -> List[Dict[str, Any]]:
        """Explicit "Test connection" action. Performs one small query each."""
        out = []
        for provider in (self.igdb, self.rawg):
            try:
                out.append(provider.test_connection())
            except Exception as exc:  # pragma: no cover - defensive
                out.append(
                    {
                        "provider": provider.name,
                        "ok": False,
                        "error": self.redactor.exception(exc),
                    }
                )
        return out

    def close(self) -> None:
        self.client.close()
