"""Tests for the SMC structure layer (Step 2.1a).

Synthetic OHLC series with controlled pivots give deterministic assertions about
swing detection, the BOS/CHoCH state machine, Premium/Discount + directional OTE,
the ATR-normalized break margin, and — the headline correctness property — that
the detector is *as-of correct* (truncating future candles never changes a past
event).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.agents.analyzer.smc import (
    analyze_structure,
    atr_series,
    average_true_range,
    detect_swings,
)
from src.agents.analyzer.smc.models import (
    LegDirection,
    MarketPhase,
    StructureEventType,
    SwingLabel,
    SwingType,
    Zone,
)
from src.providers import Kline

# ---------------------------------------------------------------------------
# Synthetic-kline builders
# ---------------------------------------------------------------------------

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
    """Plain candle (range 1.0, close == open == level); never a strict pivot."""
    return _c(idx, level, level + 0.5, level - 0.5, level)


def _shigh(idx: int, *, peak: float, base: float) -> Kline:
    """Candle whose high spikes to `peak` (a swing-high candidate)."""
    return _c(idx, base, peak, base - 0.5, base)


def _slow(idx: int, *, trough: float, base: float) -> Kline:
    """Candle whose low dips to `trough` (a swing-low candidate)."""
    return _c(idx, base, base + 0.5, trough, base)


def _break_up(idx: int, *, close_px: float, base: float) -> Kline:
    """Bullish candle that closes at `close_px` (above `base`)."""
    return _c(idx, base, close_px + 0.1, base - 0.5, close_px)


def _break_down(idx: int, *, close_px: float, base: float) -> Kline:
    """Bearish candle that closes at `close_px` (below `base`)."""
    return _c(idx, base, base + 0.5, close_px - 0.1, close_px)


# ---------------------------------------------------------------------------
# Series fixtures
# ---------------------------------------------------------------------------


def _bos_bullish_series() -> list[Kline]:
    """Swing high @4 (110), decisive close above it @8 -> BOS_BULLISH, UPTREND."""
    c = [_flat(i, 100.0) for i in range(4)]
    c.append(_shigh(4, peak=110.0, base=100.0))
    c += [_flat(i, 100.0) for i in range(5, 8)]
    c.append(_break_up(8, close_px=111.0, base=100.0))
    c += [_flat(i, 111.0) for i in range(9, 14)]
    return c


def _bos_bearish_series() -> list[Kline]:
    """Swing low @4 (90), decisive close below it @8 -> BOS_BEARISH, DOWNTREND."""
    c = [_flat(i, 100.0) for i in range(4)]
    c.append(_slow(4, trough=90.0, base=100.0))
    c += [_flat(i, 100.0) for i in range(5, 8)]
    c.append(_break_down(8, close_px=89.0, base=100.0))
    c += [_flat(i, 89.0) for i in range(9, 14)]
    return c


def _choch_bullish_series() -> list[Kline]:
    """Down break first (BOS_BEARISH @8), then up break against it (CHOCH_BULLISH @16)."""
    c = [_flat(i, 100.0) for i in range(4)]
    c.append(_slow(4, trough=95.0, base=100.0))
    c += [_flat(i, 100.0) for i in range(5, 8)]
    c.append(_break_down(8, close_px=94.0, base=100.0))
    c += [_flat(i, 96.0) for i in range(9, 12)]
    c.append(_shigh(12, peak=105.0, base=96.0))
    c += [_flat(i, 96.0) for i in range(13, 16)]
    c.append(_break_up(16, close_px=108.0, base=96.0))
    c += [_flat(i, 108.0) for i in range(17, 20)]
    return c


def _bullish_leg_series() -> list[Kline]:
    """Low @4 (90) then high @12 (120) -> BULLISH leg; price ends at 100 (discount)."""
    c = [_flat(i, 100.0) for i in range(4)]
    c.append(_slow(4, trough=90.0, base=100.0))
    c += [_flat(i, 100.0) for i in range(5, 8)]
    c += [_flat(i, 110.0) for i in range(8, 12)]
    c.append(_shigh(12, peak=120.0, base=110.0))
    c += [_flat(i, 110.0) for i in range(13, 16)]
    c += [_flat(16, 100.0), _flat(17, 100.0)]
    return c


def _bearish_leg_series() -> list[Kline]:
    """High @4 (120) then low @12 (90) -> BEARISH leg; price ends at 110 (premium)."""
    c = [_flat(i, 110.0) for i in range(4)]
    c.append(_shigh(4, peak=120.0, base=110.0))
    c += [_flat(i, 110.0) for i in range(5, 8)]
    c += [_flat(i, 100.0) for i in range(8, 12)]
    c.append(_slow(12, trough=90.0, base=100.0))
    c += [_flat(i, 100.0) for i in range(13, 16)]
    c += [_flat(16, 110.0), _flat(17, 110.0)]
    return c


def _margin_series() -> list[Kline]:
    """Swing high @12 (110) and a small close (110.5) above it @16, with ATR defined."""
    c = [_flat(i, 100.0) for i in range(12)]
    c.append(_shigh(12, peak=110.0, base=100.0))
    c += [_flat(i, 100.0) for i in range(13, 16)]
    c.append(_break_up(16, close_px=110.5, base=100.0))
    c += [_flat(i, 100.0) for i in range(17, 24)]
    return c


# ---------------------------------------------------------------------------
# Swing detection
# ---------------------------------------------------------------------------


class TestSwingDetection:
    def test_finds_high_and_low_pivots(self) -> None:
        swings = detect_swings(_bullish_leg_series(), lookback=3)
        lows = [s for s in swings if s.swing_type is SwingType.LOW]
        highs = [s for s in swings if s.swing_type is SwingType.HIGH]
        assert any(s.index == 4 and s.price == 90.0 for s in lows)
        assert any(s.index == 12 and s.price == 120.0 for s in highs)

    def test_confirmed_at_index_is_pivot_plus_lookback(self) -> None:
        swings = detect_swings(_bullish_leg_series(), lookback=3)
        low4 = next(s for s in swings if s.index == 4)
        assert low4.confirmed_at_index == 7

    def test_first_swing_of_type_is_unlabeled(self) -> None:
        swings = detect_swings(_bullish_leg_series(), lookback=3)
        first_low = next(s for s in swings if s.swing_type is SwingType.LOW)
        assert first_low.label is None

    def test_higher_high_labeled_hh(self) -> None:
        series = [_flat(i, 100.0) for i in range(4)]
        series.append(_shigh(4, peak=110.0, base=100.0))
        series += [_flat(i, 100.0) for i in range(5, 12)]
        series.append(_shigh(12, peak=115.0, base=100.0))
        series += [_flat(i, 100.0) for i in range(13, 16)]
        highs = [s for s in detect_swings(series, lookback=3) if s.swing_type is SwingType.HIGH]
        assert highs[0].label is None
        assert highs[1].label is SwingLabel.HH

    def test_lower_high_labeled_lh(self) -> None:
        series = [_flat(i, 100.0) for i in range(4)]
        series.append(_shigh(4, peak=110.0, base=100.0))
        series += [_flat(i, 100.0) for i in range(5, 12)]
        series.append(_shigh(12, peak=108.0, base=100.0))
        series += [_flat(i, 100.0) for i in range(13, 16)]
        highs = [s for s in detect_swings(series, lookback=3) if s.swing_type is SwingType.HIGH]
        assert highs[1].label is SwingLabel.LH

    def test_lookback_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="lookback"):
            detect_swings(_bos_bullish_series(), lookback=0)


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


class TestATR:
    def test_none_when_too_few_candles(self) -> None:
        assert average_true_range([_flat(i, 100.0) for i in range(10)], period=14) is None

    def test_flat_series_atr_equals_candle_range(self) -> None:
        # Every flat candle has range 1.0 and close == prior close, so TR == 1.0.
        series = [_flat(i, 100.0) for i in range(20)]
        assert average_true_range(series, period=14) == pytest.approx(1.0)

    def test_series_is_none_until_period(self) -> None:
        series = [_flat(i, 100.0) for i in range(20)]
        atr = atr_series(series, period=14)
        assert atr[13] is None
        assert atr[14] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# BOS / CHoCH state machine
# ---------------------------------------------------------------------------


class TestBosChoch:
    def test_bullish_bos_and_uptrend(self) -> None:
        result = analyze_structure(_bos_bullish_series())
        assert result.phase is MarketPhase.UPTREND
        types = [e.event_type for e in result.events]
        assert StructureEventType.BOS_BULLISH in types
        assert StructureEventType.CHOCH_BULLISH not in types

    def test_bullish_bos_breaks_correct_level(self) -> None:
        result = analyze_structure(_bos_bullish_series())
        bos = next(e for e in result.events if e.event_type is StructureEventType.BOS_BULLISH)
        assert bos.broken_level == 110.0
        assert bos.index == 8

    def test_bearish_bos_and_downtrend(self) -> None:
        result = analyze_structure(_bos_bearish_series())
        assert result.phase is MarketPhase.DOWNTREND
        assert any(e.event_type is StructureEventType.BOS_BEARISH for e in result.events)

    def test_choch_bullish_reverses_downtrend(self) -> None:
        result = analyze_structure(_choch_bullish_series())
        types = [e.event_type for e in result.events]
        # First a down break (BOS), then an up break against it (CHoCH).
        assert types == [StructureEventType.BOS_BEARISH, StructureEventType.CHOCH_BULLISH]
        assert result.phase is MarketPhase.UPTREND

    def test_consolidation_when_no_breaks(self) -> None:
        result = analyze_structure(_bullish_leg_series())
        assert result.phase is MarketPhase.CONSOLIDATION
        assert result.events == []


# ---------------------------------------------------------------------------
# Premium / Discount + directional OTE
# ---------------------------------------------------------------------------


class TestPremiumDiscount:
    def test_bullish_leg_ote_sits_in_discount(self) -> None:
        result = analyze_structure(_bullish_leg_series())
        dr = result.dealing_range
        assert dr is not None
        assert dr.leg_direction is LegDirection.BULLISH
        assert result.zone is Zone.DISCOUNT
        # OTE band must be below equilibrium (in the discount half) for a bull leg.
        assert dr.ote_upper < dr.equilibrium
        assert dr.ote_lower < dr.ote_upper

    def test_bearish_leg_ote_sits_in_premium(self) -> None:
        result = analyze_structure(_bearish_leg_series())
        dr = result.dealing_range
        assert dr is not None
        assert dr.leg_direction is LegDirection.BEARISH
        assert result.zone is Zone.PREMIUM
        # OTE band must be above equilibrium (in the premium half) for a bear leg.
        assert dr.ote_lower > dr.equilibrium

    def test_no_dealing_range_without_both_swings(self) -> None:
        # Only a swing high exists -> no Premium/Discount array.
        result = analyze_structure(_bos_bullish_series())
        assert result.dealing_range is None
        assert result.zone is None


# ---------------------------------------------------------------------------
# ATR-normalized break margin
# ---------------------------------------------------------------------------


class TestBreakMargin:
    def test_small_break_passes_with_zero_margin(self) -> None:
        result = analyze_structure(_margin_series(), min_break_atr_fraction=0.0)
        assert any(e.event_type is StructureEventType.BOS_BULLISH for e in result.events)

    def test_small_break_rejected_by_large_margin(self) -> None:
        # A 0.5 poke above the level cannot clear a 5x-ATR margin -> no event.
        result = analyze_structure(_margin_series(), min_break_atr_fraction=5.0)
        assert result.events == []


# ---------------------------------------------------------------------------
# As-of correctness (no look-ahead) — the headline invariant
# ---------------------------------------------------------------------------


class TestNoLookAhead:
    def test_truncating_future_never_changes_past_events(self) -> None:
        series = _choch_bullish_series()
        full = analyze_structure(series)
        full_events = [
            (e.event_type, e.index, e.broken_level, e.broken_swing_index) for e in full.events
        ]
        for t in range(len(series)):
            prefix = analyze_structure(series[: t + 1])
            got = [
                (e.event_type, e.index, e.broken_level, e.broken_swing_index) for e in prefix.events
            ]
            expected = [ev for ev in full_events if ev[1] <= t]
            assert got == expected, f"events diverged when truncated at bar {t}"

    def test_phase_matches_state_at_truncation(self) -> None:
        # The phase of a prefix ending at t equals the running trend the full-series
        # state machine held immediately after processing bar t.
        series = _choch_bullish_series()
        # After the bearish break (bar 8) but before the CHoCH (bar 16) -> DOWNTREND.
        assert analyze_structure(series[:13]).phase is MarketPhase.DOWNTREND
        # After the CHoCH (bar 16) -> UPTREND.
        assert analyze_structure(series[:18]).phase is MarketPhase.UPTREND


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_series_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one candle"):
            analyze_structure([])

    def test_short_series_is_safe_consolidation(self) -> None:
        result = analyze_structure([_flat(i, 100.0) for i in range(4)])
        assert result.phase is MarketPhase.CONSOLIDATION
        assert result.swings == []
        assert result.events == []
        assert result.dealing_range is None
        assert result.zone is None
        assert result.atr is None
