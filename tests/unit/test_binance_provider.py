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
