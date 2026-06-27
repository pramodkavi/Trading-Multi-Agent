"""Hard risk-management gates (SPEC §1.6) — programmatic, non-overrideable.

These checks sit between the Analyzer and the Historian in the per-signal
pipeline (SPEC §4 Step 2.11). Per FR-1.3, a proposal that violates **any** hard
rule must become a SKIP with the violating rule logged — no agent's reasoning
can overrule them. The Skeptic and Judge only ever see proposals that already
cleared every gate, so they can never argue a hard-rule violation back into a
publish.

Design
------
* **Pure check functions.** Each ``check_*`` takes exactly the data it needs
  (the proposal, a session, a pre-fetched count) and returns a
  :class:`RiskCheckResult`. They do no IO and no clock reads, so every rule is
  unit-testable in isolation — the spec's "tests for every hard rule".
* **One IO seam.** :func:`gather_risk_context` is the only async function; it
  reads the journal (open setups, recent signals) through the backend-neutral
  :class:`~src.persistence.SignalStore` and packages the result into a frozen
  :class:`RiskContext`. :func:`evaluate_risk_gates` then runs all ten checks
  against the proposal + context with zero further IO.
* **Defense in depth.** Some rules are also enforced upstream — the Analyzer's
  own premium/discount gate (rule 3) and its conservative leverage default
  (rule 8). We re-check them here so a future strategy, the Critic's proposed
  rules, or a refactor cannot smuggle a violating proposal past policy. The
  Analyzer has **no** minimum-R:R or risk-cap gate, so rules 1/2 are first
  enforced here (see ``src/common/models/signal_proposal.py`` on the
  deliberate schema-vs-policy split).

The forced-skip categories and the ``violated_rule`` identifier strings live on
:class:`~src.common.models.SkipDecision` / :class:`SkipReason`, which were
defined ahead of this step for exactly this wiring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from src.common.models import (
    JudgeRuling,
    ScanSession,
    SignalDirection,
    SignalOutcome,
    SignalProposal,
    SignalStatus,
    SkipDecision,
    SkipReason,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable, Sequence

    from src.agents.orchestration.graph import AgentState
    from src.agents.orchestration.reservations import ScanReservationLedger
    from src.common.models import ScanContext
    from src.persistence import SignalStore, StoredSignal
    from src.providers import MarketSnapshot

# ---------------------------------------------------------------------------
# Policy thresholds (SPEC §1.6) — the single source of truth for the numbers
# ---------------------------------------------------------------------------

MAX_RISK_PERCENT: float = 1.0  # rule 1: max 1% equity risk per signal
MIN_RISK_REWARD: float = 3.0  # rule 2: minimum 1:3 R:R
MAX_CONCURRENT_SETUPS: int = 3  # rule 4: max 3 concurrent active signals
MAX_SIGNALS_PER_24H: int = 5  # rule 5: max 5 signals per rolling 24h
CONSECUTIVE_LOSS_LIMIT: int = 3  # rule 6: 3 consecutive losses -> pause
LOSS_PAUSE: timedelta = timedelta(hours=24)  # rule 6: 24h mandatory pause
SIGNAL_WINDOW: timedelta = timedelta(hours=24)  # rule 5: the rolling window
MAX_LEVERAGE: float = 10.0  # rule 8: max 10x leverage recommendation

# rule 7: sessions in which NO new signal may be opened (SPEC §1.6 / §1.7).
BLOCKED_SESSIONS: frozenset[ScanSession] = frozenset({ScanSession.ASIAN, ScanSession.COOLDOWN})

# rule 9: symbols that move together; stacking SAME-direction exposure across a
# group concentrates a single bet. Slice 2's watchlist correlates BTC/ETH; the
# tuple is open for the Critic / config to extend.
CORRELATION_GROUPS: tuple[frozenset[str], ...] = (frozenset({"BTCUSDT", "ETHUSDT"}),)

# rule 10: funding-cost heuristic. Perps fund every 8h (3x/day). We estimate the
# cost over a conservative assumed hold and reject only when it would consume an
# outsized share of the move to TP1. Numbers are deliberately conservative and
# flagged for calibration — the live path has no funding data until Step 2.2
# populates derivatives, so today this gate is a no-op (funding_rate is None).
FUNDING_PERIODS_PER_DAY: float = 3.0
ASSUMED_HOLD_DAYS: float = 1.0
FUNDING_COST_REWARD_FRACTION_LIMIT: float = 0.25

# Float comparison slack so a proposal sitting exactly on a boundary (e.g.
# risk_percent == 1.0, R:R == 3.0) passes rather than tripping on FP noise.
_EPS: float = 1e-9

# How many recent signals to pull for the rolling-window / loss-streak counts.
# The caps (<=5/24h, pause after 3 losses) mean a few dozen rows is ample; 200
# is generous headroom and matches list_recent_signals' design ceiling.
RECENT_FETCH_LIMIT: int = 200

# Per-rule identifiers persisted to SkipDecision.violated_rule (FR-1.3).
RULE_MAX_RISK = "RULE_1_MAX_RISK"
RULE_MIN_RR = "RULE_2_MIN_RR"
RULE_PREMIUM_DISCOUNT = "RULE_3_PREMIUM_DISCOUNT"
RULE_MAX_CONCURRENT = "RULE_4_MAX_CONCURRENT"
RULE_DAILY_CAP = "RULE_5_DAILY_CAP"
RULE_LOSS_STREAK = "RULE_6_LOSS_STREAK"
RULE_SESSION = "RULE_7_SESSION_BLOCK"
RULE_MAX_LEVERAGE = "RULE_8_MAX_LEVERAGE"
RULE_CORRELATED = "RULE_9_CORRELATED_EXPOSURE"
RULE_FUNDING = "RULE_10_FUNDING_COST"


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class RiskCheckResult(BaseModel):
    """Outcome of one hard-rule check.

    ``skip_reason`` is intrinsic to the rule (it categorises the rejection) and
    is carried whether the check passed or failed; it is only consumed when the
    check fails and the proposal is force-skipped.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(description="SPEC §1.6 identifier, e.g. 'RULE_2_MIN_RR'.")
    rule_name: str = Field(description="Short human label for logs/dashboards.")
    passed: bool = Field(description="True when the proposal satisfies this rule.")
    detail: str = Field(
        min_length=5,
        max_length=2000,
        description="Explanation citing the offending value; reads in the skip journal.",
    )
    skip_reason: SkipReason = Field(
        description="The categorical SkipReason this rule maps to when it fails.",
    )


