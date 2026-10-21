"""Response models for the dashboard API (design §6.1).

Every route returns a Pydantic model so the OpenAPI document at
`/api/openapi.json` is the contract the UI codegens against. All models forbid
extra fields: a typo in an assembly helper fails loudly in the tests rather than
silently adding an undocumented key to the wire format.

Step 4.2 adds the read-only data payloads (design §4.2 P1 / P2 / P3, §3.4):

- `OverviewResponse` -- the landing page in one round trip: health strip, risk
  budget (numbers from `risk_gates`, never re-implemented -- design §5.3), the
  OPEN setup cards and today's four scan windows with per-symbol decision chips.
- `SignalsPage` / `SignalRowOut` -- the paged journal. A row is **trimmed**
  (design §3.4: the Data API caps a response at 1 MB and a page is up to 201
  rows): the `signals` columns minus `payload` / `features` / `outcome_metadata`,
  plus the analyzer summary lifted out of the payload in SQL (`entry_price`,
  `stop_loss`, `take_profit_1/2`, `risk_reward_ratio` for a proposal;
  `skip_reason` / `skip_details` for a skip) and the derived columns.
- `LifecycleResponse` -- one decision's timeline: the **full** signal
  (`SignalDetailOut` = the row plus `payload` / `features` / `outcome_metadata`),
  every agent run **trimmed** to the fields the timeline renders
  (`LifecycleStep.summary`), the Forecaster evaluations, the Critic's
  resolution / review and the setup. The full agent output is served per step
  by `AgentRunOut`.

Step 4.3 adds the market payloads (`KlinesResponse`, `SignalChartResponse` with
its `ChartOverlay`), the decision funnel (`FunnelResponse`, reusing the pure
`funnel.FunnelGroup`) and fills `SetupCardOut.unrealised_r` best-effort from a
live price. P6 adds the analytics bodies (`WinRateResponse`,
`SkepticValueResponse`, `EquityCurveResponse`, reusing the pure `analytics.py`
models) and the Critic's reports / reviews (list lines without markdown or
whole JSONB; the report detail carries both). P7 adds the system-health
bodies (scan history, per-role usage + LLM cost per day, Forecaster sweeps,
CloudWatch alarms + Lambda metrics, the inferred config panel) and fills the
overview's alarm strip (`HealthStrip.alarms`, null + note on a CloudWatch error).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.agents.critic.models import Resolution
from src.agents.orchestration.risk_gates import MAX_CONCURRENT_SETUPS, MAX_SIGNALS_PER_24H
from src.common.models import ScanStatus, SignalDirection, SignalOutcome, SignalStatus
from src.dashboard.api.analytics import (
    EquityCurves,
    SkepticValue,
    SourceCounts,
    WinRateBucket,
    WinRateBy,
)
from src.dashboard.api.classify import DecisionKind
from src.dashboard.api.cloudwatch import AlarmState, MetricDay
from src.dashboard.api.funnel import FunnelGroup
from src.dashboard.api.queries import CriticReportSummary, ReviewSummary, UsageDay
from src.persistence.models import (
    StoredAgentRun,
    StoredCriticReport,
    StoredReview,
    StoredSetupEvaluation,
)
from src.providers.base import Timeframe


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
    with `Retry-After`), `db_error` (502), `market_unavailable` (502),
    `not_found` (404), `bad_cursor` (400), `unknown_symbol` (400). Data routers
    list this model under `responses={...}` so it lands in OpenAPI.
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
    alarms: list[AlarmState] | None = Field(
        default=None,
        description="The CryptoSignals-Monitoring-* alarm states; null when CloudWatch failed.",
    )
    alarms_note: str | None = Field(
        default=None, description='Null on success; "cloudwatch unavailable" when `alarms` is.'
    )
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
    unrealised_r: float | None = Field(
        default=None,
        description="Open R at the latest 5m close (LONG (price-entry)/(entry-stop), SHORT "
        "(entry-price)/(stop-entry), 2 dp). Best-effort: null when no price was available.",
    )
    unrealised_r_note: str | None = Field(
        default=None,
        description="Why `unrealised_r` is null: 'price unavailable' (the fetch failed), "
        "'not fetched on this view' (lifecycle page), 'risk undefined (entry == stop)'; "
        "null when it is set.",
    )


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
    """One journal row on the wire -- mirrors `queries.JournalRow` field for field.

    Trimmed by design (§3.4): no `payload` / `features` / `outcome_metadata`.
    Fetch `/api/signals/{id}` (`SignalDetailOut`) for the full signal.
    """

    model_config = ConfigDict(extra="forbid")

    # -- signals columns (trimmed) ---------------------------------------------
    id: UUID
    scan_id: UUID
    symbol: str
    strategy: str
    direction: SignalDirection | None
    status: SignalStatus
    created_at: datetime
    tags: list[str]
    outcome: SignalOutcome | None
    # -- analyzer summary (lifted from the payload in SQL) ----------------------
    entry_price: float | None = Field(description="Proposal rows only; null for skips.")
    stop_loss: float | None
    take_profit_1: float | None
    take_profit_2: float | None
    risk_reward_ratio: float | None
    skip_reason: str | None = Field(description="Skip rows only (`SkipDecision.reason`).")
    skip_details: str | None = Field(description="Skip rows only; first 200 chars of `details`.")
    # -- derived (design §5.1) --------------------------------------------------
    decision_kind: DecisionKind
    judge_ruling: str | None
    session: str | None
    has_setup: bool
    violated_rule: str | None
    resolution_outcome: str | None
    realized_r: float | None


class SignalDetailOut(SignalRowOut):
    """`SignalRowOut` plus the full stored signal -- the lifecycle's `signal` (one row)."""

    payload: dict[str, Any]
    features: dict[str, Any]
    outcome_metadata: dict[str, Any] | None


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

    signal: SignalDetailOut = Field(description="The journal row plus the full stored signal.")
    steps: list[LifecycleStep] = Field(description="Agent runs of the scan, oldest first.")
    evaluations: list[EvaluationOut] = Field(description="Forecaster evaluations, oldest first.")
    resolution: ResolutionOut | None
    review: ReviewOut | None
    setup: SetupCardOut | None


