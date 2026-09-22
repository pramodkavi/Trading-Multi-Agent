"""Pydantic models for Critic v0 (design §3, §5.3, §6.2, §6.4)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.common.models import JudgeConfidence, SignalProposal
from src.common.models.skip_decision import SkipDecision
from src.persistence.models import StoredSignal


class DecisionKind(StrEnum):
    """How a (scan, symbol) decision ended, derived from the Judge ruling."""

    PUBLISHED = "PUBLISHED"  # Judge PUBLISH / PUBLISH_WITH_CAVEAT -> Telegram
    VETOED = "VETOED"  # proposal existed, Judge ruled SKIP
    GATE_REJECTED = "GATE_REJECTED"  # proposal existed, a SPEC §1.6 hard rule blocked it
    NO_SETUP = "NO_SETUP"  # Analyzer found nothing (SkipDecision without violated_rule)


class ResolutionOutcome(StrEnum):
    """Deterministic price-replay result (design §5.3)."""

    WIN = "WIN"
    LOSS = "LOSS"
    EXPIRED = "EXPIRED"
    UNRESOLVABLE = "UNRESOLVABLE"  # proposal missing/corrupt; never retried


def _aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (use UTC)")
    return v


class Resolution(BaseModel):
    """One row of decision_resolutions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_id: UUID
    decision_kind: DecisionKind
    outcome: ResolutionOutcome
    decided_at: datetime
    entered_at: datetime | None = None
    resolved_at: datetime
    realized_r: float
    max_adverse_excursion_r: float | None = None
    max_favorable_excursion_r: float | None = None
    tp2_reached: bool = False
    candles_used: int = Field(ge=0)
    engine_version: str = Field(min_length=1, max_length=40)

    @field_validator("decided_at", "resolved_at")
    @classmethod
    def _aware_required(cls, v: datetime) -> datetime:
        return _aware(v)

    @field_validator("entered_at")
    @classmethod
    def _aware_optional(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _aware(v)


class DecisionRecord(BaseModel):
    """A signal row stitched to the agent outputs that produced it (design §3)."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    signal: StoredSignal
    kind: DecisionKind
    proposal: SignalProposal | None = None
    skip: SkipDecision | None = None
    historian: dict[str, Any] | None = None
    skeptic: dict[str, Any] | None = None
    judge: dict[str, Any] | None = None


class SkepticVerdict(StrEnum):
    YES = "YES"
    NO = "NO"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class FailureCategory(StrEnum):
    NONE = "NONE"
    BAD_STRUCTURE_READ = "BAD_STRUCTURE_READ"
    WRONG_ZONE = "WRONG_ZONE"
    RR_UNREALISTIC = "RR_UNREALISTIC"
    IGNORED_SKEPTIC = "IGNORED_SKEPTIC"
    OVERRULED_GOOD_SETUP = "OVERRULED_GOOD_SETUP"
    MACRO_MISSED = "MACRO_MISSED"
    EXPIRED_NO_TRIGGER = "EXPIRED_NO_TRIGGER"
    OTHER = "OTHER"


class DecisionReview(BaseModel):
    """Claude's graded judgement of one decision, given its resolution (design §6.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    smc_read_valid: bool = Field(description="Was the Analyzer's SMC structure read defensible?")
    skeptic_vindicated: SkepticVerdict = Field(
        description="Did the market bear out the Skeptic's objection? "
        "NOT_APPLICABLE if no Skeptic ran."
    )
    judge_correct: bool = Field(description="Given the outcome, was the Judge's ruling right?")
    gate_correct: bool | None = Field(
        default=None, description="GATE_REJECTED only: did the rule save us from a loss?"
    )
    failure_category: FailureCategory
    lesson: str = Field(min_length=5, max_length=300)
    confidence: JudgeConfidence


class ProposedRuleChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    target: str = Field(
        min_length=1,
        max_length=120,
        description="SKEPTIC_SYSTEM_PROMPT | JUDGE_SYSTEM_PROMPT | FORECASTER_SYSTEM_PROMPT | "
        "risk_gates.<CONSTANT> | SPEC §1.6 rule N",
    )
    change: str = Field(min_length=10, max_length=2000)
    rationale: str = Field(min_length=10, max_length=2000)
    evidence_signal_ids: list[UUID] = Field(default_factory=list, max_length=50)


class CriticReport(BaseModel):
    """Aggregate weekly/period judgement (design §6.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    summary: str = Field(min_length=20, max_length=4000)
    key_patterns: list[str] = Field(default_factory=list, max_length=20)
    skeptic_assessment: str = Field(min_length=1, max_length=2000)
    judge_assessment: str = Field(min_length=1, max_length=2000)
    gate_assessment: str = Field(min_length=1, max_length=2000)
    proposed_changes: list[ProposedRuleChange] = Field(default_factory=list, max_length=10)


class GateCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int = Field(ge=1, le=4)
    title: str = Field(min_length=1, max_length=120)
    passed: bool
    evidence: str = Field(min_length=1, max_length=2000)


class ExitGateReport(BaseModel):
    """SPEC §5 'End of Slice 2' verdict (design §6.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criteria: list[GateCriterion] = Field(min_length=4, max_length=4)
    all_passed: bool

    @model_validator(mode="after")
    def _all_passed_matches(self) -> ExitGateReport:
        if self.all_passed != all(c.passed for c in self.criteria):
            raise ValueError("all_passed must equal all(criteria.passed)")
        return self
