"""Tests for src.agents.analyzer.smc_analyzer with synthetic 4H kline data.

Coverage per SPEC §4 Step 1.5 acceptance:
- bullish (HH+HL) -> SignalProposal LONG
- bearish (LH+LL) -> SignalProposal SHORT
- ranging / consolidation -> SkipDecision (NO_CLEAR_BIAS)
- insufficient history -> SkipDecision (DATA_UNAVAILABLE)
- missing H4 timeframe -> SkipDecision (DATA_UNAVAILABLE)

The synthetic-candle factory in this module builds OHLC series with controlled
swing pivots. That gives us deterministic assertions about the analyzer's
output without depending on real market data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.agents.analyzer import analyze
from src.common.models import SignalDirection, SignalProposal, SkipDecision, SkipReason
from src.providers import Kline, MarketSnapshot, Timeframe

# ---------------------------------------------------------------------------
# Synthetic-kline builders
# ---------------------------------------------------------------------------

_ANCHOR = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)


def _kline(
    minutes_offset: int,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 100.0,
) -> Kline:
    """Build one Kline at a fixed offset from the anchor (most-recent-last)."""
    return Kline(
        open_time=_ANCHOR + timedelta(minutes=minutes_offset),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _flat(idx: int, *, level: float) -> Kline:
    """Plain candle around `level` — small wick, not a pivot."""
    return _kline(
        idx * 240,  # 4h spacing
        open_=level,
        high=level + 0.1,
        low=level - 0.1,
        close=level,
    )


def _swing_high(idx: int, *, peak: float, base: float) -> Kline:
    """Candle whose high spikes to `peak`; OHL all stay <= peak."""
    return _kline(
        idx * 240,
        open_=base,
        high=peak,
        low=base - 0.1,
        close=base,
    )


def _swing_low(idx: int, *, trough: float, base: float) -> Kline:
    """Candle whose low dips to `trough`; OHC all stay >= trough."""
    return _kline(
        idx * 240,
        open_=base,
        high=base + 0.1,
        low=trough,
        close=base,
    )


def _snapshot(candles: list[Kline], symbol: str = "BTCUSDT") -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        venue="binance",
        fetched_at=datetime(2026, 5, 26, 0, 0, 0, tzinfo=UTC),
        klines={Timeframe.H4: candles},
    )


def _bullish_series() -> list[Kline]:
    """4H staircase up: every ~10 candles, a higher swing high and higher swing low.

    Layout (index : role):
      0-4   flat ~100
      5     swing low @ 99    (pivot — base flat = 100)
      6-9   flat ~101
      10    swing high @ 103   (HIGH-1)
      11-14 flat ~102
      15    swing low @ 101    (LOW-2: higher than LOW-1=99 -> HL)
      16-19 flat ~103
      20    swing high @ 106   (HIGH-2: higher than HIGH-1=103 -> HH)
      21-29 flat ~104 (latest_close ~104)
    """
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


def _bearish_series() -> list[Kline]:
    """4H staircase down: LH + LL pattern."""
    candles: list[Kline] = []
    for i in range(0, 5):
        candles.append(_flat(i, level=104.0))
    candles.append(_swing_high(5, peak=106.0, base=104.0))
    for i in range(6, 10):
        candles.append(_flat(i, level=103.0))
    candles.append(_swing_low(10, trough=101.0, base=103.0))
    for i in range(11, 15):
        candles.append(_flat(i, level=102.0))
    candles.append(_swing_high(15, peak=103.5, base=102.0))  # LH (< 106)
    for i in range(16, 20):
        candles.append(_flat(i, level=101.0))
    candles.append(_swing_low(20, trough=99.0, base=101.0))  # LL (< 101)
    for i in range(21, 30):
        candles.append(_flat(i, level=100.0))
    return candles


def _ranging_series() -> list[Kline]:
    """4H choppy series: alternating pivots that produce neither HH+HL nor LH+LL."""
    candles: list[Kline] = []
    for i in range(0, 5):
        candles.append(_flat(i, level=100.0))
    candles.append(_swing_low(5, trough=99.0, base=100.0))
    for i in range(6, 10):
        candles.append(_flat(i, level=100.5))
    candles.append(_swing_high(10, peak=102.0, base=100.5))
    for i in range(11, 15):
        candles.append(_flat(i, level=100.0))
    candles.append(_swing_low(15, trough=98.0, base=100.0))  # LOWER low (vs 99)
    for i in range(16, 20):
        candles.append(_flat(i, level=101.0))
    candles.append(_swing_high(20, peak=103.0, base=101.0))  # HIGHER high (vs 102)
    for i in range(21, 30):
        candles.append(_flat(i, level=101.0))
    return candles


# ---------------------------------------------------------------------------
# Bullish (UPTREND) path
# ---------------------------------------------------------------------------


class TestBullishBias:
    def test_returns_long_signal_proposal(self) -> None:
        result = analyze(_snapshot(_bullish_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert result.direction is SignalDirection.LONG

    def test_proposal_geometry_is_consistent(self) -> None:
        # The SignalProposal model has its own R:R consistency validator; if
        # we get here without ValidationError, geometry is already valid.
        # Additionally check the stub used the swing-low anchor below entry.
        result = analyze(_snapshot(_bullish_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert result.stop_loss < result.entry_price
        assert result.take_profit_1 > result.entry_price
        assert result.risk_reward_ratio == pytest.approx(3.0)

    def test_tags_include_slice1_stub_marker(self) -> None:
        result = analyze(_snapshot(_bullish_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert "slice-1-stub" in result.tags
        assert "bias-uptrend" in result.tags

    def test_features_include_htf_bias(self) -> None:
        result = analyze(_snapshot(_bullish_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert result.features["htf_bias"] == "UPTREND"
        assert "latest_swing_low" in result.features

    def test_scan_id_propagated(self) -> None:
        sid = uuid4()
        result = analyze(_snapshot(_bullish_series()), scan_id=sid)
        assert isinstance(result, SignalProposal)
        assert result.scan_id == sid


# ---------------------------------------------------------------------------
# Bearish (DOWNTREND) path
# ---------------------------------------------------------------------------


class TestBearishBias:
    def test_returns_short_signal_proposal(self) -> None:
        result = analyze(_snapshot(_bearish_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert result.direction is SignalDirection.SHORT

    def test_short_proposal_geometry(self) -> None:
        result = analyze(_snapshot(_bearish_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert result.stop_loss > result.entry_price
        assert result.take_profit_1 < result.entry_price
        assert result.risk_reward_ratio == pytest.approx(3.0)

    def test_short_tags_marker(self) -> None:
        result = analyze(_snapshot(_bearish_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert "bias-downtrend" in result.tags


# ---------------------------------------------------------------------------
# Ranging / CONSOLIDATION path
# ---------------------------------------------------------------------------


class TestRangingBias:
    def test_returns_skip_decision_with_no_clear_bias(self) -> None:
        result = analyze(_snapshot(_ranging_series()), scan_id=uuid4())
        assert isinstance(result, SkipDecision)
        assert result.reason is SkipReason.NO_CLEAR_BIAS

    def test_skip_details_mention_consolidation(self) -> None:
        result = analyze(_snapshot(_ranging_series()), scan_id=uuid4())
        assert isinstance(result, SkipDecision)
        assert "CONSOLIDATION" in result.details


# ---------------------------------------------------------------------------
# Insufficient data
# ---------------------------------------------------------------------------


class TestInsufficientData:
    def test_short_history_returns_skip(self) -> None:
        candles = [_flat(i, level=100.0) for i in range(0, 10)]  # below MIN_KLINES_REQUIRED
        result = analyze(_snapshot(candles), scan_id=uuid4())
        assert isinstance(result, SkipDecision)
        assert result.reason is SkipReason.DATA_UNAVAILABLE
        assert "Insufficient" in result.details

    def test_missing_h4_timeframe_returns_skip(self) -> None:
        # Snapshot with only D1, no H4 — Slice 1 analyzer requires H4.
        snap = MarketSnapshot(
            symbol="BTCUSDT",
            venue="binance",
            fetched_at=datetime(2026, 5, 26, 0, 0, 0, tzinfo=UTC),
            klines={Timeframe.D1: [_flat(i, level=100.0) for i in range(0, 5)]},
        )
        result = analyze(snap, scan_id=uuid4())
        assert isinstance(result, SkipDecision)
        assert result.reason is SkipReason.DATA_UNAVAILABLE
        assert "no 4H klines" in result.details


# ---------------------------------------------------------------------------
# Stale pivot rejection
# ---------------------------------------------------------------------------


class TestStalePivots:
    def test_old_pivots_classified_as_consolidation(self) -> None:
        """If the most recent pivot is older than MAX_PIVOT_AGE (20) candles,
        the analyzer must NOT call it a trend even if the structure is HH+HL.
        """
        # Build a bullish series but then append 30 flat candles, pushing the
        # last pivot well out of the freshness window.
        candles = _bullish_series()
        for i in range(30, 70):
            candles.append(_flat(i, level=104.0))
        result = analyze(_snapshot(candles), scan_id=uuid4())
        assert isinstance(result, SkipDecision)
        assert result.reason is SkipReason.NO_CLEAR_BIAS


# ---------------------------------------------------------------------------
# Strategy & symbol propagation
# ---------------------------------------------------------------------------


class TestPropagation:
    def test_default_strategy_is_smc(self) -> None:
        result = analyze(_snapshot(_bullish_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal | SkipDecision) and result.strategy == "smc"

    def test_custom_strategy_name_propagated(self) -> None:
        result = analyze(_snapshot(_bullish_series()), scan_id=uuid4(), strategy="smc-v2")
        assert result.strategy == "smc-v2"

    def test_symbol_propagated_on_skip(self) -> None:
        result = analyze(_snapshot(_ranging_series(), symbol="ETHUSDT"), scan_id=uuid4())
        assert isinstance(result, SkipDecision)
        assert result.symbol == "ETHUSDT"
