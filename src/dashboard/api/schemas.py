"""Response models for the dashboard API (design §6.1).

Every route returns a Pydantic model so the OpenAPI document at
`/api/openapi.json` is the contract the UI codegens against. All models forbid
extra fields: a typo in an assembly helper fails loudly in the tests rather than
silently adding an undocumented key to the wire format.

Step 4.2 adds the read-only data payloads (design §4.2 P1 / P2 / P3, §3.4):

- `OverviewResponse` -- the landing page in one round trip: health strip, risk
  budget (numbers from `risk_gates`, never re-implemented -- design §5.3), the
  OPEN setup cards and today's four scan windows with per-symbol decision chips.
- `SignalsPage` / `SignalRowOut` -- the paged journal (`JournalRow` flattened).
- `LifecycleResponse` -- one decision's timeline: the journal row, every agent
  run **trimmed** to the fields the timeline renders (`LifecycleStep.summary`),
  the Forecaster evaluations, the Critic's resolution / review and the setup.
  The full agent output is served per step by `AgentRunOut` (design §3.4:
  the Data API caps a response at 1 MB).

Fields marked "pending Step 4.3" are placeholders the UI can already bind to:
CloudWatch alarm states and unrealised R need the metrics / price providers
that Step 4.3 wires in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.agents.critic.models import Resolution
from src.agents.orchestration.risk_gates import MAX_CONCURRENT_SETUPS, MAX_SIGNALS_PER_24H
from src.common.models import ScanStatus, SignalDirection, SignalOutcome, SignalStatus
from src.dashboard.api.classify import DecisionKind
from src.persistence.models import StoredAgentRun, StoredReview, StoredSetupEvaluation

PENDING_4_3 = "pending Step 4.3"


class HealthResponse(BaseModel):
    """`GET /api/health` body — deploy smoke test only, no I/O behind it."""

    status: Literal["ok"]
    version: str


class DbProbeResponse(BaseModel):
    """`GET /api/system/db` body — one `SELECT 1` round trip through the Data API."""

    db: Literal["ok", "resuming", "error"]
    latency_ms: int = Field(ge=0, description="Wall-clock time of the probe statement.")


class ErrorResponse(BaseModel):
    """Body of the app-wide error replies (design §3.4).

    `reason` is a stable machine token the UI switches on: `db_resuming` (503,
    with `Retry-After`), `db_error` (502), `not_found` (404), `bad_cursor`
    (400). Data routers list this model under `responses={...}` so it lands in
    OpenAPI.
    """

    reason: str


# ---------------------------------------------------------------------------
# Overview (P1)
# ---------------------------------------------------------------------------


class WindowOutcome(BaseModel):
    """How one scan window (or an ad-hoc invocation) went.

    The scan Lambda writes one `scan_runs` row **per symbol**, so a window is a
    group of rows: `started_at` is the earliest, `status` the worst (FAILED >
    RUNNING > SUCCESS), `symbols` the sorted union and `error` the first
    `error_message` seen.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(description="LONDON / NY / OVERLAP / DAILY_WRAP, or AD_HOC.")
    started_at: datetime
    status: ScanStatus
    symbols: list[str]
    error: str | None = None


class ForecasterCoverage(BaseModel):
    """Did the Forecaster look at every OPEN setup in the last sweep?"""

    model_config = ConfigDict(extra="forbid")

    open_setups: int = Field(ge=0)
    stale: int = Field(
        ge=0,
        description="OPEN setups never evaluated, or last evaluated before the most "
        "recent completed sweep (see `overview.last_sweep_at`).",
    )
    evaluations_last_window: int = Field(ge=0)
    errors_last_window: int = Field(ge=0)


class HealthStrip(BaseModel):
    """The one-glance 'is it alive' badges (design §4.2 P1)."""

    model_config = ConfigDict(extra="forbid")

    last_scan: WindowOutcome | None = Field(
        description="Most recent scan since the start of yesterday UTC (overnight this is "
        "yesterday's DAILY_WRAP); null only if nothing ran in that span."
    )
    forecaster: ForecasterCoverage
    alarms: None = Field(default=None, description="CloudWatch alarm states; " + PENDING_4_3)
    alarms_note: str = PENDING_4_3
    db: Literal["ok"] = Field(description="Reaching this response proves the reads succeeded.")


