"""Orchestration layer: LangGraph state, nodes, and graph builder.

Slice 1 wires only a single node (`analyzer_node`). Slice 2 extends the graph
with historian / skeptic / judge nodes and the Forecaster background loop.
Slice 2 Step 2.11 inserts the hard risk gates (SPEC §1.6) between analyzer and
historian: a proposal violating any hard rule is force-skipped (FR-1.3).

Why this package is lazy (PEP 562): the dashboard Lambda imports
``src.agents.orchestration.risk_gates`` for the pure SPEC §1.6 rules (design
§5.3) and must not pay for LangGraph + the Anthropic SDK at cold start (design
§3.2). Importing the package therefore loads nothing; each public name below is
resolved from its submodule on first attribute access, so
``from src.agents.orchestration import build_pipeline_graph`` still works for the
scan pipeline while ``import src.agents.orchestration.risk_gates`` leaves
``graph.py`` (and everything it pulls in) untouched.

Public API:
    AgentState           -- TypedDict carrying per-scan state across nodes
    build_graph          -- compile the Slice-1 analyzer-only graph
    build_pipeline_graph -- compile the full Slice-2 pipeline (Steps 2.7, 2.11)
    run_scan             -- convenience wrapper that invokes the analyzer graph

    risk_gates surface (Step 2.11):
    make_risk_gate_node  -- LangGraph node enforcing the SPEC §1.6 hard rules
    evaluate_risk_gates  -- pure aggregate evaluation given a RiskContext
    gather_risk_context  -- the single IO seam (reads the journal into RiskContext)
    to_skip_decision     -- build the forced SkipDecision for a violation
    RiskGateReport / RiskCheckResult / RiskContext -- result + input models

    Step 2.13 (multi-symbol parallelism):
    ScanReservationLedger -- per-batch in-process ledger that keeps the §1.6
        stateful caps exact when the watchlist is scanned concurrently
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only; runtime resolution is lazy
    from src.agents.orchestration.graph import (
        AgentState,
        analyzer_node,
        build_graph,
        build_pipeline_graph,
        run_scan,
    )
    from src.agents.orchestration.reservations import ScanReservationLedger
    from src.agents.orchestration.risk_gates import (
        RiskCheckResult,
        RiskContext,
        RiskGateReport,
        evaluate_risk_gates,
        gather_risk_context,
        make_risk_gate_node,
        to_skip_decision,
    )

__all__ = [
    "AgentState",
    "RiskCheckResult",
    "RiskContext",
    "RiskGateReport",
    "ScanReservationLedger",
    "analyzer_node",
    "build_graph",
    "build_pipeline_graph",
    "evaluate_risk_gates",
    "gather_risk_context",
    "make_risk_gate_node",
    "run_scan",
    "to_skip_decision",
]

# Public name -> submodule that defines it (resolved on first access).
_LAZY: dict[str, str] = {
    "AgentState": "graph",
    "analyzer_node": "graph",
    "build_graph": "graph",
    "build_pipeline_graph": "graph",
    "run_scan": "graph",
    "ScanReservationLedger": "reservations",
    "RiskCheckResult": "risk_gates",
    "RiskContext": "risk_gates",
    "RiskGateReport": "risk_gates",
    "evaluate_risk_gates": "risk_gates",
    "gather_risk_context": "risk_gates",
    "make_risk_gate_node": "risk_gates",
    "to_skip_decision": "risk_gates",
}


def __getattr__(name: str) -> Any:
    try:
        module = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib

    return getattr(importlib.import_module(f"src.agents.orchestration.{module}"), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
