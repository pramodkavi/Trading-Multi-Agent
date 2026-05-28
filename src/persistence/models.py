"""Pydantic wrappers for rows read out of the persistence layer.

These are NOT the in-flight agent models (those live in src/common/models/).
They are read-side projections that mirror the database schema row shape and
hold the JSONB payload as raw dict so callers can choose how strictly to
validate -- important because rows persist across model schema evolutions
(SPEC §3.4 adds embeddings, Slice 2 Step 2.4 adds tags/features columns to
signals, etc.).

Why a separate set of models:
- Write side accepts a typed SignalProposal | SkipDecision and serialises it
  to JSONB. Read side returns the *row*, which has columns the writer did not
  produce (id, created_at default, status, direction NULL for skips). Mixing
  the two would entangle DB concerns with agent boundary types.
- The .as_proposal() / .as_skip() helpers let callers re-validate the JSONB
  back into the latest typed model when they want to. Tests cover the happy
  path; production callers wrap with try/except to tolerate older shapes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.common.models import (
    AgentRole,
    ScanStatus,
    SignalDirection,
    SignalProposal,
    SignalStatus,
    SkipDecision,
)

# ---------------------------------------------------------------------------
# StoredSignal -- mirrors a row of `signals`
# ---------------------------------------------------------------------------


class StoredSignal(BaseModel):
    """Read-side projection of one row in the `signals` table.

    `payload` is the raw JSONB exactly as it landed in the DB. Use
    `.as_proposal()` or `.as_skip()` to re-parse into a typed model when the
    caller cares about field-level validation. Direct field reads (`symbol`,
    `direction`) come straight from indexed DB columns so they stay cheap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(description="Primary key (signals.id).")
    scan_id: UUID = Field(description="FK to scan_runs.id.")
    symbol: str = Field(min_length=1, max_length=20)
    strategy: str = Field(min_length=1, max_length=64)
    direction: SignalDirection | None = Field(
        description="LONG / SHORT for PUBLISHED rows; NULL for SKIPPED.",
    )
    status: SignalStatus = Field(description="PUBLISHED or SKIPPED.")
    created_at: datetime = Field(description="UTC; must be timezone-aware.")
    payload: dict[str, Any] = Field(
        description="Raw JSONB payload. Use as_proposal() / as_skip() to parse.",
    )

    @field_validator("created_at")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (use UTC)")
        return v

    def as_proposal(self) -> SignalProposal:
        """Re-parse the payload back into a SignalProposal.

        Raises:
            ValueError: row is not a PUBLISHED row (skip rows have no
                proposal to extract).
            pydantic.ValidationError: payload does not validate against the
                current SignalProposal schema (i.e., it was written by an
                older version with incompatible field shapes).
        """
        if self.status is not SignalStatus.PUBLISHED:
            raise ValueError(f"as_proposal() requires status=PUBLISHED, got {self.status.value}")
        return SignalProposal.model_validate(self.payload)

    def as_skip(self) -> SkipDecision:
        """Re-parse the payload back into a SkipDecision.

        Raises:
            ValueError: row is not a SKIPPED row.
            pydantic.ValidationError: payload does not validate against the
                current SkipDecision schema.
        """
        if self.status is not SignalStatus.SKIPPED:
            raise ValueError(f"as_skip() requires status=SKIPPED, got {self.status.value}")
        return SkipDecision.model_validate(self.payload)


# ---------------------------------------------------------------------------
# StoredScanRun -- mirrors a row of `scan_runs`
# ---------------------------------------------------------------------------


class StoredScanRun(BaseModel):
    """Read-side projection of one row in the `scan_runs` table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    started_at: datetime
    completed_at: datetime | None = None
    status: ScanStatus
    error_message: str | None = None
    session: str | None = None
    strategy: str | None = None
    symbols: list[str] | None = None

    @field_validator("started_at")
    @classmethod
    def _start_must_be_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("started_at must be timezone-aware (use UTC)")
        return v

    @field_validator("completed_at")
    @classmethod
    def _complete_must_be_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("completed_at must be timezone-aware (use UTC)")
        return v


# ---------------------------------------------------------------------------
# StoredAgentRun -- mirrors a row of `agent_runs`
# ---------------------------------------------------------------------------


class StoredAgentRun(BaseModel):
    """Read-side projection of one row in the `agent_runs` table.

    Slice 1's analyzer doesn't yet hit the LLM, so token_usage / cost_usd may
    be empty / None for those rows. Slice 2's Skeptic and Judge will populate
    them via StructuredCompletionResult fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    scan_id: UUID
    agent_role: AgentRole
    strategy: str | None = None
    input_hash: str = Field(min_length=1, max_length=128)
    output: dict[str, Any]
    latency_ms: int = Field(ge=0)
    token_usage: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (use UTC)")
        return v