class RiskGateReport(BaseModel):
    """Aggregate of every hard-rule check for one proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checks: tuple[RiskCheckResult, ...] = Field(
        description="One entry per hard rule, in SPEC §1.6 order.",
    )

    @property
    def passed(self) -> bool:
        """True only when every hard rule passed."""
        return all(c.passed for c in self.checks)

    @property
    def violations(self) -> tuple[RiskCheckResult, ...]:
        """The failing checks, in evaluation order."""
        return tuple(c for c in self.checks if not c.passed)

    @property
    def first_violation(self) -> RiskCheckResult | None:
        """The first failing check (drives the forced SkipDecision), or None."""
        violations = self.violations
        return violations[0] if violations else None


class RiskContext(BaseModel):
    """Pre-fetched journal/market state the stateful gates need.

    Built by :func:`gather_risk_context` (the only IO) so that
    :func:`evaluate_risk_gates` and every ``check_*`` stay pure. ``open_exposure``
    is the (symbol, direction) of each currently-open setup, used by the
    correlated-exposure rule.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    now: datetime = Field(description="Evaluation wall-clock (UTC); the rolling-window anchor.")
    open_setup_count: int = Field(ge=0, description="Number of OPEN active setups (rule 4).")
    published_last_24h: int = Field(
        ge=0, description="PUBLISHED signals created within the last 24h (rule 5)."
    )
    consecutive_losses: int = Field(
        ge=0, description="Leading run of LOSS outcomes among resolved signals (rule 6)."
    )
    latest_loss_at: datetime | None = Field(
        default=None,
        description="Timestamp of the most recent loss in that run; None if no losses (rule 6).",
    )
    open_exposure: tuple[tuple[str, SignalDirection], ...] = Field(
        default=(),
        description="(symbol, direction) of each open setup, for the correlation rule (rule 9).",
    )
    funding_rate: float | None = Field(
        default=None,
        description="Perp funding rate from the snapshot; None when not fetched (rule 10).",
    )


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


