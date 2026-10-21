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

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import ccxt
import pytest

from src.providers import (
    BinanceProvider,
    ProviderError,
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


def _provider_with_ohlcv(*, side_effect: Any) -> tuple[BinanceProvider, MagicMock]:
    """Construct a BinanceProvider whose ccxt ``fetch_ohlcv`` follows ``side_effect``.

    Unlike ``_make_provider`` this returns the whole mock exchange so paging tests
    can inspect every ``fetch_ohlcv`` call (``since``/``limit`` per page).
    """
    exchange = MagicMock()
    exchange.fetch_ohlcv = AsyncMock(side_effect=side_effect)
    exchange.close = AsyncMock()
    provider = BinanceProvider(client=exchange)
    return provider, exchange


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
        # The ccxt session reference is nulled so Exchange.__del__ does not log its
        # "release all resources" warning at GC (Lambda container freeze).
        assert provider._client.session is None

    async def test_async_context_manager_closes_on_exit(self) -> None:
        mock_client = MagicMock()
        mock_client.fetch_ohlcv = AsyncMock(return_value=[_ccxt_row(1_700_000_000_000)])
        mock_client.close = AsyncMock()
        async with BinanceProvider(client=mock_client) as provider:
            await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])
        mock_client.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Market-loading seam (Slice 4 Step 4.3: dashboard caches markets per container).
# The scan pipeline passes no new argument; its client must be built exactly as before.
# ---------------------------------------------------------------------------


class TestMarketSeam:
    async def test_default_constructs_client_exactly_as_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.providers import binance as binance_module

        built = MagicMock()
        built.close = AsyncMock()
        factory = MagicMock(return_value=built)
        monkeypatch.setattr(binance_module.ccxt_async, "binance", factory)
        provider = BinanceProvider()
        try:
            # The one and only constructor argument, byte-for-byte the pre-4.3 config.
            factory.assert_called_once_with(
                {"enableRateLimit": True, "options": {"defaultType": "future"}}
            )
            built.set_markets.assert_not_called()
            assert provider._client is built
            assert built.session is provider._session  # ThreadedResolver session attached
        finally:
            await provider.aclose()

    async def test_default_real_client_options_unchanged(self) -> None:
        provider = BinanceProvider()
        try:
            client = provider._client
            assert client.options["defaultType"] == "future"
            # ccxt's own default market types stay in force (spot + linear + inverse).
            assert client.options["fetchMarkets"]["types"] == ["spot", "linear", "inverse"]
            assert not client.markets
            assert provider.loaded_markets() is None
        finally:
            await provider.aclose()

    async def test_market_types_restricts_fetch_markets(self) -> None:
        provider = BinanceProvider(market_types=["linear"])
        try:
            client = provider._client
            assert client.options["fetchMarkets"]["types"] == ["linear"]
            assert client.options["defaultType"] == "future"
            assert client.enableRateLimit is True
        finally:
            await provider.aclose()

    async def test_markets_are_set_and_fetch_klines_skips_market_load(self) -> None:
        markets: dict[str, Any] = {"BTC/USDT:USDT": {"id": "BTCUSDT"}}
        exchange = MagicMock()
        start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        exchange.fetch_ohlcv = AsyncMock(
            return_value=[[int(start.timestamp() * 1000), 1.0, 2.0, 0.5, 1.5, 3.0]]
        )
        exchange.load_markets = AsyncMock()
        exchange.close = AsyncMock()
        exchange.markets = markets
        provider = BinanceProvider(client=exchange, markets=markets)
        exchange.set_markets.assert_called_once_with(markets)
        out = await provider.fetch_klines(
            "BTCUSDT", Timeframe.M5, start=start, end=start + timedelta(minutes=5)
        )
        assert len(out) == 1
        exchange.load_markets.assert_not_awaited()
        assert provider.loaded_markets() is markets

    async def test_markets_preloaded_real_client_never_loads_over_network(self) -> None:
        # A real ccxt client: once markets are set, `load_markets` returns them without
        # calling `fetch_markets` (the multi-MB exchangeInfo download).
        seed = BinanceProvider(market_types=["linear"])
        try:
            seed._client.fetch_markets = AsyncMock(  # type: ignore[method-assign]
                return_value=[_linear_market()]
            )
            seed._client.fetch_currencies = AsyncMock(  # type: ignore[method-assign]
                return_value=None
            )
            await seed._client.load_markets()
            markets = seed.loaded_markets()
            assert markets is not None and "BTC/USDT:USDT" in markets
        finally:
            await seed.aclose()

        provider = BinanceProvider(market_types=["linear"], markets=markets)
        try:
            provider._client.fetch_markets = AsyncMock(  # type: ignore[method-assign]
                side_effect=AssertionError("network market load")
            )
            loaded = await provider._client.load_markets()
            assert "BTC/USDT:USDT" in loaded
            assert provider._client.market("BTCUSDT")["id"] == "BTCUSDT"
            provider._client.fetch_markets.assert_not_awaited()
        finally:
            await provider.aclose()


