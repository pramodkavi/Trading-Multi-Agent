"""Minimal SMC analyzer for Slice 1: HTF (4H) bias detection only.

Scope per SPEC §4 Step 1.5:
    "For Slice 1, implement only HTF bias detection — always returns SKIP
     unless 4H bias is clear."

This module is intentionally narrow. It does *not* implement:
- the 5-gate MTF POI validation (Slice 2 Step 2.1)
- the LTF execution trigger (Slice 2 Step 2.1)
- BOS / CHoCH structural break detection (Slice 2 Step 2.1)
- Premium/Discount with OTE Fibonacci (Slice 2 Step 2.1)

What it *does*:
1. Find swing highs and swing lows on the 4H series via the standard pivot-K
   method (a candle whose high beats the K candles on each side).
2. Classify bias as UPTREND (HH+HL), DOWNTREND (LH+LL), or CONSOLIDATION (mixed).
3. When bias is clear, synthesize a minimal stub SignalProposal so the rest
   of Slice 1 (Telegram delivery, Postgres journaling) can be exercised
   end-to-end per SPEC §5.3. The stub uses the most-recent opposite swing
   as the SL anchor and a fixed 1:3 R:R for TP1. Tagged 'slice-1-stub' so
   downstream agents (and the Critic later) can distinguish these from
   real Slice 2+ proposals.

Design choices documented inline; trade-offs surfaced as constants at the top
of the module so future iterations can tune them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final
from uuid import UUID

from src.common.models import (
    SignalDirection,
    SignalProposal,
    SkipDecision,
    SkipReason,
)
from src.providers import Kline, MarketSnapshot, Timeframe

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence


# ---------------------------------------------------------------------------
# Tunable parameters
# ---------------------------------------------------------------------------

PIVOT_LOOKBACK: Final[int] = 3
"""How many candles on each side a swing must dominate to count as a pivot.

K=3 means a swing high beats both the 3 candles before *and* the 3 after.
Larger K = fewer, more reliable pivots. Smaller K = more pivots, more noise.
3 is the conservative SMC default for higher timeframes.
"""

MIN_KLINES_REQUIRED: Final[int] = 30
"""Minimum 4H candles needed to confidently classify bias.

We need at least 2 swing highs and 2 swing lows to compare HH/HL or LH/LL,
each pivot needs K candles on each side, and we want pivots in the recent
portion of the series (see MAX_PIVOT_AGE). 30 gives comfortable headroom.
"""

MAX_PIVOT_AGE: Final[int] = 20
"""How far back (in candles) the latest pivot may be before we declare CONSOLIDATION.

If the most recent swing happened 25 candles ago, the bias signal is stale —
market is probably ranging, not in a verified trend. 20 candles on 4H = ~3.3 days
of history, which is the right freshness window for an HTF bias.
"""

STUB_SL_BUFFER: Final[float] = 0.002
"""Padding applied past the swing-anchor when placing SL on the stub proposal.

0.2% buffer beyond the swing low/high keeps the SL from being clipped by
normal wick volatility. Production SMC (Slice 2) places SL beyond the
*liquidity sweep wick* — this buffer is a stand-in until that's implemented.
"""

STUB_RR_RATIO: Final[float] = 3.0
"""Fixed R:R ratio for the stub TP1.

