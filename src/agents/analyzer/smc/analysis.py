"""Full SMC assembly — combines the four detector layers into a scored proposal.

This is the capstone of Step 2.1: it runs the structure, liquidity, order-block,
and FVG detectors and applies the SMC 5-layer / 5-gate protocol (SPEC §1.5),
producing a complete `SignalProposal` or a structured `SkipDecision`.

**Hybrid gating (evidence-weighted, per docs/research/smc-evidence-review.md).**
The reference design was "all 5 gates must pass" (binary), which the research shows
manufactures false confidence. Instead:
  - HARD gates (no setup without them): a clear directional bias (structure), the
    Premium/Discount constraint (a §1.6 hard rule), and a valid order-block POI.
  - WEIGHTED confluence (drives a publish/skip threshold, NOT a win probability):
    liquidity sweep (highest evidence weight), OB displacement, FVG, fresh OB, OTE
    (lowest weight). `confluence_score` is a raw heuristic tally surfaced in the
    proposal's features/tags; real confidence is the Judge's job and is calibrated
    by forward-testing (Historian/Critic), never asserted here.

**Single-timeframe for now.** The protocol is inherently multi-timeframe (HTF bias
→ LTF entry); Slice 1 only feeds H4. This runs all detectors on the best available
timeframe. Step 2.2 adds D1/H1/M15/M5 and the top-down split. The derivatives gate
(Gate 4) is likewise deferred to 2.2 (no funding/OI in the snapshot yet).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from src.agents.analyzer.smc.liquidity import analyze_liquidity
from src.agents.analyzer.smc.models import (
    LiquidityAnalysis,
    MarketPhase,
    OrderBlock,
    OrderBlockDirection,
    StructureAnalysis,
    SweepType,
    Zone,
)
from src.agents.analyzer.smc.order_block import detect_order_blocks
from src.agents.analyzer.smc.structure import analyze_structure
from src.common.models import SignalDirection, SignalProposal, SkipDecision, SkipReason
from src.providers.base import Timeframe

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.providers.base import Kline, MarketSnapshot

MIN_CANDLES: Final[int] = 21
"""Enough for ATR(14) plus several confirmed swings."""

DEFAULT_LEVERAGE: Final[float] = 3.0
"""Conservative leverage recommendation; risk_gates enforces the 10x policy cap."""

DEFAULT_RISK_PERCENT: Final[float] = 1.0
"""One signal risks at most 1% equity (SPEC §1.6 rule 1)."""

SL_ATR_BUFFER: Final[float] = 0.25
"""Stop sits beyond the POI/sweep by this fraction of ATR (SPEC §1.5 Layer 5)."""

RECENT_SWEEP_WINDOW: Final[int] = 12
"""A liquidity sweep counts as confluence only if it happened within this many bars."""

MIN_CONFLUENCE_SCORE: Final[int] = 2
"""Minimum weighted confluence to publish (else SKIP GATE_FAILED)."""

SWEEP_WEIGHT: Final[int] = 2
"""Liquidity sweep carries double weight (highest-evidence SMC component)."""

PRIMARY_POI_TYPE_ORDER_BLOCK: Final[str] = "order_block"
"""Value of the ``primary_poi_type`` feature: every SMC entry is anchored to an
order block today. Surfaced first-class for the Historian's stage-1 hard filter."""

# Order in which to fall back when choosing the analysis timeframe (HTF preferred).
_TIMEFRAME_PRIORITY: Final[tuple[Timeframe, ...]] = (
    Timeframe.H4,
    Timeframe.D1,
    Timeframe.H1,
    Timeframe.M15,
    Timeframe.M5,
)


