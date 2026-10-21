"""`/api/critic/*` -- the Critic's reports and per-decision reviews (design §4.2 P6).

- `GET /api/critic/reports?limit=10` -- the latest reports, newest first
  (`limit` 1..50, 422 outside). List lines carry no markdown and no JSONB beyond
  `summary` (`report->>'summary'`), so the list stays small.
- `GET /api/critic/reports/{id}` -- one report with the full `report` JSONB and
  the rendered `markdown`; 404 `not_found` when it does not exist.
- `GET /api/critic/reviews?limit&cursor` -- `decision_reviews` newest first,
  keyset-paged on `(created_at, signal_id)` DESC exactly like the journal:
  `limit` defaults to 50 (1..200, 422 outside), the query fetches `limit + 1`
  and the extra row becomes `next_cursor`; a cursor that does not decode is 400
  `bad_cursor`. Lines carry named extracts of the `review` JSONB (null when the
  review call errored; `error` holds the first 500 chars of why).
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query

from src.dashboard.api.cursor import decode_cursor, encode_cursor
from src.dashboard.api.deps import StoreDep
from src.dashboard.api.errors import DATA_ROUTE_RESPONSES, NotFound
from src.dashboard.api.queries import get_critic_report, list_critic_reports, list_reviews_page
from src.dashboard.api.schemas import (
    CriticReportOut,
    CriticReportsPage,
    CriticReportSummaryOut,
    ErrorResponse,
    ReviewsPage,
    ReviewSummaryOut,
)

router = APIRouter(tags=["critic"], responses=DATA_ROUTE_RESPONSES)

DEFAULT_REPORTS = 10
MAX_REPORTS = 50
DEFAULT_REVIEWS = 50
MAX_REVIEWS = 200

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No such Critic report."}
}


@router.get("/api/critic/reports", response_model=CriticReportsPage)
async def critic_reports(
    store: StoreDep,
    limit: Annotated[int, Query(ge=1, le=MAX_REPORTS)] = DEFAULT_REPORTS,
) -> CriticReportsPage:
    """The latest Critic reports, newest first, without their markdown."""
    rows = await list_critic_reports(store, limit=limit)
    return CriticReportsPage(
        items=[CriticReportSummaryOut.model_validate(r.model_dump()) for r in rows]
    )


@router.get("/api/critic/reports/{report_id}", response_model=CriticReportOut, responses=_NOT_FOUND)
async def critic_report(store: StoreDep, report_id: UUID) -> CriticReportOut:
    """One Critic report: the full JSONB and the rendered markdown."""
    report = await get_critic_report(store, report_id)
    if report is None:
        raise NotFound
    return CriticReportOut.model_validate(report.model_dump())


@router.get(
    "/api/critic/reviews",
    response_model=ReviewsPage,
    responses={400: {"model": ErrorResponse, "description": "The cursor does not decode."}},
)
async def critic_reviews(
    store: StoreDep,
    limit: Annotated[int, Query(ge=1, le=MAX_REVIEWS)] = DEFAULT_REVIEWS,
    cursor: Annotated[
        str | None, Query(description="Opaque `next_cursor` of the prior page.")
    ] = None,
) -> ReviewsPage:
    """One page of Critic reviews, newest first."""
    after = decode_cursor(cursor) if cursor else None  # BadCursor -> 400 via errors.py
    rows = await list_reviews_page(store, limit=limit, after=after)
    page = rows[:limit]
    next_cursor = (
        encode_cursor(page[-1].created_at, page[-1].signal_id) if len(rows) > limit else None
    )
    return ReviewsPage(
        items=[ReviewSummaryOut.model_validate(r.model_dump()) for r in page],
        next_cursor=next_cursor,
    )