# ---------------------------------------------------------------------------
# Market data (Step 4.3)
# ---------------------------------------------------------------------------


class KlineOut(BaseModel):
    """One OHLCV candle on the wire (`providers.base.Kline` field for field)."""

    model_config = ConfigDict(extra="forbid")

    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class KlinesResponse(BaseModel):
    """`GET /api/market/klines` body: the most recent candles, ascending."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    klines: list[KlineOut]


class ChartOverlay(BaseModel):
    """The levels drawn over a signal's chart; every field optional (older payloads).

    Prices come from the proposal -- the stored payload, or for a gate-rejected
    decision the Analyzer run's rejected proposal. The POI zone is the order
    block the Analyzer anchored on (`features.ob_zone_low` / `ob_zone_high`).
    """

    model_config = ConfigDict(extra="forbid")

    direction: SignalDirection | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    poi_low: float | None = None
    poi_high: float | None = None


class SignalChartResponse(BaseModel):
    """`GET /api/signals/{id}/chart` body: H4 candles around the decision plus its levels."""

    model_config = ConfigDict(extra="forbid")

    signal_id: UUID
    symbol: str
    timeframe: Timeframe = Timeframe.H4
    created_at: datetime
    klines: list[KlineOut]
    overlay: ChartOverlay | None = Field(
        description="Null for a NO_SETUP decision (no proposal ever existed) or when the "
        "rejected proposal of a gate-rejected decision cannot be found."
    )


# ---------------------------------------------------------------------------
# Decision funnel (P4, Step 4.3)
# ---------------------------------------------------------------------------


class RejectionCount(BaseModel):
    """One row of a top-rejections table: a gate rule and how often it fired."""

    model_config = ConfigDict(extra="forbid")

    rule: str
    count: int = Field(ge=1)


class FunnelResponse(BaseModel):
    """`GET /api/funnel` body: the funnel per group, the totals and the top gate rules.

    `groups` / `totals` cover `[since, until)`. The two rejection tables are the
    last 7 and 30 days **anchored to now** (design §4.2 P4), whatever the
    requested range -- a historical `until` does not move them.
    """

    model_config = ConfigDict(extra="forbid")

    since: datetime
    until: datetime
    group_by: Literal["day", "window", "symbol"]
    groups: list[FunnelGroup]
    totals: FunnelGroup = Field(description='The same counts over every row (key "total").')
    top_rejections_7d: list[RejectionCount]
    top_rejections_30d: list[RejectionCount]


# ---------------------------------------------------------------------------
# Performance analytics + Critic (P6, Step 4.3)
# ---------------------------------------------------------------------------


class WinRateResponse(BaseModel):
    """`GET /api/analytics/win-rate` body: published win rate per bucket (`analytics.py`)."""

    model_config = ConfigDict(extra="forbid")

    since: datetime
    until: datetime
    by: WinRateBy
    buckets: list[WinRateBucket]
    source_counts: SourceCounts


class SkepticValueResponse(SkepticValue):
    """`GET /api/analytics/skeptic-value` body: the three bars plus the period."""

    since: datetime
    until: datetime


class EquityCurveResponse(EquityCurves):
    """`GET /api/analytics/equity-curve` body: the three R curves plus the period."""

    since: datetime
    until: datetime


class CriticReportSummaryOut(CriticReportSummary):
    """One `critic_reports` list line (no markdown; `summary` = `report->>'summary'`)."""


class CriticReportsPage(BaseModel):
    """`GET /api/critic/reports` body: the latest reports, newest first."""

    model_config = ConfigDict(extra="forbid")

    items: list[CriticReportSummaryOut]


class CriticReportOut(StoredCriticReport):
    """`GET /api/critic/reports/{id}` body: the full report JSONB and markdown."""


class ReviewSummaryOut(ReviewSummary):
    """One `decision_reviews` list line (named `review` extracts; null when it errored)."""


class ReviewsPage(BaseModel):
    """`GET /api/critic/reviews` body; `next_cursor` is null on the last page."""

    model_config = ConfigDict(extra="forbid")

    items: list[ReviewSummaryOut]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# System health (P7, Step 4.3)
# ---------------------------------------------------------------------------


class ScanRowOut(BaseModel):
    """One `scan_runs` row of the scan history table (one row per symbol per invocation)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    window: str = Field(description="schedule.window_for(started_at): a window label or AD_HOC.")
    started_at: datetime
    completed_at: datetime | None
    duration_s: float | None = Field(
        ge=0, description="completed_at - started_at (never negative); null while RUNNING."
    )
    status: ScanStatus
    symbols: list[str]
    error_message: str | None


