"""Pure assembly helpers for the dashboard's data routes (design §4.2, §5.3, §3.4).

Everything here is a function of its arguments and the ``now`` it is handed:
no store, no clock reads except :func:`utcnow` (the one seam the routers call
and the tests monkeypatch), no HTTP. The routers (``routers/overview.py``,
``routers/setups.py``, ``routers/signals.py``) do the I/O through ``queries``
and the typed store methods, then hand the models to these helpers.

Three rules of the overview are decided here and nowhere else:

* **Risk numbers come from ``risk_gates``** (design §5.3): :func:`build_risk_budget`
  takes the :class:`RiskContext` that ``gather_risk_context`` built and runs the
  gate's own ``check_loss_streak`` / ``check_session`` over it, so the dashboard
  can never disagree with what the gate will see.
* **A window is a group of per-symbol scan rows.** The scan Lambda writes one
  ``scan_runs`` row per symbol per invocation, so :func:`window_outcome`
  aggregates the group (earliest start, worst status, sorted symbol union,
  first error) and :func:`bucket_windows` keys the groups by
  ``schedule.window_for``; an ``AD_HOC`` run belongs to no window.
* **Forecaster staleness is relative to the last *finished* sweep.** The
  Forecaster is a **separate** invocation of the scan Lambda
  (``{"mode": "forecaster"}``, ``SchedulingStack`` cron ``5 8,13,15,22``): it
  starts :data:`SWEEP_OFFSET` (2 min) after each scan fire time and, under
  NFR-4.2 (<= 30 s per setup, <= 3 open setups), finishes well inside
  :data:`SWEEP_RUN_BUDGET` (3 min). :func:`last_sweep_at` therefore returns the
  *start* of the most recent sweep whose budget has elapsed, looking across
  yesterday's and today's windows so the 22:05 sweep stays the reference until
  08:08 the next morning (no blind spot before the day's first window). An OPEN
  setup is stale when it was never evaluated or last evaluated before that
  start (:func:`count_stale`); a setup the sweep itself evaluated has
  ``last_evaluated_at >= sweep start`` and reads fresh, and while a sweep is
  still inside its budget the previous one is the reference, so a sweep in
  flight never produces false "stale" badges.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from src.agents.orchestration.risk_gates import (
    LOSS_PAUSE,
    RiskContext,
    check_loss_streak,
    check_session,
)
from src.common.models import AgentRole, ScanStatus, SignalDirection
from src.common.schedule import AD_HOC, SCAN_WINDOWS, next_scan_at, window_for, windows_for_day
from src.common.sessions import session_for
from src.dashboard.api import market
from src.dashboard.api.queries import JournalRow, SetupCard
from src.dashboard.api.schemas import (
    ChartOverlay,
    LifecycleStep,
    RiskBudget,
    SetupCardOut,
    SignalDetailOut,
    SignalRowOut,
    SymbolChip,
    WindowOutcome,
    WindowSummary,
)
from src.persistence.models import StoredActiveSetup, StoredAgentRun, StoredScanRun, StoredSignal

__all__ = [
    "AD_HOC_GROUP_TOLERANCE",
    "PRICE_NOT_FETCHED",
    "PRICE_UNAVAILABLE",
    "SWEEP_OFFSET",
    "SWEEP_RUN_BUDGET",
    "ZERO_RISK",
    "bucket_windows",
    "build_risk_budget",
    "chart_overlay",
    "count_stale",
    "last_scan_outcome",
    "last_sweep_at",
    "lifecycle_step",
    "rejected_proposal",
    "scans_on",
    "setup_card_for_signal",
    "setup_card_out",
    "signal_detail_out",
    "signal_row_out",
    "trim_output",
    "utcnow",
    "window_outcome",
]

logger = logging.getLogger(__name__)

# The Forecaster sweep is its own EventBridge schedule, `cron(5 8,13,15,22)` UTC in
# `infrastructure/stacks/scheduling_stack.py` (`{"mode": "forecaster"}`): it starts
# this long after each scan fire time (:03 -> :05). Mirrors that cron literal; the
# unit test `test_sweep_offset_matches_scheduling_stack` pins the two equal.
SWEEP_OFFSET = timedelta(minutes=2)

# How long a sweep is allowed before it counts as finished: NFR-4.2 budgets <= 30 s
# per setup with <= 3 open setups (rule 4), i.e. under ~2 min; 3 min is the margin.
SWEEP_RUN_BUDGET = timedelta(minutes=3)

# Per-symbol rows of one AD_HOC invocation start within seconds of each other; rows
# further apart than this are separate invocations (e.g. two console tests).
AD_HOC_GROUP_TOLERANCE = timedelta(minutes=5)

# Worst-first status precedence for a window's aggregate outcome.
_STATUS_RANK: dict[ScanStatus, int] = {
    ScanStatus.FAILED: 0,
    ScanStatus.RUNNING: 1,
    ScanStatus.SUCCESS: 2,
}


def utcnow() -> datetime:
    """The routers' clock (tests monkeypatch this module attribute)."""
    return datetime.now(UTC)


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _age_seconds(since: datetime, *, now: datetime) -> int:
    """Whole seconds from ``since`` to ``now``, never negative (clock skew / in-flight rows)."""
    return max(0, int((now - since).total_seconds()))


