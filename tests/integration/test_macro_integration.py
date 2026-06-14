"""Live integration tests for the macro providers (Step 2.3).

Marked `integration` (skipped in CI's `-m "not integration"`) AND skipped unless
the relevant API key is present in the environment, so a local run without keys
is a clean skip rather than a failure.

    FRED_API_KEY=...        pytest -m integration tests/integration/test_macro_integration.py
    TWELVE_DATA_API_KEY=...
"""

from __future__ import annotations

import os

import pytest

from src.providers import FREDProvider, MacroContext, TwelveDataProvider

pytestmark = pytest.mark.integration

_FRED_KEY = os.environ.get("FRED_API_KEY", "")
_TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")


@pytest.mark.skipif(not _FRED_KEY, reason="FRED_API_KEY not set")
async def test_real_fred_macro_context() -> None:
    async with FREDProvider(api_key=_FRED_KEY) as provider:
        ctx = await provider.fetch_macro_context()
    assert isinstance(ctx, MacroContext), "FRED should return data with a valid key"
    # The broad dollar index sits roughly in the 90-140 band; 10Y yield is a small percent.
    assert ctx.dxy is not None and 50.0 < ctx.dxy < 200.0
    assert ctx.us10y_yield is not None and -1.0 < ctx.us10y_yield < 25.0


@pytest.mark.skipif(not _TWELVE_DATA_KEY, reason="TWELVE_DATA_API_KEY not set")
async def test_real_twelvedata_macro_context() -> None:
    async with TwelveDataProvider(api_key=_TWELVE_DATA_KEY) as provider:
        ctx = await provider.fetch_macro_context()
    assert isinstance(ctx, MacroContext), "Twelve Data should return data with a valid key"
    assert ctx.spx is not None and ctx.spx > 100.0
    assert ctx.vix is not None and ctx.vix > 0.0
