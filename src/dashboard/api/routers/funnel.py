"""`/api/funnel` -- the decision funnel (design §4.2 P4, §5.1).

`GET /api/funnel?since&until&group_by=day` counts every stage from scans down to
published setups per day / window / symbol (counting rules in `funnel.py`).
The period defaults to the last 30 days and is capped at 365 (`period.py`; a
longer, empty or inverted range is 422 before any SQL runs). Naive timestamps
are read as UTC.

The groups and totals cover the requested `[since, until)`. The top-rejection
tables are always the **last** 7 / 30 days, anchored to now (design §4.2 P4),
whatever range was asked for: when the requested range already covers
`[now - 30 d, now)` its rows are reused, otherwise one extra fetch of exactly
that window serves both tables. Both fetches use the minimal, keyset-paged
projection (`queries.list_decisions_between`), so a capped year stays inside the
Data API's 1 MB response.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from src.dashboard.api import overview as assembly
from src.dashboard.api.deps import StoreDep
from src.dashboard.api.errors import DATA_ROUTE_RESPONSES
from src.dashboard.api.funnel import GroupBy, build_funnel, summarise, top_rejections
from src.dashboard.api.period import resolve_period
from src.dashboard.api.queries import list_decisions_between
from src.dashboard.api.schemas import FunnelResponse, RejectionCount

router = APIRouter(tags=["funnel"], responses=DATA_ROUTE_RESPONSES)

DEFAULT_FUNNEL_DAYS = 30
_TOP_REJECTIONS_SHORT = timedelta(days=7)
_TOP_REJECTIONS_LONG = timedelta(days=30)


def _table(rows: list[tuple[str, int]]) -> list[RejectionCount]:
    return [RejectionCount(rule=rule, count=count) for rule, count in rows]


@router.get("/api/funnel", response_model=FunnelResponse)
async def decision_funnel(
    store: StoreDep,
    since: Annotated[datetime | None, Query(description="created_at >= since")] = None,
    until: Annotated[datetime | None, Query(description="created_at < until")] = None,
    group_by: Annotated[GroupBy, Query(description="Group key.")] = "day",
) -> FunnelResponse:
    """Scans -> symbols -> proposals -> gate -> Judge -> published, per group."""
    now = assembly.utcnow()
    start, end = resolve_period(since, until, now=now, default_days=DEFAULT_FUNNEL_DAYS)
    rows = await list_decisions_between(store, since=start, until=end)

    recent_since = now - _TOP_REJECTIONS_LONG
    if start <= recent_since and end >= now:
        recent = [r for r in rows if recent_since <= r.created_at < now]
    else:  # a historical or partial range: fetch the last 30 days once for both tables
        recent = await list_decisions_between(store, since=recent_since, until=now)

    return FunnelResponse(
        since=start,
        until=end,
        group_by=group_by,
        groups=build_funnel(rows, group_by=group_by),
        totals=summarise(rows, key="total"),
        top_rejections_7d=_table(top_rejections(recent, since=now - _TOP_REJECTIONS_SHORT)),
        top_rejections_30d=_table(top_rejections(recent, since=recent_since)),
    )
