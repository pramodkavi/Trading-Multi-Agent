"""`GET /api/market/klines` -- a read-only candle proxy for the dashboard charts (Step 4.3).

- `symbol` must be on the watchlist (`DashboardSettings.scan_symbols`); anything
  else is 400 `unknown_symbol`, raised **before** a provider is built (the
  dependency hands the route a factory, not a provider).
- `tf` is a `Timeframe` value (`1d`, `4h`, `1h`, `15m`, `5m`; default `4h`);
  `limit` is 1..500 (default 200) and out-of-range values are rejected with 422.
- The window ends at the request clock (`overview.utcnow()`), so the last candle
  may still be forming. The provider work is bounded by `market.MARKET_BUDGET_S`
  (12 s); a blown budget or any `ProviderError` becomes 502 `market_unavailable`
  (`errors.py`). No database access.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from src.dashboard.api import market
from src.dashboard.api import overview as assembly
from src.dashboard.api.deps import SettingsDep
from src.dashboard.api.errors import MARKET_ROUTE_RESPONSES, UnknownSymbol
from src.dashboard.api.market import ProviderFactoryDep
from src.dashboard.api.schemas import ErrorResponse, KlineOut, KlinesResponse
from src.providers.base import Timeframe

router = APIRouter(tags=["market"], responses=MARKET_ROUTE_RESPONSES)

DEFAULT_KLINES = 200
MAX_KLINES = 500

_UNKNOWN_SYMBOL: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "`unknown_symbol`: not on the watchlist."}
}


@router.get("/api/market/klines", response_model=KlinesResponse, responses=_UNKNOWN_SYMBOL)
async def klines(
    settings: SettingsDep,
    factory: ProviderFactoryDep,
    symbol: Annotated[str, Query(min_length=1, max_length=20, description="e.g. BTCUSDT")],
    tf: Annotated[Timeframe, Query(description="Candle timeframe.")] = Timeframe.H4,
    limit: Annotated[int, Query(ge=1, le=MAX_KLINES)] = DEFAULT_KLINES,
) -> KlinesResponse:
    """The most recent ``limit`` candles of a watchlist symbol, ascending."""
    if symbol not in settings.scan_symbols:
        raise UnknownSymbol  # -> 400 via errors.py; no provider has been built
    now = assembly.utcnow()
    candles = await market.fetch_recent_klines(factory, symbol, tf, limit=limit, end=now)
    return KlinesResponse(
        symbol=symbol,
        timeframe=tf,
        klines=[KlineOut.model_validate(k.model_dump()) for k in candles],
    )
