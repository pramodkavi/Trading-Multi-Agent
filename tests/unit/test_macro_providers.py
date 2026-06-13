"""Unit tests for the macro providers (Step 2.3), driven by httpx.MockTransport.

No network: a mock transport returns canned FRED / Twelve Data responses so we can
verify parsing, partial/total graceful degradation (NoMacroData), the unsupported
market-snapshot path, lifecycle, and httpx-error translation.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from src.providers import (
    FREDProvider,
    MacroContext,
    NoMacroData,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    Timeframe,
    TwelveDataProvider,
)

_Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: _Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fred_handler(values: dict[str, str]) -> _Handler:
    """FRED observations handler returning `values[series_id]` (or '.' if absent)."""

    def handler(request: httpx.Request) -> httpx.Response:
        series_id = request.url.params.get("series_id", "")
        value = values.get(series_id, ".")
        return httpx.Response(200, json={"observations": [{"date": "2026-06-12", "value": value}]})

    return handler


def _twelvedata_handler(prices: dict[str, str]) -> _Handler:
    """Twelve Data /price handler; a missing symbol returns an error-status body."""

    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params.get("symbol", "")
        if symbol in prices:
            return httpx.Response(200, json={"price": prices[symbol]})
        return httpx.Response(200, json={"status": "error", "message": f"no data for {symbol}"})

    return handler


# ---------------------------------------------------------------------------
# FRED
# ---------------------------------------------------------------------------


class TestFREDProvider:
    async def test_success_populates_dollar_yield_fedfunds(self) -> None:
        handler = _fred_handler({"DTWEXBGS": "103.5", "DGS10": "4.25", "DFF": "5.33"})
        async with _client(handler) as client:
            provider = FREDProvider(api_key="k", client=client)
            ctx = await provider.fetch_macro_context()
        assert isinstance(ctx, MacroContext)
        assert ctx.dxy == pytest.approx(103.5)
        assert ctx.us10y_yield == pytest.approx(4.25)
        assert ctx.fed_funds == pytest.approx(5.33)
        assert ctx.spx is None and ctx.vix is None  # FRED does not own these

    async def test_missing_observation_degrades_that_field(self) -> None:
        # DGS10 missing ("." sentinel) -> us10y None, others still present.
        handler = _fred_handler({"DTWEXBGS": "103.5", "DFF": "5.33"})
        async with _client(handler) as client:
            ctx = await FREDProvider(api_key="k", client=client).fetch_macro_context()
        assert isinstance(ctx, MacroContext)
        assert ctx.dxy == pytest.approx(103.5)
        assert ctx.us10y_yield is None

    async def test_total_failure_returns_sentinel(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="server error")

        async with _client(handler) as client:
            result = await FREDProvider(api_key="k", client=client).fetch_macro_context()
        assert isinstance(result, NoMacroData)
        assert result.provider == "fred"


# ---------------------------------------------------------------------------
# Twelve Data
# ---------------------------------------------------------------------------


class TestTwelveDataProvider:
    async def test_success_populates_spx_vix(self) -> None:
        handler = _twelvedata_handler({"SPX": "5400.50", "VIX": "18.20"})
        async with _client(handler) as client:
            ctx = await TwelveDataProvider(api_key="k", client=client).fetch_macro_context()
        assert isinstance(ctx, MacroContext)
        assert ctx.spx == pytest.approx(5400.50)
        assert ctx.vix == pytest.approx(18.20)
        assert ctx.dxy is None  # not FRED's job nor this provider's

    async def test_partial_when_one_symbol_errors(self) -> None:
        handler = _twelvedata_handler({"SPX": "5400.50"})  # VIX -> error status
        async with _client(handler) as client:
            ctx = await TwelveDataProvider(api_key="k", client=client).fetch_macro_context()
        assert isinstance(ctx, MacroContext)
        assert ctx.spx == pytest.approx(5400.50)
        assert ctx.vix is None

    async def test_total_failure_returns_sentinel(self) -> None:
        handler = _twelvedata_handler({})  # both symbols error
        async with _client(handler) as client:
            result = await TwelveDataProvider(api_key="k", client=client).fetch_macro_context()
        assert isinstance(result, NoMacroData)
        assert result.provider == "twelvedata"


# ---------------------------------------------------------------------------
# Interface / lifecycle
# ---------------------------------------------------------------------------


class TestInterfaceAndLifecycle:
    async def test_market_snapshot_unsupported(self) -> None:
        async with _client(_fred_handler({})) as client:
            provider = FREDProvider(api_key="k", client=client)
            with pytest.raises(NotImplementedError, match="macro provider"):
                await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4])

    async def test_injected_client_not_closed_by_aclose(self) -> None:
        async with _client(_fred_handler({})) as client:
            provider = FREDProvider(api_key="k", client=client)
            await provider.aclose()
            assert not client.is_closed  # provider does not own an injected client

    async def test_owned_client_closed_by_aclose(self) -> None:
        provider = FREDProvider(api_key="k")  # constructs and owns its own client
        await provider.aclose()
        assert provider._client.is_closed


# ---------------------------------------------------------------------------
# httpx error translation (via the shared _get_json)
# ---------------------------------------------------------------------------


class TestErrorTranslation:
    async def test_429_maps_to_rate_limit(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="too many requests")

        async with _client(handler) as client:
            provider = FREDProvider(api_key="k", client=client)
            with pytest.raises(ProviderRateLimitError):
                await provider._get_json("/x", params={})

    async def test_500_maps_to_unavailable(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="maintenance")

        async with _client(handler) as client:
            provider = FREDProvider(api_key="k", client=client)
            with pytest.raises(ProviderUnavailableError):
                await provider._get_json("/x", params={})

    async def test_timeout_maps_to_timeout(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=_request)

        async with _client(handler) as client:
            provider = FREDProvider(api_key="k", client=client)
            with pytest.raises(ProviderTimeoutError):
                await provider._get_json("/x", params={})
