"""Schema tests for the Critic v0 models (SPEC design 2026-09-22 §3, §5.3, §6.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.agents.critic.models import (
    CriticReport,
    DecisionKind,
    DecisionReview,
    ExitGateReport,
    FailureCategory,
    GateCriterion,
    ProposedRuleChange,
    Resolution,
    ResolutionOutcome,
    SkepticVerdict,
)
from src.common.models import JudgeConfidence


def _resolution(**overrides: object) -> Resolution:
    base: dict[str, object] = {
        "signal_id": uuid4(),
        "decision_kind": DecisionKind.PUBLISHED,
        "outcome": ResolutionOutcome.WIN,
        "decided_at": datetime(2026, 7, 1, 8, 3, tzinfo=UTC),
        "entered_at": datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        "resolved_at": datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
        "realized_r": 3.0,
        "max_adverse_excursion_r": -0.4,
        "max_favorable_excursion_r": 3.2,
        "tp2_reached": False,
        "candles_used": 84,
        "engine_version": "replay-v1",
    }
    base.update(overrides)
    return Resolution(**base)  # type: ignore[arg-type]


def test_resolution_round_trips_json() -> None:
    res = _resolution()
    dumped = res.model_dump(mode="json")
    assert dumped["outcome"] == "WIN"
    assert Resolution.model_validate(dumped) == res


def test_resolution_rejects_naive_datetimes() -> None:
    with pytest.raises(ValidationError):
        _resolution(decided_at=datetime(2026, 7, 1, 8, 3))


def test_resolution_rejects_negative_candles() -> None:
    with pytest.raises(ValidationError):
        _resolution(candles_used=-1)


def test_resolution_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _resolution(bogus=1)


def test_decision_kind_values() -> None:
    assert {k.value for k in DecisionKind} == {"PUBLISHED", "VETOED", "GATE_REJECTED", "NO_SETUP"}


def test_resolution_outcome_values() -> None:
    assert {o.value for o in ResolutionOutcome} == {"WIN", "LOSS", "EXPIRED", "UNRESOLVABLE"}


def test_decision_review_minimal() -> None:
    review = DecisionReview(
        smc_read_valid=True,
        skeptic_vindicated=SkepticVerdict.NOT_APPLICABLE,
        judge_correct=True,
        failure_category=FailureCategory.NONE,
        lesson="Clean discount-zone long; nothing to change.",
        confidence=JudgeConfidence.HIGH,
    )
    assert review.gate_correct is None


def test_decision_review_lesson_length_bounds() -> None:
    with pytest.raises(ValidationError):
        DecisionReview(
            smc_read_valid=True,
            skeptic_vindicated=SkepticVerdict.NO,
            judge_correct=True,
            failure_category=FailureCategory.NONE,
            lesson="x" * 301,
            confidence=JudgeConfidence.LOW,
        )


def test_critic_report_caps_proposed_changes() -> None:
    change = ProposedRuleChange(
        target="JUDGE_SYSTEM_PROMPT",
        change="Require the Judge to cite the Skeptic's headline when overruling it.",
        rationale="Overruled HIGH-severity objections lost 4 of 5 times in the period.",
        evidence_signal_ids=[uuid4()],
    )
    with pytest.raises(ValidationError):
        CriticReport(
            summary="s" * 40,
            key_patterns=["p"],
            skeptic_assessment="ok",
            judge_assessment="ok",
            gate_assessment="ok",
            proposed_changes=[change] * 11,
        )


def test_exit_gate_report_requires_exactly_four_criteria() -> None:
    crit = GateCriterion(number=1, title="7 days autonomous", passed=True, evidence="16/day")
    with pytest.raises(ValidationError):
        ExitGateReport(criteria=[crit], all_passed=True)
