"""`/api/signals` -- the journal page, one decision's lifecycle, one agent run (design §4.2 P2/P3).

- `GET /api/signals` pages the journal by keyset cursor (design §3.4): `limit`
  defaults to 50 and is **rejected** with 422 outside 1..200 (`Query(ge=1,
  le=200)`; the plan's ruling is to reject, not clamp). The query fetches
  `limit + 1` rows; the extra one only proves a next page exists and becomes
  `next_cursor` from the `limit`-th row. A cursor that does not decode is 400
  `bad_cursor` (handler in `errors.py`). Naive `since` / `until` values are read
  as UTC. Rows are **trimmed** (`SignalRowOut`: analyzer summary, no
  `payload` / `features` / `outcome_metadata`) so a 201-row page stays under the
  Data API's 1 MB response cap.
- `GET /api/signals/{signal_id}` is the lifecycle: the journal row (same SQL
  classification as the page) **plus the full stored signal** (`SignalDetailOut`
  -- the only place the payload travels), every agent run of the scan trimmed to
  its timeline summary, the Forecaster evaluations oldest first, the Critic's
  resolution and review, and the setup card. 404 `not_found` when the signal
  does not exist.
- `GET /api/signals/{signal_id}/runs/{run_id}` is the full agent output for one
  step; 404 when the run is missing **or belongs to another scan** than the
  signal's, so a run id cannot be read through an unrelated signal.
- `GET /api/signals/{signal_id}/chart` (Step 4.3) is the H4 chart of a decision:
  candles from `before` bars (1..500, default 60) before `created_at` to `after`
  bars (0..500, default 30) after it, clamped to the request clock; plus the
  overlay -- the stored proposal's levels for a PUBLISHED / VETOED decision, the
  Analyzer run's rejected proposal for a GATE_REJECTED one, `null` for NO_SETUP.
  404 before any provider is built; a `ProviderError` is 502 `market_unavailable`.
  The lifecycle's setup card carries no live price (`unrealised_r` null with
  "not fetched on this view" -- distinct from "price unavailable", which means
  the fetch failed); the overview / setups pages fetch it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query

from src.common.models import SignalOutcome
from src.dashboard.api import market
from src.dashboard.api import overview as assembly
from src.dashboard.api.classify import DecisionKind, decision_kind_from_columns
from src.dashboard.api.cursor import decode_cursor, encode_cursor
from src.dashboard.api.deps import StoreDep
from src.dashboard.api.errors import DATA_ROUTE_RESPONSES, MARKET_ROUTE_RESPONSES, NotFound
from src.dashboard.api.market import ProviderFactoryDep
from src.dashboard.api.queries import (
    SignalFilters,
    get_review,
    get_setup_for_signal,
    list_signals_for_scans,
    list_signals_page,
)
from src.dashboard.api.schemas import (
    AgentRunOut,
    ChartOverlay,
    ErrorResponse,
    EvaluationOut,
    KlineOut,
    LifecycleResponse,
    ResolutionOut,
    ReviewOut,
    SignalChartResponse,
    SignalsPage,
)
from src.providers.base import Timeframe

router = APIRouter(tags=["signals"], responses=DATA_ROUTE_RESPONSES)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_CHART_BARS = 500

# `list_setup_evaluations` clamps to 200 anyway; four sweeps a day makes that ~50 days.
_EVALUATION_FETCH_LIMIT = 200

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No such signal / run."}
}


def _as_utc(value: datetime | None) -> datetime | None:
    """Read a naive query timestamp as UTC; leave aware ones alone."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


@router.get(
    "/api/signals",
    response_model=SignalsPage,
    responses={400: {"model": ErrorResponse, "description": "The cursor does not decode."}},
)
async def list_signals(
    store: StoreDep,
    since: Annotated[datetime | None, Query(description="created_at >= since")] = None,
    until: Annotated[datetime | None, Query(description="created_at < until")] = None,
    symbol: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
    kind: Annotated[DecisionKind | None, Query(description="Derived decision kind.")] = None,
    outcome: Annotated[SignalOutcome | None, Query()] = None,
    tag: Annotated[str | None, Query(min_length=1)] = None,
    session: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
    violated_rule: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[
        str | None, Query(description="Opaque `next_cursor` of the prior page.")
    ] = None,
) -> SignalsPage:
    """One page of the journal, newest first, with the derived decision kind per row."""
    after = decode_cursor(cursor) if cursor else None  # BadCursor -> 400 via errors.py
    filters = SignalFilters(
        since=_as_utc(since),
        until=_as_utc(until),
        symbol=symbol,
        decision_kind=kind,
        outcome=outcome,
        tag=tag,
        session=session,
        violated_rule=violated_rule,
    )
    rows = await list_signals_page(store, filters=filters, limit=limit, after=after)
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
    return SignalsPage(
        items=[assembly.signal_row_out(row) for row in page], next_cursor=next_cursor
    )


