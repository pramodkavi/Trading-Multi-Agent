"""SkipDecision: the structured non-action a strategy returns when it has no setup.

Per SPEC §3.1.1 FR-1.2, Analyzer strategies must return *either* a
SignalProposal *or* a SkipDecision — never None, never a free-form string.
Skipped reasoning is still persisted (FR-1.7) so the Critic can analyze
*why* the system chose not to act.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SkipReason(StrEnum):
    """Categorical reason a strategy or risk gate declined to publish.

    Open enum: extend as new skip categories emerge from operations.
    Categorical (vs free text) so analytics can group skip reasons by frequency.
    """

    NO_CLEAR_BIAS = "NO_CLEAR_BIAS"  # HTF state machine ambiguous
    GATE_FAILED = "GATE_FAILED"  # One of the 5 SMC gates failed
    PREMIUM_DISCOUNT_VIOLATION = "PREMIUM_DISCOUNT_VIOLATION"  # SPEC §1.5 hard rule
    INSUFFICIENT_RR = "INSUFFICIENT_RR"  # SPEC §1.6 rule 2 (<1:3)
    EXCESSIVE_RISK = "EXCESSIVE_RISK"  # SPEC §1.6 rule 1 (>1% equity)
    LEVERAGE_CAP = "LEVERAGE_CAP"  # SPEC §1.6 rule 8 (>10x)
    CONCURRENT_SETUPS_LIMIT = "CONCURRENT_SETUPS_LIMIT"  # SPEC §1.6 rule 4
    DAILY_SIGNAL_CAP = "DAILY_SIGNAL_CAP"  # SPEC §1.6 rule 5
    LOSS_STREAK_PAUSE = "LOSS_STREAK_PAUSE"  # SPEC §1.6 rule 6
    SESSION_BLOCKED = "SESSION_BLOCKED"  # SPEC §1.6 rule 7
    CORRELATED_EXPOSURE = "CORRELATED_EXPOSURE"  # SPEC §1.6 rule 9
    FUNDING_COST_PROHIBITIVE = "FUNDING_COST_PROHIBITIVE"  # SPEC §1.6 rule 10
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"  # Provider failure, gracefully degraded
    OTHER = "OTHER"  # Free text in `details`


class SkipDecision(BaseModel):
    """A strategy or risk gate's structured 'no-op' for a given scan + symbol.

    Persisted to the journal alongside published signals so the Critic can
    learn from non-actions as well as actions.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        frozen=True,
    )

    decision_id: UUID = Field(
        default_factory=uuid4,
        description="Stable identifier for this skip; joined to agent_runs.",
    )
    scan_id: UUID = Field(
        description="The scan run that produced this skip; join key with "
        "ScanContext and scan_runs.",
    )
    strategy: str = Field(
        min_length=1,
        max_length=64,
        description="Strategy registry name that emitted the skip.",
    )
    symbol: str = Field(
        min_length=1,
        max_length=20,
        description="Market symbol that was evaluated (e.g., 'BTCUSDT').",
    )
    reason: SkipReason = Field(
        description="Categorical skip reason; drives analytics groupings.",
    )
    details: str = Field(
        min_length=5,
        max_length=2000,
        description="Free-form explanation; reads in dashboards and Critic context. "
        "Should cite specific data (price, gate name, rule number) when applicable.",
    )
    violated_rule: str | None = Field(
        default=None,
        max_length=120,
        description="When `reason` is a hard-rule rejection, the rule identifier "
        "from SPEC §1.6 (e.g., 'RULE_2_MIN_RR', 'RULE_7_ASIAN_SESSION'). "
        "Required by FR-1.3 logging when a proposal is force-skipped by a gate.",
    )
    created_at: datetime = Field(
        default_factory=_utc_now,
        description="UTC timestamp of skip emission. Must be timezone-aware.",
    )

    @field_validator("created_at")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (use UTC)")
        return v
