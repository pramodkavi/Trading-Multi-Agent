"""`GET /api/overview` -- the landing page in one round trip (design §4.2 P1, §6.1).

Thin by design: five reads through the store (the risk gate's own
`gather_risk_context`, the OPEN setup cards, today's scan rows, their journal
rows, the Forecaster evaluation counts) and then the pure assembly in
`src.dashboard.api.overview`. The clock comes from `overview.utcnow()` so the
ages, countdown and session in the payload are testable against a pinned
instant. `db` is reported `"ok"` because reaching the response means every read
succeeded; a Data API failure is mapped to 503 / 502 by `errors.py` instead.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from src.agents.orchestration.risk_gates import gather_risk_context
from src.dashboard.api import overview as assembly
from src.dashboard.api.deps import StoreDep
from src.dashboard.api.errors import DATA_ROUTE_RESPONSES
from src.dashboard.api.queries import (
    count_evaluations_since,
    list_open_setup_cards,
    list_scan_runs_since,
    list_signals_for_scans,
)
from src.dashboard.api.schemas import ForecasterCoverage, HealthStrip, OverviewResponse

router = APIRouter(tags=["overview"], responses=DATA_ROUTE_RESPONSES)


@router.get("/api/overview", response_model=OverviewResponse)
async def overview(store: StoreDep) -> OverviewResponse:
    """Health strip, risk budget, OPEN setups and today's scan windows."""
    now = assembly.utcnow()
    day = now.astimezone(UTC).date()
    start_of_day = datetime(day.year, day.month, day.day, tzinfo=UTC)

    # Design §5.3: the caps are computed by the gate's own context builder (no
    # market snapshot on the dashboard -> funding is simply unknown).
    ctx = await gather_risk_context(store=store, snapshot=None, now=now)
    cards = await list_open_setup_cards(store)
    scans = await list_scan_runs_since(store, since=start_of_day)  # bounded to the UTC day
    rows = await list_signals_for_scans(store, scan_ids=[scan.id for scan in scans])
    # The last finished Forecaster sweep (a separate :05 invocation; spans yesterday's
    # windows, so it is set in practice -- the start-of-day fallback is degenerate-only).
    sweep_at = assembly.last_sweep_at(now)
    evaluations, errors = await count_evaluations_since(store, since=sweep_at or start_of_day)

    health = HealthStrip(
        last_scan=assembly.last_scan_outcome(scans, day=day),
        forecaster=ForecasterCoverage(
            open_setups=len(cards),
            stale=assembly.count_stale(cards, sweep_at=sweep_at),
            evaluations_last_window=evaluations,
            errors_last_window=errors,
        ),
        db="ok",
    )
    return OverviewResponse(
        generated_at=now,
        health=health,
        risk=assembly.build_risk_budget(ctx, now=now),
        open_setups=[assembly.setup_card_out(card, now=now) for card in cards],
        windows_today=assembly.bucket_windows(scans, rows, day=day),
    )