@router.get("/api/signals/{signal_id}", response_model=LifecycleResponse, responses=_NOT_FOUND)
async def signal_lifecycle(store: StoreDep, signal_id: UUID) -> LifecycleResponse:
    """The decision's whole story: journal row, agent steps, evaluations, Critic rows, setup."""
    signal = await store.get_signal(signal_id)
    if signal is None:
        raise NotFound
    # The journal projection classifies in SQL (decision kind, Judge ruling, session,
    # setup / resolution joins) -- reuse it so the page and the detail never disagree.
    rows = await list_signals_for_scans(store, scan_ids=[signal.scan_id])
    row = next((r for r in rows if r.id == signal_id), None)
    if row is None:
        raise NotFound

    runs = await store.list_agent_runs_for_scan(signal.scan_id)  # one scan == one decision
    setup = await get_setup_for_signal(store, signal_id)
    evaluations = (
        []
        if setup is None
        else await store.list_setup_evaluations(setup.id, limit=_EVALUATION_FETCH_LIMIT)
    )
    resolution = await store.get_resolution(signal_id)
    review = await get_review(store, signal_id)

    now = assembly.utcnow()
    card = None if setup is None else assembly.setup_card_for_signal(setup, signal)
    return LifecycleResponse(
        signal=assembly.signal_detail_out(row, signal),  # the one place the full payload travels
        steps=[assembly.lifecycle_step(run) for run in runs],
        evaluations=[
            EvaluationOut.model_validate(e.model_dump())
            for e in sorted(evaluations, key=lambda e: (e.evaluated_at, str(e.id)))
        ],
        resolution=None
        if resolution is None
        else ResolutionOut.model_validate(resolution.model_dump()),
        review=None if review is None else ReviewOut.model_validate(review.model_dump()),
        setup=None
        if card is None
        else assembly.setup_card_out(
            card, now=now, price=None, unavailable_note=assembly.PRICE_NOT_FETCHED
        ),
    )


@router.get(
    "/api/signals/{signal_id}/chart",
    response_model=SignalChartResponse,
    responses={**_NOT_FOUND, **MARKET_ROUTE_RESPONSES},
)
async def signal_chart(
    store: StoreDep,
    factory: ProviderFactoryDep,
    signal_id: UUID,
    before: Annotated[int, Query(ge=1, le=MAX_CHART_BARS, description="H4 bars before")] = 60,
    after: Annotated[int, Query(ge=0, le=MAX_CHART_BARS, description="H4 bars after")] = 30,
) -> SignalChartResponse:
    """H4 candles around the decision plus its levels (null overlay for NO_SETUP)."""
    signal = await store.get_signal(signal_id)
    if signal is None:
        raise NotFound  # before any provider is built

    kind = decision_kind_from_columns(
        direction=None if signal.direction is None else signal.direction.value,
        violated_rule=signal.payload.get("violated_rule"),
        judge_ruling=None,  # a veto still has its proposal: PUBLISHED / VETOED draw alike
    )
    overlay: ChartOverlay | None = None
    if kind is DecisionKind.PUBLISHED:
        overlay = assembly.chart_overlay(
            {"features": signal.features, **signal.payload}  # payload features win if present
        )
    elif kind is DecisionKind.GATE_REJECTED:
        runs = await store.list_agent_runs_for_scan(signal.scan_id)
        proposal = assembly.rejected_proposal(runs, symbol=signal.symbol)
        overlay = None if proposal is None else assembly.chart_overlay(proposal)

    bar = market.TIMEFRAME_DURATION[Timeframe.H4]
    start = signal.created_at - before * bar
    end = min(signal.created_at + after * bar, assembly.utcnow())
    candles = await market.fetch_klines_window(
        factory, signal.symbol, Timeframe.H4, start=start, end=end
    )
    return SignalChartResponse(
        signal_id=signal.id,
        symbol=signal.symbol,
        timeframe=Timeframe.H4,
        created_at=signal.created_at,
        klines=[KlineOut.model_validate(k.model_dump()) for k in candles],
        overlay=overlay,
    )


@router.get(
    "/api/signals/{signal_id}/runs/{run_id}", response_model=AgentRunOut, responses=_NOT_FOUND
)
async def signal_run(store: StoreDep, signal_id: UUID, run_id: UUID) -> AgentRunOut:
    """The full `agent_runs` row for one lifecycle step (the timeline shows a trimmed summary)."""
    signal = await store.get_signal(signal_id)
    if signal is None:
        raise NotFound
    run = await store.get_agent_run(run_id)
    if run is None or run.scan_id != signal.scan_id:
        raise NotFound
    return AgentRunOut.model_validate(run.model_dump())
