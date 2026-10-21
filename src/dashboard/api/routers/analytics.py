"""`/api/analytics/*` -- P6 performance analytics (design §4.2 P6, §5.2a).

- `GET /api/analytics/win-rate?by=tag|session|strategy|time&since&until` --
  published win rate per bucket (`time` = ISO week).
- `GET /api/analytics/skeptic-value?since&until` -- respected / overruled with
  caveat / overruled, NoMacroData runs excluded and counted.
- `GET /api/analytics/equity-curve?since&until` -- the published R curve plus the
  vetoed / gate-rejected counterfactual curves (replays only).

The counting rules (outcome source, decisive, R per outcome) live in
`analytics.py`. Every body carries `source_counts` (replayed vs
forecaster-judged). The period defaults to the last 90 days and is capped at 365
(`period.py`: a longer, empty or inverted range is 422 before any SQL runs);
naive timestamps are read as UTC. Each route makes one keyset-paged read of the
minimal outcome projection (`queries.list_outcome_rows`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from src.dashboard.api import overview as assembly
from src.dashboard.api.analytics import (
    WinRateBy,
    equity_curves,
    skeptic_value,
    source_counts,
    win_rate_by,
)
from src.dashboard.api.deps import StoreDep
from src.dashboard.api.errors import DATA_ROUTE_RESPONSES
from src.dashboard.api.period import resolve_period
from src.dashboard.api.queries import DashboardReader, OutcomeRow, list_outcome_rows
from src.dashboard.api.schemas import (
    EquityCurveResponse,
    SkepticValueResponse,
    WinRateResponse,
)

router = APIRouter(tags=["analytics"], responses=DATA_ROUTE_RESPONSES)

DEFAULT_ANALYTICS_DAYS = 90

SinceQuery = Annotated[datetime | None, Query(description="created_at >= since")]
UntilQuery = Annotated[datetime | None, Query(description="created_at < until")]


async def _rows(
    store: DashboardReader, since: datetime | None, until: datetime | None
) -> tuple[datetime, datetime, list[OutcomeRow]]:
    start, end = resolve_period(
        since, until, now=assembly.utcnow(), default_days=DEFAULT_ANALYTICS_DAYS
    )
    return start, end, await list_outcome_rows(store, since=start, until=end)


@router.get("/api/analytics/win-rate", response_model=WinRateResponse)
async def win_rate(
    store: StoreDep,
    since: SinceQuery = None,
    until: UntilQuery = None,
    by: Annotated[WinRateBy, Query(description="Bucket key.")] = "tag",
) -> WinRateResponse:
    """Published win rate per tag / session / strategy / ISO week."""
    start, end, rows = await _rows(store, since, until)
    return WinRateResponse(
        since=start,
        until=end,
        by=by,
        buckets=win_rate_by(rows, by=by),
        source_counts=source_counts(rows),
    )


@router.get("/api/analytics/skeptic-value", response_model=SkepticValueResponse)
async def skeptic_value_route(
    store: StoreDep, since: SinceQuery = None, until: UntilQuery = None
) -> SkepticValueResponse:
    """How decisions ended when the Judge respected vs overruled a counted objection."""
    start, end, rows = await _rows(store, since, until)
    return SkepticValueResponse(since=start, until=end, **dict(skeptic_value(rows)))


@router.get("/api/analytics/equity-curve", response_model=EquityCurveResponse)
async def equity_curve(
    store: StoreDep, since: SinceQuery = None, until: UntilQuery = None
) -> EquityCurveResponse:
    """Hypothetical 1 R equity: published, plus vetoed / gate-rejected counterfactuals."""
    start, end, rows = await _rows(store, since, until)
    return EquityCurveResponse(since=start, until=end, **dict(equity_curves(rows)))
