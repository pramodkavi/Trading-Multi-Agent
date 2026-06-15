"""Forecaster agent package (Slice 2 Step 2.9).

Background loop that re-evaluates every OPEN setup each scan and emits
STILL_VALID / AT_RISK / INVALIDATED, sending Telegram updates and closing
resolved setups with a logged outcome (SPEC §3.1.2 FR-2.1).

Public API:
    Forecaster       -- re-evaluates open setups and acts on each verdict.
    ForecasterUpdate -- the Forecaster's structured per-setup verdict.
"""

from src.agents.forecaster.forecaster import Forecaster
from src.agents.forecaster.models import ForecasterUpdate

__all__ = [
    "Forecaster",
    "ForecasterUpdate",
]