def full_smc_analysis(
    snapshot: MarketSnapshot,
    *,
    scan_id: UUID,
    strategy: str = "smc",
) -> SignalProposal | SkipDecision:
    """Run the full SMC protocol on a snapshot; emit a SignalProposal or SkipDecision."""
    selected = _select_candles(snapshot)
    if selected is None:
        return _skip(
            scan_id,
            strategy,
            snapshot.symbol,
            SkipReason.DATA_UNAVAILABLE,
            "MarketSnapshot has no usable timeframe for SMC analysis.",
        )
    timeframe, candles = selected
    if len(candles) < MIN_CANDLES:
        return _skip(
            scan_id,
            strategy,
            snapshot.symbol,
            SkipReason.DATA_UNAVAILABLE,
            f"Insufficient {timeframe.value} history ({len(candles)} candles, need {MIN_CANDLES}).",
        )

    current_price = candles[-1].close
    structure = analyze_structure(candles)

    # --- HARD gate: directional bias from HTF structure (SPEC §1.5 Layer 2) ---
    direction = _bias_direction(structure.phase)
    if direction is None:
        return _skip(
            scan_id,
            strategy,
            snapshot.symbol,
            SkipReason.NO_CLEAR_BIAS,
            f"{timeframe.value} structure is CONSOLIDATION; no directional bias.",
        )

    # --- HARD gate: Premium/Discount constraint (SPEC §1.5 / §1.6 rule 3) ---
    if structure.dealing_range is None or structure.zone is None:
        return _skip(
            scan_id,
            strategy,
            snapshot.symbol,
            SkipReason.NO_CLEAR_BIAS,
            "No dealing range available to locate price in premium/discount.",
        )
    pd_skip = _premium_discount_gate(direction, structure.zone, scan_id, strategy, snapshot.symbol)
    if pd_skip is not None:
        return pd_skip

    # --- HARD gate: a valid order-block POI in the bias direction ---
    order_blocks = detect_order_blocks(candles)
    poi = _select_poi(order_blocks, direction, current_price)
    if poi is None:
        return _skip(
            scan_id,
            strategy,
            snapshot.symbol,
            SkipReason.GATE_FAILED,
            f"No valid {direction.value} order-block POI relative to price {current_price:.4f}.",
        )

    # --- Risk geometry (SPEC §1.5 Layer 5) ---
    liquidity = analyze_liquidity(candles)
    geometry = _build_geometry(direction, poi, liquidity, structure, current_price)
    if geometry is None:
        return _skip(
            scan_id,
            strategy,
            snapshot.symbol,
            SkipReason.OTHER,
            "No valid risk geometry (no liquidity target or degenerate stop).",
        )
    entry, stop_loss, take_profit_1, risk_reward_ratio = geometry

    # --- Weighted confluence (publish/skip threshold; NOT a win probability) ---
    score, factors = _confluence(direction, poi, liquidity, structure, entry, len(candles))
    if score < MIN_CONFLUENCE_SCORE:
        return _skip(
            scan_id,
            strategy,
            snapshot.symbol,
            SkipReason.GATE_FAILED,
            f"Confluence score {score} below threshold {MIN_CONFLUENCE_SCORE} "
            f"(factors: {factors}).",
        )

    return _build_proposal(
        scan_id=scan_id,
        strategy=strategy,
        symbol=snapshot.symbol,
        timeframe=timeframe,
        direction=direction,
        structure=structure,
        poi=poi,
        entry=entry,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        risk_reward_ratio=risk_reward_ratio,
        score=score,
        factors=factors,
        current_price=current_price,
    )


def _select_candles(snapshot: MarketSnapshot) -> tuple[Timeframe, list[Kline]] | None:
    """Pick the analysis timeframe (HTF preferred), or None if the snapshot is empty."""
    for tf in _TIMEFRAME_PRIORITY:
        candles = snapshot.klines.get(tf)
        if candles:
            return tf, candles
    return None


def _bias_direction(phase: MarketPhase) -> SignalDirection | None:
    if phase is MarketPhase.UPTREND:
        return SignalDirection.LONG
    if phase is MarketPhase.DOWNTREND:
        return SignalDirection.SHORT
    return None


