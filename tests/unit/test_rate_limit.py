"""Tests for the async TokenBucket rate limiter (Step 2.2).

A fake clock drives time deterministically: `sleep()` advances the clock instead
of waiting, so we can assert blocking behaviour with no real delay.
"""

from __future__ import annotations

import pytest

from src.providers.rate_limit import (
    BINANCE_FUTURES_REFILL_PER_SEC,
    BINANCE_FUTURES_WEIGHT_PER_MINUTE,
    TokenBucket,
)


class _FakeClock:
    """Controllable monotonic clock whose async sleep advances time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _bucket(clock: _FakeClock, *, capacity: float = 10.0, refill: float = 2.0) -> TokenBucket:
    return TokenBucket(
        capacity=capacity,
        refill_per_sec=refill,
        time_func=clock.time,
        sleep_func=clock.sleep,
    )


class TestAcquire:
    async def test_acquire_within_budget_does_not_sleep(self) -> None:
        clock = _FakeClock()
        bucket = _bucket(clock)
        await bucket.acquire(5)
        assert clock.slept == []
        assert bucket.available_tokens == pytest.approx(5.0)

    async def test_acquire_deficit_sleeps_for_refill(self) -> None:
        clock = _FakeClock()
        bucket = _bucket(clock, capacity=10.0, refill=2.0)
        await bucket.acquire(10)  # drains the bucket, no sleep needed
        await bucket.acquire(4)  # needs 4 at 2/s -> sleep 2.0s, then deduct
        assert clock.slept == [pytest.approx(2.0)]
        assert bucket.available_tokens == pytest.approx(0.0)

    async def test_refill_caps_at_capacity(self) -> None:
        clock = _FakeClock()
        bucket = _bucket(clock, capacity=10.0, refill=2.0)
        await bucket.acquire(10)  # empty
        clock.now += 100.0  # plenty of time passes
        await bucket.acquire(1)
        # Refill is capped at capacity (10), not 100*2; after deducting 1 -> 9.
        assert bucket.available_tokens == pytest.approx(9.0)

    async def test_weight_above_capacity_raises(self) -> None:
        bucket = _bucket(_FakeClock(), capacity=10.0)
        with pytest.raises(ValueError, match="exceeds bucket capacity"):
            await bucket.acquire(11)

    async def test_non_positive_weight_raises(self) -> None:
        bucket = _bucket(_FakeClock())
        with pytest.raises(ValueError, match="weight must be positive"):
            await bucket.acquire(0)


class TestConstruction:
    def test_rejects_non_positive_capacity(self) -> None:
        with pytest.raises(ValueError, match="capacity"):
            TokenBucket(capacity=0, refill_per_sec=1)

    def test_rejects_non_positive_refill(self) -> None:
        with pytest.raises(ValueError, match="refill"):
            TokenBucket(capacity=1, refill_per_sec=0)

    def test_binance_futures_preset(self) -> None:
        bucket = TokenBucket.for_binance_futures()
        assert bucket.available_tokens == pytest.approx(BINANCE_FUTURES_WEIGHT_PER_MINUTE)
        assert pytest.approx(40.0) == BINANCE_FUTURES_REFILL_PER_SEC
