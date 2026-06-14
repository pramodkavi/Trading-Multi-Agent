"""Market-structure layer: BOS/CHoCH state machine + Premium/Discount + OTE.

Ports `detect_structure.py` into typed, as-of-correct logic and fixes the
reference script's defects (documented in the Slice 2 thread):

1. **No look-ahead.** A swing is only usable after `confirmed_at_index`; a break
   of it is detected strictly after confirmation. The reference allowed a "BOS"
   at a candle before the broken swing was even confirmable.
2. **Proper BOS vs CHoCH.** A sequential state machine tracks the running trend:
   a close beyond the active swing is a BOS if it continues the trend, a CHoCH if
   it reverses an established opposite trend. (The reference inferred CHoCH from a
   noisy HH/HL label heuristic.)
3. **Body-close break.** A break is a body *close* beyond the level (standard SMC),
   not the reference's over-restrictive "open one side AND close the other".
4. **Directional OTE.** The OTE band is placed in the discount half for a bullish
   leg and the premium half for a bearish leg. The reference always used the
   bullish formula, making OTE wrong for ~half of setups.
5. **Volatility-normalized break margin.** A break must clear the level by a
   fraction of the *as-of* ATR, filtering 1-tick pokes. The margin at bar j uses
   `atr[j]`, never a single ATR over the whole (future-containing) series.

This module computes structure only; it does not emit a SignalProposal. Wiring
the full SMC stack into `smc_analyzer.analyze()` happens at the 2.1d assembly
step once FVG/OB/liquidity/derivatives exist.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from src.agents.analyzer.smc.models import (
    DealingRange,
    LegDirection,
    MarketPhase,
    StructureAnalysis,
    StructureEvent,
    StructureEventType,
    SwingPoint,
    SwingType,
    Zone,
)
from src.agents.analyzer.smc.swings import DEFAULT_LOOKBACK, detect_swings
from src.agents.analyzer.smc.volatility import DEFAULT_ATR_PERIOD, atr_series

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.providers.base import Kline

# OTE (Optimal Trade Entry) Fibonacci retracement band, SPEC §1.5.
OTE_SHALLOW: Final[float] = 0.618
OTE_DEEP: Final[float] = 0.786

DEFAULT_MIN_BREAK_ATR_FRACTION: Final[float] = 0.10
"""A break must clear the swing by >= this fraction of the as-of ATR to count.

