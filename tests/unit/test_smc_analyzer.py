"""Tests for the full SMC analyzer (Step 2.1e) via the public `analyze` entry.

As of Step 2.1e `analyze` delegates to the full 5-layer SMC assembly (structure →
liquidity → order blocks → FVG → hard gates + evidence-weighted confluence),
replacing the Slice-1 HTF-bias stub. These series are hand-built (and verified
against the running pipeline) to exercise each gate path:

- LONG / SHORT publish: clear bias + correct zone + a fresh order-block POI + a
  resting liquidity target.
- PREMIUM_DISCOUNT_VIOLATION: right bias, wrong zone.
- NO_CLEAR_BIAS: consolidation.
- DATA_UNAVAILABLE: insufficient history.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.agents.analyzer import analyze
from src.common.models import SignalDirection, SignalProposal, SkipDecision, SkipReason
from src.providers import Kline, MarketSnapshot, Timeframe

_ANCHOR = datetime(2026, 5, 1, tzinfo=UTC)


def _c(i: int, o: float, h: float, low: float, cl: float) -> Kline:
    return Kline(
        open_time=_ANCHOR + timedelta(hours=4 * i),
        open=o,
        high=h,
        low=low,
        close=cl,
        volume=100.0,
    )


def _flat(i: int, level: float) -> Kline:
    return _c(i, level, level + 0.5, level - 0.5, level)


def _long_series() -> list[Kline]:
    """Bullish BOS then a shallow pullback into discount over a fresh demand OB."""
    s = [_flat(i, 100.0) for i in range(10)]
    s.append(_c(10, 100.0, 105.0, 99.5, 100.0))  # swing high 105
    s += [_flat(11, 100.0), _flat(12, 100.0)]
    s.append(_c(13, 101.0, 101.5, 98.5, 99.0))  # demand OB (bearish candle)
    s.append(_c(14, 99.0, 104.0, 99.0, 103.5))  # impulse up
    s.append(_c(15, 103.5, 108.0, 103.0, 107.0))  # BOS above 105
    s.append(_c(16, 107.0, 110.0, 106.5, 109.0))  # rally high 110 (resting BSL target)
    s += [_flat(17, 107.5), _flat(18, 107.5)]
    s.append(_c(19, 106.0, 106.5, 102.0, 103.0))  # higher-low pullback (holds uptrend)
    s += [_flat(i, 103.5) for i in range(20, 32)]  # price rests in discount
    return s


def _short_series() -> list[Kline]:
    """Mirror of the long setup: bearish BOS then a pullback into premium under supply."""
    s = [_flat(i, 100.0) for i in range(10)]
    s.append(_c(10, 100.0, 100.5, 95.0, 100.0))  # swing low 95
    s += [_flat(11, 100.0), _flat(12, 100.0)]
    s.append(_c(13, 99.0, 101.5, 98.5, 101.0))  # supply OB (bullish candle)
    s.append(_c(14, 101.0, 101.0, 96.0, 96.5))  # impulse down
    s.append(_c(15, 96.5, 97.0, 92.0, 93.0))  # BOS below 95
    s.append(_c(16, 93.0, 93.5, 90.0, 91.0))  # drop low 90 (resting SSL target)
    s += [_flat(17, 92.5), _flat(18, 92.5)]
    s.append(_c(19, 94.0, 98.0, 93.5, 97.0))  # lower-high pullback (holds downtrend)
    s += [_flat(i, 96.5) for i in range(20, 32)]  # price rests in premium
    return s


def _premium_series() -> list[Kline]:
    """Bullish BOS but price stays elevated -> LONG bias in PREMIUM (violation)."""
    s = [_flat(i, 100.0) for i in range(10)]
    s.append(_c(10, 100.0, 105.0, 99.5, 100.0))
    s += [_flat(11, 100.0), _flat(12, 100.0)]
    s.append(_c(13, 101.0, 101.5, 98.5, 99.0))
    s.append(_c(14, 99.0, 104.0, 99.0, 103.5))
    s.append(_c(15, 103.5, 108.0, 103.0, 107.0))
    s.append(_c(16, 107.0, 110.0, 106.5, 109.0))
    s += [_flat(i, 108.0) for i in range(17, 32)]
    return s


def _snap(candles: list[Kline], timeframe: Timeframe = Timeframe.H4) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        venue="binance",
        fetched_at=_ANCHOR,
        klines={timeframe: candles},
    )


# ---------------------------------------------------------------------------
# Publish paths
# ---------------------------------------------------------------------------


class TestLongSetup:
    def test_returns_long_proposal(self) -> None:
        result = analyze(_snap(_long_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert result.direction is SignalDirection.LONG

    def test_long_geometry_is_valid(self) -> None:
        result = analyze(_snap(_long_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert result.stop_loss < result.entry_price < result.take_profit_1
        assert result.risk_reward_ratio > 0

    def test_long_tags_and_features(self) -> None:
        result = analyze(_snap(_long_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert "bullish-ob" in result.tags
        assert "discount" in result.tags
        assert "bias-uptrend" in result.tags
        assert result.features["phase"] == "UPTREND"
        assert result.features["zone"] == "DISCOUNT"
        assert "confluence_score" in result.features


class TestShortSetup:
    def test_returns_short_proposal(self) -> None:
        result = analyze(_snap(_short_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert result.direction is SignalDirection.SHORT

    def test_short_geometry_is_valid(self) -> None:
        result = analyze(_snap(_short_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert result.stop_loss > result.entry_price > result.take_profit_1

    def test_short_tags(self) -> None:
        result = analyze(_snap(_short_series()), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert "bearish-ob" in result.tags
        assert "premium" in result.tags


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


class TestSkips:
    def test_premium_discount_violation(self) -> None:
        result = analyze(_snap(_premium_series()), scan_id=uuid4())
        assert isinstance(result, SkipDecision)
        assert result.reason is SkipReason.PREMIUM_DISCOUNT_VIOLATION
        assert result.violated_rule == "RULE_3_PREMIUM_DISCOUNT"

    def test_consolidation_is_no_clear_bias(self) -> None:
        result = analyze(_snap([_flat(i, 100.0) for i in range(30)]), scan_id=uuid4())
        assert isinstance(result, SkipDecision)
        assert result.reason is SkipReason.NO_CLEAR_BIAS

    def test_insufficient_history_is_data_unavailable(self) -> None:
        result = analyze(_snap([_flat(i, 100.0) for i in range(10)]), scan_id=uuid4())
        assert isinstance(result, SkipDecision)
        assert result.reason is SkipReason.DATA_UNAVAILABLE


# ---------------------------------------------------------------------------
# Timeframe fallback + propagation
# ---------------------------------------------------------------------------


class TestTimeframeAndPropagation:
    def test_falls_back_to_available_timeframe(self) -> None:
        # No H4, only D1 -> the analyzer uses D1 rather than skipping.
        result = analyze(_snap(_long_series(), timeframe=Timeframe.D1), scan_id=uuid4())
        assert isinstance(result, SignalProposal)
        assert result.direction is SignalDirection.LONG

    def test_scan_id_and_strategy_propagate(self) -> None:
        sid = uuid4()
        result = analyze(_snap(_long_series()), scan_id=sid, strategy="smc-v2")
        assert result.scan_id == sid
        assert result.strategy == "smc-v2"

    def test_symbol_propagates_on_skip(self) -> None:
        snap = MarketSnapshot(
            symbol="ETHUSDT",
            venue="binance",
            fetched_at=_ANCHOR,
            klines={Timeframe.H4: [_flat(i, 100.0) for i in range(30)]},
        )
        result = analyze(snap, scan_id=uuid4())
        assert isinstance(result, SkipDecision)
        assert result.symbol == "ETHUSDT"