def _result(
    *, rule_id: str, rule_name: str, passed: bool, detail: str, skip_reason: SkipReason
) -> RiskCheckResult:
    return RiskCheckResult(
        rule_id=rule_id,
        rule_name=rule_name,
        passed=passed,
        detail=detail,
        skip_reason=skip_reason,
    )


# ---------------------------------------------------------------------------
# Rules 1, 2, 8 — pure functions of the proposal
# ---------------------------------------------------------------------------


def check_max_risk_percent(proposal: SignalProposal) -> RiskCheckResult:
    """Rule 1: at most 1% of equity at risk per signal."""
    ok = proposal.risk_percent <= MAX_RISK_PERCENT + _EPS
    detail = (
        f"risk_percent {proposal.risk_percent:.3f}% within the {MAX_RISK_PERCENT:.0f}% cap"
        if ok
        else f"risk_percent {proposal.risk_percent:.3f}% exceeds the {MAX_RISK_PERCENT:.0f}% cap"
    )
    return _result(
        rule_id=RULE_MAX_RISK,
        rule_name="max equity risk",
        passed=ok,
        detail=detail,
        skip_reason=SkipReason.EXCESSIVE_RISK,
    )


def check_min_risk_reward(proposal: SignalProposal) -> RiskCheckResult:
    """Rule 2: minimum 1:3 reward-to-risk."""
    ok = proposal.risk_reward_ratio >= MIN_RISK_REWARD - _EPS
    detail = (
        f"R:R {proposal.risk_reward_ratio:.2f} meets the {MIN_RISK_REWARD:.0f}:1 minimum"
        if ok
        else f"R:R {proposal.risk_reward_ratio:.2f} below the {MIN_RISK_REWARD:.0f}:1 minimum"
    )
    return _result(
        rule_id=RULE_MIN_RR,
        rule_name="minimum risk-reward",
        passed=ok,
        detail=detail,
        skip_reason=SkipReason.INSUFFICIENT_RR,
    )


def check_max_leverage(proposal: SignalProposal) -> RiskCheckResult:
    """Rule 8: recommended leverage capped at 10x."""
    ok = proposal.leverage <= MAX_LEVERAGE + _EPS
    detail = (
        f"leverage {proposal.leverage:.1f}x within the {MAX_LEVERAGE:.0f}x cap"
        if ok
        else f"leverage {proposal.leverage:.1f}x exceeds the {MAX_LEVERAGE:.0f}x cap"
    )
    return _result(
        rule_id=RULE_MAX_LEVERAGE,
        rule_name="max leverage",
        passed=ok,
        detail=detail,
        skip_reason=SkipReason.LEVERAGE_CAP,
    )


# ---------------------------------------------------------------------------
# Rule 3 — premium/discount, read from the proposal's features
# ---------------------------------------------------------------------------


def check_premium_discount(proposal: SignalProposal) -> RiskCheckResult:
    """Rule 3: longs only in Discount, shorts only in Premium.

    Reads the zone the Analyzer stamped into ``features['zone']``. Fails closed:
    if the zone is missing, EQUILIBRIUM, or UNKNOWN we cannot certify the hard
    rule, so we skip rather than assume compliance.
    """
    zone = str(proposal.features.get("zone", "")).upper()
    if proposal.direction is SignalDirection.LONG:
        ok = zone == "DISCOUNT"
        requirement = "LONG requires price in DISCOUNT"
    else:
        ok = zone == "PREMIUM"
        requirement = "SHORT requires price in PREMIUM"
    shown_zone = zone or "UNKNOWN"
    detail = (
        f"{requirement}; zone is {shown_zone}"
        if ok
        else f"{requirement}; zone is {shown_zone} — premium/discount violation"
    )
    return _result(
        rule_id=RULE_PREMIUM_DISCOUNT,
        rule_name="premium/discount",
        passed=ok,
        detail=detail,
        skip_reason=SkipReason.PREMIUM_DISCOUNT_VIOLATION,
    )


