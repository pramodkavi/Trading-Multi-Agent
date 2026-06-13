"""Liquidity layer: pools, equal highs/lows, and stop-hunt sweeps — Step 2.1d.

Ports `detect_liquidity.py`. Per the evidence review this is the SMC concept with
the most empirical support: resting orders cluster at obvious levels (swing highs/
lows, equal highs/lows ~ round numbers), and a measurable stop-loss asymmetry is
the real mechanism behind "liquidity sweeps / stop hunts". So this layer carries
high weight in the eventual scoring step.

Fixes / improvements over the reference script:
- **As-of correct sweeps.** The reference scanned every candle against every level,
  including levels formed *later in time* (look-ahead). Here each pool is anchored
  to a confirmed swing (`confirmed_at_index`), and its sweep/break is the FIRST
  candle after confirmation to interact with the immutable swing price — so a sweep
  at bar j depends only on candles <= j.
- **Volatility-normalized "equal" tolerance** (replacing the fixed 0.08% of price).
- **Sweep vs break distinction.** A wick-through with the body rejecting is a SWEEP
  (stop hunt); a body close through is a BREAK (level taken) — only the former is a
  reversal signal. The reference conflated the level's fate.

`equal_count`/`strength` are a current-state magnet measure (they grow as price
reprints a level); `status` evolves RESTING -> SWEPT/BROKEN like a mitigation flag.
The as-of-correct *events* are the sweeps, which is what the no-look-ahead test pins.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from src.agents.analyzer.smc.models import (
    LiquidityAnalysis,
    LiquidityPool,
    LiquiditySide,
    LiquidityStrength,
    LiquiditySweep,
    PoolStatus,
    SweepType,
    SwingPoint,
    SwingType,
)
from src.agents.analyzer.smc.swings import DEFAULT_LOOKBACK, detect_swings
from src.agents.analyzer.smc.volatility import DEFAULT_ATR_PERIOD, atr_series

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.providers.base import Kline

DEFAULT_EQUAL_ATR_FRACTION: Final[float] = 0.10
"""Two same-side swings are 'equal' (one pool) within this fraction of the as-of ATR."""

DEFAULT_EQUAL_PRICE_FRACTION: Final[float] = 0.0008
"""Fallback 'equal' tolerance (fraction of price) when ATR is unavailable (~0.08%)."""


def analyze_liquidity(
    candles: Sequence[Kline],
    *,
    lookback: int = DEFAULT_LOOKBACK,
    atr_period: int = DEFAULT_ATR_PERIOD,
    equal_atr_fraction: float = DEFAULT_EQUAL_ATR_FRACTION,
    equal_price_fraction: float = DEFAULT_EQUAL_PRICE_FRACTION,
) -> LiquidityAnalysis:
    """Map liquidity pools and stop-hunt sweeps for one timeframe's candle series.

    Pool status and sweeps are evaluated as of the last candle in `candles`.
    """
    if not candles:
        raise ValueError("analyze_liquidity requires at least one candle")

    n = len(candles)
    last = n - 1
    current_price = candles[-1].close
    atr = atr_series(candles, period=atr_period)
    atr_now = atr[-1]
    tolerance = _equal_tolerance(current_price, atr_now, equal_atr_fraction, equal_price_fraction)

    swings = detect_swings(candles, lookback=lookback)
    highs = [s for s in swings if s.swing_type is SwingType.HIGH]
    lows = [s for s in swings if s.swing_type is SwingType.LOW]

    pools: list[LiquidityPool] = []
    sweeps: list[LiquiditySweep] = []

    for sw in highs:
        pool, sweep = _make_pool(sw, LiquiditySide.BUY_SIDE, highs, candles, last, tolerance)
        pools.append(pool)
        if sweep is not None:
            sweeps.append(sweep)
    for sw in lows:
        pool, sweep = _make_pool(sw, LiquiditySide.SELL_SIDE, lows, candles, last, tolerance)
        pools.append(pool)
        if sweep is not None:
            sweeps.append(sweep)

    pools.sort(key=lambda p: p.price)
    sweeps.sort(key=lambda s: s.index)

    nearest_bsl = min(
        (
            p.price
            for p in pools
            if p.side is LiquiditySide.BUY_SIDE
            and p.status is PoolStatus.RESTING
            and p.price > current_price
        ),
        default=None,
    )
    nearest_ssl = max(
        (
            p.price
            for p in pools
            if p.side is LiquiditySide.SELL_SIDE
            and p.status is PoolStatus.RESTING
            and p.price < current_price
        ),
        default=None,
    )

    return LiquidityAnalysis(
        current_price=current_price,
        pools=pools,
        sweeps=sweeps,
        nearest_bsl=nearest_bsl,
        nearest_ssl=nearest_ssl,
        atr=atr_now,
    )


def _equal_tolerance(
    price: float, atr_now: float | None, equal_atr_fraction: float, equal_price_fraction: float
) -> float:
    """Price distance under which two same-side swings count as one pool."""
    if atr_now is not None and atr_now > 0.0:
        return equal_atr_fraction * atr_now
    return equal_price_fraction * price


def _make_pool(
    swing: SwingPoint,
    side: LiquiditySide,
    same_side_swings: list[SwingPoint],
    candles: Sequence[Kline],
    last: int,
    tolerance: float,
) -> tuple[LiquidityPool, LiquiditySweep | None]:
    """Build one pool from a swing, classifying its strength and sweep/break status."""
    equal_count = sum(1 for o in same_side_swings if abs(o.price - swing.price) <= tolerance)
    if equal_count >= 3:
        strength = LiquidityStrength.TRIPLE
    elif equal_count >= 2:
        strength = LiquidityStrength.EQUAL
    else:
        strength = LiquidityStrength.SINGLE

    status, resolved_index, sweep = _resolve_pool(swing, side, candles, last)

    pool = LiquidityPool(
        side=side,
        price=swing.price,
        swing_index=swing.index,
        equal_count=equal_count,
        strength=strength,
        confirmed_at_index=swing.confirmed_at_index,
        status=status,
        resolved_index=resolved_index,
    )
    return pool, sweep


def _resolve_pool(
    swing: SwingPoint,
    side: LiquiditySide,
    candles: Sequence[Kline],
    last: int,
) -> tuple[PoolStatus, int | None, LiquiditySweep | None]:
    """First interaction after confirmation decides the pool's fate (as-of correct).

    A wick beyond the level with the body rejecting is a SWEEP; a body close beyond
    is a BREAK. Whichever happens first wins; if neither happens, the pool is RESTING.
    """
    level = swing.price
    for idx in range(swing.confirmed_at_index + 1, last + 1):
        candle = candles[idx]
        if side is LiquiditySide.BUY_SIDE:
            if candle.high > level:
                if max(candle.open, candle.close) < level:
                    sweep = LiquiditySweep(
                        sweep_type=SweepType.SWEEP_BSL,
                        index=idx,
                        open_time=candle.open_time,
                        swept_level=level,
                        wick_extreme=candle.high,
                        swing_index=swing.index,
                    )
                    return PoolStatus.SWEPT, idx, sweep
                return PoolStatus.BROKEN, idx, None
        else:  # SELL_SIDE
            if candle.low < level:
                if min(candle.open, candle.close) > level:
                    sweep = LiquiditySweep(
                        sweep_type=SweepType.SWEEP_SSL,
                        index=idx,
                        open_time=candle.open_time,
                        swept_level=level,
                        wick_extreme=candle.low,
                        swing_index=swing.index,
                    )
                    return PoolStatus.SWEPT, idx, sweep
                return PoolStatus.BROKEN, idx, None

    return PoolStatus.RESTING, None, None
