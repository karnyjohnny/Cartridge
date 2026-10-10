"""Tests for rate limiting (brief section 10: respect provider limits).

Time is injected, so these run in microseconds and assert on *policy*, not on
how fast the machine happens to be.
"""

from __future__ import annotations

import httpx
import pytest

from cartridge.providers.base import HttpSettings, HttpClient, ProviderRateLimited
from cartridge.providers.igdb import IgdbProvider
from cartridge.providers.ratelimit import (
    IGDB_POLICY,
    IMAGE_POLICY,
    RAWG_POLICY,
    TEST_POLICY,
    TWITCH_POLICY,
    LimiterSet,
    RateLimiter,
    RateLimitPolicy,
    _human_seconds,
)
from cartridge.providers.rawg import RawgProvider


class FakeTime(object):
    """Controllable monotonic clock + sleep recorder."""

    def __init__(self, start=1000.0, wall=None):
        self.now = start
        self.wall = wall if wall is not None else 1700000000.0
        self.slept = []

    def clock(self):
        return self.now

    def wall_clock(self):
        return self.wall

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds          # sleeping advances the fake clock
        self.wall += seconds

    @property
    def total_sleep(self):
        return sum(self.slept)


def make_limiter(policy=None, time=None):
    time = time or FakeTime()
    policy = policy or RateLimitPolicy(
        name="t", burst=2, refill_rate=1.0, min_interval=0.0,
        max_per_minute=5, max_per_day=10, max_wait=3.0,
    )
    return RateLimiter(
        policy, clock=time.clock, sleep=time.sleep, wall_clock=time.wall_clock
    ), time


# --------------------------------------------------------------------------
# token bucket
# --------------------------------------------------------------------------
def test_burst_allows_immediate_requests_then_paces():
    limiter, clock = make_limiter()
    first = limiter.acquire()
    second = limiter.acquire()
    assert first.allowed and second.allowed
    assert clock.total_sleep == 0.0, "a full bucket must not wait"

    third = limiter.acquire()
    assert third.allowed
    assert clock.total_sleep > 0.0, "an empty bucket must wait for a refill"


def test_refill_restores_capacity_over_time():
    limiter, clock = make_limiter()
    limiter.acquire()
    limiter.acquire()
    clock.now += 5.0          # 5 s at 1 token/s, capped at burst=2
    assert limiter.budget()["tokens"] == pytest.approx(2.0, abs=0.01)


def test_tokens_never_exceed_burst():
    limiter, clock = make_limiter()
    clock.now += 1000.0
    assert limiter.budget()["tokens"] == 2.0


def test_minimum_interval_is_enforced():
    limiter, clock = make_limiter(
        RateLimitPolicy(name="t", burst=10, refill_rate=10.0, min_interval=2.0,
                        max_per_minute=100, max_per_day=100, max_wait=5.0)
    )
    limiter.acquire()
    limiter.acquire()
    assert clock.total_sleep == pytest.approx(2.0, abs=0.01)


def test_wait_is_capped_and_refused_instead():
    """A long wait must become a refusal, never a frozen UI."""
    limiter, clock = make_limiter(
        RateLimitPolicy(name="t", burst=1, refill_rate=0.01, min_interval=0.0,
                        max_per_minute=100, max_per_day=1000, max_wait=2.0)
    )
    limiter.acquire()
    result = limiter.acquire()
    assert result.allowed is False
    assert result.retry_after is not None
    assert "faster than its policy" in result.reason


def test_non_blocking_acquire_never_sleeps():
    limiter, clock = make_limiter()
    limiter.acquire(block=False)
    limiter.acquire(block=False)
    result = limiter.acquire(block=False)
    assert result.allowed is False
    assert clock.slept == []


# --------------------------------------------------------------------------
# window caps
# --------------------------------------------------------------------------
def test_per_minute_cap_refuses_and_explains():
    limiter, clock = make_limiter(
        RateLimitPolicy(name="igdb", burst=100, refill_rate=100.0, min_interval=0.0,
                        max_per_minute=3, max_per_day=1000, max_wait=0.5)
    )
    for _ in range(3):
        assert limiter.acquire().allowed
    result = limiter.acquire()
    assert result.allowed is False
    assert "per-minute limit" in result.reason
    assert result.retry_after is not None and result.retry_after <= 60.0


def test_minute_window_rolls_over():
    limiter, clock = make_limiter(
        RateLimitPolicy(name="t", burst=100, refill_rate=100.0, min_interval=0.0,
                        max_per_minute=2, max_per_day=1000, max_wait=70.0)
    )
    limiter.acquire()
    limiter.acquire()
    assert limiter.acquire(block=False).allowed is False
    clock.now += 61.0
    assert limiter.acquire().allowed is True