# ---------------------------------------------------------------------------
# Risk budget (design §5.3)
# ---------------------------------------------------------------------------


def build_risk_budget(ctx: RiskContext, *, now: datetime) -> RiskBudget:
    """The SPEC §1.6 caps as the gate will see them, from the gate's own context + checks.

    ``pause_until`` is set only while the loss-streak pause is active (rule 6):
    the latest loss plus the 24 h window. ``session`` / ``session_blocked`` come
    from the clock via ``sessions.session_for`` and the gate's ``check_session``
    (rule 7). Raises ``ValueError`` on a naive ``now``.
    """
    now_utc = _require_aware(now, "now")
    streak = check_loss_streak(
        consecutive_losses=ctx.consecutive_losses,
        latest_loss_at=ctx.latest_loss_at,
        now=now_utc,
    )
    pause_active = not streak.passed
    pause_until = (
        ctx.latest_loss_at + LOSS_PAUSE if pause_active and ctx.latest_loss_at is not None else None
    )
    session = session_for(now_utc)
    next_fire = next_scan_at(now_utc)
    return RiskBudget(
        open_setups=ctx.open_setup_count,
        published_24h=ctx.published_last_24h,
        loss_streak=ctx.consecutive_losses,
        pause_active=pause_active,
        pause_until=pause_until,
        session=session.value,
        session_blocked=not check_session(session).passed,
        next_scan_at=next_fire,
        seconds_to_next_scan=_age_seconds(now_utc, now=next_fire),
    )


# ---------------------------------------------------------------------------
# Scan windows (design §4.2 P1 "last scan per window")
# ---------------------------------------------------------------------------


def window_outcome(label: str, scans: Sequence[StoredScanRun]) -> WindowOutcome:
    """Aggregate the per-symbol ``scan_runs`` rows of one window / invocation.

    Earliest ``started_at``, worst ``status`` (FAILED > RUNNING > SUCCESS), the
    sorted union of ``symbols`` and the first ``error_message`` in start order.
    Raises ``ValueError`` when ``scans`` is empty (a window with no rows is
    ``None`` on the wire, never an empty outcome).
    """
    if not scans:
        raise ValueError("window_outcome needs at least one scan run")
    ordered = sorted(scans, key=lambda s: (s.started_at, str(s.id)))
    symbols = sorted({sym for s in ordered for sym in (s.symbols or [])})
    status = min((s.status for s in ordered), key=lambda st: _STATUS_RANK.get(st, 1))
    error = next((s.error_message for s in ordered if s.error_message), None)
    return WindowOutcome(
        label=label,
        started_at=ordered[0].started_at,
        status=status,
        symbols=symbols,
        error=error,
    )


def scans_on(scans: Sequence[StoredScanRun], day: date) -> list[StoredScanRun]:
    """The scan rows that started on the UTC date ``day`` (order preserved)."""
    return [s for s in scans if s.started_at.astimezone(UTC).date() == day]


def _chips_for(rows: Sequence[JournalRow], scan_ids: set[UUID]) -> list[SymbolChip]:
    chips = [
        SymbolChip(
            symbol=row.symbol,
            kind=row.decision_kind,
            violated_rule=row.violated_rule,
            signal_id=row.id,
        )
        for row in rows
        if row.scan_id in scan_ids
    ]
    return sorted(chips, key=lambda c: (c.symbol, str(c.signal_id)))


