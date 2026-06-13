"""Async token-bucket rate limiter for provider API calls (Step 2.2).

Binance USDT-M Futures imposes a request-weight budget (2400 weight / minute per
IP for market-data endpoints). A token bucket meters *weighted* calls against a
steady refill rate: each call `acquire(weight)`s before hitting the API and
blocks (cooperatively) only when the budget is temporarily exhausted.

The clock and sleep are injectable so unit tests can drive the bucket with a fake
clock and assert blocking behaviour deterministically, without real delays.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Final

BINANCE_FUTURES_WEIGHT_PER_MINUTE: Final[int] = 2400
"""Binance USDT-M Futures market-data request-weight budget per minute, per IP."""

BINANCE_FUTURES_REFILL_PER_SEC: Final[float] = BINANCE_FUTURES_WEIGHT_PER_MINUTE / 60.0
"""Steady refill rate (40 weight/second) that the per-minute budget implies."""


class TokenBucket:
    """A cooperative async token bucket.

    `acquire(weight)` deducts `weight` tokens, sleeping for the shortfall (at the
    refill rate) when the bucket lacks them. An internal lock serializes waiters so
    a single bucket can be shared safely across concurrently-gathered callers
    (e.g. fetching five timeframes at once).
    """

    def __init__(
        self,
        *,
        capacity: float,
        refill_per_sec: float,
        time_func: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive (got {capacity})")
        if refill_per_sec <= 0:
            raise ValueError(f"refill_per_sec must be positive (got {refill_per_sec})")
        self._capacity = float(capacity)
        self._refill = float(refill_per_sec)
        self._tokens = float(capacity)
        self._time = time_func
        self._sleep = sleep_func
        self._updated = time_func()
        self._lock = asyncio.Lock()

    @classmethod
    def for_binance_futures(
        cls,
        *,
        time_func: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> TokenBucket:
        """A bucket sized to Binance Futures' 2400 weight/minute market-data budget."""
        return cls(
            capacity=BINANCE_FUTURES_WEIGHT_PER_MINUTE,
            refill_per_sec=BINANCE_FUTURES_REFILL_PER_SEC,
            time_func=time_func,
            sleep_func=sleep_func,
        )

    @property
    def available_tokens(self) -> float:
        """Tokens available as of the last refill (does not advance the clock)."""
        return self._tokens

    async def acquire(self, weight: float = 1.0) -> None:
        """Block until `weight` tokens are available, then deduct them."""
        if weight <= 0:
            raise ValueError(f"weight must be positive (got {weight})")
        if weight > self._capacity:
            raise ValueError(f"weight {weight} exceeds bucket capacity {self._capacity}")

        async with self._lock:
            while True:
                now = self._time()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._refill
                )
                self._updated = now
                if self._tokens >= weight:
                    self._tokens -= weight
                    return
                await self._sleep((weight - self._tokens) / self._refill)