def _premium_discount_gate(
    direction: SignalDirection,
    zone: Zone,
    scan_id: UUID,
    strategy: str,
    symbol: str,
) -> SkipDecision | None:
    """Long only in Discount, short only in Premium (SPEC §1.5 hard rule)."""
    if direction is SignalDirection.LONG and zone is not Zone.DISCOUNT:
        return _skip(
            scan_id,
            strategy,
            symbol,
            SkipReason.PREMIUM_DISCOUNT_VIOLATION,
            f"LONG bias but price is in {zone.value}; longs are only allowed in Discount.",
            violated_rule="RULE_3_PREMIUM_DISCOUNT",
        )
    if direction is SignalDirection.SHORT and zone is not Zone.PREMIUM:
        return _skip(
            scan_id,
            strategy,
            symbol,
            SkipReason.PREMIUM_DISCOUNT_VIOLATION,
            f"SHORT bias but price is in {zone.value}; shorts are only allowed in Premium.",
            violated_rule="RULE_3_PREMIUM_DISCOUNT",
        )
    return None


def _select_poi(
    order_blocks: list[OrderBlock],
    direction: SignalDirection,
    current_price: float,
) -> OrderBlock | None:
    """Pick the order-block POI: nearest demand below price (long) / supply above (short).

    Unmitigated (fresh) blocks are preferred; a mitigated block is used only if no
    fresh one is positioned for a pullback entry.
    """
    if direction is SignalDirection.LONG:
        candidates = [
            ob
            for ob in order_blocks
            if ob.direction is OrderBlockDirection.BULLISH and ob.zone_high <= current_price
        ]
    else:
        candidates = [
            ob
            for ob in order_blocks
            if ob.direction is OrderBlockDirection.BEARISH and ob.zone_low >= current_price
        ]
    if not candidates:
        return None

    pool = [ob for ob in candidates if not ob.mitigated] or candidates
    if direction is SignalDirection.LONG:
        return max(pool, key=lambda ob: ob.zone_high)
    return min(pool, key=lambda ob: ob.zone_low)


def _build_geometry(
    direction: SignalDirection,
    poi: OrderBlock,
    liquidity: LiquidityAnalysis,
    structure: StructureAnalysis,
    current_price: float,
) -> tuple[float, float, float, float] | None:
    """Entry at the POI, stop beyond it, target the nearest resting opposing liquidity."""
    buffer = (structure.atr if structure.atr is not None else current_price * 0.001) * SL_ATR_BUFFER
    dealing_range = structure.dealing_range
    assert dealing_range is not None  # guarded by the caller

    if direction is SignalDirection.LONG:
        entry = poi.zone_high
        stop_loss = poi.zone_low - buffer
        target = (
            liquidity.nearest_bsl if liquidity.nearest_bsl is not None else dealing_range.range_high
        )
        if stop_loss >= entry or target <= entry:
            return None
    else:
        entry = poi.zone_low
        stop_loss = poi.zone_high + buffer
        target = (
            liquidity.nearest_ssl if liquidity.nearest_ssl is not None else dealing_range.range_low
        )
        if stop_loss <= entry or target >= entry:
            return None

    risk = abs(entry - stop_loss)
    reward = abs(target - entry)
    if risk <= 0:
        return None
    return entry, stop_loss, target, reward / risk


def _confluence(
    direction: SignalDirection,
    poi: OrderBlock,
    liquidity: LiquidityAnalysis,
    structure: StructureAnalysis,
    entry: float,
    n_candles: int,
) -> tuple[int, dict[str, bool]]:
    """Weighted confluence tally + the boolean factors that produced it."""
    recent_threshold = n_candles - RECENT_SWEEP_WINDOW
    want_sweep = SweepType.SWEEP_SSL if direction is SignalDirection.LONG else SweepType.SWEEP_BSL
    recent_sweep = any(
        s.sweep_type is want_sweep and s.index >= recent_threshold for s in liquidity.sweeps
    )

    in_ote = False
    if structure.dealing_range is not None:
        in_ote = structure.dealing_range.ote_lower <= entry <= structure.dealing_range.ote_upper

    factors = {
        "liquidity_sweep": recent_sweep,
        "ob_displacement": poi.has_displacement,
        "ob_fvg": poi.has_fvg,
        "ob_unmitigated": not poi.mitigated,
        "ote": in_ote,
    }
    score = (
        (SWEEP_WEIGHT if recent_sweep else 0)
        + int(poi.has_displacement)
        + int(poi.has_fvg)
        + int(not poi.mitigated)
        + int(in_ote)
    )
    return score, factors