def bucket_windows(
    scans: Sequence[StoredScanRun], rows: Sequence[JournalRow], *, day: date
) -> list[WindowSummary]:
    """Today's four scheduled windows with their scan outcome and per-symbol chips.

    A scan row belongs to the window ``schedule.window_for`` assigns it (fire
    time to fire time + 30 min); ``AD_HOC`` rows -- console tests, retries hours
    later -- and rows from other UTC dates appear in no window. Chips are the
    journal rows of the window's scans, sorted by symbol.
    """
    todays = scans_on(scans, day)
    by_label: dict[str, list[StoredScanRun]] = {}
    for scan in todays:
        by_label.setdefault(window_for(scan.started_at), []).append(scan)

    summaries: list[WindowSummary] = []
    for window, fire_time in zip(SCAN_WINDOWS, windows_for_day(day), strict=True):
        group = by_label.get(window.label, [])
        summaries.append(
            WindowSummary(
                label=window.label,
                fire_time=fire_time,
                scan=window_outcome(window.label, group) if group else None,
                symbols=_chips_for(rows, {s.id for s in group}),
            )
        )
    return summaries


def last_scan_outcome(scans: Sequence[StoredScanRun]) -> WindowOutcome | None:
    """The most recent scan in ``scans`` as a window outcome, or ``None`` if there is none.

    Deliberately not bounded to a UTC day: the router fetches from the start of
    *yesterday*, so overnight (00:00-08:03) the badge shows yesterday's
    ``DAILY_WRAP`` rather than "no scan" -- the window list, not this badge, is
    what is scoped to today (:func:`bucket_windows`).

    The latest row names the group: every row of the same scheduled window **on
    the same UTC date** (yesterday's LONDON rows never join today's), or -- for
    an ``AD_HOC`` row -- the ``AD_HOC`` rows that started within
    :data:`AD_HOC_GROUP_TOLERANCE` before it (one invocation's per-symbol rows).
    """
    if not scans:
        return None
    latest = max(scans, key=lambda s: (s.started_at, str(s.id)))
    same_day = scans_on(scans, latest.started_at.astimezone(UTC).date())
    label = window_for(latest.started_at)
    if label == AD_HOC:
        group = [
            s
            for s in same_day
            if window_for(s.started_at) == AD_HOC
            and timedelta(0) <= latest.started_at - s.started_at <= AD_HOC_GROUP_TOLERANCE
        ]
    else:
        group = [s for s in same_day if window_for(s.started_at) == label]
    return window_outcome(label, group)


# ---------------------------------------------------------------------------
# Forecaster coverage
# ---------------------------------------------------------------------------


def last_sweep_at(now: datetime) -> datetime | None:
    """Start time of the most recent Forecaster sweep that has had time to finish.

    A sweep starts at ``fire + SWEEP_OFFSET`` for every scheduled window and
    counts once ``start + SWEEP_RUN_BUDGET <= now``. Yesterday's windows are
    considered too, so before today's first sweep (and overnight) the answer is
    yesterday's 22:05, not ``None``; ``None`` is only possible in the degenerate
    case of no finished sweep in the two-day span, which the four daily windows
    make unreachable. Raises ``ValueError`` on a naive ``now``.
    """
    now_utc = _require_aware(now, "now")
    today = now_utc.date()
    fires = [*windows_for_day(today - timedelta(days=1)), *windows_for_day(today)]
    finished = [
        fire + SWEEP_OFFSET for fire in fires if fire + SWEEP_OFFSET + SWEEP_RUN_BUDGET <= now_utc
    ]
    return finished[-1] if finished else None


def count_stale(cards: Sequence[SetupCard], *, sweep_at: datetime | None) -> int:
    """OPEN setups the last finished sweep did not cover.

    Never evaluated -> stale. Otherwise stale iff last evaluated before the
    sweep's start (``sweep_at``): a setup that sweep evaluated has
    ``last_evaluated_at >= sweep_at`` and reads fresh. With no reference sweep
    (``sweep_at is None``, degenerate) an evaluated setup cannot have missed one.
    """
    return sum(
        1
        for card in cards
        if card.last_evaluated_at is None
        or (sweep_at is not None and card.last_evaluated_at < sweep_at)
    )


# ---------------------------------------------------------------------------
# Setup cards
# ---------------------------------------------------------------------------


PRICE_UNAVAILABLE = "price unavailable"
PRICE_NOT_FETCHED = "not fetched on this view"
ZERO_RISK = "risk undefined (entry == stop)"