class RiskBudget(BaseModel):
    """SPEC §1.6 stateful caps as the gate will see them at the next scan (design §5.3)."""

    model_config = ConfigDict(extra="forbid")

    open_setups: int = Field(ge=0)
    open_cap: int = MAX_CONCURRENT_SETUPS
    published_24h: int = Field(ge=0)
    daily_cap: int = MAX_SIGNALS_PER_24H
    loss_streak: int = Field(ge=0)
    pause_active: bool
    pause_until: datetime | None = None
    session: str = Field(description="`ScanSession` for the clock (sessions.session_for).")
    session_blocked: bool = Field(description="Rule 7: ASIAN / COOLDOWN block new signals.")
    next_scan_at: datetime
    seconds_to_next_scan: int = Field(ge=0)


class SetupCardOut(BaseModel):
    """One OPEN setup as the overview / setups page renders it."""

    model_config = ConfigDict(extra="forbid")

    setup_id: UUID
    signal_id: UUID
    symbol: str
    direction: SignalDirection
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    opened_at: datetime
    age_seconds: int = Field(ge=0)
    last_evaluated_at: datetime | None
    evaluation_age_seconds: int | None = Field(default=None, ge=0)
    latest_status: str | None = Field(description="Forecaster verdict of the latest evaluation.")
    latest_reasoning: str | None
    unrealised_r: None = Field(default=None, description="Needs a live price; " + PENDING_4_3)
    unrealised_r_note: str = PENDING_4_3


class SymbolChip(BaseModel):
    """Per-symbol decision chip under a window (design §4.2 P1)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    kind: DecisionKind
    violated_rule: str | None = None
    signal_id: UUID


class WindowSummary(BaseModel):
    """One of today's four scheduled windows, fired or not."""

    model_config = ConfigDict(extra="forbid")

    label: str
    fire_time: datetime
    scan: WindowOutcome | None = Field(description="Null when the window has not fired yet.")
    symbols: list[SymbolChip]


class OverviewResponse(BaseModel):
    """`GET /api/overview` body — the whole landing page in one call."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    health: HealthStrip
    risk: RiskBudget
    open_setups: list[SetupCardOut]
    windows_today: list[WindowSummary]


# ---------------------------------------------------------------------------
# Journal (P3)
# ---------------------------------------------------------------------------


class SignalRowOut(BaseModel):
    """`queries.JournalRow` flattened: the `signals` row plus its derived columns."""

    model_config = ConfigDict(extra="forbid")

    # -- signals columns (StoredSignal) ----------------------------------------
    id: UUID
    scan_id: UUID
    symbol: str
    strategy: str
    direction: SignalDirection | None
    status: SignalStatus
    created_at: datetime
    payload: dict[str, Any]
    tags: list[str]
    features: dict[str, Any]
    outcome: SignalOutcome | None
    outcome_metadata: dict[str, Any] | None
    # -- derived (design §5.1) --------------------------------------------------
    decision_kind: DecisionKind
    judge_ruling: str | None
    session: str | None
    has_setup: bool
    violated_rule: str | None
    resolution_outcome: str | None
    realized_r: float | None


class SignalsPage(BaseModel):
    """`GET /api/signals` body; `next_cursor` is null on the last page."""

    model_config = ConfigDict(extra="forbid")

    items: list[SignalRowOut]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Lifecycle (P2)
# ---------------------------------------------------------------------------


class LifecycleStep(BaseModel):
    """One agent run on the timeline, with its output trimmed (`overview.trim_output`)."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(description="`agent_runs.agent_role` value, e.g. 'analyzer'.")
    run_id: UUID = Field(description="Fetch the full output at /api/signals/{id}/runs/{run_id}.")
    created_at: datetime
    latency_ms: int = Field(ge=0)
    cost_usd: float | None
    summary: dict[str, Any]


class EvaluationOut(StoredSetupEvaluation):
    """One Forecaster evaluation — `setup_evaluations` row fields verbatim."""


class ResolutionOut(Resolution):
    """The Critic's deterministic replay — `decision_resolutions` row fields verbatim."""


class ReviewOut(StoredReview):
    """The Critic's graded review — `decision_reviews` row fields verbatim."""


class AgentRunOut(StoredAgentRun):
    """`GET /api/signals/{id}/runs/{run_id}` body — `agent_runs` row fields verbatim."""


class LifecycleResponse(BaseModel):
    """`GET /api/signals/{id}` body — the decision's whole story (design §4.2 P2)."""

    model_config = ConfigDict(extra="forbid")

    signal: SignalRowOut
    steps: list[LifecycleStep] = Field(description="Agent runs of the scan, oldest first.")
    evaluations: list[EvaluationOut] = Field(description="Forecaster evaluations, oldest first.")
    resolution: ResolutionOut | None
    review: ReviewOut | None
    setup: SetupCardOut | None
