"""Full-pipeline graph test (Slice 2 Step 2.7) -- all agents mocked, fully offline.

Exercises build_pipeline_graph end to end:
- a publishing snapshot flows analyzer -> historian -> skeptic -> judge and the
  final state carries every agent's output + decision == the (mocked) ruling
- a skip snapshot short-circuits at the conditional edge: historian / skeptic /
  judge are never invoked (no LLM calls), decision == SKIP
- the optional checkpointer seam compiles and persists state (InMemorySaver,
  the bundled saver -- no new dependency)
- the tracer wraps every node

The real (pure-Python) Analyzer runs; the three downstream agents are mocked via
a fake store, fake macro provider, and mocked Anthropic clients.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from src.agents.historian import HistorianReport, HistorianRepository
from src.agents.judge import Judge, JudgeDecision
from src.agents.orchestration import AgentState, build_pipeline_graph
from src.agents.skeptic import Skeptic, SkepticObjection
from src.common.models import (
    JudgeRuling,
    ScanContext,
    ScanSession,
    SignalProposal,
    SkipDecision,
)
from src.providers import (
    DataProvider,
    Kline,
    MacroContext,
    MarketSnapshot,
    NoMacroData,
    Timeframe,
)

# ---------------------------------------------------------------------------
# Mocks / fakes
# ---------------------------------------------------------------------------


class FakeStore:
    """Records find_similar_signals calls; returns no precedents.

    Also serves the risk gate's reads (Step 2.11): no open setups and no recent
    signals, so every stateful hard rule passes by default.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def find_similar_signals(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        return []

    async def list_open_active_setups(self) -> list[Any]:
        return []

    async def get_signal(self, signal_id: Any) -> Any:
        return None

    async def list_recent_signals(self, **kwargs: Any) -> list[Any]:
        return []


class FakeMacroProvider(DataProvider):
    name = "fake"

    async def fetch_market_snapshot(
        self,
        symbol: str,
        timeframes: list[Timeframe],
        *,
        limit: int = 200,
        include_derivatives: bool = False,
    ) -> MarketSnapshot:
        raise NotImplementedError

    async def fetch_macro_context(self) -> MacroContext | NoMacroData:
        return MacroContext(fetched_at=datetime(2026, 6, 1, 12, tzinfo=UTC), dxy=104.0, vix=18.0)


def _fake_tool_response(payload: dict[str, Any], *, tool_name: str) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name=tool_name, id="toolu_p", input=payload)
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(input_tokens=100, output_tokens=40),
    )


def _mock_client(responses: list[Any]) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    return client


_OBJECTION: dict[str, Any] = {
    "severity": "LOW",
    "recommends_against": False,
    "headline": "Macro is broadly neutral for this setup.",
    "reasoning": "No strong cross-asset headwind is evident in the supplied snapshot.",
    "cited_macro": ["broad USD index 104.0"],
}
_RULING: dict[str, Any] = {
    "ruling": "PUBLISH",
    "confidence": "HIGH",
    "reasoning": "Clean structure, neutral macro, no decisive negative precedents.",
    "caveat": None,
}


def _skeptic(client: MagicMock | None) -> Skeptic:
    return Skeptic([FakeMacroProvider()], client=client)


def _build(
    store: FakeStore,
    skeptic_client: MagicMock,
    judge_client: MagicMock,
    **kwargs: Any,
) -> Any:
    return build_pipeline_graph(
        store=store,
        historian=HistorianRepository(store),
        skeptic=_skeptic(skeptic_client),
        judge=Judge(client=judge_client),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Synthetic klines (local copies; tests stay self-contained)
# ---------------------------------------------------------------------------

_ANCHOR = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)


def _c(idx: int, *, open_: float, high: float, low: float, close: float) -> Kline:
    return Kline(
        open_time=_ANCHOR + timedelta(hours=4 * idx),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100.0,
    )


def _flat(idx: int, *, level: float) -> Kline:
    return _c(idx, open_=level, high=level + 0.5, low=level - 0.5, close=level)


def _bullish_series() -> list[Kline]:
    """A full SMC LONG setup verified to publish AND clear the hard gates.

    Candle 16's high is 113 (not 110) so the nearest-BSL target yields R:R 3.45,
    clearing the 1:3 minimum (SPEC §1.6 rule 2) the risk gate now enforces; the
    confluence and DISCOUNT zone are unchanged.
    """
    s = [_flat(i, level=100.0) for i in range(10)]
    s.append(_c(10, open_=100.0, high=105.0, low=99.5, close=100.0))
    s += [_flat(11, level=100.0), _flat(12, level=100.0)]
    s.append(_c(13, open_=101.0, high=101.5, low=98.5, close=99.0))
    s.append(_c(14, open_=99.0, high=104.0, low=99.0, close=103.5))
    s.append(_c(15, open_=103.5, high=108.0, low=103.0, close=107.0))
    s.append(_c(16, open_=107.0, high=113.0, low=106.5, close=109.0))
    s += [_flat(17, level=107.5), _flat(18, level=107.5)]
    s.append(_c(19, open_=106.0, high=106.5, low=102.0, close=103.0))
    s += [_flat(i, level=103.5) for i in range(20, 32)]
    return s


