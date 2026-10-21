"""`/api/funnel` -- the decision funnel (design §4.2 P4, §5.1).

`GET /api/funnel?since&until&group_by=day` counts every stage from scans down to
published setups per day / window / symbol (counting rules in `funnel.py`).
The period defaults to the last 30 days and is capped at 365 (`period.py`; a
longer, empty or inverted range is 422 before any SQL runs). Naive timestamps
are read as UTC.

One statement serves the whole response: the decisions of
`[min(since, until - 30 d), until)` are fetched once; the groups and totals use
the rows of the requested range, the top-rejection tables the rows of the 7 /
30 days before `until`. The projection is minimal (eight short columns, no
payload) so a capped year stays inside the Data API's 1 MB response.
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
    start, end = resolve_period(
        since, until, now=assembly.utcnow(), default_days=DEFAULT_FUNNEL_DAYS
    )
    fetch_since = min(start, end - _TOP_REJECTIONS_LONG)
    rows = await list_decisions_between(store, since=fetch_since, until=end)
    in_range = [r for r in rows if r.created_at >= start]
    return FunnelResponse(
        since=start,
        until=end,
        group_by=group_by,
        groups=build_funnel(in_range, group_by=group_by),
        totals=summarise(in_range, key="total"),
        top_rejections_7d=_table(top_rejections(rows, since=end - _TOP_REJECTIONS_SHORT)),
        top_rejections_30d=_table(top_rejections(rows, since=end - _TOP_REJECTIONS_LONG)),
    )