0.10 ATR filters insignificant pokes without demanding a large displacement.
When ATR is unavailable (too few candles) the margin is 0 (pure close-beyond).
"""

EQUILIBRIUM_BAND_FRACTION: Final[float] = 0.02
"""Half-width of the EQUILIBRIUM no-trade band, as a fraction of the range size."""


def analyze_structure(
    candles: Sequence[Kline],
    *,
    lookback: int = DEFAULT_LOOKBACK,
    atr_period: int = DEFAULT_ATR_PERIOD,
    min_break_atr_fraction: float = DEFAULT_MIN_BREAK_ATR_FRACTION,
) -> StructureAnalysis:
    """Compute the full structure picture for one timeframe's candle series.

    Best-effort and total: on a series too short to produce swings it returns a
    CONSOLIDATION analysis with empty swings/events and no dealing range, rather
    than raising. The Analyzer (2.1d) owns the "insufficient data -> SKIP" policy.
    """
    if not candles:
        raise ValueError("analyze_structure requires at least one candle")

    current_price = candles[-1].close
    atr_per_bar = atr_series(candles, period=atr_period)
    swings = detect_swings(candles, lookback=lookback)
    events, phase = _detect_bos_choch(
        candles,
        swings,
        atr_per_bar=atr_per_bar,
        min_break_atr_fraction=min_break_atr_fraction,
    )
    dealing_range = _compute_dealing_range(swings)
    zone = _classify_zone(current_price, dealing_range)

    return StructureAnalysis(
        phase=phase,
        current_price=current_price,
        zone=zone,
        swings=swings,
        events=events,
        dealing_range=dealing_range,
        atr=atr_per_bar[-1],
        lookback=lookback,
    )


def _detect_bos_choch(
    candles: Sequence[Kline],
    swings: list[SwingPoint],
    *,
    atr_per_bar: list[float | None],
    min_break_atr_fraction: float,
) -> tuple[list[StructureEvent], MarketPhase]:
    """Sequential, as-of-correct BOS/CHoCH detection.

    Walks candles forward tracking the most recent confirmed, unbroken swing high
    (`active_high`) and swing low (`active_low`) and the running `trend`. A swing
    becomes active only once it is confirmed (strictly before the current bar). A
    body close beyond an active swing by the ATR margin emits an event, flips/sets
    the trend, and consumes that swing (the next break needs a fresh swing). The
    trend after the final candle is the current market phase.
    """
    events: list[StructureEvent] = []
    trend = MarketPhase.CONSOLIDATION
    active_high: SwingPoint | None = None
    active_low: SwingPoint | None = None

    # Swings are already chronological by index; activate them as they confirm.
    pending = swings
    next_swing = 0

    for j in range(len(candles)):
        # Activate every swing confirmed strictly before bar j. A swing confirmed
        # exactly at j is not yet usable for a break at j (it isn't known until
        # bar j closes); requiring confirmed_at_index <= j-1 enforces that.
        while next_swing < len(pending) and pending[next_swing].confirmed_at_index <= j - 1:
            sw = pending[next_swing]
            next_swing += 1
            if sw.swing_type is SwingType.HIGH:
                active_high = sw
            else:
                active_low = sw

        close = candles[j].close
        margin = (atr_per_bar[j] or 0.0) * min_break_atr_fraction

        if active_high is not None and close > active_high.price + margin:
            event_type = (
                StructureEventType.CHOCH_BULLISH
                if trend is MarketPhase.DOWNTREND
                else StructureEventType.BOS_BULLISH
            )
            events.append(
                StructureEvent(
                    event_type=event_type,
                    index=j,
                    open_time=candles[j].open_time,
                    broken_level=active_high.price,
                    close_price=close,
                    broken_swing_index=active_high.index,
                )
            )
            trend = MarketPhase.UPTREND
            active_high = None  # consumed; the next bullish break needs a new high
        elif active_low is not None and close < active_low.price - margin:
            event_type = (
                StructureEventType.CHOCH_BEARISH
                if trend is MarketPhase.UPTREND
                else StructureEventType.BOS_BEARISH
            )
            events.append(
                StructureEvent(
                    event_type=event_type,
                    index=j,
                    open_time=candles[j].open_time,
                    broken_level=active_low.price,
                    close_price=close,
                    broken_swing_index=active_low.index,
                )
            )
            trend = MarketPhase.DOWNTREND
            active_low = None

    return events, trend


def _compute_dealing_range(swings: list[SwingPoint]) -> DealingRange | None:
    """Build the Premium/Discount array from the most recent confirmed swings.

    The range spans the most recent confirmed swing high and swing low. The
    impulse leg is bullish when the high is more recent than the low (leg ran
    low -> high) and bearish otherwise, which determines on which side the OTE
    retracement band sits. Returns None when there is not both a high and a low,
    or when the range is degenerate (high not above low).
    """
    last_high = _last_of_type(swings, SwingType.HIGH)
    last_low = _last_of_type(swings, SwingType.LOW)
    if last_high is None or last_low is None:
        return None

    range_high = last_high.price
    range_low = last_low.price
    if range_high <= range_low:
        # Inverted/degenerate (e.g. the recent low printed above the recent high).
        return None

    size = range_high - range_low
    equilibrium = (range_high + range_low) / 2.0

    if last_high.index > last_low.index:
        leg_direction = LegDirection.BULLISH
        # Retrace down from the high into discount.
        ote_upper = range_high - size * OTE_SHALLOW
        ote_lower = range_high - size * OTE_DEEP
    else:
        leg_direction = LegDirection.BEARISH
        # Retrace up from the low into premium.
        ote_lower = range_low + size * OTE_SHALLOW
        ote_upper = range_low + size * OTE_DEEP

    return DealingRange(
        range_high=range_high,
        range_low=range_low,
        equilibrium=equilibrium,
        leg_direction=leg_direction,
        ote_lower=ote_lower,
        ote_upper=ote_upper,
    )


def _classify_zone(price: float, dealing_range: DealingRange | None) -> Zone | None:
    """PREMIUM above equilibrium, DISCOUNT below, EQUILIBRIUM within the band."""
    if dealing_range is None:
        return None
    size = dealing_range.range_high - dealing_range.range_low
    band = size * EQUILIBRIUM_BAND_FRACTION
    if abs(price - dealing_range.equilibrium) <= band:
        return Zone.EQUILIBRIUM
    return Zone.PREMIUM if price > dealing_range.equilibrium else Zone.DISCOUNT


def _last_of_type(swings: list[SwingPoint], swing_type: SwingType) -> SwingPoint | None:
    """Most recent confirmed swing of the given type, or None."""
    for sw in reversed(swings):
        if sw.swing_type is swing_type:
            return sw
    return None
