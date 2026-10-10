"""Rate limiting: keep Cartridge a polite API citizen.

The brief asks for bounded concurrency, respect for provider rate limits and
finite retries. This module is where "respect" is implemented, and it exists as
a separate piece so the policy can be reviewed and tuned without touching
transport code.

Three mechanisms, applied together:

1. **Token bucket** — smooths bursts. Capacity ``burst`` tokens, refilled at
   ``refill_rate`` per second. A request costs one token; if none is available
   the caller waits (never longer than ``max_wait``) or is refused.
2. **Sliding windows** — hard caps per minute and per day, well below the
   published provider limits, so a bug that loops cannot burn a monthly quota in
   an afternoon.
3. **Retry-After / server hints** — when a provider says "come back in N
   seconds" (or publishes ``X-RateLimit-*`` headers), the limiter cools down for
   exactly that long instead of guessing.

The limiter never sleeps longer than ``max_wait`` and never blocks indefinitely:
when a wait would exceed it, :meth:`RateLimiter.acquire` returns a refusal with a
``reason`` the UI can show ("IGDB hourly budget exhausted — try again in 42 min").
Everything is injectable (``clock``, ``sleep``) so tests run in microseconds
instead of minutes.

Published limits these presets stay well under (as of 2026-10):

* IGDB — 4 requests/second, 40,000 requests/day.
* RAWG — 20,000 requests/month on the free key (~650/day).
* Image CDNs — no published limit, but hammering them is how an IP gets banned.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional


@dataclass
class RateLimitPolicy(object):
    """Declarative limits for one provider or host."""

    name: str = "default"
    burst: int = 3                     # token bucket capacity
    refill_rate: float = 3.0           # tokens per second
    min_interval: float = 0.3          # hard floor between two requests
    max_per_minute: int = 60
    max_per_day: int = 2000
    max_wait: float = 12.0             # never sleep longer than this
    respect_retry_after: bool = True
    cooldown_cap: float = 120.0        # a server-suggested wait longer than this
                                       # becomes a refusal, not a sleep

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "burst": self.burst,
            "refill_rate": self.refill_rate,
            "min_interval": self.min_interval,
            "max_per_minute": self.max_per_minute,
            "max_per_day": self.max_per_day,
            "max_wait": self.max_wait,
        }


# Presets: deliberately more conservative than the published limits. Running out
# of quota mid-import is far more annoying than an import that takes two seconds
# longer.
IGDB_POLICY = RateLimitPolicy(
    name="igdb",
    burst=3,
    refill_rate=3.0,          # <= IGDB's 4 req/s
    min_interval=0.30,
    max_per_minute=90,        # published: 240/min
    max_per_day=1500,         # published: 40,000/day
)

RAWG_POLICY = RateLimitPolicy(
    name="rawg",
    burst=2,
    refill_rate=1.5,
    min_interval=0.60,
    max_per_minute=30,
    max_per_day=250,          # ~7,500/month, well under the 20,000 free quota
)

IMAGE_POLICY = RateLimitPolicy(
    name="images",
    burst=4,
    refill_rate=4.0,
    min_interval=0.15,
    max_per_minute=180,
    max_per_day=1200,
)

# Test policy: effectively unlimited, and always paired with a no-op sleep so a
# suite does not spend wall-clock time proving that throttling works.
TEST_POLICY = RateLimitPolicy(
    name="test",
    burst=10 ** 6,
    refill_rate=float(10 ** 6),
    min_interval=0.0,
    max_per_minute=10 ** 9,
    max_per_day=10 ** 9,
    max_wait=60.0,
)

TWITCH_POLICY = RateLimitPolicy(
    name="twitch_auth",
    burst=1,
    refill_rate=0.2,          # one token request per 5 s at most
    min_interval=2.0,
    max_per_minute=6,
    max_per_day=200,          # tokens last an hour; 200/day is already absurd
)


@dataclass
class AcquireResult(object):
    """Outcome of an :meth:`RateLimiter.acquire` call."""

    allowed: bool
    waited: float = 0.0
    reason: str = ""
    retry_after: Optional[float] = None

    def user_text(self) -> str:
        if self.allowed:
            return ""
        return self.reason


class RateLimiter(object):
    """Token bucket + sliding windows + server cooldown, thread-safe."""

    def __init__(
        self,
        policy: Optional[RateLimitPolicy] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Optional[Callable[[], float]] = None,
    ):
        self.policy = policy or RateLimitPolicy()
        self._clock = clock
        self._sleep = sleep
        # A separate wall clock keeps day boundaries meaningful while tests can
        # still control time.
        self._wall_clock = wall_clock or time.time
        self._lock = threading.RLock()

        self._tokens = float(self.policy.burst)
        self._last_refill = clock()
        self._last_request: Optional[float] = None
        self._minute_hits: Deque[float] = deque()
        self._day_key: str = ""
        self._day_hits: int = 0
        self._cooldown_until: float = 0.0

        # counters for Diagnostics
        self.acquired = 0
        self.refused = 0
        self.total_wait = 0.0
        self.cooldowns = 0

    # ------------------------------------------------------------------
    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(
            float(self.policy.burst), self._tokens + elapsed * self.policy.refill_rate
        )
        self._last_refill = now

    def _day_stamp(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self._wall_clock()))

    def _prune_minute(self, now: float) -> None:
        cutoff = now - 60.0
        while self._minute_hits and self._minute_hits[0] < cutoff:
            self._minute_hits.popleft()

    def _roll_day(self) -> None:
        stamp = self._day_stamp()
        if stamp != self._day_key:
            self._day_key = stamp
            self._day_hits = 0

    # ------------------------------------------------------------------
    def acquire(self, cost: float = 1.0, block: bool = True) -> AcquireResult:
        """Reserve permission for one request, waiting if that is cheap.

        Returns an :class:`AcquireResult`; when ``allowed`` is False the caller
        must not send the request and should show ``reason`` to the user.
        """
        policy = self.policy
        with self._lock:
            self._refill()
            self._prune_minute(self._clock())
            self._roll_day()

            now = self._clock()

            # 1) explicit server cooldown (Retry-After / X-RateLimit-Reset)
            if self._cooldown_until > now:
                remaining = self._cooldown_until - now
                if not block or remaining > policy.max_wait:
                    self.refused += 1
                    return AcquireResult(
                        allowed=False,
                        reason=(
                            "%s asked us to wait; %s left in cooldown."
                            % (policy.name, _human_seconds(remaining))
                        ),
                        retry_after=round(remaining, 1),
                    )
                wait = remaining
            else:
                wait = 0.0

            # 2) hard window caps are never waited out
            if self._day_hits >= policy.max_per_day:
                self.refused += 1
                return AcquireResult(
                    allowed=False,
                    reason=(
                        "%s daily budget of %d requests is used up. Cartridge "
                        "stops here rather than risk your quota; it resets at "
                        "midnight UTC." % (policy.name, policy.max_per_day)
                    ),
                )
            if len(self._minute_hits) >= policy.max_per_minute:
                oldest = self._minute_hits[0]
                retry_after = max(0.0, 60.0 - (now - oldest))
                if not block or retry_after > policy.max_wait:
                    self.refused += 1
                    return AcquireResult(
                        allowed=False,
                        reason=(
                            "%s per-minute limit reached (%d). Retry in %s."
                            % (policy.name, policy.max_per_minute,
                               _human_seconds(retry_after))
                        ),
                        retry_after=round(retry_after, 1),
                    )
                wait = max(wait, retry_after)

            # 3) token bucket + minimum interval
            needed = max(cost, 1.0)
            if self._tokens < needed:
                deficit = needed - self._tokens
                refill_wait = deficit / max(0.001, policy.refill_rate)
                if not block or refill_wait > policy.max_wait:
                    self.refused += 1
                    return AcquireResult(
                        allowed=False,
                        reason=(
                            "%s is being asked for requests faster than its "
                            "policy allows (wait would be %s)."
                            % (policy.name, _human_seconds(refill_wait))
                        ),
                        retry_after=round(refill_wait, 1),
                    )
                wait = max(wait, refill_wait)

            if self._last_request is not None:
                gap = policy.min_interval - (now - self._last_request)
                if gap > 0:
                    if not block or gap > policy.max_wait:
                        self.refused += 1
                        return AcquireResult(
                            allowed=False,
                            reason=(
                                "%s requests must be at least %.2f s apart."
                                % (policy.name, policy.min_interval)
                            ),
                            retry_after=round(gap, 2),
                        )
                    wait = max(wait, gap)

            if wait > 0:
                wait = min(wait, policy.max_wait)
                self._sleep(wait)
                self.total_wait += wait
                # Time passed while sleeping: refill and re-check the floor.
                self._refill()
                now = self._clock()
                self._prune_minute(now)

            self._tokens = max(0.0, self._tokens - needed)
            self._last_request = self._clock()
            self._minute_hits.append(self._last_request)
            self._day_hits += 1
            self.acquired += 1
            return AcquireResult(allowed=True, waited=round(wait, 3))

    # ------------------------------------------------------------------
    def note_retry_after(self, seconds: Optional[float]) -> None:
        """Honour a server-provided wait before the next request."""
        if not self.policy.respect_retry_after or not seconds:
            return
        try:
            delay = float(seconds)
        except (TypeError, ValueError):
            return
        if delay <= 0:
            return
        with self._lock:
            delay = min(delay, self.policy.cooldown_cap)
            self._cooldown_until = max(self._cooldown_until, self._clock() + delay)
            self.cooldowns += 1

    def note_headers(self, headers: Optional[Dict[str, str]]) -> None:
        """Read standard rate-limit headers if the provider publishes them."""
        if not headers:
            return
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}
        remaining = lowered.get("x-ratelimit-remaining")
        reset = lowered.get("x-ratelimit-reset")
        if remaining is not None:
            try:
                if int(remaining) <= 0 and reset:
                    # Reset may be epoch seconds or a delta; handle both.
                    value = float(reset)
                    now_wall = self._wall_clock()
                    delay = value - now_wall if value > 1e9 else value
                    self.note_retry_after(max(0.0, delay))
            except (TypeError, ValueError):
                pass

    def cooldown_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until - self._clock())

    def budget(self) -> Dict[str, Any]:
        """Remaining quota, for the Settings/Diagnostics display."""
        with self._lock:
            self._refill()
            self._prune_minute(self._clock())
            self._roll_day()
            return {
                "tokens": round(self._tokens, 2),
                "burst": self.policy.burst,
                "used_this_minute": len(self._minute_hits),
                "max_per_minute": self.policy.max_per_minute,
                "used_today": self._day_hits,
                "max_per_day": self.policy.max_per_day,
                "cooldown_seconds": round(self.cooldown_remaining(), 1),
                "acquired": self.acquired,
                "refused": self.refused,
                "total_wait_seconds": round(self.total_wait, 2),
                "server_cooldowns": self.cooldowns,
            }

    def stats(self) -> Dict[str, Any]:
        out = self.policy.describe()
        out.update(self.budget())
        return out

    def reset(self) -> None:
        """Clear counters (used after a settings change or by tests)."""
        with self._lock:
            self._tokens = float(self.policy.burst)
            self._last_refill = self._clock()
            self._last_request = None
            self._minute_hits.clear()
            self._day_hits = 0
            self._cooldown_until = 0.0
            self.acquired = 0
            self.refused = 0
            self.total_wait = 0.0
            self.cooldowns = 0


class LimiterSet(object):
    """The limiter registry the application shares across workers."""

    def __init__(
        self,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        wall_clock: Optional[Callable[[], float]] = None,
        unlimited: bool = False,
    ):
        kwargs = {}
        if clock is not None:
            kwargs["clock"] = clock
        if sleep is not None:
            kwargs["sleep"] = sleep
        if wall_clock is not None:
            kwargs["wall_clock"] = wall_clock
        self._kwargs = kwargs
        if unlimited:
            # Tests: same code path, no waiting and no quota.
            self.igdb = RateLimiter(TEST_POLICY, **kwargs)
            self.rawg = RateLimiter(TEST_POLICY, **kwargs)
            self.images = RateLimiter(TEST_POLICY, **kwargs)
            self.twitch = RateLimiter(TEST_POLICY, **kwargs)
            return
        self.igdb = RateLimiter(IGDB_POLICY, **kwargs)
        self.rawg = RateLimiter(RAWG_POLICY, **kwargs)
        self.images = RateLimiter(IMAGE_POLICY, **kwargs)
        self.twitch = RateLimiter(TWITCH_POLICY, **kwargs)

    def for_provider(self, name: Optional[str]) -> Optional[RateLimiter]:
        return {
            "igdb": self.igdb,
            "rawg": self.rawg,
            "images": self.images,
            "twitch_auth": self.twitch,
        }.get(name or "")

    def summary(self) -> Dict[str, Any]:
        return {
            "igdb": self.igdb.stats(),
            "rawg": self.rawg.stats(),
            "images": self.images.stats(),
            "twitch_auth": self.twitch.stats(),
        }

    def reset(self) -> None:
        for limiter in (self.igdb, self.rawg, self.images, self.twitch):
            limiter.reset()


def _human_seconds(seconds: float) -> str:
    value = max(0.0, float(seconds))
    if value < 1.0:
        return "%.1f s" % value
    if value < 90.0:
        return "%d s" % int(round(value))
    minutes = int(round(value / 60.0))
    if minutes < 90:
        return "%d min" % minutes
    return "%d h" % int(round(minutes / 60.0))