def test_per_day_cap_is_hard():
    limiter, clock = make_limiter(
        RateLimitPolicy(name="rawg", burst=100, refill_rate=100.0, min_interval=0.0,
                        max_per_minute=100, max_per_day=4, max_wait=1.0)
    )
    for _ in range(4):
        assert limiter.acquire().allowed
    result = limiter.acquire()
    assert result.allowed is False
    assert "daily budget" in result.reason
    # Waiting cannot help: the daily cap is a hard stop.
    clock.now += 3600.0
    assert limiter.acquire().allowed is False


def test_day_counter_resets_on_a_new_utc_day():
    limiter, clock = make_limiter(
        RateLimitPolicy(name="t", burst=100, refill_rate=100.0, min_interval=0.0,
                        max_per_minute=100, max_per_day=2, max_wait=1.0)
    )
    limiter.acquire()
    limiter.acquire()
    assert limiter.acquire().allowed is False
    clock.wall += 86400.0     # next UTC day
    assert limiter.acquire().allowed is True


# --------------------------------------------------------------------------
# server-driven cooldowns
# --------------------------------------------------------------------------
def test_retry_after_creates_a_cooldown():
    limiter, clock = make_limiter()
    limiter.note_retry_after(2.0)
    assert limiter.cooldown_remaining() == pytest.approx(2.0, abs=0.01)
    result = limiter.acquire()
    assert result.allowed is True, "a 2 s cooldown is inside max_wait, so we wait it out"
    assert clock.total_sleep >= 1.9


def test_long_retry_after_refuses_instead_of_sleeping():
    limiter, clock = make_limiter(
        RateLimitPolicy(name="t", burst=5, refill_rate=5.0, min_interval=0.0,
                        max_per_minute=100, max_per_day=1000, max_wait=2.0)
    )
    limiter.note_retry_after(600.0)
    result = limiter.acquire()
    assert result.allowed is False
    assert "cooldown" in result.reason
    assert result.retry_after > 0


def test_cooldown_is_capped_by_policy():
    limiter, clock = make_limiter(
        RateLimitPolicy(name="t", burst=5, refill_rate=5.0, min_interval=0.0,
                        max_per_minute=100, max_per_day=1000,
                        max_wait=2.0, cooldown_cap=30.0)
    )
    limiter.note_retry_after(10000.0)
    assert limiter.cooldown_remaining() <= 30.0


def test_cooldown_expires():
    limiter, clock = make_limiter()
    limiter.note_retry_after(1.0)
    clock.now += 2.0
    assert limiter.cooldown_remaining() == 0.0


def test_zero_and_invalid_retry_after_are_ignored():
    limiter, _clock = make_limiter()
    limiter.note_retry_after(0)
    limiter.note_retry_after(None)
    limiter.note_retry_after("soon")
    limiter.note_retry_after(-5)
    assert limiter.cooldown_remaining() == 0.0


def test_rate_limit_headers_are_honoured():
    limiter, clock = make_limiter()
    limiter.note_headers({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "12"})
    assert limiter.cooldown_remaining() > 0


def test_rate_limit_headers_with_epoch_reset():
    limiter, clock = make_limiter()
    future = clock.wall + 20.0
    limiter.note_headers(
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(future)}
    )
    assert 0 < limiter.cooldown_remaining() <= 21.0


def test_healthy_headers_do_not_trigger_a_cooldown():
    limiter, _clock = make_limiter()
    limiter.note_headers({"X-RateLimit-Remaining": "350", "Content-Type": "application/json"})
    assert limiter.cooldown_remaining() == 0.0


def test_headers_can_be_disabled_by_policy():
    policy = RateLimitPolicy(name="t", respect_retry_after=False, max_wait=1.0)
    clock = FakeTime()
    limiter = RateLimiter(policy, clock=clock.clock, sleep=clock.sleep)
    limiter.note_retry_after(30.0)
    assert limiter.cooldown_remaining() == 0.0


# --------------------------------------------------------------------------
# accounting
# --------------------------------------------------------------------------
def test_budget_reports_usage_without_secrets():
    limiter, _clock = make_limiter()
    limiter.acquire()
    limiter.acquire()
    budget = limiter.budget()
    assert budget["used_this_minute"] == 2
    assert budget["used_today"] == 2
    assert budget["max_per_day"] == 10
    assert budget["acquired"] == 2
    assert budget["refused"] == 0


def test_refusals_are_counted():
    limiter, _clock = make_limiter(
        RateLimitPolicy(name="t", burst=1, refill_rate=0.001, min_interval=0.0,
                        max_per_minute=100, max_per_day=1000, max_wait=0.5)
    )
    limiter.acquire()
    limiter.acquire()
    assert limiter.budget()["refused"] == 1


def test_reset_clears_everything():
    limiter, _clock = make_limiter()
    limiter.acquire()
    limiter.note_retry_after(5)
    limiter.reset()
    budget = limiter.budget()
    assert budget["used_this_minute"] == 0
    assert budget["used_today"] == 0
    assert budget["cooldown_seconds"] == 0.0
    assert budget["acquired"] == 0


def test_stats_include_the_policy():
    limiter, _clock = make_limiter()
    stats = limiter.stats()
    assert stats["name"] == "t"
    assert stats["burst"] == 2
    assert "used_today" in stats


