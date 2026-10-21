"""`GET /api/setups/open` -- every OPEN setup as a card (design §4.2 P1 / P5, §6.1).

One query (`queries.list_open_setup_cards`) plus the clock-relative ages from
`overview.setup_card_out`, and a best-effort unrealised R (Step 4.3): one
latest-price fetch per distinct symbol under a 5 s budget
(`market.latest_prices`). A slow or failing provider leaves `unrealised_r` null
with "price unavailable" -- it never fails the route.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.dashboard.api import market
from src.dashboard.api import overview as assembly
from src.dashboard.api.deps import StoreDep
from src.dashboard.api.errors import DATA_ROUTE_RESPONSES
from src.dashboard.api.market import ProviderFactoryDep
from src.dashboard.api.queries import list_open_setup_cards
from src.dashboard.api.schemas import SetupCardOut

router = APIRouter(tags=["setups"], responses=DATA_ROUTE_RESPONSES)


@router.get("/api/setups/open", response_model=list[SetupCardOut])
async def open_setups(store: StoreDep, factory: ProviderFactoryDep) -> list[SetupCardOut]:
    """OPEN setups newest first, with ages and unrealised R measured at the request clock."""
    now = assembly.utcnow()
    cards = await list_open_setup_cards(store)
    prices = await market.latest_prices(
        factory, sorted({card.symbol for card in cards}), now=now, budget_s=market.PRICE_BUDGET_S
    )
    return [assembly.setup_card_out(card, now=now, price=prices.get(card.symbol)) for card in cards]