# ---------------------------------------------------------------------------
# Rule 7 — session window
# ---------------------------------------------------------------------------


def check_session(session: ScanSession) -> RiskCheckResult:
    """Rule 7: no new signals in the Asian (00-08) or Cooldown (21-00) windows."""
    ok = session not in BLOCKED_SESSIONS
    detail = (
        f"{session.value} session permits new signals"
        if ok
        else f"{session.value} session is blocked for new signals"
    )
    return _result(
        rule_id=RULE_SESSION,
        rule_name="session window",
        passed=ok,
        detail=detail,
        skip_reason=SkipReason.SESSION_BLOCKED,
    )


# ---------------------------------------------------------------------------
# Rules 4, 5, 6, 9 — pure functions of pre-fetched journal state
# ---------------------------------------------------------------------------


def check_max_concurrent(open_setup_count: int) -> RiskCheckResult:
    """Rule 4: at most 3 concurrent active signals (the 4th is blocked)."""
    ok = open_setup_count < MAX_CONCURRENT_SETUPS
    detail = (
        f"{open_setup_count} open setup(s), under the {MAX_CONCURRENT_SETUPS} concurrent cap"
        if ok
        else f"{open_setup_count} open setup(s) already at/over the "
        f"{MAX_CONCURRENT_SETUPS} concurrent cap"
    )
    return _result(
        rule_id=RULE_MAX_CONCURRENT,
        rule_name="max concurrent setups",
        passed=ok,
        detail=detail,
        skip_reason=SkipReason.CONCURRENT_SETUPS_LIMIT,
    )


def check_daily_cap(published_last_24h: int) -> RiskCheckResult:
    """Rule 5: at most 5 signals published in any rolling 24h window."""
    ok = published_last_24h < MAX_SIGNALS_PER_24H
    detail = (
        f"{published_last_24h} signal(s) in the last 24h, under the {MAX_SIGNALS_PER_24H} cap"
        if ok
        else f"{published_last_24h} signal(s) in the last 24h, at/over the "
        f"{MAX_SIGNALS_PER_24H} cap"
    )
    return _result(
        rule_id=RULE_DAILY_CAP,
        rule_name="daily signal cap",
        passed=ok,
        detail=detail,
        skip_reason=SkipReason.DAILY_SIGNAL_CAP,
    )


def check_loss_streak(
    *, consecutive_losses: int, latest_loss_at: datetime | None, now: datetime
) -> RiskCheckResult:
    """Rule 6: 3 consecutive losses trigger a mandatory 24h pause.

    Fails while the streak is at the limit AND the most recent loss is within
    the 24h pause window. Once 24h elapse since that loss, the pause lifts.
    """
    at_limit = consecutive_losses >= CONSECUTIVE_LOSS_LIMIT
    paused = False
    if at_limit and latest_loss_at is not None and (now - latest_loss_at) < LOSS_PAUSE:
        paused = True
        resumes_at = latest_loss_at + LOSS_PAUSE
        detail = (
            f"{consecutive_losses} consecutive losses; in mandatory 24h pause until "
            f"{resumes_at.isoformat()}"
        )
    elif at_limit:
        detail = f"{consecutive_losses} consecutive losses; 24h pause window has elapsed"
    else:
        detail = f"{consecutive_losses} consecutive loss(es); not in a loss-streak pause"
    return _result(
        rule_id=RULE_LOSS_STREAK,
        rule_name="consecutive-loss pause",
        passed=not paused,
        detail=detail,
        skip_reason=SkipReason.LOSS_STREAK_PAUSE,
    )


