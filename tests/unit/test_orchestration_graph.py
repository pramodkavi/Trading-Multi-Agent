"""End-to-end tests for the Slice 1 LangGraph shell.

Coverage per SPEC §4 Step 1.7 acceptance:
- graph compiles without errors
- bullish snapshot -> final state has SignalProposal + decision=PUBLISH
- ranging snapshot -> final state has SkipDecision + decision=SKIP
- insufficient data -> SkipDecision DATA_UNAVAILABLE + decision=SKIP
- scan_context and snapshot are preserved through node execution

Note: we reuse the synthetic-kline factories from test_smc_analyzer by
re-implementing minimal versions locally, rather than importing private
helpers across test modules. Tests should be self-contained.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.agents.orchestration import AgentState, build_graph, run_scan
from src.common.models import (
    JudgeRuling,
    ScanContext,
    ScanSession,
    SignalProposal,
    SkipDecision,
    SkipReason,
)
from src.providers import Kline, MarketSnapshot, Timeframe

# ---------------------------------------------------------------------------
# Synthetic-kline factories (local copies; see test_smc_analyzer for context)
# ---------------------------------------------------------------------------

_ANCHOR = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)


def _kline(
    minutes_offset: int,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> Kline:
    return Kline(
        open_time=_ANCHOR + timedelta(minutes=minutes_offset),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100.0,
    )


def _flat(idx: int, *, level: float) -> Kline:
    return _kline(idx * 240, open_=level, high=level + 0.1, low=level - 0.1, close=level)


def _swing_high(idx: int, *, peak: float, base: float) -> Kline:
    return _kline(idx * 240, open_=base, high=peak, low=base - 0.1, close=base)


def _swing_low(idx: int, *, trough: float, base: float) -> Kline:
    return _kline(idx * 240, open_=base, high=base + 0.1, low=trough, close=base)


def _bullish_series() -> list[Kline]:
    """HH+HL staircase (mirrors test_smc_analyzer._bullish_series)."""
    candles: list[Kline] = []
    for i in range(0, 5):
        candles.append(_flat(i, level=100.0))
    candles.append(_swing_low(5, trough=99.0, base=100.0))
    for i in range(6, 10):
        candles.append(_flat(i, level=101.0))
    candles.append(_swing_high(10, peak=103.0, base=101.5))
    for i in range(11, 15):
        candles.append(_flat(i, level=102.0))
    candles.append(_swing_low(15, trough=101.0, base=102.0))
    for i in range(16, 20):
        candles.append(_flat(i, level=103.0))
    candles.append(_swing_high(20, peak=106.0, base=103.5))
    for i in range(21, 30):
        candles.append(_flat(i, level=104.0))
    return candles


def _ranging_series() -> list[Kline]:
    candles: list[Kline] = []
    for i in range(0, 5):
        candles.append(_flat(i, level=100.0))
    candles.append(_swing_low(5, trough=99.0, base=100.0))
    for i in range(6, 10):
        candles.append(_flat(i, level=100.5))
    candles.append(_swing_high(10, peak=102.0, base=100.5))
    for i in range(11, 15):
        candles.append(_flat(i, level=100.0))
    candles.append(_swing_low(15, trough=98.0, base=100.0))  # LL
    for i in range(16, 20):
        candles.append(_flat(i, level=101.0))
    candles.append(_swing_high(20, peak=103.0, base=101.0))  # HH (mixed)
    for i in range(21, 30):
        candles.append(_flat(i, level=101.0))
    return candles


def _snapshot(candles: list[Kline], symbol: str = "BTCUSDT") -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        venue="binance",
        fetched_at=datetime(2026, 5, 26, 0, 0, 0, tzinfo=UTC),
        klines={Timeframe.H4: candles},
    )


def _scan_context(symbol: str = "BTCUSDT") -> ScanContext:
    return ScanContext(
        session=ScanSession.LONDON,
        symbols=[symbol],
        strategy="smc",
    )


# ---------------------------------------------------------------------------
# Graph compilation
# ---------------------------------------------------------------------------


class TestGraphCompilation:
    def test_build_graph_returns_compiled_object(self) -> None:
        graph = build_graph()
        # CompiledStateGraph exposes .ainvoke / .invoke; sanity-check we got
        # a working object rather than a builder.
        assert hasattr(graph, "ainvoke")
        assert hasattr(graph, "invoke")


# ---------------------------------------------------------------------------
# End-to-end: bullish path
# ---------------------------------------------------------------------------


class TestBullishGraphRun:
    async def test_bullish_snapshot_yields_publish_decision(self) -> None:
        ctx = _scan_context()
        snap = _snapshot(_bullish_series())
        final = await run_scan(scan_context=ctx, snapshot=snap)

        assert final["decision"] is JudgeRuling.PUBLISH
        assert isinstance(final["proposal"], SignalProposal)

    async def test_proposal_carries_scan_id(self) -> None:
        ctx = _scan_context()
        snap = _snapshot(_bullish_series())
        final = await run_scan(scan_context=ctx, snapshot=snap)

        assert isinstance(final["proposal"], SignalProposal)
        assert final["proposal"].scan_id == ctx.scan_id

    async def test_scan_context_preserved_in_final_state(self) -> None:
        ctx = _scan_context()
        snap = _snapshot(_bullish_series())
        final = await run_scan(scan_context=ctx, snapshot=snap)

        assert final["scan_context"].scan_id == ctx.scan_id
        assert final["scan_context"].strategy == "smc"

    async def test_snapshot_preserved_in_final_state(self) -> None:
        ctx = _scan_context()
        snap = _snapshot(_bullish_series())
        final = await run_scan(scan_context=ctx, snapshot=snap)

        # The snapshot we seeded must survive node execution unchanged.
        assert final["snapshot"].symbol == "BTCUSDT"
        assert Timeframe.H4 in final["snapshot"].klines


# ---------------------------------------------------------------------------
# End-to-end: skip paths
# ---------------------------------------------------------------------------


class TestSkipGraphRuns:
    async def test_ranging_snapshot_yields_skip_decision(self) -> None:
        ctx = _scan_context()
        snap = _snapshot(_ranging_series())
        final = await run_scan(scan_context=ctx, snapshot=snap)

        assert final["decision"] is JudgeRuling.SKIP
        assert isinstance(final["proposal"], SkipDecision)
        assert final["proposal"].reason is SkipReason.NO_CLEAR_BIAS

    async def test_insufficient_data_yields_data_unavailable_skip(self) -> None:
        # Only 10 candles -> below MIN_KLINES_REQUIRED.
        candles = [_flat(i, level=100.0) for i in range(10)]
        ctx = _scan_context()
        snap = _snapshot(candles)
        final = await run_scan(scan_context=ctx, snapshot=snap)

        assert final["decision"] is JudgeRuling.SKIP
        assert isinstance(final["proposal"], SkipDecision)
        assert final["proposal"].reason is SkipReason.DATA_UNAVAILABLE


# ---------------------------------------------------------------------------
# Direct ainvoke (bypassing run_scan) so we can probe the contract
# ---------------------------------------------------------------------------


class TestDirectGraphInvoke:
    async def test_ainvoke_with_partial_initial_state(self) -> None:
        """Caller may seed only the keys they own; proposal/decision come back
        populated by analyzer_node. LangGraph deep-merges node return values."""
        graph = build_graph()
        ctx = _scan_context()
        snap = _snapshot(_bullish_series())

        initial: AgentState = {
            "scan_context": ctx,
            "snapshot": snap,
            "proposal": None,
            "decision": None,
        }
        final = await graph.ainvoke(initial)

        assert final["decision"] is JudgeRuling.PUBLISH
        assert isinstance(final["proposal"], SignalProposal)

    async def test_symbol_propagated_through_graph(self) -> None:
        ctx = _scan_context(symbol="ETHUSDT")
        snap = _snapshot(_bullish_series(), symbol="ETHUSDT")
        final = await run_scan(scan_context=ctx, snapshot=snap)

        assert isinstance(final["proposal"], SignalProposal)
        assert final["proposal"].symbol == "ETHUSDT"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    async def test_same_inputs_same_outputs(self) -> None:
        """The Slice 1 graph is deterministic (no LLM in the loop yet).
        Two identical scan_id inputs should produce identical results."""
        ctx = _scan_context()
        snap = _snapshot(_bullish_series())
        final_a = await run_scan(scan_context=ctx, snapshot=snap)
        final_b = await run_scan(scan_context=ctx, snapshot=snap)

        assert isinstance(final_a["proposal"], SignalProposal)
        assert isinstance(final_b["proposal"], SignalProposal)
        assert final_a["proposal"].entry_price == pytest.approx(final_b["proposal"].entry_price)
        assert final_a["proposal"].stop_loss == pytest.approx(final_b["proposal"].stop_loss)
