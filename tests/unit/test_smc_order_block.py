"""Tests for the SMC Order Block detector (Step 2.1c).

Order Blocks are anchored to confirmed BOS/CHoCH events, so these series embed a
swing, a pullback that prints an opposite-color OB candle, an impulse that breaks
the swing (leaving an FVG), and trailing candles. Covers bullish/bearish OBs,
displacement + FVG confluence, mitigation as of the last candle, and the
as-of-correctness invariant (truncation never changes an OB's formation).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.agents.analyzer.smc import detect_order_blocks
from src.agents.analyzer.smc.models import OrderBlockDirection, StructureEventType
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


def _bullish_ob_series() -> list[Kline]:
    """Swing high@10 (106); OB (down candle)@13; impulse@14; break@15 closes 108 (>106).

    The 13-14-15 window leaves a bullish FVG (c13.high 101.5 < c15.low 102), so the
    resulting bullish OB@13 has displacement + FVG confluence and is unmitigated.
    """
    c = [_flat(i, 100.0) for i in range(10)]
    c.append(_c(10, 100.0, 106.0, 99.5, 100.0))  # swing high 106
    c += [_flat(11, 100.0), _flat(12, 100.0)]
    c.append(_c(13, 101.0, 101.5, 98.5, 99.0))  # OB: bearish
    c.append(_c(14, 99.0, 105.5, 99.0, 105.0))  # impulse up (no break yet: 105 < 106)
    c.append(_c(15, 105.0, 108.5, 102.0, 108.0))  # break: close 108 > 106; low 102 -> FVG
    c += [_flat(i, 108.0) for i in range(16, 20)]
    return c


def _bearish_ob_series() -> list[Kline]:
    """Swing low@10 (94); OB (up candle)@13; impulse down@14; break@15 closes 92 (<94)."""
    c = [_flat(i, 100.0) for i in range(10)]
    c.append(_c(10, 100.0, 100.5, 94.0, 100.0))  # swing low 94
    c += [_flat(11, 100.0), _flat(12, 100.0)]
    c.append(_c(13, 99.0, 101.5, 98.5, 101.0))  # OB: bullish
    c.append(_c(14, 101.0, 101.0, 95.0, 95.0))  # impulse down (no break yet: 95 > 94)
    c.append(_c(15, 95.0, 98.0, 92.0, 92.0))  # break: close 92 < 94; high 98 -> FVG
    c += [_flat(i, 92.0) for i in range(16, 20)]
    return c


# ---------------------------------------------------------------------------
# Bullish / bearish detection + confluence
# ---------------------------------------------------------------------------


class TestBullishOrderBlock:
    def test_detects_single_bullish_ob(self) -> None:
        obs = detect_order_blocks(_bullish_ob_series())
        assert len(obs) == 1
        ob = obs[0]
        assert ob.direction is OrderBlockDirection.BULLISH
        assert ob.ob_index == 13
        assert ob.break_index == 15
        assert ob.break_event_type is StructureEventType.BOS_BULLISH

    def test_zone_is_ob_candle_range(self) -> None:
        ob = detect_order_blocks(_bullish_ob_series())[0]
        assert ob.zone_high == 101.5
        assert ob.zone_low == 98.5

    def test_displacement_and_fvg_confluence(self) -> None:
        ob = detect_order_blocks(_bullish_ob_series())[0]
        assert ob.has_displacement is True
        assert ob.displacement_atr_multiple > 1.0
        assert ob.has_fvg is True
        assert ob.mitigated is False
        assert ob.confluence_count == 3


class TestBearishOrderBlock:
    def test_detects_single_bearish_ob(self) -> None:
        obs = detect_order_blocks(_bearish_ob_series())
        assert len(obs) == 1
        ob = obs[0]
        assert ob.direction is OrderBlockDirection.BEARISH
        assert ob.ob_index == 13
        assert ob.break_event_type is StructureEventType.BOS_BEARISH

    def test_bearish_confluence(self) -> None:
        ob = detect_order_blocks(_bearish_ob_series())[0]
        assert ob.has_displacement is True
        assert ob.has_fvg is True
        assert ob.mitigated is False


# ---------------------------------------------------------------------------
# Mitigation (as of the last candle)
# ---------------------------------------------------------------------------


class TestMitigation:
    def test_ob_marked_mitigated_when_price_returns(self) -> None:
        c = _bullish_ob_series()[:16]  # through the break at index 15
        # Candle 16 dips to 101 (<= zone_high 101.5) -> taps the demand zone.
        c.append(_c(16, 108.0, 108.5, 101.0, 102.0))
        c += [_flat(i, 102.0) for i in range(17, 20)]
        ob = detect_order_blocks(c)[0]
        assert ob.mitigated is True
        assert ob.mitigation_index == 16
        assert ob.confluence_count == 2  # displacement + fvg, no longer "unmitigated"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_no_events_no_order_blocks(self) -> None:
        assert detect_order_blocks([_flat(i, 100.0) for i in range(30)]) == []

    def test_too_short_returns_empty(self) -> None:
        assert detect_order_blocks([_flat(0, 100.0), _flat(1, 100.0)]) == []


# ---------------------------------------------------------------------------
# As-of correctness
# ---------------------------------------------------------------------------


class TestAsOfCorrectness:
    def test_truncation_preserves_ob_formation(self) -> None:
        series = _bullish_ob_series()
        full = detect_order_blocks(series)

        def formation(ob: object) -> tuple[object, ...]:
            assert hasattr(ob, "ob_index")
            return (
                ob.direction,  # type: ignore[attr-defined]
                ob.ob_index,  # type: ignore[attr-defined]
                ob.zone_high,  # type: ignore[attr-defined]
                ob.zone_low,  # type: ignore[attr-defined]
                ob.break_index,  # type: ignore[attr-defined]
                ob.has_fvg,  # type: ignore[attr-defined]
                ob.has_displacement,  # type: ignore[attr-defined]
            )

        for t in range(len(series)):
            prefix = detect_order_blocks(series[: t + 1])
            expected = {formation(o) for o in full if o.break_index <= t}
            got = {formation(o) for o in prefix}
            assert got == expected, f"OB formation diverged when truncated at bar {t}"

    def test_freshly_anchored_ob_is_unmitigated(self) -> None:
        series = _bullish_ob_series()
        for t in range(len(series)):
            prefix = detect_order_blocks(series[: t + 1])
            for ob in prefix:
                if ob.break_index == t:
                    assert ob.mitigated is False
                    assert ob.mitigation_index is None
