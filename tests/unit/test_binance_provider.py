"""Unit tests for src.providers.binance.BinanceProvider with mocked CCXT.

These tests do not touch the network. They verify the adapter layer:
- normalizing CCXT's list-of-lists OHLCV into MarketSnapshot + Kline
- mapping each CCXT exception subclass to the right ProviderError
- defensive validation of malformed responses
- async context manager lifecycle

The actual Binance API contract is exercised by the marked integration test
in tests/integration/test_binance_integration.py (opt-in, skipped in CI).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import ccxt
import pytest

from src.providers import (
    BinanceProvider,
    ProviderInvalidResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    Timeframe,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ccxt_row(
    ts_ms: int,
    *,
    o: float = 100.0,
    h: float = 105.0,
    lo: float = 99.0,
    c: float = 103.0,
    v: float = 1234.5,
) -> list[float]:
    """Build one CCXT-shape OHLCV row: [ts_ms, open, high, low, close, volume]."""
    return [ts_ms, o, h, lo, c, v]


def _make_provider(
    fetch_ohlcv_return: Any = None,
    fetch_ohlcv_side_effect: BaseException | None = None,
) -> tuple[BinanceProvider, AsyncMock]:
    """Construct a BinanceProvider whose underlying ccxt client is a mock.

    Returns the provider and the mock fetch_ohlcv coroutine so tests can
    assert on call arguments.
    """
    mock_client = MagicMock()
    mock_client.fetch_ohlcv = AsyncMock(
        return_value=fetch_ohlcv_return,
        side_effect=fetch_ohlcv_side_effect,
    )
    mock_client.close = AsyncMock()
    provider = BinanceProvider(client=mock_client)
    return provider, mock_client.fetch_ohlcv


# ---------------------------------------------------------------------------
# Successful path
# ---------------------------------------------------------------------------


class TestFetchMarketSnapshotSuccess:
    async def test_single_timeframe_returns_validated_snapshot(self) -> None:
        rows = [
            _ccxt_row(1_700_000_000_000),
            _ccxt_row(1_700_000_000_000 + 4 * 3600 * 1000),  # +4h
            _ccxt_row(1_700_000_000_000 + 8 * 3600 * 1000),  # +8h
        ]
        provider, mock_call = _make_provider(fetch_ohlcv_return=rows)

        snapshot = await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])

        assert snapshot.symbol == "BTCUSDT"
        assert snapshot.venue == "binance"
        assert Timeframe.H4 in snapshot.klines
        assert len(snapshot.klines[Timeframe.H4]) == 3
        # CCXT received the raw timeframe string, not the enum.
        mock_call.assert_awaited_once_with("BTCUSDT", "4h", limit=200)

    async def test_kline_timestamps_normalized_to_utc(self) -> None:
        rows = [_ccxt_row(1_700_000_000_000)]
        provider, _ = _make_provider(fetch_ohlcv_return=rows)
        snapshot = await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])
        kline = snapshot.klines[Timeframe.H4][0]
        assert kline.open_time.tzinfo is not None
        # 1.7e12 ms = 2023-11-14T22:13:20Z, just sanity-check the year.
        assert kline.open_time.year == 2023

    async def test_limit_passed_through_to_ccxt(self) -> None:
        provider, mock_call = _make_provider(fetch_ohlcv_return=[_ccxt_row(1_700_000_000_000)])
        await provider.fetch_market_snapshot("ETHUSDT", [Timeframe.H4], limit=50)
        mock_call.assert_awaited_once_with("ETHUSDT", "4h", limit=50)


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    async def test_empty_timeframes_list_rejected(self) -> None:
        provider, _ = _make_provider(fetch_ohlcv_return=[])
        with pytest.raises(ProviderInvalidResponseError, match="at least one"):
            await provider.fetch_market_snapshot("BTCUSDT", [])

    async def test_zero_limit_rejected(self) -> None:
        provider, _ = _make_provider(fetch_ohlcv_return=[])
        with pytest.raises(ProviderInvalidResponseError, match="limit"):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4], limit=0)

    async def test_negative_limit_rejected(self) -> None:
        provider, _ = _make_provider(fetch_ohlcv_return=[])
        with pytest.raises(ProviderInvalidResponseError, match="limit"):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4], limit=-5)


# ---------------------------------------------------------------------------
# Malformed CCXT responses
# ---------------------------------------------------------------------------


class TestMalformedResponses:
    async def test_non_list_response_rejected(self) -> None:
        provider, _ = _make_provider(fetch_ohlcv_return="not a list")
        with pytest.raises(ProviderInvalidResponseError, match="expected list"):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])

    async def test_short_row_rejected(self) -> None:
        # 5 columns instead of 6 — corrupted vendor response.
        provider, _ = _make_provider(
            fetch_ohlcv_return=[[1_700_000_000_000, 100.0, 105.0, 99.0, 103.0]]
        )
        with pytest.raises(ProviderInvalidResponseError, match="malformed candle"):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])

    async def test_non_numeric_price_rejected(self) -> None:
        provider, _ = _make_provider(
            fetch_ohlcv_return=[[1_700_000_000_000, "oops", 105.0, 99.0, 103.0, 1.0]],
        )
        with pytest.raises(ProviderInvalidResponseError, match="failed validation"):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])

    async def test_inverted_high_low_rejected(self) -> None:
        # high < low — vendor data corruption; Kline validator catches it.
        provider, _ = _make_provider(
            fetch_ohlcv_return=[_ccxt_row(1_700_000_000_000, h=95.0, lo=99.0)],
        )
        with pytest.raises(ProviderInvalidResponseError, match="failed validation"):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])

    async def test_non_chronological_rows_rejected(self) -> None:
        # The MarketSnapshot model rejects out-of-order candles; that
        # propagates as ProviderInvalidResponseError from the adapter.
        rows = [
            _ccxt_row(1_700_000_000_000 + 4 * 3600 * 1000),
            _ccxt_row(1_700_000_000_000),  # earlier than the row above
        ]
        provider, _ = _make_provider(fetch_ohlcv_return=rows)
        with pytest.raises(ProviderInvalidResponseError, match="validation failed"):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])


# ---------------------------------------------------------------------------
# CCXT exception translation
# ---------------------------------------------------------------------------


class TestExceptionTranslation:
    async def test_request_timeout_maps_to_timeout(self) -> None:
        provider, _ = _make_provider(fetch_ohlcv_side_effect=ccxt.RequestTimeout("slow"))
        with pytest.raises(ProviderTimeoutError) as exc_info:
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])
        assert exc_info.value.provider == "binance"

    async def test_rate_limit_maps_to_rate_limit(self) -> None:
        provider, _ = _make_provider(fetch_ohlcv_side_effect=ccxt.RateLimitExceeded("418"))
        with pytest.raises(ProviderRateLimitError):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])

    async def test_ddos_protection_maps_to_rate_limit(self) -> None:
        # DDoSProtection is the Cloudflare-style soft block; same handling as 429.
        provider, _ = _make_provider(fetch_ohlcv_side_effect=ccxt.DDoSProtection("cf"))
        with pytest.raises(ProviderRateLimitError):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])

    async def test_network_error_maps_to_unavailable(self) -> None:
        provider, _ = _make_provider(fetch_ohlcv_side_effect=ccxt.NetworkError("dns"))
        with pytest.raises(ProviderUnavailableError):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])

    async def test_bad_response_maps_to_invalid(self) -> None:
        provider, _ = _make_provider(
            fetch_ohlcv_side_effect=ccxt.BadResponse("html instead of json")
        )
        with pytest.raises(ProviderInvalidResponseError):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])

    async def test_exchange_error_maps_to_unavailable(self) -> None:
        # Generic ExchangeError (e.g., "Invalid symbol") — treat as unavailable
        # so the orchestrator does not crash on a bad watchlist entry.
        provider, _ = _make_provider(fetch_ohlcv_side_effect=ccxt.ExchangeError("bad symbol"))
        with pytest.raises(ProviderUnavailableError):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _make_full_client(
    *,
    ohlcv_by_tf: dict[str, Any] | None = None,
    funding: Any = None,
    oi: Any = None,
) -> MagicMock:
    """A mock ccxt client with per-timeframe klines + funding/OI endpoints."""
    client = MagicMock()

    async def _ohlcv(symbol: str, tf: str, limit: int = 200) -> Any:
        table = ohlcv_by_tf or {}
        return table.get(tf, [_ccxt_row(1_700_000_000_000)])

    client.fetch_ohlcv = AsyncMock(side_effect=_ohlcv)
    client.fetch_funding_rate = AsyncMock(return_value=funding)
    client.fetch_open_interest = AsyncMock(return_value=oi)
    client.close = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Multi-timeframe (Step 2.2)
# ---------------------------------------------------------------------------


class TestMultiTimeframe:
    async def test_fetches_all_requested_timeframes(self) -> None:
        rows = [
            _ccxt_row(1_700_000_000_000),
            _ccxt_row(1_700_000_000_000 + 4 * 3600 * 1000),
        ]
        client = _make_full_client(ohlcv_by_tf={"1d": rows, "4h": rows, "1h": rows})
        provider = BinanceProvider(client=client)

        snapshot = await provider.fetch_market_snapshot(
            "BTCUSDT", [Timeframe.D1, Timeframe.H4, Timeframe.H1]
        )

        assert set(snapshot.klines) == {Timeframe.D1, Timeframe.H4, Timeframe.H1}
        assert client.fetch_ohlcv.await_count == 3

    async def test_one_timeframe_failure_propagates(self) -> None:
        async def _ohlcv(symbol: str, tf: str, limit: int = 200) -> Any:
            if tf == "1h":
                raise ccxt.NetworkError("1h down")
            return [_ccxt_row(1_700_000_000_000)]

        client = MagicMock()
        client.fetch_ohlcv = AsyncMock(side_effect=_ohlcv)
        client.close = AsyncMock()
        provider = BinanceProvider(client=client)

        with pytest.raises(ProviderUnavailableError):
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4, Timeframe.H1])


# ---------------------------------------------------------------------------
# Derivatives: funding rate + open interest (Step 2.2)
# ---------------------------------------------------------------------------


class TestDerivatives:
    async def test_fetch_funding_rate(self) -> None:
        provider = BinanceProvider(client=_make_full_client(funding={"fundingRate": 0.0001}))
        assert await provider.fetch_funding_rate("BTCUSDT") == pytest.approx(0.0001)

    async def test_fetch_funding_rate_none_when_absent(self) -> None:
        provider = BinanceProvider(client=_make_full_client(funding={}))
        assert await provider.fetch_funding_rate("BTCUSDT") is None

    async def test_fetch_open_interest_amount_key(self) -> None:
        provider = BinanceProvider(client=_make_full_client(oi={"openInterestAmount": 12345.6}))
        assert await provider.fetch_open_interest("BTCUSDT") == pytest.approx(12345.6)

    async def test_fetch_open_interest_fallback_key(self) -> None:
        provider = BinanceProvider(client=_make_full_client(oi={"openInterest": 999.0}))
        assert await provider.fetch_open_interest("BTCUSDT") == pytest.approx(999.0)

    async def test_include_derivatives_populates_snapshot(self) -> None:
        client = _make_full_client(
            funding={"fundingRate": -0.0002}, oi={"openInterestAmount": 5000.0}
        )
        provider = BinanceProvider(client=client)
        snapshot = await provider.fetch_market_snapshot(
            "BTCUSDT", [Timeframe.H4], include_derivatives=True
        )
        assert snapshot.funding_rate == pytest.approx(-0.0002)
        assert snapshot.open_interest == pytest.approx(5000.0)

    async def test_derivatives_off_by_default(self) -> None:
        client = _make_full_client(funding={"fundingRate": 0.1}, oi={"openInterestAmount": 1.0})
        provider = BinanceProvider(client=client)
        snapshot = await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])
        assert snapshot.funding_rate is None
        assert snapshot.open_interest is None
        client.fetch_funding_rate.assert_not_awaited()

    async def test_derivative_failure_degrades_to_none(self) -> None:
        client = _make_full_client(oi={"openInterestAmount": 5000.0})
        client.fetch_funding_rate = AsyncMock(side_effect=ccxt.NetworkError("down"))
        provider = BinanceProvider(client=client)
        snapshot = await provider.fetch_market_snapshot(
            "BTCUSDT", [Timeframe.H4], include_derivatives=True
        )
        # Funding failed -> None; OI still present. The snapshot survives.
        assert snapshot.funding_rate is None
        assert snapshot.open_interest == pytest.approx(5000.0)


# ---------------------------------------------------------------------------
# Rate limiting (Step 2.2)
# ---------------------------------------------------------------------------


class TestRateLimiting:
    async def test_acquires_kline_weight_for_limit(self) -> None:
        bucket = MagicMock()
        bucket.acquire = AsyncMock()
        provider = BinanceProvider(client=_make_full_client(), rate_limiter=bucket)
        await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4], limit=200)
        # limit 200 -> Binance kline weight 2.
        bucket.acquire.assert_awaited_with(2)

    def test_kline_weight_scales_with_limit(self) -> None:
        assert BinanceProvider._klines_weight(100) == 1
        assert BinanceProvider._klines_weight(200) == 2
        assert BinanceProvider._klines_weight(1000) == 5
        assert BinanceProvider._klines_weight(1500) == 10


class TestLifecycle:
    async def test_aclose_calls_underlying_close(self) -> None:
        provider, _ = _make_provider(fetch_ohlcv_return=[_ccxt_row(1_700_000_000_000)])
        # Pull out the mock client so we can assert close was awaited.
        mock_client = provider._client
        await provider.aclose()
        mock_client.close.assert_awaited_once()  # type: ignore[union-attr]

    async def test_async_context_manager_closes_on_exit(self) -> None:
        mock_client = MagicMock()
        mock_client.fetch_ohlcv = AsyncMock(return_value=[_ccxt_row(1_700_000_000_000)])
        mock_client.close = AsyncMock()
        async with BinanceProvider(client=mock_client) as provider:
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])
        mock_client.close.assert_awaited_once()
