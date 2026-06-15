"""Orchestration layer: LangGraph state, nodes, and graph builder.

Slice 1 wires only a single node (`analyzer_node`). Slice 2 extends the graph
with historian / skeptic / judge nodes and the Forecaster background loop.
Slice 2 Step 2.11 inserts the hard risk gates (SPEC §1.6) between analyzer and
historian: a proposal violating any hard rule is force-skipped (FR-1.3).

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
"""

from src.agents.orchestration.graph import (
    AgentState,
    analyzer_node,
    build_graph,
    build_pipeline_graph,
    run_scan,
)
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
    "analyzer_node",
    "build_graph",
    "build_pipeline_graph",
    "evaluate_risk_gates",
    "gather_risk_context",
    "make_risk_gate_node",
    "run_scan",
    "to_skip_decision",
]