def check_correlated_exposure(
    *,
    candidate_symbol: str,
    candidate_direction: SignalDirection,
    open_exposure: Sequence[tuple[str, SignalDirection]],
    groups: tuple[frozenset[str], ...] = CORRELATION_GROUPS,
) -> RiskCheckResult:
    """Rule 9: no stacking SAME-direction exposure across a correlated group.

    Fails when an already-open setup on a *different* symbol in the same
    correlation group as the candidate shares the candidate's direction (e.g.
    a new BTC long while ETH is already long).
    """
    candidate = candidate_symbol.upper()
    correlated_symbols = {s for group in groups if candidate in group for s in group}
    clash = next(
        (
            sym
            for sym, direction in open_exposure
            if sym.upper() != candidate
            and sym.upper() in correlated_symbols
            and direction is candidate_direction
        ),
        None,
    )
    ok = clash is None
    detail = (
        f"no correlated same-direction exposure for {candidate} {candidate_direction.value}"
        if ok
        else f"{candidate} {candidate_direction.value} stacks correlated exposure with open "
        f"{clash} {candidate_direction.value}"
    )
    return _result(
        rule_id=RULE_CORRELATED,
        rule_name="correlated exposure",
        passed=ok,
        detail=detail,
        skip_reason=SkipReason.CORRELATED_EXPOSURE,
    )


def check_funding_cost(proposal: SignalProposal, funding_rate: float | None) -> RiskCheckResult:
    """Rule 10: reject when funding cost over the hold would erode the edge.

    Cost is borne only when the trade pays funding (a long with positive
    funding, a short with negative funding). We estimate the cost over a
    conservative assumed hold and reject only if it would consume more than a
    quarter of the move to TP1. Missing funding data (the live path until Step
    2.2) passes — we never block on absent data (cf. the Skeptic's FR-4.3
    graceful degradation).
    """
    if funding_rate is None:
        return _result(
            rule_id=RULE_FUNDING,
            rule_name="funding cost",
            passed=True,
            detail="funding rate unavailable; cost not evaluated",
            skip_reason=SkipReason.FUNDING_COST_PROHIBITIVE,
        )

    pays_funding = (proposal.direction is SignalDirection.LONG and funding_rate > 0) or (
        proposal.direction is SignalDirection.SHORT and funding_rate < 0
    )
    if not pays_funding:
        return _result(
            rule_id=RULE_FUNDING,
            rule_name="funding cost",
            passed=True,
            detail=(
                f"{proposal.direction.value} receives funding at rate {funding_rate:.5f}; "
                "no holding cost"
            ),
            skip_reason=SkipReason.FUNDING_COST_PROHIBITIVE,
        )

    periods = FUNDING_PERIODS_PER_DAY * ASSUMED_HOLD_DAYS
    cost_fraction = abs(funding_rate) * periods
    reward_fraction = abs(proposal.take_profit_1 - proposal.entry_price) / proposal.entry_price
    limit = reward_fraction * FUNDING_COST_REWARD_FRACTION_LIMIT
    ok = cost_fraction <= limit + _EPS
    detail = (
        f"funding cost ~{cost_fraction * 100:.2f}% of notional over ~{ASSUMED_HOLD_DAYS:.0f}d "
        f"within {FUNDING_COST_REWARD_FRACTION_LIMIT:.0%} of the {reward_fraction * 100:.2f}% "
        "target move"
        if ok
        else f"funding cost ~{cost_fraction * 100:.2f}% of notional over ~{ASSUMED_HOLD_DAYS:.0f}d "
        f"exceeds {FUNDING_COST_REWARD_FRACTION_LIMIT:.0%} of the {reward_fraction * 100:.2f}% "
        "target move"
    )
    return _result(
        rule_id=RULE_FUNDING,
        rule_name="funding cost",
        passed=ok,
        detail=detail,
        skip_reason=SkipReason.FUNDING_COST_PROHIBITIVE,
    )


# ---------------------------------------------------------------------------
# Pure journal-state helpers (feed RiskContext)
# ---------------------------------------------------------------------------


def published_within(signals: Sequence[StoredSignal], *, since: datetime) -> int:
    """Count PUBLISHED signals created at/after ``since`` (rule 5 input)."""
    return sum(1 for s in signals if s.status is SignalStatus.PUBLISHED and s.created_at >= since)


