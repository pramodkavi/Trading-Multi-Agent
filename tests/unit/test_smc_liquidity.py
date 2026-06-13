"""Tests for the SMC liquidity layer (Step 2.1d).

Covers stop-hunt sweeps (SSL/BSL), the sweep-vs-break distinction, equal-high
clustering (strength), nearest resting targets, edge cases, and the
as-of-correctness invariant on sweeps (the layer's as-of-correct events).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.agents.analyzer.smc import analyze_liquidity
from src.agents.analyzer.smc.models import (
    LiquiditySide,
    LiquidityStrength,
    PoolStatus,
    SweepType,
)
from src.providers import Kline

_ANCHOR = datetime(2026, 5, 1, tzinfo=UTC)


def _c(idx: int, o: float, h: float, low: float, c: float, vol: float = 100.0) -> Kline:
    return Kline(
        open_time=_ANCHOR + timedelta(hours=4 * idx),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=vol,
    )


def _flat(idx: int, level: float) -> Kline:
    return _c(idx, level, level + 0.5, level - 0.5, level)


def _ssl_sweep_series() -> list[Kline]:
    """Swing low @4 (95); candle @8 wicks to 93 but closes back at 99 -> SWEEP_SSL."""
    c = [_flat(i, 100.0) for i in range(4)]
    c.append(_c(4, 100.0, 100.5, 95.0, 100.0))  # swing low 95
    c += [_flat(i, 100.0) for i in range(5, 8)]
    c.append(_c(8, 99.0, 99.5, 93.0, 99.0))  # wick 93 < 95, body 99 > 95 -> sweep
    c += [_flat(i, 100.0) for i in range(9, 14)]
    return c


def _bsl_sweep_series() -> list[Kline]:
    """Swing high @4 (106); candle @8 wicks to 108 but closes back at 101 -> SWEEP_BSL."""
    c = [_flat(i, 100.0) for i in range(4)]
    c.append(_c(4, 100.0, 106.0, 99.5, 100.0))  # swing high 106
    c += [_flat(i, 100.0) for i in range(5, 8)]
    c.append(_c(8, 101.0, 108.0, 100.5, 101.0))  # wick 108 > 106, body 101 < 106 -> sweep
    c += [_flat(i, 100.0) for i in range(9, 14)]
    return c


def _break_not_sweep_series() -> list[Kline]:
    """Swing low @4 (95); candle @8 CLOSES at 94 (below 95) -> BROKEN, not a sweep."""
    c = [_flat(i, 100.0) for i in range(4)]
    c.append(_c(4, 100.0, 100.5, 95.0, 100.0))
    c += [_flat(i, 100.0) for i in range(5, 8)]
    c.append(_c(8, 99.0, 99.5, 93.0, 94.0))  # body close 94 < 95 -> break
    c += [_flat(i, 94.0) for i in range(9, 14)]
    return c


def _equal_highs_series() -> list[Kline]:
    """Two swing highs at 106.00 and 105.98 (within tolerance) -> EQUAL strength."""
    c = [_flat(i, 100.0) for i in range(4)]
    c.append(_c(4, 100.0, 106.0, 99.5, 100.0))  # swing high 106.00
    c += [_flat(i, 100.0) for i in range(5, 12)]
    c.append(_c(12, 100.0, 105.98, 99.5, 100.0))  # swing high 105.98 (~equal)
    c += [_flat(i, 100.0) for i in range(13, 18)]
    return c


def _targets_series() -> list[Kline]:
    """Resting BSL @110 above and resting SSL @90 below; price ends at 100."""
    c = [_flat(i, 100.0) for i in range(4)]
    c.append(_c(4, 100.0, 110.0, 99.5, 100.0))  # swing high 110 (never swept)
    c += [_flat(i, 100.0) for i in range(5, 8)]
    c.append(_c(8, 100.0, 100.5, 90.0, 100.0))  # swing low 90 (never swept)
    c += [_flat(i, 100.0) for i in range(9, 14)]
    return c


def _multi_sweep_series() -> list[Kline]:
    """SWEEP_SSL @8 (level 95) then SWEEP_BSL @16 (level 107)."""
    c = [_flat(i, 100.0) for i in range(4)]
    c.append(_c(4, 100.0, 100.5, 95.0, 100.0))  # swing low 95
    c += [_flat(i, 100.0) for i in range(5, 8)]
    c.append(_c(8, 99.0, 99.5, 93.0, 99.0))  # SWEEP_SSL
    c += [_flat(i, 101.0) for i in range(9, 12)]
    c.append(_c(12, 101.0, 107.0, 100.5, 101.0))  # swing high 107
    c += [_flat(i, 101.0) for i in range(13, 16)]
    c.append(_c(16, 102.0, 109.0, 101.5, 102.0))  # SWEEP_BSL
    c += [_flat(i, 102.0) for i in range(17, 20)]
    return c


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------


class TestSweeps:
    def test_ssl_sweep_detected(self) -> None:
        result = analyze_liquidity(_ssl_sweep_series())
        ssl_sweeps = [s for s in result.sweeps if s.sweep_type is SweepType.SWEEP_SSL]
        assert any(s.index == 8 and s.swept_level == 95.0 for s in ssl_sweeps)
        pool = next(p for p in result.pools if p.price == 95.0)
        assert pool.status is PoolStatus.SWEPT
        assert pool.resolved_index == 8

    def test_bsl_sweep_detected(self) -> None:
        result = analyze_liquidity(_bsl_sweep_series())
        bsl_sweeps = [s for s in result.sweeps if s.sweep_type is SweepType.SWEEP_BSL]
        assert any(s.index == 8 and s.swept_level == 106.0 for s in bsl_sweeps)

    def test_body_close_through_is_break_not_sweep(self) -> None:
        result = analyze_liquidity(_break_not_sweep_series())
        assert result.sweeps == []
        pool = next(p for p in result.pools if p.price == 95.0)
        assert pool.status is PoolStatus.BROKEN


# ---------------------------------------------------------------------------
# Equal highs / strength
# ---------------------------------------------------------------------------


class TestEqualLevels:
    def test_equal_highs_marked_equal_strength(self) -> None:
        result = analyze_liquidity(_equal_highs_series())
        bsl = [p for p in result.pools if p.side is LiquiditySide.BUY_SIDE]
        assert len(bsl) == 2
        assert all(p.equal_count == 2 for p in bsl)
        assert all(p.strength is LiquidityStrength.EQUAL for p in bsl)

    def test_isolated_swing_is_single_strength(self) -> None:
        result = analyze_liquidity(_targets_series())
        bsl = next(p for p in result.pools if p.side is LiquiditySide.BUY_SIDE)
        assert bsl.equal_count == 1
        assert bsl.strength is LiquidityStrength.SINGLE


# ---------------------------------------------------------------------------
# Nearest resting targets
# ---------------------------------------------------------------------------


class TestTargets:
    def test_nearest_resting_pools(self) -> None:
        result = analyze_liquidity(_targets_series())
        assert result.nearest_bsl == 110.0
        assert result.nearest_ssl == 90.0

    def test_swept_pool_is_not_a_target(self) -> None:
        # The swept SSL @95 must not be reported as a resting target.
        result = analyze_liquidity(_ssl_sweep_series())
        assert result.nearest_ssl != 95.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_series_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one candle"):
            analyze_liquidity([])

    def test_flat_series_has_no_pools(self) -> None:
        result = analyze_liquidity([_flat(i, 100.0) for i in range(20)])
        assert result.pools == []
        assert result.sweeps == []
        assert result.nearest_bsl is None
        assert result.nearest_ssl is None


# ---------------------------------------------------------------------------
# As-of correctness (sweeps)
# ---------------------------------------------------------------------------


class TestAsOfCorrectness:
    def test_truncation_preserves_sweeps(self) -> None:
        series = _multi_sweep_series()
        full = analyze_liquidity(series)
        full_sweeps = [(s.sweep_type, s.index, s.swept_level, s.swing_index) for s in full.sweeps]
        for t in range(len(series)):
            prefix = analyze_liquidity(series[: t + 1])
            got = [(s.sweep_type, s.index, s.swept_level, s.swing_index) for s in prefix.sweeps]
            expected = [s for s in full_sweeps if s[1] <= t]
            assert got == expected, f"sweeps diverged when truncated at bar {t}"
