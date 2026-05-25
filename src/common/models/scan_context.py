"""ScanContext: run metadata passed into every agent on a scheduled scan.

The Analyzer, Historian, Skeptic, Judge, and Forecaster all receive this so
that journal entries, Langfuse traces, and Postgres rows can be joined by
`scan_id` after the fact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.common.models.enums import ScanSession


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ScanContext(BaseModel):
    """Per-scan run metadata.

    One ScanContext is created at the top of each scheduled run and
    propagates through the entire agent graph. It is *not* mutated downstream.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    scan_id: UUID = Field(
        default_factory=uuid4,
        description="Unique identifier for this scan run; used as the join "
        "key across signals, agent_runs, and scan_runs tables.",
    )
    started_at: datetime = Field(
        default_factory=_utc_now,
        description="UTC timestamp the scan was initiated. Must be timezone-aware.",
    )
    session: ScanSession = Field(
        description="Which scheduler window triggered this scan; consumed by "
        "risk gates to enforce SPEC §1.6 rule 7 (no signals in ASIAN/COOLDOWN).",
    )
    symbols: list[str] = Field(
        min_length=1,
        description="Watchlist symbols being scanned (e.g., ['BTCUSDT', 'ETHUSDT']). "
        "Order is not significant; symbols run in parallel downstream.",
    )
    strategy: str = Field(
        min_length=1,
        max_length=64,
        description="Strategy name from the registry (SPEC §3.2). Slice 1 uses 'smc'.",
    )
    triggered_by: str = Field(
        default="scheduler",
        max_length=64,
        description="Origin of the scan: 'scheduler', 'manual', 'replay', etc. "
        "Used by analytics to distinguish autonomous vs operator-driven runs.",
    )

    @field_validator("started_at")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("started_at must be timezone-aware (use UTC)")
        return v

    @field_validator("symbols")
    @classmethod
    def _symbols_uppercase_unique(cls, v: list[str]) -> list[str]:
        normalized = [s.strip().upper() for s in v]
        if any(not s for s in normalized):
            raise ValueError("symbols must be non-empty strings")
        if len(set(normalized)) != len(normalized):
            raise ValueError("symbols must be unique")
        return normalized