def loss_streak(signals: Sequence[StoredSignal]) -> tuple[int, datetime | None]:
    """Leading run of LOSS outcomes among *resolved* signals (rule 6 input).

    ``signals`` must be most-recent-first (``list_recent_signals`` order). Only
    PUBLISHED rows with a terminal outcome count; OPEN/unresolved and SKIPPED
    rows are ignored (they are neither wins nor losses). The streak ends at the
    first resolved non-LOSS outcome. The returned timestamp is the most recent
    loss's ``created_at`` — a proxy for resolution time (the journal does not
    store a separate resolved-at column today).
    """
    streak = 0
    latest_loss_at: datetime | None = None
    for s in signals:
        if s.status is not SignalStatus.PUBLISHED or s.outcome is None:
            continue  # not a resolved trade — skip, do not break the streak
        if s.outcome is SignalOutcome.LOSS:
            streak += 1
            if latest_loss_at is None:
                latest_loss_at = s.created_at
        else:
            break
    return streak, latest_loss_at


# ---------------------------------------------------------------------------
# Aggregate evaluation (pure) + forced-skip construction
# ---------------------------------------------------------------------------


def evaluate_risk_gates(
    proposal: SignalProposal,
    scan_context: ScanContext,
    context: RiskContext,
) -> RiskGateReport:
    """Run all ten SPEC §1.6 hard rules; pure given a pre-built RiskContext."""
    checks = (
        check_max_risk_percent(proposal),
        check_min_risk_reward(proposal),
        check_premium_discount(proposal),
        check_max_concurrent(context.open_setup_count),
        check_daily_cap(context.published_last_24h),
        check_loss_streak(
            consecutive_losses=context.consecutive_losses,
            latest_loss_at=context.latest_loss_at,
            now=context.now,
        ),
        check_session(scan_context.session),
        check_max_leverage(proposal),
        check_correlated_exposure(
            candidate_symbol=proposal.symbol,
            candidate_direction=proposal.direction,
            open_exposure=context.open_exposure,
        ),
        check_funding_cost(proposal, context.funding_rate),
    )
    return RiskGateReport(checks=checks)


def to_skip_decision(proposal: SignalProposal, report: RiskGateReport) -> SkipDecision:
    """Build the forced SkipDecision for a proposal that failed a hard rule.

    Uses the first violation for the categorical reason + rule id (FR-1.3). The
    ``details`` enumerate every violation so the journal records the full set,
    not just the first, and embed the proposal's identity for the Critic.
    """
    first = report.first_violation
    if first is None:  # pragma: no cover - callers guard on report.passed
        raise ValueError("to_skip_decision called on a report with no violations")
    all_detail = "; ".join(f"{c.rule_id}: {c.detail}" for c in report.violations)
    details = (
        f"{proposal.symbol} {proposal.direction.value} force-skipped by hard rule(s). {all_detail}"
    )
    return SkipDecision(
        scan_id=proposal.scan_id,
        strategy=proposal.strategy,
        symbol=proposal.symbol,
        reason=first.skip_reason,
        details=details[:2000],
        violated_rule=first.rule_id,
    )


# ---------------------------------------------------------------------------
# IO seam: gather journal/market state into a RiskContext
# ---------------------------------------------------------------------------


async def gather_risk_context(
    *,
    store: SignalStore,
    snapshot: MarketSnapshot,
    now: datetime,
) -> RiskContext:
    """Read the journal + snapshot to build the stateful-rule inputs.

    The only IO in this module. Open setups are bounded by rule 4 (<= a few), so
    fetching each one's signal to learn its (symbol, direction) is cheap. Recent
    signals power the rolling-window and loss-streak counts.
    """
    open_setups = await store.list_open_active_setups()
    open_exposure: list[tuple[str, SignalDirection]] = []
    for setup in open_setups:
        signal = await store.get_signal(setup.signal_id)
        if (
            signal is not None
            and signal.status is SignalStatus.PUBLISHED
            and signal.direction is not None
        ):
            open_exposure.append((signal.symbol, signal.direction))

    recent = await store.list_recent_signals(limit=RECENT_FETCH_LIMIT)
    published_last_24h = published_within(recent, since=now - SIGNAL_WINDOW)
    consecutive_losses, latest_loss_at = loss_streak(recent)

    return RiskContext(
        now=now,
        open_setup_count=len(open_setups),
        published_last_24h=published_last_24h,
        consecutive_losses=consecutive_losses,
        latest_loss_at=latest_loss_at,
        open_exposure=tuple(open_exposure),
        funding_rate=snapshot.funding_rate,
    )