def _ranging_series() -> list[Kline]:
    """Flat consolidation -> NO_CLEAR_BIAS skip."""
    return [_flat(i, level=100.0) for i in range(30)]


def _initial(candles: list[Kline], *, session: ScanSession = ScanSession.LONDON) -> AgentState:
    return {
        "scan_context": ScanContext(session=session, symbols=["BTCUSDT"], strategy="smc"),
        "snapshot": MarketSnapshot(
            symbol="BTCUSDT",
            venue="binance",
            fetched_at=datetime(2026, 5, 26, 0, 0, 0, tzinfo=UTC),
            klines={Timeframe.H4: candles},
        ),
        "proposal": None,
        "decision": None,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_pipeline_publishes_through_all_nodes() -> None:
    store = FakeStore()
    skeptic_client = _mock_client([_fake_tool_response(_OBJECTION, tool_name="emit_objection")])
    judge_client = _mock_client([_fake_tool_response(_RULING, tool_name="emit_ruling")])
    graph = _build(store, skeptic_client, judge_client)

    final = await graph.ainvoke(_initial(_bullish_series()))

    assert isinstance(final["proposal"], SignalProposal)
    assert isinstance(final["historian_report"], HistorianReport)
    assert isinstance(final["skeptic_objection"], SkepticObjection)
    assert isinstance(final["judge_decision"], JudgeDecision)
    assert final["decision"] is JudgeRuling.PUBLISH
    # Every downstream agent ran exactly once.
    assert len(store.calls) == 1
    skeptic_client.messages.create.assert_awaited_once()
    judge_client.messages.create.assert_awaited_once()


async def test_pipeline_skip_short_circuits_remaining_agents() -> None:
    store = FakeStore()
    skeptic_client = _mock_client([])
    judge_client = _mock_client([])
    graph = _build(store, skeptic_client, judge_client)

    final = await graph.ainvoke(_initial(_ranging_series()))

    assert isinstance(final["proposal"], SkipDecision)
    assert final["decision"] is JudgeRuling.SKIP
    assert final.get("judge_decision") is None
    # Conditional edge skipped historian / skeptic / judge entirely.
    assert store.calls == []
    skeptic_client.messages.create.assert_not_awaited()
    judge_client.messages.create.assert_not_awaited()


async def test_pipeline_risk_gate_force_skips_and_short_circuits() -> None:
    # A valid publishing setup, but the ASIAN session is hard-blocked (rule 7),
    # so the risk gate force-skips it BEFORE the historian/skeptic/judge run.
    store = FakeStore()
    skeptic_client = _mock_client([])
    judge_client = _mock_client([])
    graph = _build(store, skeptic_client, judge_client)

    final = await graph.ainvoke(_initial(_bullish_series(), session=ScanSession.ASIAN))

    assert isinstance(final["proposal"], SkipDecision)
    assert final["proposal"].violated_rule == "RULE_7_SESSION_BLOCK"
    assert final["decision"] is JudgeRuling.SKIP
    # The Analyzer's original proposal is preserved for the journal (FR-1.7).
    assert isinstance(final["rejected_proposal"], SignalProposal)
    assert not final["risk_gate_report"].passed
    # Downstream agents never ran.
    assert store.calls == []
    skeptic_client.messages.create.assert_not_awaited()
    judge_client.messages.create.assert_not_awaited()


async def test_pipeline_with_checkpointer_persists_state() -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    store = FakeStore()
    skeptic_client = _mock_client([_fake_tool_response(_OBJECTION, tool_name="emit_objection")])
    judge_client = _mock_client([_fake_tool_response(_RULING, tool_name="emit_ruling")])
    graph = _build(store, skeptic_client, judge_client, checkpointer=InMemorySaver())

    config = {"configurable": {"thread_id": "scan-1"}}
    final = await graph.ainvoke(_initial(_bullish_series()), config=config)
    assert final["decision"] is JudgeRuling.PUBLISH

    # The checkpointer persisted the terminal state for this thread.
    saved = await graph.aget_state(config)
    assert saved.values["decision"] is JudgeRuling.PUBLISH


def test_tracer_wraps_every_node() -> None:
    recorded: list[str] = []

    def recording_tracer(name: str, fn: Any) -> Any:
        recorded.append(name)
        return fn

    _build(FakeStore(), _mock_client([]), _mock_client([]), tracer=recording_tracer)

    assert sorted(recorded) == ["analyzer", "historian", "judge", "risk_gate", "skeptic"]
