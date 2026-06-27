"""BinanceProvider: Binance Futures market-data provider implementing DataProvider.

Slice 1 Step 1.4 scope: one method, fetch 4H klines for one symbol. The
interface is multi-timeframe by design (see `base.MarketSnapshot`) so this
implementation already loops over the requested timeframe list — even though
callers pass `[Timeframe.H4]` for now. Step 2.2 expands the test surface to
cover D1/H1/M15/M5; no signature change required.

CCXT mapping notes:
- `ccxt.async_support.binance` uses the *spot* endpoint by default. We use
  `options={'defaultType': 'future'}` to point at Futures klines, matching the
  SMC analyzer's domain (perpetuals with funding/OI).
- CCXT's OHLCV response is `list[list[int, float, float, float, float, float]]`:
  `[timestamp_ms, open, high, low, close, volume]`. We normalize per-row to
  Kline; per-snapshot to MarketSnapshot.
- Exceptions are mapped in `_translate_ccxt_error`. The vendor's hierarchy is
  thorough (`NetworkError`, `RequestTimeout`, `DDoSProtection`,
  `RateLimitExceeded`, `BadResponse`, `ExchangeError`, etc.); we collapse into
  the four ProviderError subclasses defined in `base.py`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import aiohttp
import ccxt
import ccxt.async_support as ccxt_async

from src.providers.base import (
    DataProvider,
    Kline,
    MarketSnapshot,
    ProviderInvalidResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    Timeframe,
)
from src.providers.rate_limit import TokenBucket

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

# Binance USDT-M Futures request weights used to meter the rate limiter.
_FUNDING_WEIGHT: Final[int] = 1
_OPEN_INTEREST_WEIGHT: Final[int] = 1


class BinanceProvider(DataProvider):
    """Concrete DataProvider for Binance USDT-M Futures klines.

    Lifecycle:
        Either use as an async context manager (`async with BinanceProvider() as p:`)
        which closes the aiohttp session automatically, or call `await p.aclose()`
        explicitly when done. Forgetting to close leaves a warning on event-loop
        shutdown.
    """

    name = "binance"

    def __init__(
        self,
        *,
        client: ccxt_async.binance | None = None,
        rate_limiter: TokenBucket | None = None,
    ) -> None:
        """Construct a BinanceProvider.

        Args:
            client: Optional pre-built ccxt async client. Tests inject a mock
                here. Production code passes None to get a fresh Futures client.
            rate_limiter: Optional token bucket metering request weight. Defaults
                to Binance Futures' 2400 weight/minute budget (Step 2.2). Tests
                inject one with a fake clock to assert blocking deterministically.

        Resolver note:
            When we build the client ourselves, we attach an aiohttp session
            that uses the portable ThreadedResolver instead of aiodns.
            CCXT's default async resolver (aiodns / c-ares) fails to read the
            system DNS configuration on some Windows machines, raising
            "Could not contact DNS servers" even when the OS resolver works
            fine. ThreadedResolver delegates to the standard getaddrinfo in a
            thread pool -- negligible cost at our scan volume, and portable
            across platforms. The session is only created when no client is
            injected, so tests (which inject a mock) never touch the network.
        """
        self._bucket = (
            rate_limiter if rate_limiter is not None else TokenBucket.for_binance_futures()
        )
        self._session: aiohttp.ClientSession | None = None
        if client is not None:
            self._client: ccxt_async.binance = client
        else:
            self._client = ccxt_async.binance(
                {
                    "enableRateLimit": True,
                    "options": {"defaultType": "future"},
                }
            )
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            self._session = aiohttp.ClientSession(connector=connector)
            self._client.session = self._session

    async def aclose(self) -> None:
        # ccxt's close() shuts down the session it holds (ours, when we set
        # it). The extra guarded close is belt-and-suspenders for the case
        # where ccxt did not adopt our session.
        await self._client.close()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        # ccxt's Exchange.__del__ logs "requires to release all resources with an
        # explicit call to the .close() coroutine" whenever exchange.session is not
        # None at garbage-collection -- even after close(), because close() does not
        # null the reference and we assigned our own session. On Lambda the GC runs
        # at container freeze, so that (benign) warning fired on every scan. Null the
        # reference now that we are done with the client to keep the logs clean.
        self._client.session = None

    async def fetch_market_snapshot(
        self,
        symbol: str,
        timeframes: list[Timeframe],
        *,
        limit: int = 200,
        include_derivatives: bool = False,
    ) -> MarketSnapshot:
        if not timeframes:
            raise ProviderInvalidResponseError(
                "timeframes must contain at least one entry", provider=self.name
            )
        if limit <= 0:
            raise ProviderInvalidResponseError(
                f"limit must be positive (got {limit})", provider=self.name
            )

        # Fetch all timeframes concurrently. return_exceptions keeps every result
        # retrievable, so a failure in one timeframe does not leave an un-awaited
        # task whose exception would surface as a fatal warning under
        # filterwarnings=error; we re-raise the first error after the gather.
        results = await asyncio.gather(
            *(self._fetch_one_timeframe(symbol, tf, limit) for tf in timeframes),
            return_exceptions=True,
        )
        klines: dict[Timeframe, list[Kline]] = {}
        for tf, result in zip(timeframes, results, strict=True):
            if isinstance(result, BaseException):
                raise result
            klines[tf] = result

        funding_rate: float | None = None
        open_interest: float | None = None
        if include_derivatives:
            funding_rate, open_interest = await self._fetch_derivatives(symbol)

        try:
            return MarketSnapshot(
                symbol=symbol,
                venue=self.name,
                fetched_at=datetime.now(UTC),
                klines=klines,
                funding_rate=funding_rate,
                open_interest=open_interest,
            )
        except ValueError as exc:
            # Pydantic ValidationError is a ValueError subclass.
            raise ProviderInvalidResponseError(
                f"MarketSnapshot validation failed: {exc}", provider=self.name
            ) from exc

    async def fetch_funding_rate(self, symbol: str) -> float | None:
        """Latest perpetual funding rate as a decimal (e.g. 0.0001 = 0.01%).

        Returns None when the venue does not expose a funding rate for the symbol.
        """
        await self._bucket.acquire(_FUNDING_WEIGHT)
        try:
            result = await self._client.fetch_funding_rate(symbol)
        except ccxt.BaseError as exc:
            raise self._translate_ccxt_error(exc) from exc
        rate = result.get("fundingRate") if isinstance(result, dict) else None
        return float(rate) if rate is not None else None

    async def fetch_open_interest(self, symbol: str) -> float | None:
        """Latest open interest in base-asset units, or None when unavailable."""
        await self._bucket.acquire(_OPEN_INTEREST_WEIGHT)
        try:
            result = await self._client.fetch_open_interest(symbol)
        except ccxt.BaseError as exc:
            raise self._translate_ccxt_error(exc) from exc
        if not isinstance(result, dict):
            return None
        amount = result.get("openInterestAmount")
        if amount is None:
            amount = result.get("openInterest")
        return float(amount) if amount is not None else None

    async def _fetch_derivatives(self, symbol: str) -> tuple[float | None, float | None]:
        """Fetch funding rate + open interest concurrently, degrading to None on error.

        Per FR-4.3, a derivatives hiccup should not sink the whole snapshot — the
        analyzer's derivatives gate treats absence as neutral, not as a failure.
        """
        funding, oi = await asyncio.gather(
            self.fetch_funding_rate(symbol),
            self.fetch_open_interest(symbol),
            return_exceptions=True,
        )
        funding_rate = None if isinstance(funding, BaseException) else funding
        open_interest = None if isinstance(oi, BaseException) else oi
        return funding_rate, open_interest

    @staticmethod
    def _klines_weight(limit: int) -> int:
        """Binance Futures /klines request weight as a function of the limit."""
        if limit <= 100:
            return 1
        if limit <= 500:
            return 2
        if limit <= 1000:
            return 5
        return 10

    async def _fetch_one_timeframe(
        self, symbol: str, timeframe: Timeframe, limit: int
    ) -> list[Kline]:
        await self._bucket.acquire(self._klines_weight(limit))
        try:
            raw = await self._client.fetch_ohlcv(symbol, timeframe.value, limit=limit)
        except ccxt.BaseError as exc:
            raise self._translate_ccxt_error(exc) from exc

        return self._normalize_ohlcv(raw, timeframe=timeframe)

    def _normalize_ohlcv(
        self, raw: Sequence[Sequence[Any]], *, timeframe: Timeframe
    ) -> list[Kline]:
        """Convert CCXT's list-of-lists OHLCV into validated Kline models.

        Any row that fails validation or has the wrong arity raises
        ProviderInvalidResponseError — we do not silently drop rows, because
        the analyzer relies on contiguous candle history.
        """
        if not isinstance(raw, list):
            raise ProviderInvalidResponseError(
                f"expected list of candles for {timeframe.value}, got {type(raw).__name__}",
                provider=self.name,
            )

        result: list[Kline] = []
        for idx, row in enumerate(raw):
            if not isinstance(row, list) or len(row) < 6:
                raise ProviderInvalidResponseError(
                    f"malformed candle at index {idx} for {timeframe.value}: {row!r}",
                    provider=self.name,
                )
            ts_ms, o, h, lo, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
            try:
                kline = Kline(
                    open_time=datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC),
                    open=float(o),
                    high=float(h),
                    low=float(lo),
                    close=float(c),
                    volume=float(v),
                )
            except (ValueError, TypeError) as exc:
                raise ProviderInvalidResponseError(
                    f"candle at index {idx} for {timeframe.value} failed validation: {exc}",
                    provider=self.name,
                ) from exc
            result.append(kline)
        return result

    def _translate_ccxt_error(self, exc: ccxt.BaseError) -> Exception:
        """Map a CCXT exception to our typed ProviderError hierarchy.

        Order matters: more specific subclasses must be checked first. CCXT's
        `RateLimitExceeded` and `DDoSProtection` both inherit from
        `NetworkError`, so the rate-limit branches sit above the generic
        network branch.
        """
        if isinstance(exc, ccxt.RequestTimeout):
            return ProviderTimeoutError(str(exc), provider=self.name)
        if isinstance(exc, ccxt.RateLimitExceeded | ccxt.DDoSProtection):
            return ProviderRateLimitError(str(exc), provider=self.name)
        if isinstance(exc, ccxt.BadResponse):
            return ProviderInvalidResponseError(str(exc), provider=self.name)
        if isinstance(exc, ccxt.NetworkError):
            return ProviderUnavailableError(str(exc), provider=self.name)
        # ccxt.ExchangeError and the rest — usually means the venue accepted
        # the request shape but rejected the content (bad symbol, etc.).
        return ProviderUnavailableError(str(exc), provider=self.name)
