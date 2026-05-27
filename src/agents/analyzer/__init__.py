"""Analyzer strategies — produce SignalProposal or SkipDecision from MarketSnapshot.

Slice 1 hosts only the SMC analyzer with HTF-bias-only scope. Slice 2 Step 2.1
expands SMC to the full 5-layer protocol. Slice 3 Step 3.1 moves this behind a
Strategy abstract base class and a registry; until then, callers import
`smc_analyzer.analyze` directly.
"""

from src.agents.analyzer.smc_analyzer import HTFBias, analyze

__all__ = ["HTFBias", "analyze"]
