"""Tests for the SMC Fair Value Gap detector (Step 2.1b).

Covers gap geometry (bullish/bearish), the no-gap case, ATR-normalized size and
displacement filters, mitigation/fill status as of the last candle, and the
as-of-correctness invariant: detecting on a prefix never changes the formation of
a gap already formed within that prefix, and a freshly-formed gap is unmitigated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.agents.analyzer.smc import detect_fvgs
from src.agents.analyzer.smc.models import FVGType
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


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _bullish_fvg_series() -> list[Kline]:
    """Candle 0 high=100, big up-displacement candle 1, candle 2 low=102 -> gap [100,102]."""
    return [
        _c(0, 99.0, 100.0, 98.5, 99.5),  # c1: high 100
        _c(1, 99.6, 103.0, 99.5, 102.5),  # c2: impulsive bullish body (2.9)
        _c(2, 102.5, 104.0, 102.0, 103.5),  # c3: low 102 -> gap 100..102
    ]


def _bearish_fvg_series() -> list[Kline]:
    """Candle 0 low=100, big down-displacement, candle 2 high=98 -> gap [98,100]."""
    return [
        _c(0, 101.0, 101.5, 100.0, 100.5),  # c1: low 100
        _c(1, 100.4, 100.5, 97.0, 97.5),  # c2: impulsive bearish body
        _c(2, 97.5, 98.0, 96.0, 96.5),  # c3: high 98 -> gap 98..100
    ]


class TestGeometry:
    def test_bullish_fvg_detected(self) -> None:
        gaps = detect_fvgs(_bullish_fvg_series())
        assert len(gaps) == 1
        g = gaps[0]
        assert g.fvg_type is FVGType.BULLISH
        assert g.bottom == 100.0
        assert g.top == 102.0
        assert g.midpoint == 101.0
        assert g.size == 2.0
        assert g.formation_index == 2
        assert g.displacement_index == 1

    def test_bearish_fvg_detected(self) -> None:
        gaps = detect_fvgs(_bearish_fvg_series())
        assert len(gaps) == 1
        g = gaps[0]
        assert g.fvg_type is FVGType.BEARISH
        assert g.bottom == 98.0
        assert g.top == 100.0

    def test_no_gap_when_candles_overlap(self) -> None:
        # Three plain candles around the same level — no imbalance.
        assert detect_fvgs([_flat(i, 100.0) for i in range(5)]) == []

    def test_too_short_returns_empty(self) -> None:
        assert detect_fvgs([_flat(0, 100.0), _flat(1, 100.0)]) == []


# ---------------------------------------------------------------------------
# Mitigation / fill status (as of the last candle)
# ---------------------------------------------------------------------------


class TestFillStatus:
    def test_unmitigated_when_price_stays_away(self) -> None:
        series = _bullish_fvg_series()
        series += [_c(i, 103.5, 104.5, 103.0, 104.0) for i in range(3, 7)]  # stays above gap
        g = detect_fvgs(series)[0]
        assert g.mitigated is False
        assert g.filled is False
        assert g.mitigation_index is None

    def test_mitigated_when_price_taps_gap(self) -> None:
        series = _bullish_fvg_series()
        # A candle dips to 101 (inside the 100-102 gap) but not through 100.
        series.append(_c(3, 103.0, 103.5, 101.0, 102.5))
        series += [_flat(i, 102.5) for i in range(4, 6)]
        g = detect_fvgs(series)[0]
        assert g.mitigated is True
        assert g.filled is False
        assert g.mitigation_index == 3

    def test_filled_when_price_traverses_gap(self) -> None:
        series = _bullish_fvg_series()
        series.append(_c(3, 102.0, 102.5, 99.0, 99.5))  # low 99 < bottom 100 -> filled
        g = detect_fvgs(series)[0]
        assert g.mitigated is True
        assert g.filled is True


# ---------------------------------------------------------------------------
# ATR-normalized filters
# ---------------------------------------------------------------------------


class TestAtrFilters:
    def test_size_filter_drops_small_gap(self) -> None:
        # 20 flat candles (ATR ~1.0) then a tiny 0.2-wide gap. A min size of 1.0 ATR drops it.
        series = [_flat(i, 100.0) for i in range(20)]
        series.append(_c(20, 100.0, 100.2, 99.9, 100.1))  # c1 high 100.2
        series.append(_c(21, 100.2, 100.8, 100.2, 100.6))  # displacement
        series.append(_c(22, 100.6, 101.0, 100.4, 100.8))  # c3 low 100.4 > 100.2 -> gap 0.2 wide
        assert detect_fvgs(series, min_size_atr_fraction=0.0)  # kept with no filter
        assert detect_fvgs(series, min_size_atr_fraction=1.0) == []  # dropped: 0.2 < 1.0*ATR

    def test_displacement_flag_requires_impulsive_body(self) -> None:
        # Build a gap after enough history for ATR to exist, with a large middle body.
        series = [_flat(i, 100.0) for i in range(20)]
        series.append(_c(20, 99.0, 100.0, 98.5, 99.5))  # c1 high 100
        series.append(_c(21, 99.6, 106.0, 99.5, 105.5))  # c2 body ~5.9 >> ATR(~1)
        series.append(_c(22, 105.5, 107.0, 102.0, 106.0))  # c3 low 102 -> gap 100..102
        g = detect_fvgs(series)[-1]
        assert g.is_displacement is True
        assert g.displacement_atr_multiple > 1.0


# ---------------------------------------------------------------------------
# As-of correctness
# ---------------------------------------------------------------------------


def _multi_gap_series() -> list[Kline]:
    """A series with two bullish gaps at different times, later partially revisited."""
    s = [_flat(i, 100.0) for i in range(3)]
    # Gap 1 around index 4-5.
    s.append(_c(3, 100.0, 100.5, 99.5, 100.0))
    s.append(_c(4, 100.1, 103.0, 100.0, 102.5))
    s.append(_c(5, 102.5, 104.0, 101.0, 103.0))  # c1=idx3 high100.5 < c3=idx5 low101 -> gap
    s += [_flat(i, 103.0) for i in range(6, 9)]
    # Gap 2 around index 10-11.
    s.append(_c(9, 103.0, 103.5, 102.5, 103.0))
    s.append(_c(10, 103.1, 107.0, 103.0, 106.5))
    s.append(_c(11, 106.5, 108.0, 105.0, 107.0))  # gap ~103.5..105
    s += [_flat(i, 107.0) for i in range(12, 15)]
    return s


class TestAsOfCorrectness:
    def test_truncation_preserves_formed_gap_geometry(self) -> None:
        series = _multi_gap_series()
        full = detect_fvgs(series)
        for t in range(len(series)):
            prefix = detect_fvgs(series[: t + 1])
            # Every gap formed by bar t must appear in the prefix with identical geometry.
            full_by_t = {
                g.formation_index: (g.fvg_type, g.top, g.bottom, g.displacement_index)
                for g in full
                if g.formation_index <= t
            }
            prefix_geo = {
                g.formation_index: (g.fvg_type, g.top, g.bottom, g.displacement_index)
                for g in prefix
            }
            assert prefix_geo == full_by_t, f"gap geometry diverged at truncation {t}"

    def test_freshly_formed_gap_is_unmitigated(self) -> None:
        series = _multi_gap_series()
        for t in range(len(series)):
            prefix = detect_fvgs(series[: t + 1])
            fresh = [g for g in prefix if g.formation_index == t]
            for g in fresh:
                assert g.mitigated is False
                assert g.filled is False
                assert g.mitigation_index is None
