"""Fair Value Gap (FVG / imbalance) detection — Step 2.1b.

Ports `detect_fvg.py` into typed, as-of-correct logic. An FVG is a 3-candle
imbalance: a bullish gap exists when candle 1's high is below candle 3's low
(price displaced up so fast it left an unfilled gap), and the mirror for bearish.

Fixes / improvements over the reference script:
- **Volatility-normalized thresholds.** The reference filtered gaps by a fixed
  0.05% of price and called a candle "displacement" at a fixed 0.3% body. Both
  now scale with the *as-of* ATR (`atr[i]` uses only candles 0..i), so the same
  code behaves sensibly across BTC 4H and SOL 5m.
- **As-of correct.** The gap is known once candle 3 closes (`formation_index`),
  and its mitigated/filled status reflects only candles between formation and the
  last candle ("now") — never beyond. Detecting on a prefix never changes the
  formation of a gap already formed within that prefix.

Per the evidence review, FVGs are *context* (a place price may revisit), not a
standalone edge; they earn weight only in confluence with structure + liquidity.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from src.agents.analyzer.smc.models import FairValueGap, FVGType
from src.agents.analyzer.smc.volatility import DEFAULT_ATR_PERIOD, atr_series

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.providers.base import Kline

DEFAULT_MIN_SIZE_ATR_FRACTION: Final[float] = 0.0
"""Minimum gap size as a fraction of the as-of ATR. 0.0 keeps every gap (default).

Raise it to filter micro-gaps. When ATR is unavailable (too few candles) the size
filter is skipped rather than guessed, so early gaps are never silently dropped.
"""

DEFAULT_DISPLACEMENT_ATR_FRACTION: Final[float] = 1.0
"""A middle candle is 'displacement' when its body >= this multiple of the as-of ATR.

1.0 = a candle whose body spans a full ATR in a single bar (genuinely impulsive).
"""


def detect_fvgs(
    candles: Sequence[Kline],
    *,
    atr_period: int = DEFAULT_ATR_PERIOD,
    min_size_atr_fraction: float = DEFAULT_MIN_SIZE_ATR_FRACTION,
    displacement_atr_fraction: float = DEFAULT_DISPLACEMENT_ATR_FRACTION,
) -> list[FairValueGap]:
    """Return all Fair Value Gaps in chronological order (by formation index).

    Mitigated/filled status is evaluated as of the last candle in `candles`.
    Returns an empty list for series shorter than 3 candles.
    """
    n = len(candles)
    if n < 3:
        return []

    atr = atr_series(candles, period=atr_period)
    last = n - 1
    gaps: list[FairValueGap] = []

    for i in range(2, n):
        c1, c2, c3 = candles[i - 2], candles[i - 1], candles[i]

        if c1.high < c3.low:
            gap = _build_gap(
                FVGType.BULLISH,
                top=c3.low,
                bottom=c1.high,
                formation_index=i,
                displacement=c2,
                atr_at_formation=atr[i],
                atr_at_displacement=atr[i - 1],
                candles=candles,
                last=last,
                min_size_atr_fraction=min_size_atr_fraction,
                displacement_atr_fraction=displacement_atr_fraction,
            )
            if gap is not None:
                gaps.append(gap)
        elif c1.low > c3.high:
            # A 3-candle window cannot be both bullish and bearish, so `elif` is safe.
            gap = _build_gap(
                FVGType.BEARISH,
                top=c1.low,
                bottom=c3.high,
                formation_index=i,
                displacement=c2,
                atr_at_formation=atr[i],
                atr_at_displacement=atr[i - 1],
                candles=candles,
                last=last,
                min_size_atr_fraction=min_size_atr_fraction,
                displacement_atr_fraction=displacement_atr_fraction,
            )
            if gap is not None:
                gaps.append(gap)

    return gaps


def _build_gap(
    fvg_type: FVGType,
    *,
    top: float,
    bottom: float,
    formation_index: int,
    displacement: Kline,
    atr_at_formation: float | None,
    atr_at_displacement: float | None,
    candles: Sequence[Kline],
    last: int,
    min_size_atr_fraction: float,
    displacement_atr_fraction: float,
) -> FairValueGap | None:
    """Assemble one FairValueGap, or None if it fails the (as-of) size filter."""
    size = top - bottom

    # Size filter, normalized by the ATR known at the formation bar. Skipped when
    # ATR is unavailable so early-history gaps are not silently dropped.
    if (
        min_size_atr_fraction > 0.0
        and atr_at_formation is not None
        and size < min_size_atr_fraction * atr_at_formation
    ):
        return None

    body = abs(displacement.close - displacement.open)
    if atr_at_displacement is not None and atr_at_displacement > 0.0:
        displacement_multiple = body / atr_at_displacement
        is_displacement = displacement_multiple >= displacement_atr_fraction
    else:
        displacement_multiple = 0.0
        is_displacement = False

    mitigated, filled, mitigation_index = _fill_status(
        fvg_type,
        top=top,
        bottom=bottom,
        formation_index=formation_index,
        candles=candles,
        last=last,
    )

    return FairValueGap(
        fvg_type=fvg_type,
        top=top,
        bottom=bottom,
        midpoint=(top + bottom) / 2.0,
        size=size,
        formation_index=formation_index,
        displacement_index=formation_index - 1,
        open_time=displacement.open_time,
        is_displacement=is_displacement,
        displacement_atr_multiple=displacement_multiple,
        mitigated=mitigated,
        filled=filled,
        mitigation_index=mitigation_index,
    )


def _fill_status(
    fvg_type: FVGType,
    *,
    top: float,
    bottom: float,
    formation_index: int,
    candles: Sequence[Kline],
    last: int,
) -> tuple[bool, bool, int | None]:
    """Mitigation/fill status as of the last candle.

    A bullish gap is *mitigated* when a later candle dips into it (low <= top) and
    *filled* when price fully traverses it (low <= bottom); the bearish case is the
    mirror on highs. Only candles after formation up to `last` ("now") are scanned.
    """
    mitigated = False
    filled = False
    mitigation_index: int | None = None

    for j in range(formation_index + 1, last + 1):
        cj = candles[j]
        if fvg_type is FVGType.BULLISH:
            entered = cj.low <= top
            through = cj.low <= bottom
        else:
            entered = cj.high >= bottom
            through = cj.high >= top

        if entered and not mitigated:
            mitigated = True
            mitigation_index = j
        if through:
            filled = True
            if not mitigated:  # a gap jumped clean through is still mitigated
                mitigated = True
                mitigation_index = j
            break

    return mitigated, filled, mitigation_index
