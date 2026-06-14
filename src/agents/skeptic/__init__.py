"""Skeptic agent package (Slice 2 Step 2.5).

Independently fetches macro / cross-asset context (FRED + Twelve Data) the
Analyzer never saw and emits the strongest adversarial objection to a proposal,
or a ``NoMacroData`` sentinel when macro is unavailable (FR-4.3).

Public API:
    Skeptic              -- macro fetch + adversarial LLM evaluation.
    make_skeptic_node    -- factory building the LangGraph 'skeptic' node.
    build_macro_providers -- construct the configured macro providers from Settings.
    SkepticObjection     -- the Skeptic's structured objection (severity + reasoning).
    SPX_PROXY_SYMBOL / VIX_PROXY_SYMBOL -- the free-tier ETF proxy symbols.
"""

from src.agents.skeptic.models import SkepticObjection
from src.agents.skeptic.skeptic import (
    SPX_PROXY_SYMBOL,
    VIX_PROXY_SYMBOL,
    Skeptic,
    build_macro_providers,
    make_skeptic_node,
)

__all__ = [
    "SPX_PROXY_SYMBOL",
    "VIX_PROXY_SYMBOL",
    "Skeptic",
    "SkepticObjection",
    "build_macro_providers",
    "make_skeptic_node",
]
