"""LangGraph state, nodes, and graph builder for the per-signal pipeline.

Slice 1 Step 1.7 scope: one node (`analyzer_node`) wired START -> analyzer -> END.
The state shape, node signatures, and edge wiring are designed so Slice 2
Steps 2.4-2.7 can insert historian / skeptic / judge nodes without redefining
AgentState or rewriting the analyzer node.

Notes on departures from the SPEC §4 Step 1.7 wording:
- SPEC lists three state fields (scan_context, proposal, decision). We add a
  fourth: `snapshot`. The analyzer needs a MarketSnapshot; the caller is
  Step 1.12's scan runner, which fetches via BinanceProvider and seeds the
  state. Keeping the snapshot in state (rather than closing over a provider)
  makes the graph a pure function of state -- easier to test and easier to
  resume from a checkpoint once the Postgres checkpointer ships in Step 2.7.
- SPEC says the Judge sets `decision`. Slice 1 has no Judge, so analyzer_node
  derives a stub decision (PUBLISH for proposals, SKIP for skip-decisions)
  using the same JudgeRuling enum. Step 2.6 will overwrite this in
  judge_node; existing downstream consumers (Telegram dispatcher in
  Step 1.12) need no change.
"""

from __future__ import annotations

from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from src.agents.analyzer import analyze

# Runtime (not TYPE_CHECKING) imports: LangGraph resolves AgentState's annotations
# via get_type_hints() at StateGraph construction, so every type referenced in
# AgentState must exist at runtime. The historian / skeptic packages import
# AgentState only under TYPE_CHECKING, so this direction introduces no cycle.
from src.agents.historian import HistorianReport
from src.agents.skeptic import SkepticObjection
from src.common.models import (
    JudgeRuling,
    ScanContext,
    SignalProposal,
    SkipDecision,
)
from src.providers import MarketSnapshot, NoMacroData

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class AgentState(TypedDict, total=False):
    """Per-scan state passed between LangGraph nodes.

    `total=False` lets each node return only the keys it sets; LangGraph
    merges these into the running state. Slice 2's nodes (historian, skeptic,
    judge) add their own keys without changing this declaration's shape --
    they extend it. Until then, only the four fields below are populated.

    Field lifecycle:
        scan_context     : seeded by the caller (scan runner); never mutated.
        snapshot         : seeded by the caller after fetching market data.
        proposal         : set by analyzer_node.
        historian_report : set by the historian node (Step 2.4b's
                           make_historian_node); None for skips / when the
                           node is not wired. The edge analyzer -> historian is
                           added in Step 2.7.
        skeptic_objection: set by the skeptic node (Step 2.5's
                           make_skeptic_node): a SkepticObjection, or NoMacroData
                           when macro is unavailable (FR-4.3 -> Judge downgrades
                           confidence to medium), or None for skips / when the
                           node is not wired. Edge added in Step 2.7.
        decision         : set by analyzer_node (stub) -> overwritten by
                           judge_node in Slice 2 Step 2.6.
    """

    scan_context: ScanContext
    snapshot: MarketSnapshot
    proposal: SignalProposal | SkipDecision | None
    historian_report: HistorianReport | None
    skeptic_objection: SkepticObjection | NoMacroData | None
    decision: JudgeRuling | None


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def analyzer_node(state: AgentState) -> AgentState:
    """Run the SMC analyzer and stamp a Slice-1 stub decision.

    Returns a *partial* AgentState: only the keys this node owns. LangGraph
    merges the returned dict into the running state, so we don't restate
    scan_context / snapshot.

    The `decision` we emit here is the Slice-1 stub:
        SignalProposal  -> JudgeRuling.PUBLISH
        SkipDecision    -> JudgeRuling.SKIP

    Slice 2 Step 2.6's judge_node will overwrite this with real deliberation
    (PUBLISH / PUBLISH_WITH_CAVEAT / SKIP) using historian + skeptic context.
    """
    ctx = state["scan_context"]
    snapshot = state["snapshot"]

    result = analyze(snapshot, scan_id=ctx.scan_id, strategy=ctx.strategy)

    decision = JudgeRuling.PUBLISH if isinstance(result, SignalProposal) else JudgeRuling.SKIP

    return {"proposal": result, "decision": decision}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


def build_graph() -> Any:
    """Compile the Slice 1 per-signal graph: START -> analyzer_node -> END.

    Returned graph is stateless (no checkpointer). Step 2.7 adds the Postgres
    checkpointer to support crash-resume per SPEC §3.3.1 NFR-1.3.

    Return annotated as Any because LangGraph's CompiledStateGraph generic
    parameters are inferred differently between local mypy and the
    pre-commit hook's isolated env. The runtime object exposes the standard
    .ainvoke / .invoke methods; callers go through `run_scan()` which types
    the final state as AgentState.
    """
    graph = StateGraph(AgentState)
    graph.add_node("analyzer", analyzer_node)
    graph.add_edge(START, "analyzer")
    graph.add_edge("analyzer", END)
    return graph.compile()


async def run_scan(*, scan_context: ScanContext, snapshot: MarketSnapshot) -> AgentState:
    """Build the graph and run one scan to completion.

    Thin convenience wrapper. The eventual Step 1.12 scan runner will likely
    cache a compiled graph across symbols rather than rebuilding per call;
    this wrapper exists so tests and ad-hoc usage can stay one-liner.
    """
    graph = build_graph()
    initial: AgentState = {
        "scan_context": scan_context,
        "snapshot": snapshot,
        "proposal": None,
        "decision": None,
    }
    # LangGraph's ainvoke returns dict[str, Any] generically; the runtime
    # contents conform to AgentState because that's the schema we registered.
    return cast("AgentState", await graph.ainvoke(initial))
