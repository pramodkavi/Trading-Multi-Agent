"""Order Block (POI) detection — Step 2.1c.

Ports `detect_ob.py`, but anchors each Order Block to a *confirmed* BOS/CHoCH
event from the structure layer (2.1a) instead of scanning for displacement
independently. A bullish OB is the last down-close candle before the impulsive
up-move that produced a confirmed bullish structure break; the bearish case is
the mirror.

Why anchor to confirmed structure: it makes detection **as-of correct**. The
structure event at break index j is itself as-of correct (uses only candles <= j);
the OB candle is then found by scanning *backward* from j (never forward), its
confluences (displacement, FVG from 2.1b) use only candles <= j, and only its
mitigation status looks forward — and then only up to the last candle ("now").
The reference script computed an OB's BOS/FVG/quality by scanning forward, so a
freshly-formed OB was systematically under-scored versus historical ones.

Scope: this ships the core OB (zone + displacement + FVG confluence + mitigation).
Breaker/mitigation-block reclassification (the reference's forward-scan step) is a
later refinement. `confluence_count` is a raw tally, not a calibrated probability —
the evidence review is explicit that confidence must be earned by forward-testing,
not asserted by counting confluences.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from src.agents.analyzer.smc.fvg import detect_fvgs
from src.agents.analyzer.smc.models import (
    FairValueGap,
    FVGType,
    OrderBlock,
    OrderBlockDirection,
    StructureEvent,
    StructureEventType,
)
from src.agents.analyzer.smc.structure import DEFAULT_MIN_BREAK_ATR_FRACTION, analyze_structure
from src.agents.analyzer.smc.swings import DEFAULT_LOOKBACK
from src.agents.analyzer.smc.volatility import DEFAULT_ATR_PERIOD, atr_series

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.providers.base import Kline

DEFAULT_OB_SEARCH_WINDOW: Final[int] = 10
"""How far back from a break to look for the originating opposite-color candle."""

DEFAULT_DISPLACEMENT_ATR_FRACTION: Final[float] = 1.0
"""Move past the OB must be >= this multiple of the as-of ATR to count as displacement."""

_BULLISH_EVENTS: Final[frozenset[StructureEventType]] = frozenset(
    {StructureEventType.BOS_BULLISH, StructureEventType.CHOCH_BULLISH}
)
_BEARISH_EVENTS: Final[frozenset[StructureEventType]] = frozenset(
    {StructureEventType.BOS_BEARISH, StructureEventType.CHOCH_BEARISH}
)


def detect_order_blocks(
    candles: Sequence[Kline],
    *,
    lookback: int = DEFAULT_LOOKBACK,
    atr_period: int = DEFAULT_ATR_PERIOD,
    min_break_atr_fraction: float = DEFAULT_MIN_BREAK_ATR_FRACTION,
    search_window: int = DEFAULT_OB_SEARCH_WINDOW,
    displacement_atr_fraction: float = DEFAULT_DISPLACEMENT_ATR_FRACTION,
) -> list[OrderBlock]:
    """Return Order Blocks (one per qualifying structure break), ordered by OB index.

    Mitigation status is as of the last candle in `candles`. Returns an empty list
    for series too short to produce structure events.
    """
    n = len(candles)
    if n < 3:
        return []

    structure = analyze_structure(
        candles,
        lookback=lookback,
        atr_period=atr_period,
        min_break_atr_fraction=min_break_atr_fraction,
    )
    atr = atr_series(candles, period=atr_period)
    # FVG formation is as-of correct, so detecting once over the whole series and
    # filtering by formation_index <= break is equivalent to detecting per-prefix.
    fvgs = detect_fvgs(candles, atr_period=atr_period)
    last = n - 1

    blocks: list[OrderBlock] = []
    seen: set[tuple[OrderBlockDirection, int]] = set()

    for event in structure.events:
        if event.event_type in _BULLISH_EVENTS:
            direction = OrderBlockDirection.BULLISH
        elif event.event_type in _BEARISH_EVENTS:
            direction = OrderBlockDirection.BEARISH
        else:  # pragma: no cover - structure only emits BOS/CHoCH
            continue

        block = _build_order_block(
            direction,
            event,
            candles=candles,
            atr=atr,
            fvgs=fvgs,
            last=last,
            search_window=search_window,
            displacement_atr_fraction=displacement_atr_fraction,
        )
        if block is None:
            continue
        key = (block.direction, block.ob_index)
        if key in seen:  # two events sharing one OB candle -> keep the earliest
            continue
        seen.add(key)
        blocks.append(block)

    blocks.sort(key=lambda b: b.ob_index)
    return blocks


def _build_order_block(
    direction: OrderBlockDirection,
    event: StructureEvent,
    *,
    candles: Sequence[Kline],
    atr: list[float | None],
    fvgs: list[FairValueGap],
    last: int,
    search_window: int,
    displacement_atr_fraction: float,
) -> OrderBlock | None:
    j = event.index
    k = _find_ob_candle(direction, candles, break_index=j, search_window=search_window)
    if k is None:
        return None

    ob_candle = candles[k]
    zone_high = ob_candle.high
    zone_low = ob_candle.low
    if zone_high <= zone_low:  # degenerate (doji-flat) candle — no usable zone
        return None

    atr_j = atr[j]
    if atr_j is not None and atr_j > 0.0:
        if direction is OrderBlockDirection.BULLISH:
            move = event.close_price - zone_high
        else:
            move = zone_low - event.close_price
        displacement_multiple = max(move, 0.0) / atr_j
        has_displacement = displacement_multiple >= displacement_atr_fraction
    else:
        displacement_multiple = 0.0
        has_displacement = False

    want = FVGType.BULLISH if direction is OrderBlockDirection.BULLISH else FVGType.BEARISH
    has_fvg = any(g.fvg_type is want and k < g.formation_index <= j for g in fvgs)

    mitigated, mitigation_index = _mitigation_status(
        direction, zone_high=zone_high, zone_low=zone_low, break_index=j, candles=candles, last=last
    )

    confluence_count = int(has_displacement) + int(has_fvg) + int(not mitigated)

    return OrderBlock(
        direction=direction,
        ob_index=k,
        open_time=ob_candle.open_time,
        zone_high=zone_high,
        zone_low=zone_low,
        break_index=j,
        break_event_type=event.event_type,
        has_displacement=has_displacement,
        displacement_atr_multiple=displacement_multiple,
        has_fvg=has_fvg,
        mitigated=mitigated,
        mitigation_index=mitigation_index,
        confluence_count=confluence_count,
    )


def _find_ob_candle(
    direction: OrderBlockDirection,
    candles: Sequence[Kline],
    *,
    break_index: int,
    search_window: int,
) -> int | None:
    """Most recent opposite-color candle before the break (the OB origin), or None.

    Bullish OB = last down-close candle before an up-break; bearish = last up-close
    candle before a down-break. Scans backward only (as-of correct).
    """
    lower = max(0, break_index - search_window)
    for k in range(break_index - 1, lower - 1, -1):
        candle = candles[k]
        if direction is OrderBlockDirection.BULLISH and candle.close < candle.open:
            return k
        if direction is OrderBlockDirection.BEARISH and candle.close > candle.open:
            return k
    return None


def _mitigation_status(
    direction: OrderBlockDirection,
    *,
    zone_high: float,
    zone_low: float,
    break_index: int,
    candles: Sequence[Kline],
    last: int,
) -> tuple[bool, int | None]:
    """Whether price has returned into the OB zone after the break, as of the last candle."""
    for idx in range(break_index + 1, last + 1):
        candle = candles[idx]
        if direction is OrderBlockDirection.BULLISH and candle.low <= zone_high:
            return True, idx
        if direction is OrderBlockDirection.BEARISH and candle.high >= zone_low:
            return True, idx
    return False, None
