"""Slice 4 Step 4.2 — pure overview assembly helpers (`src.dashboard.api.overview`).

No HTTP and no store here: every helper takes models in and returns API schemas out,
so the risk-budget numbers (design §5.3: via `risk_gates`, never re-implemented), the
window bucketing (`schedule.window_for`), the Forecaster staleness rule and the
per-role lifecycle trimming (design §3.4) are pinned against a fixed clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.agents.orchestration.risk_gates import (
    LOSS_PAUSE,
    MAX_CONCURRENT_SETUPS,
    MAX_SIGNALS_PER_24H,
    RiskContext,
)
from src.common.models import (
    ActiveSetupStatus,
    AgentRole,
    ScanStatus,
    SignalDirection,
    SignalStatus,
)
from src.common.schedule import AD_HOC
from src.dashboard.api.classify import DecisionKind
from src.dashboard.api.overview import (
    AD_HOC_GROUP_TOLERANCE,
    SWEEP_OFFSET,
    SWEEP_RUN_BUDGET,
    bucket_windows,
    build_risk_budget,
    count_stale,
    last_scan_outcome,
    last_sweep_at,
    lifecycle_step,
    setup_card_for_signal,
    setup_card_out,
    signal_detail_out,
    signal_row_out,
    trim_output,
    utcnow,
    window_outcome,
)
from src.dashboard.api.queries import JournalRow, SetupCard
from src.dashboard.api.schemas import SignalDetailOut, SignalRowOut, WindowOutcome
from src.persistence.models import StoredActiveSetup, StoredAgentRun, StoredScanRun, StoredSignal

DAY = date(2026, 10, 20)
NARRATIVE = "Synthetic fixture narrative for the overview helper tests."


def _at(hour: int, minute: int = 0, second: int = 0, *, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)


def _ctx(
    *,
    now: datetime,
    open_setups: int = 0,
    published: int = 0,
    losses: int = 0,
    latest_loss_at: datetime | None = None,
) -> RiskContext:
    return RiskContext(
        now=now,
        open_setup_count=open_setups,
        published_last_24h=published,
        consecutive_losses=losses,
        latest_loss_at=latest_loss_at,
    )


def _scan(
    started_at: datetime,
    *,
    symbol: str = "BTCUSDT",
    status: ScanStatus = ScanStatus.SUCCESS,
    error: str | None = None,
    session: str = "LONDON",
) -> StoredScanRun:
    return StoredScanRun(
        id=uuid4(),
        started_at=started_at,
        completed_at=None if status is ScanStatus.RUNNING else started_at + timedelta(seconds=40),
        status=status,
        error_message=error,
        session=session,
        strategy="smc",
        symbols=[symbol],
    )


def _proposal_payload(scan_id: UUID, symbol: str, direction: str) -> dict[str, Any]:
    long = direction == "LONG"
    return {
        "proposal_id": str(uuid4()),
        "scan_id": str(scan_id),
        "strategy": "smc",
        "created_at": _at(8, 3, 55).isoformat(),
        "symbol": symbol,
        "direction": direction,
        "entry_price": 100.0,
        "stop_loss": 98.0 if long else 102.0,
        "take_profit_1": 106.0 if long else 94.0,
        "take_profit_2": 110.0 if long else 90.0,
        "risk_reward_ratio": 3.0,
        "leverage": 3.0,
        "risk_percent": 1.0,
        "tags": ["fvg", "order_block"],
        "confluence_narrative": NARRATIVE,
        "features": {"rr": 3.0},
    }


def _skip_payload(scan_id: UUID, symbol: str, violated_rule: str | None) -> dict[str, Any]:
    return {
        "decision_id": str(uuid4()),
        "scan_id": str(scan_id),
        "strategy": "smc",
        "symbol": symbol,
        "reason": "INSUFFICIENT_RR" if violated_rule else "NO_CLEAR_BIAS",
        "details": "gate rejected the proposal" if violated_rule else "no valid structure",
        "violated_rule": violated_rule,
        "created_at": _at(8, 3, 55).isoformat(),
    }


def _signal(
    scan: StoredScanRun,
    *,
    symbol: str = "BTCUSDT",
    direction: SignalDirection | None = SignalDirection.LONG,
    violated_rule: str | None = None,
    created_at: datetime | None = None,
) -> StoredSignal:
    payload = (
        _proposal_payload(scan.id, symbol, direction.value)
        if direction is not None
        else _skip_payload(scan.id, symbol, violated_rule)
    )
    return StoredSignal(
        id=uuid4(),
        scan_id=scan.id,
        symbol=symbol,
        strategy="smc",
        direction=direction,
        status=SignalStatus.PUBLISHED if direction is not None else SignalStatus.SKIPPED,
        created_at=created_at or scan.started_at + timedelta(seconds=3),
        payload=payload,
        tags=list(payload.get("tags", [])),
        features=dict(payload.get("features", {})),
    )


def _row(
    signal: StoredSignal,
    *,
    kind: DecisionKind,
    ruling: str | None = None,
    has_setup: bool = False,
    violated_rule: str | None = None,
    resolution_outcome: str | None = None,
    realized_r: float | None = None,
) -> JournalRow:
    payload = signal.payload
    return JournalRow(
        id=signal.id,
        scan_id=signal.scan_id,
        symbol=signal.symbol,
        strategy=signal.strategy,
        direction=signal.direction,
        status=signal.status,
        created_at=signal.created_at,
        tags=list(signal.tags),
        outcome=signal.outcome,
        entry_price=payload.get("entry_price"),
        stop_loss=payload.get("stop_loss"),
        take_profit_1=payload.get("take_profit_1"),
        take_profit_2=payload.get("take_profit_2"),
        risk_reward_ratio=payload.get("risk_reward_ratio"),
        skip_reason=payload.get("reason"),
        skip_details=payload.get("details"),
        decision_kind=kind,
        judge_ruling=ruling,
        session="LONDON",
        has_setup=has_setup,
        violated_rule=violated_rule,
        resolution_outcome=resolution_outcome,
        realized_r=realized_r,
    )


def _card(
    *,
    opened_at: datetime,
    last_evaluated_at: datetime | None,
    latest_evaluation: dict[str, Any] | None = None,
) -> SetupCard:
    return SetupCard(
        setup_id=uuid4(),
        signal_id=uuid4(),
        symbol="ETHUSDT",
        direction=SignalDirection.SHORT,
        entry_price=100.0,
        stop_loss=102.0,
        take_profit_1=94.0,
        take_profit_2=None,
        opened_at=opened_at,
        last_evaluated_at=last_evaluated_at,
        latest_evaluation=latest_evaluation,
    )


# ---------------------------------------------------------------------------
# clock seam
# ---------------------------------------------------------------------------


def test_utcnow_is_timezone_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# build_risk_budget
# ---------------------------------------------------------------------------


def test_risk_budget_counts_caps_session_and_countdown() -> None:
    now = _at(8, 0)  # LONDON session, three minutes before the 08:03 scan
    budget = build_risk_budget(_ctx(now=now, open_setups=1, published=2), now=now)
    assert budget.open_setups == 1 and budget.open_cap == MAX_CONCURRENT_SETUPS == 3
    assert budget.published_24h == 2 and budget.daily_cap == MAX_SIGNALS_PER_24H == 5
    assert budget.loss_streak == 0
    assert budget.pause_active is False and budget.pause_until is None
    assert budget.session == "LONDON" and budget.session_blocked is False
    assert budget.next_scan_at == _at(8, 3)
    assert budget.seconds_to_next_scan == 180


def test_risk_budget_pause_active_until_loss_plus_24h() -> None:
    now = _at(10, 0)
    latest_loss = now - timedelta(hours=2)
    budget = build_risk_budget(_ctx(now=now, losses=3, latest_loss_at=latest_loss), now=now)
    assert budget.loss_streak == 3
    assert budget.pause_active is True
    assert budget.pause_until == latest_loss + LOSS_PAUSE


def test_risk_budget_pause_lifted_after_24h() -> None:
    now = _at(10, 0)
    latest_loss = now - timedelta(hours=30)
    budget = build_risk_budget(_ctx(now=now, losses=3, latest_loss_at=latest_loss), now=now)
    assert budget.loss_streak == 3
    assert budget.pause_active is False and budget.pause_until is None


def test_risk_budget_two_losses_is_not_a_pause() -> None:
    now = _at(10, 0)
    budget = build_risk_budget(
        _ctx(now=now, losses=2, latest_loss_at=now - timedelta(hours=1)), now=now
    )
    assert budget.pause_active is False and budget.pause_until is None


def test_risk_budget_session_blocked_at_daily_wrap() -> None:
    now = _at(22, 3)  # the DAILY_WRAP scan fires inside COOLDOWN (rule 7: review-only)
    budget = build_risk_budget(_ctx(now=now), now=now)
    assert budget.session == "COOLDOWN" and budget.session_blocked is True
    assert budget.next_scan_at == _at(8, 3, day=DAY + timedelta(days=1))
    assert budget.seconds_to_next_scan == 10 * 3600


def test_risk_budget_rejects_naive_now() -> None:
    naive = datetime(2026, 10, 20, 8, 0)  # the naive case is the point
    with pytest.raises(ValueError, match="timezone-aware"):
        build_risk_budget(_ctx(now=_at(8, 0)), now=naive)


# ---------------------------------------------------------------------------
# window_outcome / bucket_windows / last_scan_outcome
# ---------------------------------------------------------------------------


def test_window_outcome_aggregates_the_per_symbol_rows() -> None:
    btc = _scan(_at(8, 3, 52), symbol="BTCUSDT")
    eth = _scan(_at(8, 3, 53), symbol="ETHUSDT", status=ScanStatus.FAILED, error="boom")
    sol = _scan(_at(8, 3, 51), symbol="SOLUSDT", status=ScanStatus.RUNNING)
    outcome = window_outcome("LONDON", [btc, eth, sol])
    assert outcome == WindowOutcome(
        label="LONDON",
        started_at=_at(8, 3, 51),  # earliest row
        status=ScanStatus.FAILED,  # FAILED beats RUNNING beats SUCCESS
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],  # sorted union
        error="boom",
    )
    assert window_outcome("NY", [btc, sol]).status is ScanStatus.RUNNING
    assert window_outcome("NY", [btc]).status is ScanStatus.SUCCESS
    assert window_outcome("NY", [btc]).error is None


def test_window_outcome_requires_at_least_one_scan() -> None:
    with pytest.raises(ValueError, match="at least one"):
        window_outcome("LONDON", [])


def test_bucket_windows_places_the_london_scan_and_drops_the_manual_run() -> None:
    btc = _scan(_at(8, 3, 52), symbol="BTCUSDT")
    eth = _scan(_at(8, 3, 53), symbol="ETHUSDT", status=ScanStatus.FAILED, error="boom")
    manual = _scan(_at(10, 0), symbol="BTCUSDT", session="AD_HOC")
    published = _signal(btc, symbol="BTCUSDT")
    rejected = _signal(eth, symbol="ETHUSDT", direction=None, violated_rule="RULE_2_MIN_RR")
    manual_signal = _signal(manual, symbol="BTCUSDT", direction=None)
    rows = [
        _row(rejected, kind=DecisionKind.GATE_REJECTED, violated_rule="RULE_2_MIN_RR"),
        _row(published, kind=DecisionKind.PUBLISHED, ruling="PUBLISH", has_setup=True),
        _row(manual_signal, kind=DecisionKind.NO_SETUP),
    ]

    windows = bucket_windows([btc, eth, manual], rows, day=DAY)

    assert [w.label for w in windows] == ["LONDON", "NY", "OVERLAP", "DAILY_WRAP"]
    assert [w.fire_time for w in windows] == [_at(8, 3), _at(13, 3), _at(15, 3), _at(22, 3)]
    london = windows[0]
    assert london.scan is not None
    assert london.scan.label == "LONDON" and london.scan.status is ScanStatus.FAILED
    assert london.scan.started_at == _at(8, 3, 52)
    assert london.scan.symbols == ["BTCUSDT", "ETHUSDT"] and london.scan.error == "boom"
    # Chips are sorted by symbol and carry the journal's derived kind / rule / id.
    assert [(c.symbol, c.kind, c.violated_rule, c.signal_id) for c in london.symbols] == [
        ("BTCUSDT", DecisionKind.PUBLISHED, None, published.id),
        ("ETHUSDT", DecisionKind.GATE_REJECTED, "RULE_2_MIN_RR", rejected.id),
    ]
    # The 10:00 console run is AD_HOC: it belongs to no window, so nothing of it shows.
    for later in windows[1:]:
        assert later.scan is None and later.symbols == []
    assert all(c.signal_id != manual_signal.id for w in windows for c in w.symbols)


def test_bucket_windows_ignores_scans_from_other_days() -> None:
    yesterday = _scan(_at(8, 3, 52, day=DAY - timedelta(days=1)))
    tomorrow = _scan(_at(13, 3, 10, day=DAY + timedelta(days=1)))
    windows = bucket_windows([yesterday, tomorrow], [], day=DAY)
    assert all(w.scan is None and w.symbols == [] for w in windows)


def test_bucket_windows_empty_day() -> None:
    windows = bucket_windows([], [], day=DAY)
    assert len(windows) == 4 and all(w.scan is None for w in windows)


def test_last_scan_outcome_groups_the_latest_window() -> None:
    btc = _scan(_at(8, 3, 52), symbol="BTCUSDT")
    eth = _scan(_at(8, 3, 53), symbol="ETHUSDT", status=ScanStatus.FAILED, error="boom")
    assert last_scan_outcome([]) is None
    outcome = last_scan_outcome([eth, btc])
    assert outcome is not None
    assert outcome.label == "LONDON" and outcome.symbols == ["BTCUSDT", "ETHUSDT"]
    assert outcome.status is ScanStatus.FAILED and outcome.started_at == _at(8, 3, 52)


def test_last_scan_outcome_ad_hoc_groups_only_co_started_rows() -> None:
    london = _scan(_at(8, 3, 52), symbol="BTCUSDT")
    early_manual = _scan(_at(9, 0), symbol="BTCUSDT", session="AD_HOC")
    manual_btc = _scan(_at(10, 0, 0), symbol="BTCUSDT", session="AD_HOC")
    manual_eth = _scan(_at(10, 0, 3), symbol="ETHUSDT", session="AD_HOC", status=ScanStatus.RUNNING)
    outcome = last_scan_outcome([london, early_manual, manual_btc, manual_eth])
    assert outcome is not None
    assert outcome.label == AD_HOC
    assert outcome.symbols == ["BTCUSDT", "ETHUSDT"]  # the 09:00 run is a separate invocation
    assert outcome.started_at == _at(10, 0, 0) and outcome.status is ScanStatus.RUNNING
    assert timedelta(seconds=3) <= AD_HOC_GROUP_TOLERANCE < timedelta(hours=1)


def test_last_scan_outcome_spans_days() -> None:
    # Overnight (before today's first window) the badge must show YESTERDAY's wrap,
    # not "no scan": the latest row wins regardless of its UTC date.
    yesterday = DAY - timedelta(days=1)
    wrap_btc = _scan(_at(22, 3, 5, day=yesterday), symbol="BTCUSDT", session="COOLDOWN")
    wrap_eth = _scan(_at(22, 3, 6, day=yesterday), symbol="ETHUSDT", session="COOLDOWN")
    outcome = last_scan_outcome([wrap_eth, wrap_btc])
    assert outcome is not None
    assert outcome.label == "DAILY_WRAP" and outcome.started_at == _at(22, 3, 5, day=yesterday)
    assert outcome.symbols == ["BTCUSDT", "ETHUSDT"]
    # Once today's first window has run, it is the latest -- yesterday's rows do not
    # leak into its group.
    london = _scan(_at(8, 3, 52), symbol="SOLUSDT")
    today = last_scan_outcome([wrap_eth, wrap_btc, london])
    assert today is not None
    assert today.label == "LONDON" and today.symbols == ["SOLUSDT"]
    # A window label groups by the window *and* its day: yesterday's LONDON rows
    # are not part of today's LONDON outcome.
    old_london = _scan(_at(8, 3, 50, day=yesterday), symbol="BNBUSDT")
    again = last_scan_outcome([old_london, london])
    assert again is not None and again.symbols == ["SOLUSDT"]


# ---------------------------------------------------------------------------
# last_sweep_at / count_stale
# ---------------------------------------------------------------------------


YESTERDAY = DAY - timedelta(days=1)


def test_sweep_offset_matches_scheduling_stack() -> None:
    # SchedulingStack: scans at :03, the Forecaster sweep at :05 (`cron(5 8,13,15,22)`).
    assert timedelta(minutes=2) == SWEEP_OFFSET
    assert timedelta(minutes=3) == SWEEP_RUN_BUDGET


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_at(0, 30), _at(22, 5, day=YESTERDAY)),  # overnight: yesterday's wrap sweep
        (_at(7, 0), _at(22, 5, day=YESTERDAY)),  # before today's first window: not None
        (_at(8, 5), _at(22, 5, day=YESTERDAY)),  # today's first sweep just started
        (_at(8, 7, 59), _at(22, 5, day=YESTERDAY)),  # still inside its run budget
        (_at(8, 8), _at(8, 5)),  # start + SWEEP_RUN_BUDGET -> finished
        (_at(12, 0), _at(8, 5)),
        (_at(13, 6), _at(8, 5)),  # the 13:05 sweep has not finished yet
        (_at(13, 8), _at(13, 5)),
        (_at(15, 7), _at(13, 5)),
        (_at(23, 59), _at(22, 5)),
    ],
)
def test_last_sweep_at(now: datetime, expected: datetime) -> None:
    assert last_sweep_at(now) == expected


def test_last_sweep_at_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        last_sweep_at(datetime(2026, 10, 20, 9, 0))


def test_count_stale() -> None:
    sweep = _at(13, 5)
    fresh = _card(opened_at=_at(8, 4), last_evaluated_at=_at(13, 5, 40))  # this sweep
    old = _card(opened_at=_at(8, 4), last_evaluated_at=_at(8, 5, 30))  # previous sweep
    never = _card(opened_at=_at(8, 4), last_evaluated_at=None)
    exactly = _card(opened_at=_at(8, 4), last_evaluated_at=sweep)  # at the sweep start: fresh
    cards = [fresh, old, never, exactly]
    assert count_stale(cards, sweep_at=sweep) == 2
    assert count_stale(cards, sweep_at=None) == 1  # degenerate: only never-evaluated
    assert count_stale([], sweep_at=sweep) == 0


def test_count_stale_across_midnight() -> None:
    # 07:00: the reference is yesterday's 22:05 sweep; a 15:05-yesterday evaluation missed it.
    sweep = last_sweep_at(_at(7, 0))
    assert sweep == _at(22, 5, day=YESTERDAY)
    missed = _card(
        opened_at=_at(8, 4, day=YESTERDAY), last_evaluated_at=_at(15, 5, 20, day=YESTERDAY)
    )
    covered = _card(
        opened_at=_at(8, 4, day=YESTERDAY), last_evaluated_at=_at(22, 5, 20, day=YESTERDAY)
    )
    assert count_stale([missed, covered], sweep_at=sweep) == 1


# ---------------------------------------------------------------------------
# setup_card_out / setup_card_for_signal
# ---------------------------------------------------------------------------


def test_setup_card_out_ages_from_the_clock() -> None:
    now = _at(13, 10)
    card = _card(
        opened_at=now - timedelta(hours=1),
        last_evaluated_at=now - timedelta(minutes=2),
        latest_evaluation={"status": "AT_RISK", "reasoning": "near SL", "outcome": None},
    )
    out = setup_card_out(card, now=now, price=None)
    assert out.setup_id == card.setup_id and out.signal_id == card.signal_id
    assert (out.symbol, out.direction) == ("ETHUSDT", SignalDirection.SHORT)
    assert (out.entry_price, out.stop_loss, out.take_profit_1, out.take_profit_2) == (
        100.0,
        102.0,
        94.0,
        None,
    )
    assert out.opened_at == card.opened_at and out.age_seconds == 3600
    assert out.last_evaluated_at == card.last_evaluated_at and out.evaluation_age_seconds == 120
    assert out.latest_status == "AT_RISK" and out.latest_reasoning == "near SL"
    assert out.unrealised_r is None and out.unrealised_r_note == "price unavailable"


def test_setup_card_out_unrealised_r_from_price() -> None:
    now = _at(13, 10)
    card = _card(opened_at=now, last_evaluated_at=None)  # SHORT entry 100 / stop 102
    out = setup_card_out(card, now=now, price=97.0)
    assert out.unrealised_r == 1.5 and out.unrealised_r_note is None
    out = setup_card_out(card, now=now, price=101.0)
    assert out.unrealised_r == -0.5 and out.unrealised_r_note is None
    zero_risk = card.model_copy(update={"stop_loss": 100.0})
    out = setup_card_out(zero_risk, now=now, price=101.0)
    assert out.unrealised_r is None and out.unrealised_r_note == "risk undefined (entry == stop)"


def test_setup_card_out_without_evaluation() -> None:
    now = _at(13, 10)
    out = setup_card_out(_card(opened_at=now, last_evaluated_at=None), now=now)
    assert out.age_seconds == 0
    assert out.last_evaluated_at is None and out.evaluation_age_seconds is None
    assert out.latest_status is None and out.latest_reasoning is None


def test_setup_card_out_clamps_negative_ages() -> None:
    now = _at(13, 10)
    future = _card(
        opened_at=now + timedelta(seconds=5), last_evaluated_at=now + timedelta(seconds=5)
    )
    out = setup_card_out(future, now=now)
    assert out.age_seconds == 0 and out.evaluation_age_seconds == 0


def test_setup_card_for_signal_lifts_prices_from_the_payload() -> None:
    scan = _scan(_at(8, 3, 52))
    signal = _signal(scan, symbol="BTCUSDT", direction=SignalDirection.LONG)
    setup = StoredActiveSetup(
        id=uuid4(),
        signal_id=signal.id,
        opened_at=_at(8, 4),
        status=ActiveSetupStatus.OPEN,
        last_evaluated_at=_at(13, 4),
        latest_evaluation={"status": "STILL_VALID", "reasoning": "fine", "outcome": None},
    )
    card = setup_card_for_signal(setup, signal)
    assert card == SetupCard(
        setup_id=setup.id,
        signal_id=signal.id,
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        entry_price=100.0,
        stop_loss=98.0,
        take_profit_1=106.0,
        take_profit_2=110.0,
        opened_at=_at(8, 4),
        last_evaluated_at=_at(13, 4),
        latest_evaluation={"status": "STILL_VALID", "reasoning": "fine", "outcome": None},
    )


def test_setup_card_for_signal_is_none_for_a_skip_row(caplog: pytest.LogCaptureFixture) -> None:
    scan = _scan(_at(8, 3, 52))
    skip = _signal(scan, direction=None)
    setup = StoredActiveSetup(
        id=uuid4(), signal_id=skip.id, opened_at=_at(8, 4), status=ActiveSetupStatus.OPEN
    )
    with caplog.at_level("WARNING", logger="src.dashboard.api.overview"):
        assert setup_card_for_signal(setup, skip) is None
    # Never silent: the operator can find the malformed row from the log.
    (record,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert str(setup.id) in record.getMessage() and "skipped" in record.getMessage()


def test_setup_card_for_signal_logs_a_malformed_payload(caplog: pytest.LogCaptureFixture) -> None:
    scan = _scan(_at(8, 3, 52))
    signal = _signal(scan, direction=SignalDirection.LONG)
    broken = signal.model_copy(
        update={"payload": {k: v for k, v in signal.payload.items() if k != "stop_loss"}}
    )
    setup = StoredActiveSetup(
        id=uuid4(), signal_id=broken.id, opened_at=_at(8, 4), status=ActiveSetupStatus.OPEN
    )
    with caplog.at_level("WARNING", logger="src.dashboard.api.overview"):
        assert setup_card_for_signal(setup, broken) is None
    (record,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert str(setup.id) in record.getMessage()
    assert "malformed payload" in record.getMessage()


# ---------------------------------------------------------------------------
# signal_row_out
# ---------------------------------------------------------------------------


def test_signal_row_out_flattens_the_journal_row() -> None:
    scan = _scan(_at(8, 3, 52))
    signal = _signal(scan, symbol="BTCUSDT", direction=SignalDirection.LONG)
    row = _row(
        signal,
        kind=DecisionKind.PUBLISHED,
        ruling="PUBLISH_WITH_CAVEAT",
        has_setup=True,
        resolution_outcome="WIN",
        realized_r=2.5,
    )
    out = signal_row_out(row)
    assert isinstance(out, SignalRowOut)
    assert out.id == signal.id and out.scan_id == scan.id
    assert out.symbol == "BTCUSDT" and out.direction is SignalDirection.LONG
    assert out.status is SignalStatus.PUBLISHED and out.created_at == signal.created_at
    assert out.tags == ["fvg", "order_block"] and out.outcome is None
    assert (out.entry_price, out.stop_loss, out.take_profit_1, out.take_profit_2) == (
        100.0,
        98.0,
        106.0,
        110.0,
    )
    assert out.risk_reward_ratio == 3.0
    assert out.skip_reason is None and out.skip_details is None
    assert out.decision_kind is DecisionKind.PUBLISHED and out.judge_ruling == "PUBLISH_WITH_CAVEAT"
    assert out.session == "LONDON" and out.has_setup is True and out.violated_rule is None
    assert out.resolution_outcome == "WIN" and out.realized_r == 2.5
    dumped = out.model_dump(mode="json")
    assert dumped["decision_kind"] == "PUBLISHED" and dumped["direction"] == "LONG"
    assert dumped["status"] == "PUBLISHED" and dumped["id"] == str(signal.id)
    # The wire row is the trimmed contract: no full JSONB column (design §3.4).
    for big in ("payload", "features", "outcome_metadata"):
        assert big not in SignalRowOut.model_fields, big
    assert set(SignalRowOut.model_fields) == set(JournalRow.model_fields)


def test_signal_detail_out_adds_the_full_signal() -> None:
    scan = _scan(_at(8, 3, 52))
    signal = _signal(scan, symbol="BTCUSDT", direction=SignalDirection.LONG)
    row = _row(signal, kind=DecisionKind.PUBLISHED, ruling="PUBLISH", has_setup=True)
    out = signal_detail_out(row, signal)
    assert isinstance(out, SignalDetailOut) and isinstance(out, SignalRowOut)
    assert out.payload == signal.payload and out.payload["confluence_narrative"] == NARRATIVE
    assert out.features == {"rr": 3.0} and out.outcome_metadata is None
    # Every trimmed-row field is carried over unchanged.
    assert out.model_dump(exclude={"payload", "features", "outcome_metadata"}) == row.model_dump()
    assert set(SignalDetailOut.model_fields) == set(SignalRowOut.model_fields) | {
        "payload",
        "features",
        "outcome_metadata",
    }


# ---------------------------------------------------------------------------
# trim_output / lifecycle_step
# ---------------------------------------------------------------------------


def test_trim_output_analyzer_proposal() -> None:
    payload = _proposal_payload(uuid4(), "BTCUSDT", "LONG")
    assert trim_output(AgentRole.ANALYZER, payload) == {
        "direction": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit_1": 106.0,
        "take_profit_2": 110.0,
        "risk_reward_ratio": 3.0,
        "tags": ["fvg", "order_block"],
    }


def test_trim_output_analyzer_skip() -> None:
    payload = _skip_payload(uuid4(), "BTCUSDT", "RULE_2_MIN_RR")
    assert trim_output(AgentRole.ANALYZER, payload) == {
        "reason": "INSUFFICIENT_RR",
        "violated_rule": "RULE_2_MIN_RR",
        "details": "gate rejected the proposal",
    }


def test_trim_output_historian_trims_matches() -> None:
    match_id = uuid4()
    output = {
        "query_proposal_id": str(uuid4()),
        "strategy": "smc",
        "direction": "LONG",
        "sample_size": 2,
        "wins": 1,
        "losses": 1,
        "breakeven": 0,
        "inconclusive": 0,
        "win_rate": 0.5,
        "matches": [
            {
                "signal_id": str(match_id),
                "symbol": "ETHUSDT",
                "direction": "LONG",
                "created_at": "2026-10-01T08:03:55+00:00",
                "outcome": "WIN",
                "tags": ["fvg"],
                "tag_overlap": 1,
                "l2_distance": 0.2,
            },
            "not-a-match",  # tolerated: a malformed element is dropped, not a 500
        ],
        "summary": "two precedents",
    }
    assert trim_output(AgentRole.HISTORIAN, output) == {
        "win_rate": 0.5,
        "sample_size": 2,
        "wins": 1,
        "losses": 1,
        "matches": [{"signal_id": str(match_id), "symbol": "ETHUSDT", "outcome": "WIN"}],
    }


def test_trim_output_skeptic_objection() -> None:
    output = {
        "severity": "MEDIUM",
        "recommends_against": True,
        "headline": "USD strength",
        "reasoning": "a long reasoning string about the dollar index and risk appetite",
        "cited_macro": ["DXY 104.2"],
    }
    assert trim_output(AgentRole.SKEPTIC, output) == {
        "severity": "MEDIUM",
        "recommends_against": True,
        "headline": "USD strength",
    }


def test_trim_output_skeptic_no_macro_data() -> None:
    # `NoMacroData.model_dump()` is `{provider, reason}`: detected by the `provider` key
    # (the sentinel's own shape), not by the absence of `severity`.
    assert trim_output(
        AgentRole.SKEPTIC, {"provider": "skeptic", "reason": "no macro providers configured"}
    ) == {"no_macro_data": True}


def test_trim_output_skeptic_without_severity_is_not_no_macro_data() -> None:
    # An objection row written by an older model version may lack `severity`; that is
    # a thin objection (nulls), never a false "no macro data" badge.
    assert trim_output(AgentRole.SKEPTIC, {"headline": "USD strength", "reasoning": "..."}) == {
        "severity": None,
        "recommends_against": None,
        "headline": "USD strength",
    }
    assert trim_output(AgentRole.SKEPTIC, {}) == {
        "severity": None,
        "recommends_against": None,
        "headline": None,
    }


def test_trim_output_critic_is_the_decision_review_fields() -> None:
    # `DecisionReview` (Critic v0): the grading fields, minus nothing the timeline
    # needs -- `lesson` is bounded to 300 chars by the model, so it stays.
    output = {
        "smc_read_valid": True,
        "skeptic_vindicated": "NOT_APPLICABLE",
        "judge_correct": False,
        "gate_correct": None,
        "failure_category": "JUDGE_OVERRIDE",
        "lesson": "the veto was wrong",
        "confidence": "MEDIUM",
        "raw_prompt": "x" * 5000,  # anything else is dropped
    }
    assert trim_output(AgentRole.CRITIC, output) == {
        "smc_read_valid": True,
        "skeptic_vindicated": "NOT_APPLICABLE",
        "judge_correct": False,
        "gate_correct": None,
        "failure_category": "JUDGE_OVERRIDE",
        "lesson": "the veto was wrong",
        "confidence": "MEDIUM",
    }


def test_trim_output_judge() -> None:
    output = {
        "ruling": "PUBLISH_WITH_CAVEAT",
        "confidence": "MEDIUM",
        "reasoning": "weighed the proposal against the historian and the skeptic",
        "caveat": "USD strength",
    }
    assert trim_output(AgentRole.JUDGE, output) == {
        "ruling": "PUBLISH_WITH_CAVEAT",
        "confidence": "MEDIUM",
        "caveat": "USD strength",
    }


def test_trim_output_missing_keys_become_null_not_errors() -> None:
    assert trim_output(AgentRole.JUDGE, {}) == {"ruling": None, "confidence": None, "caveat": None}
    assert trim_output(AgentRole.HISTORIAN, {"wins": 1}) == {
        "win_rate": None,
        "sample_size": None,
        "wins": 1,
        "losses": None,
        "matches": [],
    }


def test_trim_output_unknown_roles_are_empty() -> None:
    # The Forecaster writes `setup_evaluations`, not a lifecycle step; any role without a
    # dedicated trim (only FORECASTER today) summarises as `{}`.
    assert trim_output(AgentRole.FORECASTER, {"anything": 1}) == {}
    trimmed_roles = {r for r in AgentRole if trim_output(r, {"anything": 1}) != {}}
    assert trimmed_roles == {
        AgentRole.ANALYZER,
        AgentRole.HISTORIAN,
        AgentRole.SKEPTIC,
        AgentRole.JUDGE,
        AgentRole.CRITIC,
    }


def test_lifecycle_step_carries_accounting_and_trimmed_summary() -> None:
    run = StoredAgentRun(
        id=uuid4(),
        scan_id=uuid4(),
        agent_role=AgentRole.JUDGE,
        strategy="smc",
        input_hash="abc",
        output={"ruling": "SKIP", "confidence": "HIGH", "reasoning": "x" * 20, "caveat": None},
        latency_ms=1234,
        token_usage={"input_tokens": 10},
        cost_usd=0.01,
        created_at=_at(8, 4),
    )
    step = lifecycle_step(run)
    assert step.role == "judge" and step.run_id == run.id and step.created_at == _at(8, 4)
    assert step.latency_ms == 1234 and step.cost_usd == 0.01
    assert step.summary == {"ruling": "SKIP", "confidence": "HIGH", "caveat": None}
