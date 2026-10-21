"""`GET /api/overview` -- the landing page in one round trip (design §4.2 P1, §6.1).

Thin by design: 6-9 serial Data API calls through the store -- the risk gate's
own `gather_risk_context` (2 reads + one `get_signal` per OPEN setup, so 2-5),
then the OPEN setup cards, the scan rows since yesterday, today's journal rows
and the Forecaster evaluation counts -- followed by the pure assembly in
`src.dashboard.api.overview`. The clock comes from `overview.utcnow()` so the
ages, countdown and session in the payload are testable against a pinned
instant. `db` is reported `"ok"` because reaching the response means every read
succeeded; a Data API failure is mapped to 503 / 502 by `errors.py` instead.

Scans are fetched from the start of *yesterday*, not today: the last-scan badge
must show yesterday's 22:03 wrap overnight instead of going blind until 08:03.
Only today's rows feed the window list and the per-symbol chips.

Step 4.3: the OPEN setup cards carry a best-effort unrealised R -- one
`market.latest_prices` call (one provider, one fetch per distinct symbol,
5 s overall budget). Binance down or slow leaves `unrealised_r` null with
"price unavailable"; the landing page is still 200.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter

from src.agents.orchestration.risk_gates import gather_risk_context
from src.dashboard.api import market
from src.dashboard.api import overview as assembly
from src.dashboard.api.deps import StoreDep
from src.dashboard.api.errors import DATA_ROUTE_RESPONSES
from src.dashboard.api.market import ProviderFactoryDep
from src.dashboard.api.queries import (
    count_evaluations_since,
    list_open_setup_cards,
    list_scan_runs_since,
    list_signals_for_scans,
)
from src.dashboard.api.schemas import ForecasterCoverage, HealthStrip, OverviewResponse

router = APIRouter(tags=["overview"], responses=DATA_ROUTE_RESPONSES)


@router.get("/api/overview", response_model=OverviewResponse)
async def overview(store: StoreDep, factory: ProviderFactoryDep) -> OverviewResponse:
    """Health strip, risk budget, OPEN setups and today's scan windows."""
    now = assembly.utcnow()
    day = now.astimezone(UTC).date()
    start_of_day = datetime(day.year, day.month, day.day, tzinfo=UTC)

    # Design §5.3: the caps are computed by the gate's own context builder (no
    # market snapshot on the dashboard -> funding is simply unknown).
    ctx = await gather_risk_context(store=store, snapshot=None, now=now)
    cards = await list_open_setup_cards(store)
    # Yesterday's rows serve the last-scan badge only (no overnight blind spot).
    scans = await list_scan_runs_since(store, since=start_of_day - timedelta(days=1))
    todays = assembly.scans_on(scans, day)
    rows = await list_signals_for_scans(store, scan_ids=[scan.id for scan in todays])
    # The last finished Forecaster sweep (a separate :05 invocation; spans yesterday's
    # windows, so it is set in practice -- the start-of-day fallback is degenerate-only).
    sweep_at = assembly.last_sweep_at(now)
    evaluations, errors = await count_evaluations_since(store, since=sweep_at or start_of_day)
    # Best-effort unrealised R (Step 4.3): bounded by the 5 s budget, never raises on a
    # provider failure -- the cards then read "price unavailable".
    prices = await market.latest_prices(
        factory, sorted({card.symbol for card in cards}), now=now, budget_s=market.PRICE_BUDGET_S
    )

    health = HealthStrip(
        last_scan=assembly.last_scan_outcome(scans),
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
        open_setups=[
            assembly.setup_card_out(card, now=now, price=prices.get(card.symbol)) for card in cards
        ],
        windows_today=assembly.bucket_windows(todays, rows, day=day),
    )
