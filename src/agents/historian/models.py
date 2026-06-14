"""Pydantic output models for the Historian agent (Slice 2 Step 2.4).

The Historian queries the signal journal for past setups that resemble the
current proposal and reports their *empirical* outcomes (SPEC §3.1 role 2,
FR-1.4). Its output is a ``HistorianReport`` consumed by the Judge (Step 2.6)
and surfaced in the Telegram alert (FR-5.2 "Historian win-rate statistic").

These are boundary types: the Judge and the dashboard read them, so they are
frozen and forbid extra fields. They deliberately do NOT assert a calibrated
probability -- ``win_rate`` is a raw empirical frequency over a (often small)
sample, and ``sample_size`` is reported alongside it so the Judge can weight it
honestly. Calibration is the Critic's long-horizon job (SPEC §3.4).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.common.models import SignalDirection, SignalOutcome


class HistoricalMatch(BaseModel):
    """One past journal signal the Historian judged similar to the query.

    ``tag_overlap`` (stage-2) and ``l2_distance`` (stage-3) are the similarity
    metrics that ranked this match; they are surfaced for transparency so the
    Judge and the dashboard can see *why* a precedent was selected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: UUID = Field(description="signals.id of the matched past signal.")
    symbol: str = Field(min_length=1, max_length=20)
    direction: SignalDirection | None = Field(
        description="LONG / SHORT of the past signal (always set for PUBLISHED matches).",
    )
    created_at: datetime = Field(description="When the past signal was journaled (UTC).")
    outcome: SignalOutcome | None = Field(
        description="Terminal result of the past setup. Always set for matches "
        "(retrieval filters to signals with a known outcome).",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="The past signal's semantic tags (basis for tag-overlap ranking).",
    )
    tag_overlap: int = Field(
        ge=0,
        description="Count of tags shared with the query proposal (stage-2 metric).",
    )
    l2_distance: float = Field(
        ge=0.0,
        description="Euclidean distance over the numeric feature vector (stage-3 metric); "
        "smaller is more similar.",
    )

    @field_validator("created_at")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (use UTC)")
        return v


class HistorianReport(BaseModel):
    """Empirical track record of setups resembling the current proposal.

    Produced by ``HistorianRepository.retrieve`` and attached to the agent state
    for the Judge. ``win_rate`` is ``wins / (wins + losses)`` -- breakeven and
    inconclusive (invalidated / expired) outcomes are excluded from the
    denominator and reported separately. ``None`` when there are no decisive
    outcomes (including an empty sample), so consumers never divide by zero or
    mistake "no data" for "0% win rate".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_proposal_id: UUID = Field(description="proposal_id of the signal being evaluated.")
    strategy: str = Field(min_length=1, max_length=64)
    direction: SignalDirection = Field(description="Direction of the query proposal.")
    session: str | None = Field(
        default=None,
        description="Scheduler session the query scan ran in (stage-1 filter); "
        "None when the caller did not constrain by session.",
    )
    primary_poi_type: str | None = Field(
        default=None,
        description="POI kind the query is anchored to (stage-1 filter), e.g. 'order_block'.",
    )

    sample_size: int = Field(ge=0, description="Number of similar past setups retrieved.")
    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    breakeven: int = Field(ge=0)
    inconclusive: int = Field(
        ge=0,
        description="Matches whose outcome was INVALIDATED or EXPIRED "
        "(excluded from the win-rate denominator).",
    )
    win_rate: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="wins / (wins + losses); None when there are no decisive outcomes.",
    )
    matches: list[HistoricalMatch] = Field(
        default_factory=list,
        description="The top-K retrieved matches, most-similar first.",
    )
    summary: str = Field(
        min_length=1,
        max_length=2000,
        description="Human-readable one-paragraph summary for the Judge and the "
        "Telegram alert (FR-5.2).",
    )

    @property
    def decisive(self) -> int:
        """Matches with a win/loss outcome -- the win-rate denominator."""
        return self.wins + self.losses
