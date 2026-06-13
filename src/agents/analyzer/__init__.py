"""Analyzer strategies — produce SignalProposal or SkipDecision from MarketSnapshot.

Slice 2 Step 2.1 implements the full SMC 5-layer protocol (in the `smc`
subpackage). `smc_analyzer.analyze` is the public entry point and delegates to
`smc.analysis.full_smc_analysis`. Slice 3 Step 3.1 moves this behind a Strategy
abstract base class and a registry; until then, callers import `analyze` directly.
"""

from src.agents.analyzer.smc_analyzer import analyze

__all__ = ["analyze"]
