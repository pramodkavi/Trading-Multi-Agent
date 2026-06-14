"""SMC analyzer entry point.

As of Slice 2 Step 2.1e this delegates to the full SMC protocol assembled in
`src.agents.analyzer.smc` (structure → liquidity → order blocks → FVG → 5-gate,
evidence-weighted scoring). It replaces the Slice-1 HTF-bias *stub* that lived
here — the first change to the live runtime path since Slice 1.

`analyze` keeps its original signature so existing callers (the LangGraph
`analyzer_node`, `scripts/run_scan.py`) are unaffected; only the behaviour behind
it changed. Step 2.2 feeds it true multi-timeframe data; Step 2.11 inserts the
hard risk gates between this and the Historian.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from src.agents.analyzer.smc.analysis import full_smc_analysis

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.common.models import SignalProposal, SkipDecision
    from src.providers import MarketSnapshot


def analyze(
    snapshot: MarketSnapshot,
    *,
    scan_id: UUID,
    strategy: str = "smc",
) -> SignalProposal | SkipDecision:
    """Run the full SMC analysis on a MarketSnapshot.

    Args:
        snapshot: market data for one symbol. The full protocol is multi-timeframe;
            until Step 2.2 populates D1/H1/M15/M5, analysis runs on the best
            available timeframe (H4 in Slice 1).
        scan_id: the scan run this analysis belongs to.
        strategy: registry name; defaults to 'smc'.

    Returns:
        A SignalProposal when a complete SMC setup clears the hard gates and the
        confluence threshold; otherwise a categorized SkipDecision.
    """
    return full_smc_analysis(snapshot, scan_id=scan_id, strategy=strategy)
