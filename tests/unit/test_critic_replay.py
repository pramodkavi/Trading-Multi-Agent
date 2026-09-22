"""Exhaustive tests for the deterministic price-path replay (design §5.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.agents.critic.models import ResolutionOutcome
from src.agents.critic.replay import (
    CANDLE_STEP,
    ENGINE_VERSION,
    ENTRY_VALIDITY,
    MAX_HOLD,
    ReplayResult,
    replay,
)
from src.common.models import SignalDirection, SignalProposal
from src.providers.base import Kline

T0 = datetime(2026, 7, 1, 8, 3, tzinfo=UTC)
STEP = timedelta(minutes=5)


def _proposal(direction: SignalDirection = SignalDirection.LONG) -> SignalProposal:
    if direction is SignalDirection.LONG:
        entry, stop, tp1, tp2 = 100.0, 98.0, 106.0, 110.0
    else:
        entry, stop, tp1, tp2 = 100.0, 102.0, 94.0, 90.0
    return SignalProposal(
        scan_id=uuid4(),
        strategy="smc",
        symbol="BTCUSDT",
        direction=direction,
        entry_price=entry,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        risk_reward_ratio=3.0,
        leverage=3.0,
        risk_percent=1.0,
        tags=["fvg"],
        confluence_narrative="Synthetic fixture for the replay engine unit tests.",
        features={},
    )


def _c(i: int, o: float, h: float, lo: float, c: float) -> Kline:
    return Kline(open_time=T0 + i * STEP, open=o, high=h, low=lo, close=c, volume=1.0)


def _flat(n: int, price: float = 101.0, start: int = 0) -> list[Kline]:
    return [_c(start + i, price, price + 0.1, price - 0.1, price) for i in range(n)]


NOW_LATE = T0 + MAX_HOLD + timedelta(hours=1)
NOW_EARLY = T0 + timedelta(hours=1)


# --- constants -------------------------------------------------------------


def test_engine_version_constant() -> None:
    assert ENGINE_VERSION == "replay-v1"
    assert isinstance(ReplayResult.__dataclass_fields__, dict)


def test_default_windows_match_design() -> None:
    assert timedelta(hours=72) == ENTRY_VALIDITY
    assert timedelta(days=7) == MAX_HOLD
    assert CANDLE_STEP == STEP


def test_replay_result_is_frozen() -> None:
    res = replay(proposal=_proposal(), decided_at=T0, candles=[], now=NOW_EARLY)
    with pytest.raises(AttributeError):
        res.candles_used = 99  # type: ignore[misc]


# --- WIN / LOSS ordering -----------------------------------------------------


def test_long_target_first_is_win_with_r_from_geometry() -> None:
    candles = [_c(0, 101, 101.5, 100.0, 100.5), _c(1, 100.5, 106.2, 100.2, 105.0)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.WIN
    assert res.entered_at == T0
    assert res.resolved_at == T0 + STEP
    assert res.realized_r == pytest.approx(3.0)
    assert res.tp2_reached is False
    assert res.candles_used == 2


def test_long_stop_first_is_loss() -> None:
    candles = [_c(0, 101, 101.5, 100.0, 100.5), _c(1, 100.5, 100.8, 97.9, 98.5)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.LOSS
    assert res.realized_r == pytest.approx(-1.0)
    assert res.resolved_at == T0 + STEP


def test_both_in_one_candle_is_loss_and_tp2_not_counted() -> None:
    candles = [_c(0, 100.0, 111.0, 97.0, 105.0)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.LOSS
    assert res.tp2_reached is False
    assert res.realized_r == pytest.approx(-1.0)


def test_stop_on_entry_candle_is_loss() -> None:
    # Entry and stop touched on the very same candle -> LOSS (stop is checked first).
    candles = [_c(0, 100.5, 100.8, 97.5, 98.0)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.LOSS
    assert res.entered_at == T0
    assert res.resolved_at == T0


def test_short_symmetry_target_first() -> None:
    candles = [_c(0, 99.5, 100.2, 99.0, 99.2), _c(1, 99.2, 99.5, 93.8, 94.5)]
    res = replay(
        proposal=_proposal(SignalDirection.SHORT), decided_at=T0, candles=candles, now=NOW_EARLY
    )
    assert res.outcome is ResolutionOutcome.WIN
    assert res.realized_r == pytest.approx(3.0)


def test_short_stop_first_is_loss() -> None:
    candles = [_c(0, 99.5, 100.2, 99.0, 99.2), _c(1, 99.2, 102.1, 99.0, 101.0)]
    res = replay(
        proposal=_proposal(SignalDirection.SHORT), decided_at=T0, candles=candles, now=NOW_EARLY
    )
    assert res.outcome is ResolutionOutcome.LOSS
    assert res.realized_r == pytest.approx(-1.0)


def test_short_both_in_one_candle_is_loss() -> None:
    candles = [_c(0, 100.0, 103.0, 89.0, 95.0)]
    res = replay(
        proposal=_proposal(SignalDirection.SHORT), decided_at=T0, candles=candles, now=NOW_EARLY
    )
    assert res.outcome is ResolutionOutcome.LOSS
    assert res.tp2_reached is False


# --- entry / expiry ---------------------------------------------------------------


def test_entry_never_touched_expires_after_validity() -> None:
    n = int(ENTRY_VALIDITY / STEP) + 2
    res = replay(proposal=_proposal(), decided_at=T0, candles=_flat(n, 101.0), now=NOW_LATE)
    assert res.outcome is ResolutionOutcome.EXPIRED
    assert res.entered_at is None
    assert res.realized_r == 0.0
    assert res.resolved_at == T0 + ENTRY_VALIDITY
    assert res.max_adverse_excursion_r is None
    assert res.max_favorable_excursion_r is None
    assert res.tp2_reached is False
    # Only candles strictly inside the validity window were consumed.
    assert res.candles_used == int(ENTRY_VALIDITY / STEP)


def test_entry_never_touched_and_window_open_is_pending() -> None:
    res = replay(proposal=_proposal(), decided_at=T0, candles=_flat(3, 101.0), now=NOW_EARLY)
    assert res.outcome is None
    assert res.entered_at is None
    assert res.resolved_at is None
    assert res.realized_r == 0.0
    assert res.candles_used == 3


def test_entry_never_touched_no_candles_after_validity_but_now_past_it_expires() -> None:
    res = replay(
        proposal=_proposal(),
        decided_at=T0,
        candles=_flat(3, 101.0),
        now=T0 + ENTRY_VALIDITY + timedelta(minutes=1),
    )
    assert res.outcome is ResolutionOutcome.EXPIRED
    assert res.resolved_at == T0 + ENTRY_VALIDITY


def test_entry_touched_on_last_candle_before_validity_deadline_enters() -> None:
    last_idx = int(ENTRY_VALIDITY / STEP) - 1
    candles = [*_flat(last_idx, 101.0), _c(last_idx, 100.5, 100.8, 99.9, 100.2)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is None
    assert res.entered_at == T0 + last_idx * STEP


def test_entry_touched_exactly_at_validity_deadline_is_expired() -> None:
    idx = int(ENTRY_VALIDITY / STEP)
    candles = [*_flat(idx, 101.0), _c(idx, 100.5, 100.8, 99.9, 100.2)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.EXPIRED
    assert res.entered_at is None


def test_custom_entry_validity_is_honoured() -> None:
    candles = _flat(12, 101.0)  # one hour of candles, none touching entry
    res = replay(
        proposal=_proposal(),
        decided_at=T0,
        candles=candles,
        now=NOW_EARLY,
        entry_validity=timedelta(minutes=30),
    )
    assert res.outcome is ResolutionOutcome.EXPIRED
    assert res.resolved_at == T0 + timedelta(minutes=30)
    assert res.candles_used == 6


def test_gap_through_entry_fills_at_open() -> None:
    # Long: first candle opens below entry (favourable gap) and never trades back to 100.
    candles = [_c(0, 99.0, 99.5, 98.5, 99.2), _c(1, 99.2, 106.5, 99.0, 106.0)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.WIN
    assert res.entered_at == T0
    # fill 99.0, target 106, unit risk 2 -> (106-99)/2 = 3.5
    assert res.realized_r == pytest.approx(3.5)


def test_short_gap_through_entry_fills_at_open() -> None:
    # Short: first candle opens above entry (favourable gap) and never trades back to 100.
    candles = [_c(0, 101.0, 101.5, 100.5, 100.8), _c(1, 100.8, 101.0, 93.5, 94.0)]
    res = replay(
        proposal=_proposal(SignalDirection.SHORT), decided_at=T0, candles=candles, now=NOW_EARLY
    )
    assert res.outcome is ResolutionOutcome.WIN
    assert res.entered_at == T0
    # fill 101.0, target 94, unit risk 2 -> (101-94)/2 = 3.5
    assert res.realized_r == pytest.approx(3.5)


def test_unfavourable_gap_is_not_an_entry() -> None:
    # Long: price opens above entry and stays there -> no fill, still pending.
    candles = [_c(0, 103.0, 103.5, 102.5, 103.0), _c(1, 103.0, 107.0, 102.8, 106.5)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is None
    assert res.entered_at is None


# --- TP2 / excursions -------------------------------------------------------------


def test_tp2_reached_flag() -> None:
    candles = [_c(0, 100.0, 100.5, 99.9, 100.2), _c(1, 100.2, 110.5, 100.1, 109.0)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.WIN
    assert res.tp2_reached is True


def test_tp2_none_never_flags() -> None:
    proposal = _proposal().model_copy(update={"take_profit_2": None})
    candles = [_c(0, 100.0, 100.5, 99.9, 100.2), _c(1, 100.2, 150.0, 100.1, 140.0)]
    res = replay(proposal=proposal, decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.WIN
    assert res.tp2_reached is False


def test_mae_mfe_tracked_after_entry() -> None:
    candles = [
        _c(0, 100.0, 100.5, 99.9, 100.2),  # entry
        _c(1, 100.2, 102.0, 99.0, 101.0),  # MAE -0.5R, MFE +1R
        _c(2, 101.0, 106.1, 100.5, 105.0),  # win
    ]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.max_adverse_excursion_r == pytest.approx(-0.5)
    assert res.max_favorable_excursion_r == pytest.approx(3.05)


def test_mae_mfe_are_clamped_to_their_sign() -> None:
    # Gap-through long filled at 99.0; the entry candle never trades below the fill
    # and never above 99.5, so MAE must be 0 (not positive) and MFE positive.
    candles = [_c(0, 99.0, 99.5, 99.0, 99.2)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is None
    assert res.max_adverse_excursion_r == pytest.approx(0.0)
    assert res.max_favorable_excursion_r == pytest.approx(0.25)


def test_short_mae_mfe_signs() -> None:
    candles = [
        _c(0, 100.0, 100.4, 99.6, 100.0),  # entry at 100
        _c(1, 100.0, 101.0, 97.0, 98.0),  # MAE -0.5R (high 101), MFE +1.5R (low 97)
    ]
    res = replay(
        proposal=_proposal(SignalDirection.SHORT), decided_at=T0, candles=candles, now=NOW_EARLY
    )
    assert res.outcome is None
    assert res.max_adverse_excursion_r == pytest.approx(-0.5)
    assert res.max_favorable_excursion_r == pytest.approx(1.5)


# --- max-hold expiry -----------------------------------------------------------------


def test_entered_but_nothing_hit_expires_at_max_hold_with_mark_to_market() -> None:
    n = int(MAX_HOLD / STEP)
    candles = [_c(0, 100.0, 100.5, 99.9, 100.2), *_flat(n, 101.0, start=1)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_LATE)
    assert res.outcome is ResolutionOutcome.EXPIRED
    assert res.entered_at == T0
    assert res.resolved_at == T0 + MAX_HOLD
    assert res.realized_r == pytest.approx(0.5)  # (101-100)/2
    # The candle whose open_time == decided_at + MAX_HOLD is outside the window.
    assert res.candles_used == n


def test_entered_short_expires_with_negative_mark_to_market() -> None:
    candles = [_c(0, 100.0, 100.4, 99.6, 100.0), *_flat(5, 101.0, start=1)]
    res = replay(
        proposal=_proposal(SignalDirection.SHORT), decided_at=T0, candles=candles, now=NOW_LATE
    )
    assert res.outcome is ResolutionOutcome.EXPIRED
    assert res.realized_r == pytest.approx(-0.5)  # (100-101)/2


def test_entered_and_window_open_is_pending() -> None:
    candles = [_c(0, 100.0, 100.5, 99.9, 100.2), *_flat(5, 101.0, start=1)]
    res = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert res.outcome is None
    assert res.entered_at == T0
    assert res.resolved_at is None
    assert res.candles_used == 6


def test_custom_max_hold_is_honoured() -> None:
    hold = timedelta(hours=1)
    candles = [_c(0, 100.0, 100.5, 99.9, 100.2), *_flat(20, 101.0, start=1)]
    res = replay(
        proposal=_proposal(),
        decided_at=T0,
        candles=candles,
        now=T0 + hold,
        max_hold=hold,
    )
    assert res.outcome is ResolutionOutcome.EXPIRED
    assert res.resolved_at == T0 + hold
    assert res.candles_used == 12  # 60 min / 5 min


# --- candle hygiene ----------------------------------------------------------------


def test_candles_before_decided_at_are_ignored() -> None:
    early = Kline(open_time=T0 - STEP, open=100, high=107, low=97, close=100, volume=1)
    res = replay(proposal=_proposal(), decided_at=T0, candles=[early], now=NOW_EARLY)
    assert res.outcome is None
    assert res.candles_used == 0


def test_candles_at_or_after_hold_end_are_ignored() -> None:
    late = Kline(open_time=T0 + MAX_HOLD, open=100, high=107, low=97, close=100, volume=1)
    res = replay(proposal=_proposal(), decided_at=T0, candles=[late], now=NOW_LATE)
    assert res.outcome is ResolutionOutcome.EXPIRED
    assert res.entered_at is None
    assert res.candles_used == 0


def test_unsorted_candles_are_sorted() -> None:
    c1 = _c(1, 100.5, 106.2, 100.2, 105.0)
    c0 = _c(0, 101, 101.5, 100.0, 100.5)
    res = replay(proposal=_proposal(), decided_at=T0, candles=[c1, c0], now=NOW_EARLY)
    assert res.outcome is ResolutionOutcome.WIN
    assert res.entered_at == T0


def test_input_sequence_is_not_mutated() -> None:
    c1 = _c(1, 100.5, 106.2, 100.2, 105.0)
    c0 = _c(0, 101, 101.5, 100.0, 100.5)
    candles = [c1, c0]
    replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert candles == [c1, c0]


def test_empty_candles_window_closed_expires() -> None:
    res = replay(proposal=_proposal(), decided_at=T0, candles=[], now=NOW_LATE)
    assert res.outcome is ResolutionOutcome.EXPIRED
    assert res.candles_used == 0
    assert res.realized_r == 0.0


def test_empty_candles_window_open_is_pending() -> None:
    res = replay(proposal=_proposal(), decided_at=T0, candles=[], now=NOW_EARLY)
    assert res.outcome is None
    assert res.candles_used == 0


def test_replay_is_deterministic() -> None:
    candles = [
        _c(0, 100.0, 100.5, 99.9, 100.2),
        _c(1, 100.2, 102.0, 99.0, 101.0),
        _c(2, 101.0, 106.1, 100.5, 105.0),
    ]
    first = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    second = replay(proposal=_proposal(), decided_at=T0, candles=candles, now=NOW_EARLY)
    assert first == second