def setup_card_out(
    card: SetupCard,
    *,
    now: datetime,
    price: float | None,
    unavailable_note: str = PRICE_UNAVAILABLE,
) -> SetupCardOut:
    """Add the clock-relative ages, the latest verdict and the best-effort unrealised R.

    ``price`` is required so every caller decides: the latest close the router
    fetched (``market.latest_prices``), or ``None``. ``None`` leaves
    ``unrealised_r`` null with ``unavailable_note`` -- :data:`PRICE_UNAVAILABLE`
    (the default: provider down, budget blown, no candles) or, for a view that
    does not fetch prices at all (the lifecycle page), :data:`PRICE_NOT_FETCHED`
    so the UI never reports a phantom Binance outage. A degenerate
    ``entry == stop`` card (no risk unit) gets :data:`ZERO_RISK` instead of
    failing the page.
    """
    evaluation = card.latest_evaluation or {}
    status = evaluation.get("status")
    reasoning = evaluation.get("reasoning")
    r_value: float | None = None
    r_note: str | None = None
    if price is None:
        r_note = unavailable_note
    else:
        try:
            r_value = market.unrealised_r(card.direction, card.entry_price, card.stop_loss, price)
        except ValueError:
            r_note = ZERO_RISK
    return SetupCardOut(
        setup_id=card.setup_id,
        signal_id=card.signal_id,
        symbol=card.symbol,
        direction=card.direction,
        entry_price=card.entry_price,
        stop_loss=card.stop_loss,
        take_profit_1=card.take_profit_1,
        take_profit_2=card.take_profit_2,
        opened_at=card.opened_at,
        age_seconds=_age_seconds(card.opened_at, now=now),
        last_evaluated_at=card.last_evaluated_at,
        evaluation_age_seconds=(
            None
            if card.last_evaluated_at is None
            else _age_seconds(card.last_evaluated_at, now=now)
        ),
        latest_status=str(status) if status is not None else None,
        latest_reasoning=str(reasoning) if reasoning is not None else None,
        unrealised_r=r_value,
        unrealised_r_note=r_note,
    )


