"""Market data for the dashboard (Step 4.3): candles, latest prices, unrealised R.

Every agent reads market data through the uniform `DataProvider` interface
(`src.providers.base`); the dashboard does the same. Three rules shape this module:

* **One provider per request, always closed.** `BinanceProvider` owns a ccxt
  client and an aiohttp session, and aiohttp sessions are bound to the event
  loop they were created on. Mangum keeps one loop per container, but building
  the provider per request and closing it in ``finally`` is the simple choice
  that cannot leak a session across loops. Routers depend on a
  :data:`ProviderFactory` (FastAPI dependency :func:`get_provider_factory`), never
  on a provider, so a request rejected before any market call (unknown symbol,
  404 signal) constructs nothing. Tests override the dependency.
* **Klines are a proxy, errors are mapped app-wide.** :func:`fetch_recent_klines`
  lets `ProviderError` propagate; `errors.py` turns it into **502**
  ``{"reason": "market_unavailable"}`` (message logged, never echoed).
* **Unrealised R is best-effort.** :func:`latest_prices` runs one fetch per
  symbol concurrently under an overall budget (:data:`PRICE_BUDGET_S`, 5 s, via
  ``asyncio.wait_for``); a per-symbol `ProviderError` or an empty window omits
  that symbol and a blown budget returns ``{}``. It never raises a provider
  error or a timeout, so a slow or down Binance cannot fail the landing page --
  the cards just show ``unrealised_r: null`` with ``"price unavailable"``.

The default factory imports `BinanceProvider` lazily: ccxt is a heavy import and
the unauthenticated `/api/health` cold start should not pay for it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Annotated, Final

from fastapi import Depends

from src.common.models import SignalDirection
from src.providers.base import DataProvider, Kline, ProviderError, Timeframe

__all__ = [
    "LATEST_PRICE_LOOKBACK",
    "PRICE_BUDGET_S",
    "TIMEFRAME_DURATION",
    "ProviderFactory",
    "ProviderFactoryDep",
    "default_provider_factory",
    "fetch_klines_window",
    "fetch_recent_klines",
    "get_provider_factory",
    "latest_prices",
    "unrealised_r",
]

logger = logging.getLogger(__name__)

ProviderFactory = Callable[[], DataProvider]

TIMEFRAME_DURATION: Final[dict[Timeframe, timedelta]] = {
    Timeframe.D1: timedelta(days=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.M5: timedelta(minutes=5),
}

# Overall budget for the setup cards' latest-price fetch (global constraints: 5 s).
# Routers pass it explicitly at call time so tests can shrink it.
PRICE_BUDGET_S: float = 5.0

# The latest price is the last 5m close inside this window (six candles).
LATEST_PRICE_LOOKBACK: Final[timedelta] = timedelta(minutes=30)


def _binance_provider() -> DataProvider:
    from src.providers.binance import BinanceProvider  # lazy: ccxt is a heavy import

    return BinanceProvider()


# The production factory. A module attribute (looked up per call by
# `get_provider_factory`) so the import-time Mangum app can be pointed at a fake.
default_provider_factory: ProviderFactory = _binance_provider


def get_provider_factory() -> ProviderFactory:
    """FastAPI dependency: the factory routers build their per-request provider with."""
    return default_provider_factory


# Route parameter annotation: `async def route(factory: ProviderFactoryDep)`. Tests
# swap it with `app.dependency_overrides[get_provider_factory] = lambda: fake_factory`.
ProviderFactoryDep = Annotated[ProviderFactory, Depends(get_provider_factory)]


async def fetch_klines_window(
    factory: ProviderFactory,
    symbol: str,
    tf: Timeframe,
    *,
    start: datetime,
    end: datetime,
) -> list[Kline]:
    """Candles with ``start <= open_time < end``, ascending, from a fresh provider.

    An empty window (``end <= start``) returns ``[]`` without building a provider.
    `ProviderError` propagates (502 via `errors.py`); the provider is closed either way.
    """
    if end <= start:
        return []
    provider = factory()
    try:
        return await provider.fetch_klines(symbol, tf, start=start, end=end)
    finally:
        await provider.aclose()


async def fetch_recent_klines(
    factory: ProviderFactory,
    symbol: str,
    tf: Timeframe,
    *,
    limit: int,
    end: datetime,
) -> list[Kline]:
    """The last ``limit`` candles opening before ``end`` (``start = end - limit * tf``).

    A half-open window of exactly ``limit`` bars holds at most ``limit`` open
    times; the trailing slice is a guard against a provider returning more.
    """
    start = end - limit * TIMEFRAME_DURATION[tf]
    klines = await fetch_klines_window(factory, symbol, tf, start=start, end=end)
    return klines[-limit:]


async def _last_close(provider: DataProvider, symbol: str, *, now: datetime) -> float | None:
    candles = await provider.fetch_klines(
        symbol, Timeframe.M5, start=now - LATEST_PRICE_LOOKBACK, end=now
    )
    return candles[-1].close if candles else None


async def latest_prices(
    factory: ProviderFactory,
    symbols: Sequence[str],
    *,
    now: datetime,
    budget_s: float = PRICE_BUDGET_S,
) -> dict[str, float]:
    """Last 5m close per symbol over the past 30 min -- best-effort, never raises on market errors.

    One provider for the whole call; one fetch per distinct symbol, concurrent,
    under an overall ``budget_s`` (``asyncio.wait_for``). A symbol whose fetch
    raises `ProviderError` or returns no candles is omitted; a blown budget
    returns ``{}``. No symbols -> ``{}`` without building a provider. Anything
    that is not a provider error (a bug) propagates.
    """
    unique = list(dict.fromkeys(symbols))
    if not unique:
        return {}
    provider = factory()
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(_last_close(provider, symbol, now=now) for symbol in unique),
                return_exceptions=True,
            ),
            timeout=budget_s,
        )
    except TimeoutError:
        logger.warning(
            "latest prices for %s exceeded the %.1f s budget; unrealised R unavailable",
            ",".join(unique),
            budget_s,
        )
        return {}
    finally:
        try:
            await provider.aclose()
        except Exception:  # best-effort path: a close failure must not fail the page
            logger.warning("closing the market provider failed", exc_info=True)

    prices: dict[str, float] = {}
    for symbol, result in zip(unique, results, strict=True):
        if isinstance(result, ProviderError):
            logger.warning(
                "latest price for %s unavailable (%s): %s", symbol, result.provider, result
            )
        elif isinstance(result, BaseException):
            raise result
        elif result is not None:
            prices[symbol] = result
    return prices


def unrealised_r(direction: SignalDirection, entry: float, stop: float, price: float) -> float:
    """Open R multiple at ``price``, rounded to 2 dp.

    LONG ``(price - entry) / (entry - stop)``; SHORT ``(entry - price) / (stop - entry)``.
    Raises ``ValueError`` when ``entry == stop`` (the risk unit is zero).
    """
    if entry == stop:
        raise ValueError(f"entry == stop ({entry}): risk is zero, R is undefined")
    if direction is SignalDirection.LONG:
        value = (price - entry) / (entry - stop)
    else:
        value = (entry - price) / (stop - entry)
    return round(value, 2)
