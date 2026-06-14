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

from typing import TYPE_CHECKING, Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from src.agents.analyzer import analyze

# Runtime (not TYPE_CHECKING) imports: LangGraph resolves AgentState's annotations
# via get_type_hints() at StateGraph construction, so every type referenced in
# AgentState must exist at runtime. The historian / skeptic / judge packages
# import AgentState only under TYPE_CHECKING, so this direction introduces no
# cycle. The node factories are imported here too for build_pipeline_graph.
from src.agents.historian import HistorianReport, make_historian_node
from src.agents.judge import JudgeDecision, make_judge_node
from src.agents.skeptic import SkepticObjection, make_skeptic_node
from src.common.models import (
    JudgeRuling,
    ScanContext,
    SignalProposal,
    SkipDecision,
)
from src.common.tracing import trace_node
from src.providers import MarketSnapshot, NoMacroData

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

    from langgraph.checkpoint.base import BaseCheckpointSaver

    from src.agents.historian import HistorianRepository
    from src.agents.judge import Judge
    from src.agents.skeptic import Skeptic

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
        judge_decision   : set by the judge node (Step 2.6's make_judge_node):
                           the full JudgeDecision (ruling + confidence +
                           reasoning + caveat), or None for skips / when the node
                           is not wired. Edge added in Step 2.7.
        decision         : set by analyzer_node (stub) -> overwritten by
                           judge_node (Step 2.6) with judge_decision.ruling, so
                           the existing dispatcher keeps consuming a JudgeRuling.
    """

    scan_context: ScanContext
    snapshot: MarketSnapshot
    proposal: SignalProposal | SkipDecision | None
    historian_report: HistorianReport | None
    skeptic_objection: SkepticObjection | NoMacroData | None
    judge_decision: JudgeDecision | None
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


# ---------------------------------------------------------------------------
# Full Slice 2 pipeline graph (Step 2.7)
# ---------------------------------------------------------------------------


def _route_after_analyzer(state: AgentState) -> str:
    """Conditional edge after the analyzer (SPEC Step 2.7).

    Continue into historian -> skeptic -> judge only when the Analyzer produced
    a real ``SignalProposal``. On a ``SkipDecision`` (or nothing) there is no
    setup to retrieve precedents for, object to, or judge, so we short-circuit
    to END -- the analyzer node already stamped a SKIP ``decision``.
    """
    return "continue" if isinstance(state.get("proposal"), SignalProposal) else "skip"


def build_pipeline_graph(
    *,
    historian: HistorianRepository,
    skeptic: Skeptic,
    judge: Judge,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    tracer: Callable[[str, Any], Any] = trace_node,
) -> Any:
    """Compile the full per-signal pipeline: analyzer -> historian -> skeptic -> judge.

    The three downstream agents are injected (their dependencies -- store,
    macro providers, Anthropic client -- live in the agent objects, never in the
    checkpointed state). A conditional edge after the analyzer short-circuits a
    SkipDecision straight to END, so skips cost no LLM calls.

    Args:
        historian / skeptic / judge: the constructed agents to bind into the
            historian / skeptic / judge nodes via their factories.
        checkpointer: optional LangGraph checkpointer for crash-resume
            (NFR-1.3). The local / asyncpg path supplies an AsyncPostgresSaver;
            the Data API Lambda passes None (it has no direct Postgres socket --
            see docs/PROJECT_STATE.md). None compiles a stateless graph.
        tracer: wraps each node for observability; defaults to the env-gated
            Langfuse ``trace_node`` (a transparent no-op until LANGFUSE_* is set).
            Injectable so tests can assert every node is wrapped.

    Returned graph is typed ``Any`` for the same reason as ``build_graph``: the
    CompiledStateGraph generics differ between local mypy and the pre-commit
    hook's isolated environment. Callers use the standard ``.ainvoke`` surface.

    NOTE: this builder is not yet on the live scan path -- ``run_scan`` /
    ``build_graph`` remain analyzer-only until the Step 2.7 follow-up wires the
    pipeline into the scan runner (Telegram + per-agent persistence).
    """
    nodes: dict[str, Callable[[AgentState], Awaitable[AgentState]]] = {
        "analyzer": analyzer_node,
        "historian": make_historian_node(historian),
        "skeptic": make_skeptic_node(skeptic),
        "judge": make_judge_node(judge),
    }

    graph = StateGraph(AgentState)
    for node_name, node_fn in nodes.items():
        graph.add_node(node_name, tracer(node_name, node_fn))

    graph.add_edge(START, "analyzer")
    graph.add_conditional_edges(
        "analyzer",
        _route_after_analyzer,
        {"continue": "historian", "skip": END},
    )
    graph.add_edge("historian", "skeptic")
    graph.add_edge("skeptic", "judge")
    graph.add_edge("judge", END)
    return graph.compile(checkpointer=checkpointer)


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
