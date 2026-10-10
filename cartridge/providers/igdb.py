"""IGDB provider (primary), including the required Twitch OAuth2 flow.

Authentication
--------------
IGDB is fronted by Twitch. A ``client_credentials`` grant exchanges the user's
IGDB client id + secret for a short-lived bearer token:

    POST https://id.twitch.tv/oauth2/token
         ?client_id=...&client_secret=...&grant_type=client_credentials

The token is held **in memory only**, with its expiry, and re-fetched when it is
near expiry or when a call comes back 401. It is never written to SQLite, to a
log, or to a diagnostics export — it is registered with the redactor the moment
it arrives so any accidental interpolation into an error string is scrubbed
(DECISIONS D-005, brief section 15).

Queries
-------
IGDB v4 takes an Apicalypse query body as ``text/plain``. Values interpolated
into that body are escaped; the only value ever interpolated is a user-supplied
search string or a validated numeric id.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from cartridge.core.models import PROVIDER_IGDB, ProviderGame
from cartridge.core.redaction import Redactor, default_redactor
from cartridge.providers.base import (
    HttpClient,
    ProviderAuthError,
    ProviderDataError,
    ProviderError,
    ProviderResponseError,
)
from cartridge.providers.mapping import map_igdb_game, map_igdb_games
from cartridge.providers.ratelimit import IGDB_POLICY, TWITCH_POLICY, RateLimiter

IGDB_BASE = "https://api.igdb.com/v4"
GAMES_ENDPOINT = "/games"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"

# Fields requested for both search and detail. Kept as one list so a search
# result can be imported without a second round trip.
FIELDS = (
    "name,alternative_names.name,summary,storyline,first_release_date,"
    "release_dates.date,rating,aggregated_rating,total_rating,genres.name,"
    "platforms.name,involved_companies.company.name,involved_companies.developer,"
    "involved_companies.publisher,collection.name,cover.url,screenshots.url,"
    "age_ratings.category,age_ratings.rating,url"
)

DEFAULT_LIMIT = 12
TOKEN_SAFETY_MARGIN = 60.0   # refresh a minute before the real expiry


class IgdbProvider(object):
    """IGDB search/detail over an injected :class:`HttpClient`."""

    name = PROVIDER_IGDB
    label = "IGDB"

    def __init__(
        self,
        client: HttpClient,
        client_id: str = "",
        client_secret: str = "",
        redactor: Optional[Redactor] = None,
        clock: Optional[Any] = None,
        limiter: Optional[RateLimiter] = None,
        auth_limiter: Optional[RateLimiter] = None,
    ):
        self.client = client
        self.redactor = redactor or default_redactor()
        # Separate policies: the IGDB API and the Twitch token endpoint are
        # different services with different limits, and burning the API budget on
        # token refreshes would be silly.
        # Inherit the client's limiter when one is not supplied: it lets a test
        # harness (or a caller with a global policy) configure pacing in one
        # place instead of per provider.
        self.limiter = limiter or client.limiter or RateLimiter(IGDB_POLICY)
        self.auth_limiter = (
            auth_limiter or client.limiter or RateLimiter(TWITCH_POLICY)
        )
        self._clock = clock or time.monotonic
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_type: str = "bearer"
        self._last_auth_error: str = ""
        self.set_credentials(client_id, client_secret)

    # ------------------------------------------------------------------
    def set_credentials(self, client_id: str = "", client_secret: str = "") -> None:
        """Update credentials and drop any cached token."""
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        # Register with the redactor *before* anything can fail, so even an
        # exception raised during authentication cannot leak the secret.
        self.redactor.register(self._client_id)
        self.redactor.register(self._client_secret)
        self.invalidate_token()

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    @property
    def last_auth_error(self) -> str:
        return self._last_auth_error

    def invalidate_token(self) -> None:
        self._access_token = None
        self._token_expires_at = 0.0

    @property
    def has_token(self) -> bool:
        return bool(self._access_token) and self._clock() < self._token_expires_at

    def token_info(self) -> Dict[str, Any]:
        """Non-secret status for Settings/Diagnostics. Never includes the token."""
        return {
            "configured": self.configured,
            "authenticated": self.has_token,
            "seconds_until_expiry": (
                max(0, int(self._token_expires_at - self._clock()))
                if self._access_token else None
            ),
            "last_auth_error": self._last_auth_error,
        }

    # ------------------------------------------------------------------
    def authenticate(self, force: bool = False) -> bool:
        """Obtain (or reuse) a Twitch access token. Raises on bad credentials."""
        if not self.configured:
            self._last_auth_error = "IGDB client id and secret are not set."
            raise ProviderAuthError(self._last_auth_error)
        if self.has_token and not force:
            return True

        try:
            info = self.client.request(
                "POST",
                TWITCH_TOKEN_URL,
                params={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
                operation="igdb_auth",
                expect="json",
                limiter=self.auth_limiter,
            )
        except (ProviderAuthError, ProviderResponseError) as exc:
            # Twitch answers a bad client id/secret with HTTP 400, not 401. Left
            # alone that surfaces as "the provider rejected the request", which
            # does not tell the user what to fix. Translate it.
            self._last_auth_error = _auth_error_text(exc)
            raise ProviderAuthError(self._last_auth_error, detail=exc.detail)
        payload = info.payload
        if not isinstance(payload, dict):
            self._last_auth_error = "Unexpected token response from Twitch."
            raise ProviderDataError(self._last_auth_error)

        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            self._last_auth_error = "Twitch did not return an access token."
            raise ProviderDataError(self._last_auth_error)

        expires_in = payload.get("expires_in")
        try:
            seconds = float(expires_in)
        except (TypeError, ValueError):
            seconds = 3600.0
        seconds = max(60.0, min(seconds, 86400.0))

        self._access_token = token.strip()
        self._token_type = str(payload.get("token_type") or "bearer").strip()
        self._token_expires_at = self._clock() + seconds - TOKEN_SAFETY_MARGIN
        self._last_auth_error = ""
        # Any accidental interpolation of the token into a log line or error
        # message is now scrubbed.
        self.redactor.register(self._access_token)
        return True

    def _headers(self) -> Dict[str, str]:
        return {
            "Client-ID": self._client_id,
            "Authorization": "%s %s" % (self._token_type, self._access_token or ""),
            "Accept": "application/json",
        }

    def _apicalypse(self, body: str) -> Any:
        return self.client.request(
            "POST",
            IGDB_BASE + GAMES_ENDPOINT,
            headers=self._headers(),
            content=body,
            operation="igdb_query",
            expect="json",
            limiter=self.limiter,
        ).payload

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        limit: int = DEFAULT_LIMIT,
        include_versions: bool = False,
    ) -> List[ProviderGame]:
        """Search IGDB by title, degrading the query before giving up.

        Order of attempts, each only if the previous one failed or returned
        nothing:

        1. ``search "q"; where version_parent = null;`` - the precise, default
           query. Excludes regional/version rows so the user is not shown six
           copies of one game.
        2. ``search "q";`` - some IGDB builds reject the version filter, and a
           HTTP 400 from Apicalypse must not surface as "no results".
        3. ``where name ~ "q"*;`` - a prefix match, for folder names that carry
           punctuation or edition suffixes the full-text index does not fold.
        """
        text = (query or "").strip()
        if not text:
            return []
        if not self.configured:
            self._last_auth_error = "IGDB client id and secret are not set."
            raise ProviderAuthError(self._last_auth_error)

        self.authenticate()
        limit = max(1, min(int(limit or DEFAULT_LIMIT), 50))

        attempts = [
            self._search_body(text, limit, include_versions, strict=True),
            self._search_body(text, limit, True, strict=True),
            self._search_body(text, limit, True, strict=False),
        ]
        # De-duplicate identical bodies (happens when include_versions=True).
        unique: List[str] = []
        for body in attempts:
            if body not in unique:
                unique.append(body)

        errors: List[str] = []
        for body in unique:
            try:
                payload = self._run_query(body)
            except ProviderAuthError:
                # Cached token may have been revoked, or the clock may be off.
                self.authenticate(force=True)
                payload = self._run_query(body)
            except (ProviderResponseError, ProviderDataError) as exc:
                errors.append(self.redactor.text(exc.user_text()))
                continue
            games = map_igdb_games(payload)
            if games:
                return games
        if errors:
            # Every attempt failed at the protocol level: report the first reason
            # rather than pretending the provider simply has no such game.
            raise ProviderResponseError(
                "IGDB rejected the search request.",
                detail="; ".join(errors)[:400],
            )
        return []

    def _run_query(self, body: str) -> Any:
        return self._apicalypse(body)

    def _search_body(
        self,
        query: str,
        limit: int,
        include_versions: bool,
        strict: bool = True,
    ) -> str:
        escaped = escape_apicalypse(query)
        if strict:
            head = 'search "%s";' % escaped
        else:
            head = 'where name ~ "%s"*;' % escaped
        parts = [head, "fields %s;" % FIELDS]
        if not include_versions:
            # Apicalypse allows exactly one `where`, so the version filter has to
            # be merged into the loose query rather than appended after it.
            if strict:
                parts.append("where version_parent = null;")
            else:
                head = 'where name ~ "%s"* & version_parent = null;' % escaped
                parts = [head, "fields %s;" % FIELDS]
        parts.append("limit %d;" % limit)
        return "\n".join(parts)

    def details(self, provider_id: Any) -> Optional[ProviderGame]:
        """Fetch one game by its numeric IGDB id."""
        text = str(provider_id or "").strip()
        # IGDB ids are plain integers. Anything else is a mistake or an attempt
        # to inject into the Apicalypse body, so it is rejected rather than
        # "cleaned up" into a different id.
        if not text.isdigit():
            raise ProviderDataError(
                "IGDB ids are numeric; %r is not usable." % (provider_id,)
            )
        digits = text
        self.authenticate()
        body = "fields %s;\nwhere id = %s;\nlimit 1;" % (FIELDS, digits)
        try:
            payload = self._apicalypse(body)
        except ProviderAuthError:
            self.authenticate(force=True)
            payload = self._apicalypse(body)
        games = map_igdb_games(payload)
        return games[0] if games else None

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
            result["error"] = self._last_auth_error or "client id and secret are not set"
            return result
        try:
            self.authenticate(force=True)
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
        result["token"] = self.token_info()
        result["rate_limit"] = self.limiter.budget()
        return result


def _auth_error_text(exc: ProviderError) -> str:
    """Turn a Twitch token-endpoint failure into an actionable sentence."""
    reason = ""
    detail = getattr(exc, "detail", "") or ""
    try:
        import json as _json

        payload = _json.loads(detail)
        if isinstance(payload, dict):
            reason = str(payload.get("message") or payload.get("error") or "").strip()
    except (ValueError, TypeError):
        # The detail is a truncated excerpt; fall back to a plain-text scan.
        for needle in ("invalid client secret", "invalid client", "unauthorized client"):
            if needle in detail.lower():
                reason = needle
                break
    status = getattr(exc, "status_code", 0) or 0
    if isinstance(exc, ProviderAuthError) or status in (400, 401, 403) or reason:
        base = "IGDB rejected the client id or client secret."
        if reason:
            return "%s Twitch said: %s" % (base, reason)
        return base + " Check both values in Settings."
    return "Could not authenticate with IGDB (%s)." % (exc.user_text() or "unknown error")


def escape_apicalypse(value: str) -> str:
    """Escape a user string for inclusion in an Apicalypse query body.

    Only quotes and backslashes are structural in a quoted Apicalypse string, so
    those are escaped; newlines are collapsed because they terminate statements.
    """
    text = str(value or "")
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r", " ").replace("\n", " ").replace(";", " ")
    return text.strip()