SPEC §1.6 rule 2 mandates minimum 1:3. The Slice 1 stub uses exactly 3.0
so risk_gates.py (Step 2.11) accepts it. Slice 2 strategies compute R:R
from real structural targets.
"""


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class HTFBias(StrEnum):
    """Higher-timeframe directional bias derived from 4H swing structure."""

    UPTREND = "UPTREND"  # higher highs + higher lows
    DOWNTREND = "DOWNTREND"  # lower highs + lower lows
    CONSOLIDATION = "CONSOLIDATION"  # mixed / stale / insufficient


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze(
    snapshot: MarketSnapshot,
    *,
    scan_id: UUID,
    strategy: str = "smc",
) -> SignalProposal | SkipDecision:
    """Run the Slice 1 SMC analyzer on a MarketSnapshot.

    Args:
        snapshot: market data for one symbol; must contain the 4H timeframe.
        scan_id: the scan run this analysis belongs to (joined to ScanContext).
        strategy: registry name; defaults to 'smc' (only strategy in Slice 1).

    Returns:
        SignalProposal when 4H bias is clearly UPTREND or DOWNTREND.
        SkipDecision otherwise — categorized so analytics can group skip
        reasons later.

    Notes:
        Signature takes scan_id as a keyword argument so that Slice 3 Step 3.1
        can replace it with `scan_context: ScanContext` without forcing every
        call site to change positional arguments.
    """
    candles = snapshot.klines.get(Timeframe.H4)
    if candles is None:
        return _skip(
            scan_id=scan_id,
            strategy=strategy,
            symbol=snapshot.symbol,
            reason=SkipReason.DATA_UNAVAILABLE,
            details="MarketSnapshot has no 4H klines; Slice 1 SMC analyzer requires Timeframe.H4.",
        )

    if len(candles) < MIN_KLINES_REQUIRED:
        return _skip(
            scan_id=scan_id,
            strategy=strategy,
            symbol=snapshot.symbol,
            reason=SkipReason.DATA_UNAVAILABLE,
            details=(
                f"Insufficient 4H history for bias detection "
                f"(got {len(candles)} candles, need {MIN_KLINES_REQUIRED})."
            ),
        )

    swing_highs = _detect_swing_highs(candles, lookback=PIVOT_LOOKBACK)
    swing_lows = _detect_swing_lows(candles, lookback=PIVOT_LOOKBACK)

    bias = _classify_bias(
        candles=candles,
        swing_highs=swing_highs,
        swing_lows=swing_lows,
    )

    if bias is HTFBias.CONSOLIDATION:
        return _skip(
            scan_id=scan_id,
            strategy=strategy,
            symbol=snapshot.symbol,
            reason=SkipReason.NO_CLEAR_BIAS,
            details=(
                "4H structure does not show HH+HL or LH+LL within the recent window "
                f"(MAX_PIVOT_AGE={MAX_PIVOT_AGE}); classifying as CONSOLIDATION."
            ),
        )

    return _build_stub_proposal(
        bias=bias,
        candles=candles,
        swing_highs=swing_highs,
        swing_lows=swing_lows,
        snapshot=snapshot,
        scan_id=scan_id,
        strategy=strategy,
    )


# ---------------------------------------------------------------------------
# Swing pivot detection
# ---------------------------------------------------------------------------


def _detect_swing_highs(candles: Sequence[Kline], *, lookback: int) -> list[int]:
    """Return indices of candles that are swing highs under the pivot-K method.

    A candle at index i is a swing high iff its high is strictly greater than
    the highs of the `lookback` candles immediately before and after. Edge
    candles (within `lookback` of either end) cannot be confirmed and are skipped.

    Returns indices in ascending order (oldest pivot first).
    """
    pivots: list[int] = []
    for i in range(lookback, len(candles) - lookback):
        center_high = candles[i].high
        is_pivot = all(
            center_high > candles[j].high for j in range(i - lookback, i + lookback + 1) if j != i
        )
        if is_pivot:
            pivots.append(i)
    return pivots


def _detect_swing_lows(candles: Sequence[Kline], *, lookback: int) -> list[int]:
    """Mirror of _detect_swing_highs for swing lows.

    A candle at index i is a swing low iff its low is strictly less than the
    lows of the `lookback` candles on each side.
    """
    pivots: list[int] = []
    for i in range(lookback, len(candles) - lookback):
        center_low = candles[i].low
        is_pivot = all(
            center_low < candles[j].low for j in range(i - lookback, i + lookback + 1) if j != i
        )
        if is_pivot:
            pivots.append(i)
    return pivots


# ---------------------------------------------------------------------------
# Bias classification
# ---------------------------------------------------------------------------


def _classify_bias(
    *,
    candles: Sequence[Kline],
    swing_highs: list[int],
    swing_lows: list[int],
) -> HTFBias:
    """Classify the most recent structure as UPTREND, DOWNTREND, or CONSOLIDATION.

    Requires at least 2 swing highs and 2 swing lows, with the most recent of
    each within MAX_PIVOT_AGE candles of the latest bar. The strict comparison
    (HH+HL or LH+LL) is the SMC market-structure definition; mixed signals
    (HH+LL, LH+HL) collapse to CONSOLIDATION.
    """
    latest_index = len(candles) - 1
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return HTFBias.CONSOLIDATION
    if (
        latest_index - swing_highs[-1] > MAX_PIVOT_AGE
        or latest_index - swing_lows[-1] > MAX_PIVOT_AGE
    ):
        return HTFBias.CONSOLIDATION

    prev_high = candles[swing_highs[-2]].high
    latest_high = candles[swing_highs[-1]].high
    prev_low = candles[swing_lows[-2]].low
    latest_low = candles[swing_lows[-1]].low

    higher_highs = latest_high > prev_high
    higher_lows = latest_low > prev_low
    lower_highs = latest_high < prev_high
    lower_lows = latest_low < prev_low

    if higher_highs and higher_lows:
        return HTFBias.UPTREND
    if lower_highs and lower_lows:
        return HTFBias.DOWNTREND
    return HTFBias.CONSOLIDATION


# ---------------------------------------------------------------------------
# Stub proposal construction
# ---------------------------------------------------------------------------


def _build_stub_proposal(
    *,
    bias: HTFBias,
    candles: Sequence[Kline],
    swing_highs: list[int],
    swing_lows: list[int],
    snapshot: MarketSnapshot,
    scan_id: UUID,
    strategy: str,
) -> SignalProposal:
    """Build a minimal Slice-1 SignalProposal from a confirmed bias.

    LONG geometry: SL = latest swing low * (1 - buffer); entry = latest close;
                   TP1 = entry + 3 * (entry - SL).
    SHORT geometry: mirrored — SL = latest swing high * (1 + buffer).
    """
    latest_close = candles[-1].close

    if bias is HTFBias.UPTREND:
        anchor_low = candles[swing_lows[-1]].low
        stop_loss = anchor_low * (1.0 - STUB_SL_BUFFER)
        risk = latest_close - stop_loss
        take_profit_1 = latest_close + STUB_RR_RATIO * risk
        direction = SignalDirection.LONG
        narrative = (
            f"4H structure shows higher highs and higher lows: latest swing high "
            f"{candles[swing_highs[-1]].high:.2f} > prior {candles[swing_highs[-2]].high:.2f}; "
            f"latest swing low {anchor_low:.2f} > prior {candles[swing_lows[-2]].low:.2f}. "
            f"Slice 1 stub proposal: SL anchored to latest swing low with "
            f"{STUB_SL_BUFFER * 100:.1f}% buffer, TP1 at 1:{STUB_RR_RATIO:.0f} R:R."
        )
    else:  # DOWNTREND
        anchor_high = candles[swing_highs[-1]].high
        stop_loss = anchor_high * (1.0 + STUB_SL_BUFFER)
        risk = stop_loss - latest_close
        take_profit_1 = latest_close - STUB_RR_RATIO * risk
        direction = SignalDirection.SHORT
        narrative = (
            f"4H structure shows lower highs and lower lows: latest swing high "
            f"{anchor_high:.2f} < prior {candles[swing_highs[-2]].high:.2f}; "
            f"latest swing low {candles[swing_lows[-1]].low:.2f} < prior "
            f"{candles[swing_lows[-2]].low:.2f}. "
            f"Slice 1 stub proposal: SL anchored to latest swing high with "
            f"{STUB_SL_BUFFER * 100:.1f}% buffer, TP1 at 1:{STUB_RR_RATIO:.0f} R:R."
        )

    return SignalProposal(
        scan_id=scan_id,
        strategy=strategy,
        symbol=snapshot.symbol,
        direction=direction,
        entry_price=latest_close,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        risk_reward_ratio=STUB_RR_RATIO,
        leverage=1.0,
        risk_percent=1.0,
        tags=["slice-1-stub", "htf-bias-only", f"bias-{bias.value.lower()}"],
        confluence_narrative=narrative,
        features={
            "htf_bias": bias.value,
            "latest_swing_high": candles[swing_highs[-1]].high,
            "latest_swing_low": candles[swing_lows[-1]].low,
            "n_candles_analyzed": len(candles),
        },
    )


def _skip(
    *,
    scan_id: UUID,
    strategy: str,
    symbol: str,
    reason: SkipReason,
    details: str,
) -> SkipDecision:
    """Small helper to make the analyze() decision-tree readable."""
    return SkipDecision(
        scan_id=scan_id,
        strategy=strategy,
        symbol=symbol,
        reason=reason,
        details=details,
    )