# ---------------------------------------------------------------------------
# LangGraph node factory
# ---------------------------------------------------------------------------


def make_risk_gate_node(
    store: SignalStore,
    *,
    clock: Callable[[], datetime] | None = None,
    reservations: ScanReservationLedger | None = None,
) -> Callable[[AgentState], Awaitable[AgentState]]:
    """Build the risk-gate node bound to ``store`` (SPEC §4 Step 2.11).

    The node runs only for a real ``SignalProposal`` (the analyzer's conditional
    edge already routes skips straight to END). On a violation it replaces the
    proposal with a forced :class:`SkipDecision`, stamps ``decision = SKIP``, and
    preserves the original proposal under ``rejected_proposal`` so the journal
    still records what the Analyzer produced (FR-1.7). On a pass it leaves the
    proposal untouched and records the report.

    ``clock`` is injectable so tests can pin "now" for the loss-streak window.

    ``reservations`` (Step 2.13) keeps the stateful caps exact under parallel
    multi-symbol scans. When supplied, the read → evaluate → reserve sequence runs
    inside the ledger's lock and the DB-derived counts are augmented with pending
    reservations from sibling symbols, so two concurrent gates can never both
    clear a cap on the same stale snapshot. A passing proposal reserves a slot the
    scan runner releases iff the symbol ends up not publishing. When ``None`` (the
    single-symbol CLI path and the unit tests) the gate behaves exactly as in
    Step 2.11 — no lock, no reservation.
    """
    now_fn: Callable[[], datetime] = clock if clock is not None else _default_now

    async def risk_gate_node(state: AgentState) -> AgentState:
        proposal = state.get("proposal")
        if not isinstance(proposal, SignalProposal):
            return {}  # defensive: routing prevents this; nothing to gate

        scan_context = state["scan_context"]
        snapshot = state["snapshot"]

        if reservations is None:
            context = await gather_risk_context(store=store, snapshot=snapshot, now=now_fn())
            report = evaluate_risk_gates(proposal, scan_context, context)
        else:
            # Serialize read → evaluate → reserve so concurrent symbols cannot both
            # clear a stateful cap on the same stale counts (Step 2.13). A passing
            # proposal reserves a pending slot the next gate counts via augment().
            async with reservations.lock:
                db_context = await gather_risk_context(store=store, snapshot=snapshot, now=now_fn())
                context = reservations.augment(db_context)
                report = evaluate_risk_gates(proposal, scan_context, context)
                if report.passed:
                    reservations.reserve(
                        scan_id=scan_context.scan_id,
                        symbol=proposal.symbol,
                        direction=proposal.direction,
                    )

        if report.passed:
            return {"risk_gate_report": report}

        skip = to_skip_decision(proposal, report)
        return {
            "proposal": skip,
            "decision": JudgeRuling.SKIP,
            "risk_gate_report": report,
            "rejected_proposal": proposal,
        }

    return risk_gate_node


def _default_now() -> datetime:
    return datetime.now(UTC)


# Public surface (the node factory, the report models, and every pure rule so
# tests / the Critic can call them directly).
__all__ = [
    "RiskCheckResult",
    "RiskContext",
    "RiskGateReport",
    "check_correlated_exposure",
    "check_daily_cap",
    "check_funding_cost",
    "check_loss_streak",
    "check_max_concurrent",
    "check_max_leverage",
    "check_max_risk_percent",
    "check_min_risk_reward",
    "check_premium_discount",
    "check_session",
    "evaluate_risk_gates",
    "gather_risk_context",
    "loss_streak",
    "make_risk_gate_node",
    "published_within",
    "to_skip_decision",
]