def _build_proposal(
    *,
    scan_id: UUID,
    strategy: str,
    symbol: str,
    timeframe: Timeframe,
    direction: SignalDirection,
    structure: StructureAnalysis,
    poi: OrderBlock,
    entry: float,
    stop_loss: float,
    take_profit_1: float,
    risk_reward_ratio: float,
    score: int,
    factors: dict[str, bool],
    current_price: float,
) -> SignalProposal:
    tags = ["smc", f"bias-{structure.phase.value.lower()}", direction.value.lower()]
    if structure.zone is not None:
        tags.append(structure.zone.value.lower())
    tags.append("bullish-ob" if direction is SignalDirection.LONG else "bearish-ob")
    if factors["liquidity_sweep"]:
        tags.append("liquidity-sweep")
    if factors["ob_displacement"]:
        tags.append("displacement")
    if factors["ob_fvg"]:
        tags.append("fvg-confluence")
    if factors["ob_unmitigated"]:
        tags.append("unmitigated-ob")
    if factors["ote"]:
        tags.append("ote")

    narrative = (
        f"{timeframe.value} structure is {structure.phase.value} with price in "
        f"{structure.zone.value if structure.zone else 'UNKNOWN'}; {direction.value} setup off a "
        f"{'bullish' if direction is SignalDirection.LONG else 'bearish'} order block "
        f"({poi.zone_low:.4f}-{poi.zone_high:.4f}). Entry {entry:.4f}, stop {stop_loss:.4f}, "
        f"target {take_profit_1:.4f} (R:R {risk_reward_ratio:.2f}). Confluence score {score} "
        f"[{', '.join(k for k, v in factors.items() if v) or 'none'}]. "
        f"Note: confluence is a heuristic tally, not a calibrated win probability."
    )

    features: dict[str, float | int | str | bool] = {
        "timeframe": timeframe.value,
        "phase": structure.phase.value,
        "zone": structure.zone.value if structure.zone else "UNKNOWN",
        # The kind of Point of Interest the entry is anchored to. The SMC analyzer
        # only produces order-block-anchored entries today, but this is a first-class
        # categorical so the Historian's stage-1 hard filter (SPEC §4 Step 2.4) can
        # match like-for-like setups -- and stays discriminating once future POI kinds
        # (breaker, FVG-as-POI) appear without a schema change.
        "primary_poi_type": PRIMARY_POI_TYPE_ORDER_BLOCK,
        "confluence_score": score,
        "current_price": current_price,
        "atr": structure.atr if structure.atr is not None else 0.0,
        "ob_index": poi.ob_index,
        "ob_zone_high": poi.zone_high,
        "ob_zone_low": poi.zone_low,
        "ob_confluence_count": poi.confluence_count,
        **{f"factor_{k}": v for k, v in factors.items()},
    }

    return SignalProposal(
        scan_id=scan_id,
        strategy=strategy,
        symbol=symbol,
        direction=direction,
        entry_price=entry,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        risk_reward_ratio=risk_reward_ratio,
        leverage=DEFAULT_LEVERAGE,
        risk_percent=DEFAULT_RISK_PERCENT,
        tags=tags,
        confluence_narrative=narrative,
        features=features,
    )


def _skip(
    scan_id: UUID,
    strategy: str,
    symbol: str,
    reason: SkipReason,
    details: str,
    *,
    violated_rule: str | None = None,
) -> SkipDecision:
    return SkipDecision(
        scan_id=scan_id,
        strategy=strategy,
        symbol=symbol,
        reason=reason,
        details=details,
        violated_rule=violated_rule,
    )