def _linear_market() -> dict[str, Any]:
    """A minimal ccxt unified market for the BTCUSDT perpetual."""
    return {
        "id": "BTCUSDT",
        "symbol": "BTC/USDT:USDT",
        "base": "BTC",
        "quote": "USDT",
        "settle": "USDT",
        "baseId": "BTC",
        "quoteId": "USDT",
        "settleId": "USDT",
        "type": "swap",
        "spot": False,
        "margin": False,
        "swap": True,
        "future": False,
        "option": False,
        "active": True,
        "contract": True,
        "linear": True,
        "inverse": False,
        "contractSize": 1.0,
        "precision": {"amount": 0.001, "price": 0.1},
        "limits": {"amount": {"min": 0.001, "max": None}},
        "info": {},
    }


# ---------------------------------------------------------------------------
# Historical candle paging (Critic v0, Task 4)
# ---------------------------------------------------------------------------


class TestFetchKlines:
    def _rows(self, start_ms: int, n: int, step_ms: int = 300_000) -> list[list[float]]:
        return [[start_ms + i * step_ms, 100.0, 101.0, 99.0, 100.5, 1.0] for i in range(n)]

    async def test_pages_until_end_and_filters_window(self) -> None:
        start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        end = start + timedelta(minutes=5 * 1500)
        start_ms = int(start.timestamp() * 1000)
        page1 = self._rows(start_ms, 1000)
        page2 = self._rows(start_ms + 1000 * 300_000, 1000)  # overshoots end
        provider, exchange = _provider_with_ohlcv(side_effect=[page1, page2])
        klines = await provider.fetch_klines("BTCUSDT", Timeframe.M5, start=start, end=end)
        assert len(klines) == 1500
        assert klines[0].open_time == start
        assert klines[-1].open_time < end
        assert exchange.fetch_ohlcv.await_count == 2
        first_call = exchange.fetch_ohlcv.await_args_list[0]
        assert first_call.args == ("BTCUSDT", "5m")
        assert first_call.kwargs["since"] == start_ms
        second_call = exchange.fetch_ohlcv.await_args_list[1]
        assert second_call.kwargs["since"] == start_ms + 1000 * 300_000
        assert second_call.kwargs["limit"] == 1000

    async def test_result_is_ascending_without_duplicates(self) -> None:
        start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        end = start + timedelta(minutes=5 * 1200)
        start_ms = int(start.timestamp() * 1000)
        page1 = self._rows(start_ms, 1000)
        page2 = self._rows(start_ms + 1000 * 300_000, 1000)
        provider, _ = _provider_with_ohlcv(side_effect=[page1, page2])
        klines = await provider.fetch_klines("BTCUSDT", Timeframe.M5, start=start, end=end)
        assert len(klines) == 1200
        times = [k.open_time for k in klines]
        assert times == sorted(times)
        assert len(set(times)) == len(times)

    async def test_stops_on_short_page(self) -> None:
        start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=30)
        rows = self._rows(int(start.timestamp() * 1000), 10)
        provider, exchange = _provider_with_ohlcv(side_effect=[rows])
        klines = await provider.fetch_klines("BTCUSDT", Timeframe.M5, start=start, end=end)
        assert len(klines) == 10
        assert exchange.fetch_ohlcv.await_count == 1

    async def test_stops_on_empty_page(self) -> None:
        start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=30)
        provider, exchange = _provider_with_ohlcv(side_effect=[[]])
        klines = await provider.fetch_klines("BTCUSDT", Timeframe.M5, start=start, end=end)
        assert klines == []
        assert exchange.fetch_ohlcv.await_count == 1

    async def test_drops_candles_before_start(self) -> None:
        # Binance may return the candle *containing* ``since`` when it is not
        # aligned to the timeframe grid; anything before ``start`` is dropped.
        start = datetime(2026, 7, 1, 0, 2, tzinfo=UTC)  # not on the 5m grid
        end = start + timedelta(minutes=20)
        grid_ms = int(datetime(2026, 7, 1, 0, 0, tzinfo=UTC).timestamp() * 1000)
        rows = self._rows(grid_ms, 6)  # 00:00 .. 00:25
        provider, _ = _provider_with_ohlcv(side_effect=[rows])
        klines = await provider.fetch_klines("BTCUSDT", Timeframe.M5, start=start, end=end)
        assert [k.open_time.minute for k in klines] == [5, 10, 15, 20]

    async def test_uses_timeframe_step_for_paging(self) -> None:
        start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        end = start + timedelta(hours=1500)
        start_ms = int(start.timestamp() * 1000)
        page1 = self._rows(start_ms, 1000, step_ms=3_600_000)
        page2 = self._rows(start_ms + 1000 * 3_600_000, 500, step_ms=3_600_000)
        provider, exchange = _provider_with_ohlcv(side_effect=[page1, page2])
        klines = await provider.fetch_klines("BTCUSDT", Timeframe.H1, start=start, end=end)
        assert len(klines) == 1500
        assert exchange.fetch_ohlcv.await_args_list[0].args == ("BTCUSDT", "1h")
        second_call = exchange.fetch_ohlcv.await_args_list[1]
        assert second_call.kwargs["since"] == start_ms + 1000 * 3_600_000

    async def test_meters_page_weight_on_rate_limiter(self) -> None:
        bucket = MagicMock()
        bucket.acquire = AsyncMock()
        exchange = MagicMock()
        start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        exchange.fetch_ohlcv = AsyncMock(return_value=self._rows(int(start.timestamp() * 1000), 3))
        exchange.close = AsyncMock()
        provider = BinanceProvider(client=exchange, rate_limiter=bucket)
        await provider.fetch_klines(
            "BTCUSDT", Timeframe.M5, start=start, end=start + timedelta(hours=1)
        )
        # A 1000-candle page costs Binance kline weight 5.
        bucket.acquire.assert_awaited_once_with(5)

    async def test_rejects_inverted_window(self) -> None:
        provider, exchange = _provider_with_ohlcv(side_effect=[[]])
        t = datetime(2026, 7, 1, tzinfo=UTC)
        with pytest.raises(ValueError):
            await provider.fetch_klines("BTCUSDT", Timeframe.M5, start=t, end=t)
        with pytest.raises(ValueError):
            await provider.fetch_klines(
                "BTCUSDT", Timeframe.M5, start=t, end=t - timedelta(minutes=5)
            )
        exchange.fetch_ohlcv.assert_not_awaited()

    async def test_translates_ccxt_errors(self) -> None:
        provider, _ = _provider_with_ohlcv(side_effect=ccxt.NetworkError("down"))
        t = datetime(2026, 7, 1, tzinfo=UTC)
        with pytest.raises(ProviderError):
            await provider.fetch_klines(
                "BTCUSDT", Timeframe.M5, start=t, end=t + timedelta(hours=1)
            )

    async def test_translates_rate_limit_error_specifically(self) -> None:
        provider, _ = _provider_with_ohlcv(side_effect=ccxt.RateLimitExceeded("slow down"))
        t = datetime(2026, 7, 1, tzinfo=UTC)
        with pytest.raises(ProviderRateLimitError):
            await provider.fetch_klines(
                "BTCUSDT", Timeframe.M5, start=t, end=t + timedelta(hours=1)
            )

    async def test_malformed_row_raises_invalid_response(self) -> None:
        provider, _ = _provider_with_ohlcv(side_effect=[[[1, 2, 3]]])
        t = datetime(2026, 7, 1, tzinfo=UTC)
        with pytest.raises(ProviderInvalidResponseError):
            await provider.fetch_klines(
                "BTCUSDT", Timeframe.M5, start=t, end=t + timedelta(hours=1)
            )