def test_human_seconds_formatting():
    assert _human_seconds(0.4) == "0.4 s"
    assert _human_seconds(45) == "45 s"
    assert _human_seconds(120) == "2 min"
    assert _human_seconds(7200) == "2 h"


# --------------------------------------------------------------------------
# presets stay conservative relative to published limits
# --------------------------------------------------------------------------
def test_igdb_preset_stays_under_published_limits():
    assert IGDB_POLICY.refill_rate <= 4.0        # IGDB publishes 4 req/s
    assert IGDB_POLICY.max_per_day <= 40000      # IGDB publishes 40k/day
    assert IGDB_POLICY.min_interval >= 0.25


def test_rawg_preset_stays_under_the_free_monthly_quota():
    assert RAWG_POLICY.max_per_day * 31 <= 20000, "must fit the 20k/month free tier"


def test_twitch_auth_preset_is_very_conservative():
    assert TWITCH_POLICY.max_per_minute <= 10
    assert TWITCH_POLICY.min_interval >= 1.0


def test_image_preset_is_bounded():
    assert IMAGE_POLICY.max_per_day <= 5000


def test_limiter_set_summary_covers_every_provider():
    clock = FakeTime()
    limiters = LimiterSet(clock=clock.clock, sleep=clock.sleep, wall_clock=clock.wall_clock)
    summary = limiters.summary()
    assert set(summary) == {"igdb", "rawg", "images", "twitch_auth"}
    assert summary["igdb"]["name"] == "igdb"
    assert limiters.for_provider("rawg") is limiters.rawg
    assert limiters.for_provider("nope") is None


def test_limiter_set_unlimited_mode_for_tests():
    limiters = LimiterSet(unlimited=True, sleep=lambda s: None)
    for _ in range(50):
        assert limiters.igdb.acquire().allowed


# --------------------------------------------------------------------------
# integration with the transport
# --------------------------------------------------------------------------
def test_http_client_refuses_when_the_limiter_says_stop():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={})

    clock = FakeTime()
    limiter = RateLimiter(
        RateLimitPolicy(name="igdb", burst=1, refill_rate=0.001, min_interval=0.0,
                        max_per_minute=100, max_per_day=1000, max_wait=0.1),
        clock=clock.clock, sleep=clock.sleep, wall_clock=clock.wall_clock,
    )
    client = HttpClient(
        settings=HttpSettings(),
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        limiter=limiter,
    )
    client.request("GET", "https://api.example.test/a")
    with pytest.raises(ProviderRateLimited) as info:
        client.request("GET", "https://api.example.test/b")
    assert calls["n"] == 1, "a refused request must never reach the network"
    assert client.throttled_count == 1
    assert "igdb" in str(info.value)


def test_http_client_feeds_retry_after_into_the_limiter():
    def handler(request):
        return httpx.Response(429, text="slow down", headers={"Retry-After": "30"})

    clock = FakeTime()
    limiter = RateLimiter(
        RateLimitPolicy(name="igdb", burst=5, refill_rate=5.0, min_interval=0.0,
                        max_per_minute=100, max_per_day=1000, max_wait=1.0),
        clock=clock.clock, sleep=clock.sleep, wall_clock=clock.wall_clock,
    )
    client = HttpClient(
        settings=HttpSettings(max_retries=0),
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        limiter=limiter,
    )
    with pytest.raises(ProviderRateLimited):
        client.request("GET", "https://api.example.test/a")
    assert limiter.cooldowns >= 1


def test_igdb_uses_a_separate_limiter_for_the_token_endpoint():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return httpx.Response(
                200, json={"access_token": "tok0123456789abcdef", "expires_in": 3600}
            )
        return httpx.Response(200, json=[])

    client = HttpClient(
        settings=HttpSettings(), transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )
    provider = IgdbProvider(client, "id", "secret")
    assert provider.limiter is not provider.auth_limiter
    assert provider.limiter.policy.name == "igdb"
    assert provider.auth_limiter.policy.name == "twitch_auth"


def test_rawg_uses_the_rawg_policy():
    client = HttpClient(
        settings=HttpSettings(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        sleep=lambda s: None,
    )
    provider = RawgProvider(client, "key")
    assert provider.limiter.policy.name == "rawg"


def test_provider_test_connection_reports_its_budget():
    def handler(request):
        if "id.twitch.tv" in str(request.url):
            return httpx.Response(
                200, json={"access_token": "tok0123456789abcdef", "expires_in": 3600}
            )
        return httpx.Response(200, json=[])

    client = HttpClient(
        settings=HttpSettings(), transport=httpx.MockTransport(handler),
        sleep=lambda s: None, limiter=RateLimiter(TEST_POLICY, sleep=lambda s: None),
    )
    provider = IgdbProvider(client, "id", "secret")
    result = provider.test_connection()
    assert "rate_limit" in result
    assert result["rate_limit"]["used_today"] >= 0
    assert "client_secret" not in str(result)
