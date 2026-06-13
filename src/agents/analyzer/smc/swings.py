"""Canonical fractal swing-point detection (as-of-correct).

The reference scripts duplicated swing detection in `detect_structure.py` and
`detect_liquidity.py` with subtly different comparisons (one used `<=`/`>=`, the
other strict `<`/`>`). This is the single canonical implementation both the
structure layer and (later) the liquidity layer share.

A swing high at index i is a candle whose high *strictly* exceeds the highs of
the `lookback` candles on each side; a swing low is the mirror. Strict dominance
matches the Slice-1 stub and avoids plateau ambiguity.

**As-of correctness:** only pivots with `lookback` candles *after* them are
returned (they cannot be confirmed otherwise), and each pivot records
`confirmed_at_index = index + lookback` so break detection downstream can refuse
to use a swing before a real-time observer could have known it existed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from src.agents.analyzer.smc.models import SwingLabel, SwingPoint, SwingType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.providers.base import Kline

DEFAULT_LOOKBACK: Final[int] = 3
"""Candles required on each side of a pivot. K=3 is the conservative SMC default."""


def detect_swings(
    candles: Sequence[Kline], *, lookback: int = DEFAULT_LOOKBACK
) -> list[SwingPoint]:
    """Return all confirmed swing pivots, oldest first, labeled HH/HL/LH/LL.

    Args:
        candles: OHLCV series, most-recent last.
        lookback: fractal half-window (candles required on each side).

    Returns:
        Chronologically ordered SwingPoints. Empty if the series is too short
        (fewer than 2*lookback+1 candles).
    """
    if lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")

    swings: list[SwingPoint] = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        center = candles[i]
        neighbors = range(1, lookback + 1)

        is_high = all(
            center.high > candles[i - j].high and center.high > candles[i + j].high
            for j in neighbors
        )
        if is_high:
            swings.append(
                SwingPoint(
                    index=i,
                    open_time=center.open_time,
                    price=center.high,
                    swing_type=SwingType.HIGH,
                    confirmed_at_index=i + lookback,
                )
            )
            # A candle cannot be both a strict swing high and a strict swing low.
            continue

        is_low = all(
            center.low < candles[i - j].low and center.low < candles[i + j].low for j in neighbors
        )
        if is_low:
            swings.append(
                SwingPoint(
                    index=i,
                    open_time=center.open_time,
                    price=center.low,
                    swing_type=SwingType.LOW,
                    confirmed_at_index=i + lookback,
                )
            )

    return _label_swings(swings)


def _label_swings(swings: list[SwingPoint]) -> list[SwingPoint]:
    """Assign HH/HL/LH/LL relative to the previous same-type swing.

    Purely backward-looking (each swing compared only to an earlier one), so the
    labels are as-of correct. The first swing of each type stays unlabeled.
    """
    labeled: list[SwingPoint] = []
    prev_high: float | None = None
    prev_low: float | None = None

    for sw in swings:
        label: SwingLabel | None = None
        if sw.swing_type is SwingType.HIGH:
            if prev_high is not None:
                label = SwingLabel.HH if sw.price > prev_high else SwingLabel.LH
            prev_high = sw.price
        else:
            if prev_low is not None:
                label = SwingLabel.HL if sw.price > prev_low else SwingLabel.LL
            prev_low = sw.price
        labeled.append(sw.model_copy(update={"label": label}))

    return labeled
