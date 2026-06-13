"""ATR (Average True Range) helpers for volatility normalization.

SMC thresholds that the reference scripts hard-coded as fixed percentages (FVG
size, displacement, equal-level tolerance, break significance) should instead
scale with the instrument's recent volatility, so the same code behaves sensibly
on BTC 4H and SOL 5m. This module is the shared volatility primitive used by the
structure layer (break-significance margin) and, later, the FVG/OB detectors.

**As-of correctness:** `atr_series` returns a per-bar ATR where `atr[j]` uses
only candles `0..j`. Detectors evaluating bar j must use `atr[j]` — never a
single ATR computed over the whole (future-containing) series.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.providers.base import Kline

DEFAULT_ATR_PERIOD: Final[int] = 14
"""Wilder's classic ATR window. Needs period+1 candles (one prior close per TR)."""


def true_ranges(candles: Sequence[Kline]) -> list[float]:
    """Per-bar true range. `tr[k]` is the true range of `candles[k]` (k >= 1).

    TR_k = max(high_k - low_k, |high_k - close_{k-1}|, |low_k - close_{k-1}|).
    `tr[0]` is 0.0 (no prior close exists); callers should ignore index 0.
    """
    trs: list[float] = [0.0] * len(candles)
    for k in range(1, len(candles)):
        curr = candles[k]
        prev_close = candles[k - 1].close
        trs[k] = max(
            curr.high - curr.low,
            abs(curr.high - prev_close),
            abs(curr.low - prev_close),
        )
    return trs


def atr_series(candles: Sequence[Kline], *, period: int = DEFAULT_ATR_PERIOD) -> list[float | None]:
    """Per-bar ATR (simple mean of the last `period` true ranges ending at bar j).

    Returns a list the same length as `candles`. `atr[j]` is None until enough
    history exists (j < period), then the mean of `tr[j-period+1 .. j]`. Each
    value depends only on candles up to j, so it is safe to use inside an
    as-of-correct detector loop.

    A simple moving average (not Wilder's recursive smoothing) is used for
    determinism and testability; Wilder smoothing is a possible later refinement.
    """
    n = len(candles)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out
    trs = true_ranges(candles)
    for j in range(period, n):
        window = trs[j - period + 1 : j + 1]  # `period` values, all with k >= 1
        out[j] = sum(window) / period
    return out


def average_true_range(
    candles: Sequence[Kline], *, period: int = DEFAULT_ATR_PERIOD
) -> float | None:
    """ATR as of the most recent candle (current volatility), or None if too few bars."""
    series = atr_series(candles, period=period)
    return series[-1] if series else None
