"""Deterministic price-path replay for one proposal (design §5.3).

Pure function over candles -- no I/O, no LLM. Whether price touched the
target before the stop is arithmetic; keeping it here (and versioned via
``ENGINE_VERSION``) means outcomes can be re-derived when the rules change.

Rules, in order of evaluation:

1. Candles are restricted to ``decided_at <= open_time < decided_at + max_hold``
   and sorted by ``open_time``.
2. Entry is *touched* when ``low <= entry <= high``. A candle that *opens*
   beyond the entry in the trade's favour (long ``open < entry``, short
   ``open > entry``) is a gap-through: the fill is that candle's ``open``.
   No fill by ``decided_at + entry_validity`` -> ``EXPIRED`` with ``realized_r = 0``.
3. After the fill, per candle: the stop is checked **before** the target, so a
   candle touching both is a ``LOSS`` (intra-candle order is unknown).
4. R is measured from the fill price with the *planned* risk ``|entry - stop|``
   as the unit, signed by direction.
5. MAE/MFE accumulate from the entry candle onward, clamped to ``<= 0`` / ``>= 0``.
   ``tp2_reached`` is only set on candles that did not also hit the stop.
6. Window exhausted with nothing hit: if ``now >= decided_at + max_hold`` ->
   ``EXPIRED`` (mark-to-market R at the last close if entered, else 0);
   otherwise *pending* (``outcome is None``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.agents.critic.models import ResolutionOutcome
from src.common.models import SignalDirection, SignalProposal
from src.providers.base import Kline

ENGINE_VERSION = "replay-v1"
ENTRY_VALIDITY = timedelta(hours=72)
MAX_HOLD = timedelta(days=7)
CANDLE_STEP = timedelta(minutes=5)


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of replaying one proposal. ``outcome is None`` == still pending."""

    outcome: ResolutionOutcome | None
    entered_at: datetime | None
    resolved_at: datetime | None
    realized_r: float
    max_adverse_excursion_r: float | None
    max_favorable_excursion_r: float | None
    tp2_reached: bool
    candles_used: int


def _expired_unentered(resolved_at: datetime, candles_used: int) -> ReplayResult:
    return ReplayResult(
        outcome=ResolutionOutcome.EXPIRED,
        entered_at=None,
        resolved_at=resolved_at,
        realized_r=0.0,
        max_adverse_excursion_r=None,
        max_favorable_excursion_r=None,
        tp2_reached=False,
        candles_used=candles_used,
    )


def replay(
    *,
    proposal: SignalProposal,
    decided_at: datetime,
    candles: Sequence[Kline],
    now: datetime,
    entry_validity: timedelta = ENTRY_VALIDITY,
    max_hold: timedelta = MAX_HOLD,
) -> ReplayResult:
    """Replay ``proposal`` over ``candles`` and return the deterministic result.

    ``decided_at`` is the signal's creation time; ``now`` decides whether an
    unresolved window is *pending* (still open) or ``EXPIRED`` (closed).
    """
    is_long = proposal.direction is SignalDirection.LONG
    entry = proposal.entry_price
    stop = proposal.stop_loss
    tp1 = proposal.take_profit_1
    tp2 = proposal.take_profit_2
    unit_risk = abs(entry - stop)
    if unit_risk <= 0:  # pragma: no cover - SignalProposal geometry validator forbids this
        raise ValueError("proposal has zero risk distance")

    hold_end = decided_at + max_hold
    entry_deadline = decided_at + entry_validity
    window = sorted(
        (c for c in candles if decided_at <= c.open_time < hold_end),
        key=lambda c: c.open_time,
    )

    fill: float | None = None
    entered_at: datetime | None = None
    mae: float | None = None
    mfe: float | None = None
    tp2_hit = False
    used = 0

    def r_of(price: float) -> float:
        assert fill is not None
        return (price - fill) / unit_risk if is_long else (fill - price) / unit_risk

    for c in window:
        if fill is None:
            if c.open_time >= entry_deadline:
                return _expired_unentered(entry_deadline, used)
            used += 1
            touched = c.low <= entry <= c.high
            gap_through = (c.open < entry) if is_long else (c.open > entry)
            if not (touched or gap_through):
                continue
            fill = entry if touched else c.open
            entered_at = c.open_time
        else:
            used += 1

        adverse = r_of(c.low if is_long else c.high)
        favorable = r_of(c.high if is_long else c.low)
        mae = min(adverse, 0.0) if mae is None else min(mae, adverse)
        mfe = max(favorable, 0.0) if mfe is None else max(mfe, favorable)

        stop_hit = (c.low <= stop) if is_long else (c.high >= stop)
        if stop_hit:
            return ReplayResult(
                outcome=ResolutionOutcome.LOSS,
                entered_at=entered_at,
                resolved_at=c.open_time,
                realized_r=r_of(stop),
                max_adverse_excursion_r=mae,
                max_favorable_excursion_r=mfe,
                tp2_reached=tp2_hit,
                candles_used=used,
            )
        if tp2 is not None and ((c.high >= tp2) if is_long else (c.low <= tp2)):
            tp2_hit = True
        tp1_hit = (c.high >= tp1) if is_long else (c.low <= tp1)
        if tp1_hit:
            return ReplayResult(
                outcome=ResolutionOutcome.WIN,
                entered_at=entered_at,
                resolved_at=c.open_time,
                realized_r=r_of(tp1),
                max_adverse_excursion_r=mae,
                max_favorable_excursion_r=mfe,
                tp2_reached=tp2_hit,
                candles_used=used,
            )

    # Ran out of candles.
    if fill is None:
        if now >= entry_deadline:
            return _expired_unentered(entry_deadline, used)
        return ReplayResult(
            outcome=None,
            entered_at=None,
            resolved_at=None,
            realized_r=0.0,
            max_adverse_excursion_r=None,
            max_favorable_excursion_r=None,
            tp2_reached=False,
            candles_used=used,
        )

    if now >= hold_end:
        return ReplayResult(
            outcome=ResolutionOutcome.EXPIRED,
            entered_at=entered_at,
            resolved_at=hold_end,
            realized_r=r_of(window[-1].close),
            max_adverse_excursion_r=mae,
            max_favorable_excursion_r=mfe,
            tp2_reached=tp2_hit,
            candles_used=used,
        )
    return ReplayResult(
        outcome=None,
        entered_at=entered_at,
        resolved_at=None,
        realized_r=0.0,
        max_adverse_excursion_r=mae,
        max_favorable_excursion_r=mfe,
        tp2_reached=tp2_hit,
        candles_used=used,
    )
