"""Live integration test for src.providers.binance.BinanceProvider.

This test actually calls Binance Futures. It is marked `integration` and
*skipped by default* — CI runs `pytest -m "not integration"`. Run locally with:

    pytest -m integration tests/integration/test_binance_integration.py

If Binance is geo-blocked from your egress IP or temporarily unavailable, the
test will fail. That is intentional — it is the canary that detects when our
adapter expectations no longer match the live API.
"""

from __future__ import annotations

import pytest

from src.providers import BinanceProvider, Timeframe

pytestmark = pytest.mark.integration


async def test_real_binance_4h_btcusdt() -> None:
    async with BinanceProvider() as provider:
        snapshot = await provider.fetch_market_snapshot("BTCUSDT", [Timeframe.H4], limit=10)

    assert snapshot.symbol == "BTCUSDT"
    assert snapshot.venue == "binance"
    candles = snapshot.klines[Timeframe.H4]
    assert 1 <= len(candles) <= 10, "Binance should return up to the requested limit"

    # Sanity-check the most recent candle has plausible BTC pricing
    # (any value > $1,000 is fine — we just want to detect totally broken parsing).
    latest = candles[-1]
    assert latest.close > 1000.0
    assert latest.high >= latest.close
    assert latest.low <= latest.close


async def test_real_binance_multi_timeframe_and_derivatives() -> None:
    """Step 2.2: fetch all SMC timeframes concurrently plus funding/OI in one call."""
    timeframes = [Timeframe.D1, Timeframe.H4, Timeframe.H1, Timeframe.M15, Timeframe.M5]
    async with BinanceProvider() as provider:
        snapshot = await provider.fetch_market_snapshot(
            "BTCUSDT", timeframes, limit=50, include_derivatives=True
        )

    # Every requested timeframe came back with candles.
    for tf in timeframes:
        assert tf in snapshot.klines, f"missing {tf.value}"
        assert snapshot.klines[tf], f"{tf.value} returned no candles"

    # Funding rate is a small decimal; open interest is positive.
    assert snapshot.funding_rate is not None
    assert -0.1 < snapshot.funding_rate < 0.1
    assert snapshot.open_interest is not None
    assert snapshot.open_interest > 0.0
