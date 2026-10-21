"""`GET /api/setups/open` -- every OPEN setup as a card (design §4.2 P1 / P5, §6.1).

One query (`queries.list_open_setup_cards`) plus the clock-relative ages from
`overview.setup_card_out`. Unrealised R waits for the price provider (Step 4.3).
"""

from __future__ import annotations

from fastapi import APIRouter

from src.dashboard.api import overview as assembly
from src.dashboard.api.deps import StoreDep
from src.dashboard.api.errors import DATA_ROUTE_RESPONSES
from src.dashboard.api.queries import list_open_setup_cards
from src.dashboard.api.schemas import SetupCardOut

router = APIRouter(tags=["setups"], responses=DATA_ROUTE_RESPONSES)


@router.get("/api/setups/open", response_model=list[SetupCardOut])
async def open_setups(store: StoreDep) -> list[SetupCardOut]:
    """OPEN setups newest first, with ages measured from the request clock."""
    now = assembly.utcnow()
    cards = await list_open_setup_cards(store)
    return [assembly.setup_card_out(card, now=now) for card in cards]