class ScansResponse(BaseModel):
    """`GET /api/system/scans` body: the latest 50 scan runs, newest first."""

    model_config = ConfigDict(extra="forbid")

    items: list[ScanRowOut]


class UsageDayOut(UsageDay):
    """One (UTC day, role) line: runs, failures and LLM cost (`queries.UsageDay`)."""


class CostDay(BaseModel):
    """LLM spend of one UTC day, total and per role."""

    model_config = ConfigDict(extra="forbid")

    date: date
    total_usd: float = Field(ge=0)
    by_role: dict[str, float]


class AgentsResponse(BaseModel):
    """`GET /api/system/agents` body: per-role usage and LLM cost per day since `since`."""

    model_config = ConfigDict(extra="forbid")

    since: datetime = Field(description="Midnight UTC `days` days ago.")
    until: datetime
    usage: list[UsageDayOut]
    cost_per_day: list[CostDay]


class SweepRow(BaseModel):
    """One Forecaster sweep: evaluations bucketed to the latest sweep start <= them."""

    model_config = ConfigDict(extra="forbid")

    sweep_start: datetime = Field(description="Scheduled fire time + SWEEP_OFFSET (:05).")
    evaluations: int = Field(ge=0)
    errors: int = Field(ge=0)


class SweepsResponse(BaseModel):
    """`GET /api/system/sweeps` body: every sweep started in [since, until], oldest first."""

    model_config = ConfigDict(extra="forbid")

    since: datetime
    until: datetime
    sweeps: list[SweepRow]


class AlarmsResponse(BaseModel):
    """`GET /api/system/alarms` body: null `alarms` + note when CloudWatch failed or denied."""

    model_config = ConfigDict(extra="forbid")

    alarms: list[AlarmState] | None
    note: str | None = None


class MetricsResponse(BaseModel):
    """`GET /api/system/metrics` body: the scan Lambda's daily metrics.

    `days` is null with a note when CloudWatch failed ("cloudwatch unavailable"),
    refused the role ("cloudwatch access denied") or no
    `CryptoSignals-Compute-ScanLambda*` metric exists ("scan function not found").
    """

    model_config = ConfigDict(extra="forbid")

    function_name: str | None
    days: list[MetricDay] | None
    note: str | None = None


_T = TypeVar("_T")


class Inferred(BaseModel, Generic[_T]):
    """A config value the dashboard *infers* (design §8): it cannot read the scan config."""

    model_config = ConfigDict(extra="forbid")

    value: _T
    inferred: Literal[True] = True
    source: str = Field(description="Where the value was inferred from.")


class ScheduleWindowOut(BaseModel):
    """One scheduled scan window (UTC wall clock)."""

    model_config = ConfigDict(extra="forbid")

    label: str
    time_utc: str = Field(description='"HH:MM" UTC.')


class ScheduleOut(BaseModel):
    """The scan schedule -- a SPEC constant (`schedule.SCAN_WINDOWS`), not inferred."""

    model_config = ConfigDict(extra="forbid")

    windows: list[ScheduleWindowOut]
    inferred: Literal[False] = False
    source: str


class ConfigResponse(BaseModel):
    """`GET /api/system/config` body: the inferred config panel (design §4.2 P7, §8)."""

    model_config = ConfigDict(extra="forbid")

    watchlist: Inferred[list[str]]
    model: Inferred[str | None]
    macro_providers_configured: Inferred[bool | None] = Field(
        description="True when any of the last 10 Skeptic outputs is a real objection; "
        "null when there is no Skeptic output to judge by."
    )
    langfuse_configured: Inferred[bool]
    schedule: ScheduleOut
