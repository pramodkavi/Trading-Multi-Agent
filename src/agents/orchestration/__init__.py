"""Orchestration layer: LangGraph state, nodes, and graph builder.

Slice 1 wires only a single node (`analyzer_node`). Slice 2 extends the graph
with historian / skeptic / judge nodes and the Forecaster background loop.
Slice 2 Step 2.11 inserts risk_gates between analyzer and historian.

Public API:
    AgentState  -- TypedDict carrying per-scan state across nodes
    build_graph -- compile a CompiledStateGraph from the current node set
    run_scan    -- convenience wrapper that invokes the compiled graph
"""

from src.agents.orchestration.graph import (
    AgentState,
    analyzer_node,
    build_graph,
    run_scan,
)

__all__ = ["AgentState", "analyzer_node", "build_graph", "run_scan"]
