"""Unit tests for the Slice 2 exit gate (design §6.4, plan Task 10).

The gate is a pure function over persistence read models, so every criterion
is exercised with hand-built ``StoredScanRun`` / ``DecisionSummary`` /
``StoredActiveSetup`` rows -- no store, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from src.agents.critic.exit_gate import evaluate_exit_gate
from src.common.models import ActiveSetupStatus, ScanStatus, SignalDirection, SignalStatus
from src.persistence.models import DecisionSummary, StoredActiveSetup, StoredScanRun

D0 = datetime(2026, 7, 1, 8, 3, tzinfo=UTC)


def _run(started_at: datetime, status: ScanStatus = ScanStatus.SUCCESS) -> StoredScanRun:
    return StoredScanRun(id=uuid4(), started_at=started_at, completed_at=None, status=status)


def _runs(
    days: int,
    per_day: int = 16,
    failed_on: set[int] | None = None,
) -> list[StoredScanRun]:
    """``per_day`` scan rows per UTC day for ``days`` days.

    Day indices listed in ``failed_on`` get their first row flipped to FAILED.
    """
    failed_days = failed_on or set()
    out: list[StoredScanRun] = []
    for d in range(days):
        for i in range(per_day):
            status = ScanStatus.FAILED if d in failed_days and i == 0 else ScanStatus.SUCCESS
            out.append(_run(D0 + timedelta(days=d, minutes=i), status))
    return out


def _decision(direction: SignalDirection | None, violated: str | None = None) -> DecisionSummary:
    return DecisionSummary(
        id=uuid4(),
        scan_id=uuid4(),
        symbol="BTCUSDT",
        direction=direction,
        status=SignalStatus.PUBLISHED if direction else SignalStatus.SKIPPED,
        created_at=D0,
        violated_rule=violated,
        outcome=None,
        decision_kind=None,
        resolution_outcome=None,
        realized_r=None,
        entered_at=None,
        review=None,
        review_error=None,
    )


def _setup(
    signal_id: UUID,
    evaluation: dict[str, Any] | None = None,
    evaluated: bool = True,
) -> StoredActiveSetup:
    return StoredActiveSetup(
        id=uuid4(),
        signal_id=signal_id,
        opened_at=D0,
        status=ActiveSetupStatus.OPEN,
        last_evaluated_at=D0 if evaluated else None,
        latest_evaluation=evaluation,
    )


# ---------------------------------------------------------------------------
# Whole report
# ---------------------------------------------------------------------------


def test_all_four_pass() -> None:
    published = [_decision(SignalDirection.LONG) for _ in range(5)]
    vetoed = [_decision(SignalDirection.SHORT) for _ in range(2)]
    skips = [_decision(None) for _ in range(3)] + [_decision(None, "RULE_2_MIN_RR")]
    setups = [_setup(p.id, {"status": "STILL_VALID", "reasoning": "ok"}) for p in published]
    rep = evaluate_exit_gate(
        scan_runs=_runs(8), decisions=published + vetoed + skips, setups=setups
    )
    assert rep.all_passed
    assert [c.number for c in rep.criteria] == [1, 2, 3, 4]
    assert "published=5 skipped=6" in rep.criteria[1].evidence
    assert "judge vetoes=2" in rep.criteria[1].evidence
    assert "analyzer/gate skips=4" in rep.criteria[1].evidence


def test_empty_inputs_fail_everything_without_error() -> None:
    rep = evaluate_exit_gate(scan_runs=[], decisions=[], setups=[])
    assert rep.all_passed is False and len(rep.criteria) == 4
    assert all(c.passed is False for c in rep.criteria)
    assert "longest healthy streak: 0 days; failed runs: 0" in rep.criteria[0].evidence
    assert "rules: none" in rep.criteria[3].evidence


# ---------------------------------------------------------------------------
# Criterion 1 -- 7 consecutive autonomous days
# ---------------------------------------------------------------------------


def test_streak_passes_at_exactly_seven_days_with_date_span() -> None:
    rep = evaluate_exit_gate(scan_runs=_runs(7), decisions=[], setups=[])
    c1 = rep.criteria[0]
    assert c1.passed is True
    assert "longest healthy streak: 7 days (2026-07-01..2026-07-07)" in c1.evidence
    assert "failed runs: 0" in c1.evidence


def test_streak_broken_by_failed_day() -> None:
    rep = evaluate_exit_gate(scan_runs=_runs(10, failed_on={4}), decisions=[], setups=[])
    assert rep.criteria[0].passed is False
    # Day indices 5..9 form the longest healthy run once day 4 has a FAILED row.
    assert "streak: 5 days (2026-07-06..2026-07-10)" in rep.criteria[0].evidence
    assert "failed runs: 1" in rep.criteria[0].evidence


def test_streak_needs_full_cadence() -> None:
    rep = evaluate_exit_gate(scan_runs=_runs(8, per_day=12), decisions=[], setups=[])
    assert rep.criteria[0].passed is False
    assert "streak: 0 days" in rep.criteria[0].evidence


def test_streak_honours_expected_scans_per_day_override() -> None:
    rep = evaluate_exit_gate(
        scan_runs=_runs(8, per_day=12), decisions=[], setups=[], expected_scans_per_day=12
    )
    assert rep.criteria[0].passed is True


def test_streak_ignores_running_rows_and_gap_days() -> None:
    # 3 healthy days, one day of only RUNNING rows, then 4 healthy days -> longest run is 4.
    runs = _runs(3)
    runs += [_run(D0 + timedelta(days=3, minutes=i), ScanStatus.RUNNING) for i in range(16)]
    runs += [_run(D0 + timedelta(days=4 + d, minutes=i)) for d in range(4) for i in range(16)]
    rep = evaluate_exit_gate(scan_runs=runs, decisions=[], setups=[])
    assert rep.criteria[0].passed is False
    assert "streak: 4 days (2026-07-05..2026-07-08)" in rep.criteria[0].evidence
    # RUNNING rows are neither healthy nor failed.
    assert "failed runs: 0" in rep.criteria[0].evidence


def test_streak_groups_by_utc_date_not_wall_clock() -> None:
    # 02:00 at UTC+5 is 21:00 UTC on the *previous* day; bucketing must use UTC.
    plus_five = timezone(timedelta(hours=5))
    runs = [
        _run(datetime(2026, 7, 2 + d, 2, i, tzinfo=plus_five)) for d in range(7) for i in range(16)
    ]
    rep = evaluate_exit_gate(scan_runs=runs, decisions=[], setups=[])
    assert rep.criteria[0].passed is True
    assert "(2026-07-01..2026-07-07)" in rep.criteria[0].evidence


# ---------------------------------------------------------------------------
# Criterion 2 -- >=5 published and >=5 skipped
# ---------------------------------------------------------------------------


def test_published_requires_setup_row_and_vetoes_count_as_skips() -> None:
    published = [_decision(SignalDirection.LONG) for _ in range(5)]
    vetoed = [_decision(SignalDirection.LONG) for _ in range(5)]
    setups = [_setup(p.id) for p in published]
    rep = evaluate_exit_gate(scan_runs=[], decisions=published + vetoed, setups=setups)
    c2 = rep.criteria[1]
    assert c2.passed is True
    assert c2.evidence == "published=5 skipped=5 (analyzer/gate skips=0, judge vetoes=5)"


def test_published_below_threshold_fails() -> None:
    published = [_decision(SignalDirection.LONG) for _ in range(4)]
    skips = [_decision(None) for _ in range(6)]
    setups = [_setup(p.id) for p in published]
    rep = evaluate_exit_gate(scan_runs=[], decisions=published + skips, setups=setups)
    assert rep.criteria[1].passed is False
    assert "published=4 skipped=6" in rep.criteria[1].evidence


def test_skipped_below_threshold_fails() -> None:
    published = [_decision(SignalDirection.LONG) for _ in range(6)]
    skips = [_decision(None) for _ in range(4)]
    setups = [_setup(p.id) for p in published]
    rep = evaluate_exit_gate(scan_runs=[], decisions=published + skips, setups=setups)
    assert rep.criteria[1].passed is False
    assert "published=6 skipped=4" in rep.criteria[1].evidence


# ---------------------------------------------------------------------------
# Criterion 3 -- Forecaster updated >=1 setup
# ---------------------------------------------------------------------------


def test_forecaster_update_counts_when_status_key_present() -> None:
    p = _decision(SignalDirection.LONG)
    rep = evaluate_exit_gate(
        scan_runs=[], decisions=[p], setups=[_setup(p.id, {"status": "AT_RISK", "reasoning": "x"})]
    )
    assert rep.criteria[2].passed is True
    assert rep.criteria[2].evidence == "forecaster-evaluated setups=1"


def test_resolver_evaluation_does_not_count_as_forecaster_update() -> None:
    p = _decision(SignalDirection.LONG)
    rep = evaluate_exit_gate(
        scan_runs=[], decisions=[p], setups=[_setup(p.id, {"source": "resolver", "outcome": "WIN"})]
    )
    assert rep.criteria[2].passed is False


def test_resolver_evaluation_with_status_key_still_excluded() -> None:
    p = _decision(SignalDirection.LONG)
    rep = evaluate_exit_gate(
        scan_runs=[],
        decisions=[p],
        setups=[_setup(p.id, {"source": "resolver", "status": "WIN"})],
    )
    assert rep.criteria[2].passed is False
    assert rep.criteria[2].evidence == "forecaster-evaluated setups=0"


def test_unevaluated_setup_does_not_count() -> None:
    p = _decision(SignalDirection.LONG)
    rep = evaluate_exit_gate(
        scan_runs=[],
        decisions=[p],
        setups=[
            _setup(p.id, None, evaluated=True),  # timestamp but no payload
            _setup(p.id, {"status": "STILL_VALID"}, evaluated=False),  # payload but no timestamp
        ],
    )
    assert rep.criteria[2].passed is False


# ---------------------------------------------------------------------------
# Criterion 4 -- risk gate prevented >=1 rule-violating signal
# ---------------------------------------------------------------------------


def test_gate_criterion_lists_rules() -> None:
    rep = evaluate_exit_gate(
        scan_runs=[],
        decisions=[
            _decision(None, "RULE_2_MIN_RR"),
            _decision(None, "RULE_2_MIN_RR"),
            _decision(None, "RULE_8_MAX_LEVERAGE"),
        ],
        setups=[],
    )
    c4 = rep.criteria[3]
    assert c4.passed and "RULE_2_MIN_RR x2" in c4.evidence
    assert "RULE_8_MAX_LEVERAGE x1" in c4.evidence
    assert "gate rejections=3" in c4.evidence
    # Most common rule is listed first.
    assert c4.evidence.index("RULE_2_MIN_RR x2") < c4.evidence.index("RULE_8_MAX_LEVERAGE x1")
    assert "CloudWatch cross-check unavailable" in c4.evidence


def test_gate_criterion_ignores_empty_string_rule() -> None:
    rep = evaluate_exit_gate(scan_runs=[], decisions=[_decision(None, "")], setups=[])
    assert rep.criteria[3].passed is False
    assert "gate rejections=0; rules: none" in rep.criteria[3].evidence
