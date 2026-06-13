"""Smart Money Concepts (SMC) detection primitives — Slice 2 Step 2.1.

This subpackage ports the user's reference SMC scripts (detect_structure,
detect_fvg, detect_ob, detect_liquidity, derivatives_data) into typed,
**as-of-correct** Python that operates on the project's `Kline` model and emits
Pydantic results — replacing the "LLM reads printed ASCII tables" design with
structured outputs the pipeline can score deterministically.

Design principles (see docs/research/smc-evidence-review.md):
- **As-of correctness / no look-ahead.** Every detector, when evaluating bar t,
  uses only bars <= t. A fractal swing at index i is *confirmed* only at bar
  i+lookback; structural breaks of it are detected strictly after confirmation.
  This is the single most important correctness property — look-ahead bias is the
  #1 reason discretionary-pattern backtests look great and lose money live.
- **Evidence-weighted, calibrated.** Premium/Discount + liquidity at obvious
  levels carry the most weight (real microstructure mechanism); OTE/PO3 are
  low-weight context. Confidence is earned via forward-testing, never asserted.
- **Volatility-normalized.** Thresholds scale with ATR rather than fixed % so the
  same code behaves sensibly across BTC 4H and SOL 5m.

Step 2.1a (this commit) ships the **structure** layer: fractal swings, the
BOS/CHoCH state machine, market phase, and Premium/Discount + directional OTE.
FVG / Order Block / liquidity / derivatives land in 2.1b-2.1d.
"""

from src.agents.analyzer.smc.fvg import detect_fvgs
from src.agents.analyzer.smc.models import (
    DealingRange,
    FairValueGap,
    FVGType,
    LegDirection,
    MarketPhase,
    OrderBlock,
    OrderBlockDirection,
    StructureAnalysis,
    StructureEvent,
    StructureEventType,
    SwingLabel,
    SwingPoint,
    SwingType,
    Zone,
)
from src.agents.analyzer.smc.order_block import detect_order_blocks
from src.agents.analyzer.smc.structure import analyze_structure
from src.agents.analyzer.smc.swings import detect_swings
from src.agents.analyzer.smc.volatility import atr_series, average_true_range

__all__ = [
    "DealingRange",
    "FVGType",
    "FairValueGap",
    "LegDirection",
    "MarketPhase",
    "OrderBlock",
    "OrderBlockDirection",
    "StructureAnalysis",
    "StructureEvent",
    "StructureEventType",
    "SwingLabel",
    "SwingPoint",
    "SwingType",
    "Zone",
    "analyze_structure",
    "atr_series",
    "average_true_range",
    "detect_fvgs",
    "detect_order_blocks",
    "detect_swings",
]
