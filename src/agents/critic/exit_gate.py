"""SPEC §5 'End of Slice 2' criteria over journal rows (design §6.4). Pure.

``evaluate_exit_gate`` turns three read-side projections -- ``scan_runs``,
``signals`` (as ``DecisionSummary``) and ``active_setups`` -- into the four
PASS / FAIL criteria the first Critic report renders. Nothing here touches a
store, the network or the clock; callers fetch the rows and pass them in.

Criteria (corrected in the ``docs/step-2.14-validation-plan.md`` review):

1. **7 consecutive days autonomous** -- a UTC day is *healthy* when it has at
   least ``expected_scans_per_day`` ``SUCCESS`` scan rows and zero ``FAILED``
   rows. Passes when the longest run of consecutive healthy dates is >= 7.
   ``RUNNING`` rows count neither way.
2. **>=5 published and >=5 skipped** -- ``signals.status`` is ``PUBLISHED`` for
   every proposal payload including Judge vetoes, so *published* means "has an
   ``active_setups`` row". Skipped = analyzer / gate skips (no direction) plus
   Judge vetoes (a direction but no setup row).
3. **Forecaster updated >=1 setup** -- a setup counts when it has a
   ``last_evaluated_at`` and its ``latest_evaluation`` carries the Forecaster's
   ``"status"`` key. The OutcomeResolver also writes ``latest_evaluation``
   (tagged ``"source": "resolver"``); those must not count.
4. **Risk gate prevented >=1 rule-violating signal** -- decisions whose
   ``violated_rule`` is set. The CloudWatch cross-check the validation plan
   asked for is not possible today because the gates do not log rule ids; the
   evidence string records that as a standing finding.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, date, timedelta
from uuid import UUID

from src.agents.critic.models import ExitGateReport, GateCriterion
from src.common.models import ScanStatus
from src.persistence.models import DecisionSummary, StoredActiveSetup, StoredScanRun

__all__ = ["DEFAULT_EXPECTED_SCANS_PER_DAY", "evaluate_exit_gate"]

DEFAULT_EXPECTED_SCANS_PER_DAY = 16
"""4 scheduled scans/day x 4 watchlist symbols, one ``scan_runs`` row each."""

_REQUIRED_STREAK_DAYS = 7
_MIN_PUBLISHED = 5
_MIN_SKIPPED = 5
_RESOLVER_SOURCE = "resolver"
_GATE_LOG_NOTE = "CloudWatch cross-check unavailable: risk gates do not log rule ids (finding)."


def _longest_streak(healthy: set[date]) -> tuple[int, date | None, date | None]:
    """Longest run of consecutive dates in ``healthy`` as ``(length, start, end)``.

    Ties resolve to the earliest run. Returns ``(0, None, None)`` when empty.
    """
    best, best_start, best_end = 0, None, None
    for start in sorted(healthy):
        if start - timedelta(days=1) in healthy:
            continue  # not the head of a run
        end = start
        while end + timedelta(days=1) in healthy:
            end += timedelta(days=1)
        length = (end - start).days + 1
        if length > best:
            best, best_start, best_end = length, start, end
    return best, best_start, best_end


def _criterion_1(runs: Sequence[StoredScanRun], per_day: int) -> GateCriterion:
    ok: dict[date, int] = defaultdict(int)
    failed: dict[date, int] = defaultdict(int)
    for r in runs:
        day = r.started_at.astimezone(UTC).date()
        if r.status is ScanStatus.SUCCESS:
            ok[day] += 1
        elif r.status is ScanStatus.FAILED:
            failed[day] += 1
    healthy = {d for d, n in ok.items() if n >= per_day and failed.get(d, 0) == 0}
    streak, start, end = _longest_streak(healthy)
    span = f" ({start.isoformat()}..{end.isoformat()})" if start and end else ""
    evidence = f"longest healthy streak: {streak} days{span}; failed runs: {sum(failed.values())}"
    return GateCriterion(
        number=1,
        title="7 consecutive days autonomous",
        passed=streak >= _REQUIRED_STREAK_DAYS,
        evidence=evidence,
    )


def _criterion_2(decisions: Sequence[DecisionSummary], setup_ids: set[UUID]) -> GateCriterion:
    published = sum(1 for d in decisions if d.id in setup_ids)
    analyzer_skips = sum(1 for d in decisions if d.direction is None)
    vetoes = sum(1 for d in decisions if d.direction is not None and d.id not in setup_ids)
    skipped = analyzer_skips + vetoes
    evidence = (
        f"published={published} skipped={skipped} "
        f"(analyzer/gate skips={analyzer_skips}, judge vetoes={vetoes})"
    )
    return GateCriterion(
        number=2,
        title=">=5 published and >=5 skipped",
        passed=published >= _MIN_PUBLISHED and skipped >= _MIN_SKIPPED,
        evidence=evidence,
    )


def _is_forecaster_evaluated(setup: StoredActiveSetup) -> bool:
    evaluation = setup.latest_evaluation
    return (
        setup.last_evaluated_at is not None
        and evaluation is not None
        and "status" in evaluation
        and evaluation.get("source") != _RESOLVER_SOURCE
    )


def _criterion_3(setups: Sequence[StoredActiveSetup]) -> GateCriterion:
    n = sum(1 for s in setups if _is_forecaster_evaluated(s))
    return GateCriterion(
        number=3,
        title="Forecaster updated >=1 active setup",
        passed=n >= 1,
        evidence=f"forecaster-evaluated setups={n}",
    )


def _criterion_4(decisions: Sequence[DecisionSummary]) -> GateCriterion:
    rules = Counter(d.violated_rule for d in decisions if d.violated_rule)
    rejections = sum(rules.values())
    # ASCII "x" rather than U+00D7: the repo's RUF001 lint rejects confusable glyphs.
    listing = ", ".join(f"{rule} x{n}" for rule, n in rules.most_common()) or "none"
    evidence = f"gate rejections={rejections}; rules: {listing}. {_GATE_LOG_NOTE}"
    return GateCriterion(
        number=4,
        title="Risk gates prevented >=1 rule-violating signal",
        passed=rejections >= 1,
        evidence=evidence,
    )


def evaluate_exit_gate(
    *,
    scan_runs: Sequence[StoredScanRun],
    decisions: Sequence[DecisionSummary],
    setups: Sequence[StoredActiveSetup],
    expected_scans_per_day: int = DEFAULT_EXPECTED_SCANS_PER_DAY,
) -> ExitGateReport:
    """Evaluate the four Slice 2 exit criteria over the given journal rows.

    Args:
        scan_runs: every ``scan_runs`` row in the review window.
        decisions: every ``signals`` row (published, vetoed and skipped) in the window.
        setups: every ``active_setups`` row in the window; membership of a
            decision's ``id`` in ``{s.signal_id}`` is what defines *published*.
        expected_scans_per_day: SUCCESS rows a UTC day needs to be healthy.

    Returns:
        An ``ExitGateReport`` with exactly four criteria in order 1..4; empty
        inputs fail every criterion rather than raising.
    """
    setup_ids: set[UUID] = {s.signal_id for s in setups}
    criteria = [
        _criterion_1(scan_runs, expected_scans_per_day),
        _criterion_2(decisions, setup_ids),
        _criterion_3(setups),
        _criterion_4(decisions),
    ]
    return ExitGateReport(criteria=criteria, all_passed=all(c.passed for c in criteria))