def setup_card_for_signal(setup: StoredActiveSetup, signal: StoredSignal) -> SetupCard | None:
    """A :class:`SetupCard` for the lifecycle page, prices lifted from the proposal payload.

    Returns ``None`` -- and logs a WARNING naming the setup, never silently --
    when the signal is a skip row (no direction / prices) or the payload lacks a
    price: a setup can only be opened for a proposal, so both are defensive
    branches for malformed data rather than expected paths.
    """
    if signal.direction is None:
        logger.warning(
            "setup %s skipped: signal %s is a skip row (no direction / prices)",
            setup.id,
            signal.id,
        )
        return None
    payload = signal.payload
    try:
        tp2 = payload.get("take_profit_2")
        return SetupCard(
            setup_id=setup.id,
            signal_id=signal.id,
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=float(payload["entry_price"]),
            stop_loss=float(payload["stop_loss"]),
            take_profit_1=float(payload["take_profit_1"]),
            take_profit_2=None if tp2 is None else float(tp2),
            opened_at=setup.opened_at,
            last_evaluated_at=setup.last_evaluated_at,
            latest_evaluation=setup.latest_evaluation,
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("setup %s skipped: malformed payload: %r", setup.id, exc)
        return None


# ---------------------------------------------------------------------------
# Signal chart overlay (Step 4.3)
# ---------------------------------------------------------------------------


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def chart_overlay(proposal: Mapping[str, Any]) -> ChartOverlay:
    """The chart levels of one ``SignalProposal`` JSON: prices plus the order-block POI.

    Lenient on purpose -- an older or malformed payload renders with the missing
    levels as ``None`` instead of failing the chart: the POI zone comes from
    ``features.ob_zone_low`` / ``ob_zone_high`` (the Analyzer's anchor), the
    direction is dropped when it is not a ``SignalDirection`` value.
    """
    features = proposal.get("features")
    features = features if isinstance(features, Mapping) else {}
    raw_direction = proposal.get("direction")
    try:
        direction = SignalDirection(raw_direction) if raw_direction is not None else None
    except ValueError:
        direction = None
    return ChartOverlay(
        direction=direction,
        entry_price=_opt_float(proposal.get("entry_price")),
        stop_loss=_opt_float(proposal.get("stop_loss")),
        take_profit_1=_opt_float(proposal.get("take_profit_1")),
        take_profit_2=_opt_float(proposal.get("take_profit_2")),
        poi_low=_opt_float(features.get("ob_zone_low")),
        poi_high=_opt_float(features.get("ob_zone_high")),
    )


def rejected_proposal(runs: Sequence[StoredAgentRun], *, symbol: str) -> Mapping[str, Any] | None:
    """The gate-rejected proposal: the scan's Analyzer run output that is a proposal.

    A gate rejection stores a skip row in ``signals``, but the Analyzer's own
    ``agent_runs`` output for the scan still holds the proposal the gate refused
    (``direction`` set). The latest such run for ``symbol`` wins; ``None`` when
    there is none (the chart then has no overlay).
    """
    candidates = [
        run.output
        for run in runs
        if run.agent_role is AgentRole.ANALYZER
        and "direction" in run.output
        and run.output.get("symbol", symbol) == symbol
    ]
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# Journal rows
# ---------------------------------------------------------------------------


def signal_row_out(row: JournalRow) -> SignalRowOut:
    """The trimmed journal row on the wire (field-for-field with :class:`JournalRow`)."""
    return SignalRowOut.model_validate(row.model_dump())


def signal_detail_out(row: JournalRow, signal: StoredSignal) -> SignalDetailOut:
    """The lifecycle's ``signal``: the journal row plus the full stored signal's JSON.

    Design §3.4: only here does ``payload`` / ``features`` / ``outcome_metadata``
    travel -- one row per request, so the 1 MB cap is not a concern.
    """
    return SignalDetailOut(
        **row.model_dump(),
        payload=signal.payload,
        features=signal.features,
        outcome_metadata=signal.outcome_metadata,
    )


# ---------------------------------------------------------------------------
# Lifecycle steps (design §3.4: trimmed on the timeline, full JSON per run)
# ---------------------------------------------------------------------------

_PROPOSAL_KEYS = (
    "direction",
    "entry_price",
    "stop_loss",
    "take_profit_1",
    "take_profit_2",
    "risk_reward_ratio",
    "tags",
)
_SKIP_KEYS = ("reason", "violated_rule", "details")
_HISTORIAN_KEYS = ("win_rate", "sample_size", "wins", "losses")
_MATCH_KEYS = ("signal_id", "symbol", "outcome")
_SKEPTIC_KEYS = ("severity", "recommends_against", "headline")
_JUDGE_KEYS = ("ruling", "confidence", "caveat")
# The Critic's `DecisionReview` fields (Critic v0 design §6.2); `lesson` is capped
# at 300 chars by the model itself, so the whole grade fits on the timeline.
_CRITIC_KEYS = (
    "smc_read_valid",
    "skeptic_vindicated",
    "judge_correct",
    "gate_correct",
    "failure_category",
    "lesson",
    "confidence",
)


def _pick(output: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {key: output.get(key) for key in keys}


def trim_output(role: AgentRole, output: Mapping[str, Any]) -> dict[str, Any]:
    """The timeline summary of one agent output; everything else is on the runs endpoint.

    - analyzer: proposal prices / R:R / tags, or a skip's reason / rule / details
      (a gate-rejected run holds the *rejected proposal*, so it summarises as one);
    - historian: the win-rate numbers and each match as ``{signal_id, symbol, outcome}``;
    - skeptic: severity / recommends_against / headline, or ``{"no_macro_data": true}``
      for the ``NoMacroData`` sentinel -- recognised by its own shape (a ``provider``
      key: it serialises as ``{provider, reason}``), so an objection row that merely
      lacks ``severity`` is a thin objection (nulls), never a false badge;
    - judge: ruling / confidence / caveat;
    - critic: the ``DecisionReview`` grade (``smc_read_valid`` ... ``confidence``);
    - any other role (the Forecaster writes ``setup_evaluations`` instead): ``{}``.

    Missing keys read as ``None`` rather than raising, so a row written by an older
    model version still renders.
    """
    if role is AgentRole.ANALYZER:
        return _pick(output, _PROPOSAL_KEYS if "direction" in output else _SKIP_KEYS)
    if role is AgentRole.HISTORIAN:
        raw_matches = output.get("matches")
        matches = (
            [_pick(m, _MATCH_KEYS) for m in raw_matches if isinstance(m, Mapping)]
            if isinstance(raw_matches, list)
            else []
        )
        return {**_pick(output, _HISTORIAN_KEYS), "matches": matches}
    if role is AgentRole.SKEPTIC:
        if "provider" in output:
            return {"no_macro_data": True}
        return _pick(output, _SKEPTIC_KEYS)
    if role is AgentRole.JUDGE:
        return _pick(output, _JUDGE_KEYS)
    if role is AgentRole.CRITIC:
        return _pick(output, _CRITIC_KEYS)
    return {}


def lifecycle_step(run: StoredAgentRun) -> LifecycleStep:
    """One timeline entry: accounting columns verbatim, output trimmed by role."""
    return LifecycleStep(
        role=run.agent_role.value,
        run_id=run.id,
        created_at=run.created_at,
        latency_ms=run.latency_ms,
        cost_usd=run.cost_usd,
        summary=trim_output(run.agent_role, run.output),
    )
