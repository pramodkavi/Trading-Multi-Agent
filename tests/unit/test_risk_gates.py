"""Unit tests for the hard risk gates (Slice 2 Step 2.11, SPEC §1.6).

Fully offline. Covers a pass and a fail for every one of the ten hard rules,
the pure journal-state helpers (loss_streak / published_within), the aggregate
evaluation, the forced-skip construction (FR-1.3), and the LangGraph node
factory end to end (pass-through and forced-skip) against a fake store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.agents.orchestration.risk_gates import (
    CORRELATION_GROUPS,
    MAX_CONCURRENT_SETUPS,
    MAX_SIGNALS_PER_24H,
    RiskContext,
    check_correlated_exposure,
    check_daily_cap,
    check_funding_cost,
    check_loss_streak,
    check_max_concurrent,
    check_max_leverage,
    check_max_risk_percent,
    check_min_risk_reward,
    check_premium_discount,
    check_session,
    evaluate_risk_gates,
    gather_risk_context,
    loss_streak,
    make_risk_gate_node,
    published_within,
    to_skip_decision,
)
from src.common.models import (
    JudgeRuling,
    ScanContext,
    ScanSession,
    SignalDirection,
    SignalOutcome,
    SignalProposal,
    SignalStatus,
    SkipDecision,
    SkipReason,
)
from src.persistence import StoredActiveSetup, StoredSignal
from src.providers import Kline, MarketSnapshot, Timeframe

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 15, 14, 0, 0, tzinfo=UTC)


def make_proposal(**overrides: Any) -> SignalProposal:
    """A compliant LONG proposal in DISCOUNT (clears every gate by default)."""
    base: dict[str, Any] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "direction": SignalDirection.LONG,
        "entry_price": 100.0,
        "stop_loss": 97.0,
        "take_profit_1": 109.0,
        "risk_reward_ratio": 3.0,
        "leverage": 3.0,
        "risk_percent": 1.0,
        "confluence_narrative": "Bullish OB in discount with a sweep below equal lows.",
        "features": {"zone": "DISCOUNT", "current_price": 99.0},
    }
    base.update(overrides)
    return SignalProposal(**base)


def make_context(**overrides: Any) -> RiskContext:
    base: dict[str, Any] = {
        "now": _NOW,
        "open_setup_count": 0,
        "published_last_24h": 0,
        "consecutive_losses": 0,
        "latest_loss_at": None,
        "open_exposure": (),
        "funding_rate": None,
    }
    base.update(overrides)
    return RiskContext(**base)


def make_scan_context(session: ScanSession = ScanSession.LONDON) -> ScanContext:
    return ScanContext(session=session, symbols=["BTCUSDT"], strategy="smc")


def make_stored_signal(
    *,
    status: SignalStatus = SignalStatus.PUBLISHED,
    outcome: SignalOutcome | None = None,
    created_at: datetime,
    direction: SignalDirection | None = SignalDirection.LONG,
    symbol: str = "BTCUSDT",
) -> StoredSignal:
    return StoredSignal(
        id=uuid4(),
        scan_id=uuid4(),
        symbol=symbol,
        strategy="smc",
        direction=direction if status is SignalStatus.PUBLISHED else None,
        status=status,
        created_at=created_at,
        payload={},
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# Rule 1 — max risk percent
# ---------------------------------------------------------------------------


def test_rule1_risk_within_cap_passes() -> None:
    assert check_max_risk_percent(make_proposal(risk_percent=1.0)).passed


def test_rule1_risk_over_cap_fails() -> None:
    result = check_max_risk_percent(make_proposal(risk_percent=2.5))
    assert not result.passed
    assert result.skip_reason is SkipReason.EXCESSIVE_RISK
    assert result.rule_id == "RULE_1_MAX_RISK"


# ---------------------------------------------------------------------------
# Rule 2 — minimum risk-reward
# ---------------------------------------------------------------------------


def test_rule2_rr_at_minimum_passes() -> None:
    assert check_min_risk_reward(make_proposal(risk_reward_ratio=3.0)).passed


def test_rule2_rr_below_minimum_fails() -> None:
    # Geometry must imply the sub-3 R:R (the schema cross-checks it): risk 3, reward ~7.65.
    proposal = make_proposal(
        entry_price=100.0, stop_loss=97.0, take_profit_1=107.65, risk_reward_ratio=2.55
    )
    result = check_min_risk_reward(proposal)
    assert not result.passed
    assert result.skip_reason is SkipReason.INSUFFICIENT_RR


# ---------------------------------------------------------------------------
# Rule 3 — premium/discount
# ---------------------------------------------------------------------------


def test_rule3_long_in_discount_passes() -> None:
    assert check_premium_discount(
        make_proposal(direction=SignalDirection.LONG, features={"zone": "DISCOUNT"})
    ).passed


def test_rule3_long_in_premium_fails() -> None:
    result = check_premium_discount(
        make_proposal(direction=SignalDirection.LONG, features={"zone": "PREMIUM"})
    )
    assert not result.passed
    assert result.skip_reason is SkipReason.PREMIUM_DISCOUNT_VIOLATION


def test_rule3_short_in_premium_passes() -> None:
    short = make_proposal(
        direction=SignalDirection.SHORT,
        entry_price=100.0,
        stop_loss=103.0,
        take_profit_1=91.0,
        risk_reward_ratio=3.0,
        features={"zone": "PREMIUM"},
    )
    assert check_premium_discount(short).passed


def test_rule3_unknown_zone_fails_closed() -> None:
    # Missing/UNKNOWN zone cannot certify the rule -> fail closed.
    assert not check_premium_discount(make_proposal(features={})).passed
    assert not check_premium_discount(make_proposal(features={"zone": "EQUILIBRIUM"})).passed


# ---------------------------------------------------------------------------
# Rule 4 — max concurrent setups
# ---------------------------------------------------------------------------


def test_rule4_under_cap_passes() -> None:
    assert check_max_concurrent(MAX_CONCURRENT_SETUPS - 1).passed


def test_rule4_at_cap_fails() -> None:
    result = check_max_concurrent(MAX_CONCURRENT_SETUPS)
    assert not result.passed
    assert result.skip_reason is SkipReason.CONCURRENT_SETUPS_LIMIT


# ---------------------------------------------------------------------------
# Rule 5 — daily signal cap
# ---------------------------------------------------------------------------


def test_rule5_under_cap_passes() -> None:
    assert check_daily_cap(MAX_SIGNALS_PER_24H - 1).passed


def test_rule5_at_cap_fails() -> None:
    result = check_daily_cap(MAX_SIGNALS_PER_24H)
    assert not result.passed
    assert result.skip_reason is SkipReason.DAILY_SIGNAL_CAP


# ---------------------------------------------------------------------------
# Rule 6 — consecutive-loss pause
# ---------------------------------------------------------------------------


def test_rule6_three_recent_losses_pauses() -> None:
    result = check_loss_streak(
        consecutive_losses=3, latest_loss_at=_NOW - timedelta(hours=2), now=_NOW
    )
    assert not result.passed
    assert result.skip_reason is SkipReason.LOSS_STREAK_PAUSE


def test_rule6_two_losses_does_not_pause() -> None:
    assert check_loss_streak(
        consecutive_losses=2, latest_loss_at=_NOW - timedelta(hours=2), now=_NOW
    ).passed


def test_rule6_pause_lifts_after_24h() -> None:
    # Three losses but the most recent was >24h ago -> the pause has elapsed.
    assert check_loss_streak(
        consecutive_losses=3, latest_loss_at=_NOW - timedelta(hours=25), now=_NOW
    ).passed


# ---------------------------------------------------------------------------
# Rule 7 — session window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("session", [ScanSession.LONDON, ScanSession.NY, ScanSession.OVERLAP])
def test_rule7_open_sessions_pass(session: ScanSession) -> None:
    assert check_session(session).passed


@pytest.mark.parametrize("session", [ScanSession.ASIAN, ScanSession.COOLDOWN])
def test_rule7_blocked_sessions_fail(session: ScanSession) -> None:
    result = check_session(session)
    assert not result.passed
    assert result.skip_reason is SkipReason.SESSION_BLOCKED


# ---------------------------------------------------------------------------
# Rule 8 — max leverage
# ---------------------------------------------------------------------------


def test_rule8_at_cap_passes() -> None:
    assert check_max_leverage(make_proposal(leverage=10.0)).passed


def test_rule8_over_cap_fails() -> None:
    result = check_max_leverage(make_proposal(leverage=12.0))
    assert not result.passed
    assert result.skip_reason is SkipReason.LEVERAGE_CAP


# ---------------------------------------------------------------------------
# Rule 9 — correlated exposure
# ---------------------------------------------------------------------------


def test_rule9_same_direction_correlated_fails() -> None:
    result = check_correlated_exposure(
        candidate_symbol="BTCUSDT",
        candidate_direction=SignalDirection.LONG,
        open_exposure=[("ETHUSDT", SignalDirection.LONG)],
    )
    assert not result.passed
    assert result.skip_reason is SkipReason.CORRELATED_EXPOSURE


def test_rule9_opposite_direction_correlated_passes() -> None:
    assert check_correlated_exposure(
        candidate_symbol="BTCUSDT",
        candidate_direction=SignalDirection.LONG,
        open_exposure=[("ETHUSDT", SignalDirection.SHORT)],
    ).passed


def test_rule9_uncorrelated_symbol_passes() -> None:
    assert check_correlated_exposure(
        candidate_symbol="BTCUSDT",
        candidate_direction=SignalDirection.LONG,
        open_exposure=[("SOLUSDT", SignalDirection.LONG)],
    ).passed


def test_rule9_no_open_exposure_passes() -> None:
    assert check_correlated_exposure(
        candidate_symbol="BTCUSDT",
        candidate_direction=SignalDirection.LONG,
        open_exposure=[],
    ).passed


def test_correlation_groups_contain_btc_eth() -> None:
    assert any({"BTCUSDT", "ETHUSDT"} <= group for group in CORRELATION_GROUPS)


# ---------------------------------------------------------------------------
# Rule 10 — funding cost
# ---------------------------------------------------------------------------


def test_rule10_no_funding_data_passes() -> None:
    assert check_funding_cost(make_proposal(), None).passed


def test_rule10_receiving_funding_passes() -> None:
    # A long with NEGATIVE funding receives funding -> never a holding cost.
    assert check_funding_cost(make_proposal(direction=SignalDirection.LONG), -0.01).passed


def test_rule10_small_paid_funding_passes() -> None:
    # Long pays small positive funding (0.01%/8h -> ~0.03%/day) vs a 9% target move.
    assert check_funding_cost(make_proposal(), 0.0001).passed


def test_rule10_large_paid_funding_fails() -> None:
    # Long pays 1%/8h -> ~3%/day, which exceeds 25% of the 9% target move.
    result = check_funding_cost(make_proposal(), 0.01)
    assert not result.passed
    assert result.skip_reason is SkipReason.FUNDING_COST_PROHIBITIVE


# ---------------------------------------------------------------------------
# Pure journal-state helpers
# ---------------------------------------------------------------------------


def test_published_within_counts_only_recent_published() -> None:
    signals = [
        make_stored_signal(created_at=_NOW - timedelta(hours=1)),
        make_stored_signal(created_at=_NOW - timedelta(hours=23)),
        make_stored_signal(created_at=_NOW - timedelta(hours=30)),  # outside window
        make_stored_signal(
            status=SignalStatus.SKIPPED, created_at=_NOW - timedelta(hours=2)
        ),  # not published
    ]
    assert published_within(signals, since=_NOW - timedelta(hours=24)) == 2


def test_loss_streak_counts_leading_losses() -> None:
    # Most-recent-first: LOSS, LOSS, LOSS, WIN -> streak of 3, latest = newest loss.
    newest = _NOW - timedelta(hours=1)
    signals = [
        make_stored_signal(outcome=SignalOutcome.LOSS, created_at=newest),
        make_stored_signal(outcome=SignalOutcome.LOSS, created_at=_NOW - timedelta(hours=5)),
        make_stored_signal(outcome=SignalOutcome.LOSS, created_at=_NOW - timedelta(hours=9)),
        make_stored_signal(outcome=SignalOutcome.WIN, created_at=_NOW - timedelta(hours=13)),
    ]
    streak, latest = loss_streak(signals)
    assert streak == 3
    assert latest == newest


def test_loss_streak_win_breaks_the_run() -> None:
    signals = [
        make_stored_signal(outcome=SignalOutcome.LOSS, created_at=_NOW - timedelta(hours=1)),
        make_stored_signal(outcome=SignalOutcome.WIN, created_at=_NOW - timedelta(hours=5)),
        make_stored_signal(outcome=SignalOutcome.LOSS, created_at=_NOW - timedelta(hours=9)),
    ]
    streak, _ = loss_streak(signals)
    assert streak == 1


def test_loss_streak_skips_unresolved_signals() -> None:
    # An OPEN (unresolved) signal between losses must not break the streak.
    signals = [
        make_stored_signal(outcome=SignalOutcome.LOSS, created_at=_NOW - timedelta(hours=1)),
        make_stored_signal(outcome=None, created_at=_NOW - timedelta(hours=3)),  # open
        make_stored_signal(outcome=SignalOutcome.LOSS, created_at=_NOW - timedelta(hours=5)),
        make_stored_signal(outcome=SignalOutcome.LOSS, created_at=_NOW - timedelta(hours=9)),
    ]
    streak, _ = loss_streak(signals)
    assert streak == 3


# ---------------------------------------------------------------------------
# Aggregate evaluation + forced skip
# ---------------------------------------------------------------------------


def test_evaluate_all_pass_for_compliant_proposal() -> None:
    report = evaluate_risk_gates(make_proposal(), make_scan_context(), make_context())
    assert report.passed
    assert len(report.checks) == 10
    assert report.first_violation is None


def test_evaluate_collects_multiple_violations() -> None:
    proposal = make_proposal(risk_percent=5.0, leverage=20.0)
    report = evaluate_risk_gates(proposal, make_scan_context(ScanSession.ASIAN), make_context())
    assert not report.passed
    rule_ids = {v.rule_id for v in report.violations}
    assert {"RULE_1_MAX_RISK", "RULE_7_SESSION_BLOCK", "RULE_8_MAX_LEVERAGE"} <= rule_ids


def test_to_skip_decision_uses_first_violation_and_lists_all() -> None:
    proposal = make_proposal(risk_percent=5.0, leverage=20.0)
    report = evaluate_risk_gates(proposal, make_scan_context(), make_context())
    skip = to_skip_decision(proposal, report)
    assert isinstance(skip, SkipDecision)
    assert skip.scan_id == proposal.scan_id
    assert skip.symbol == proposal.symbol
    first = report.first_violation
    assert first is not None
    assert skip.reason is first.skip_reason
    assert skip.violated_rule == first.rule_id
    # details enumerate every violation
    assert "RULE_1_MAX_RISK" in skip.details
    assert "RULE_8_MAX_LEVERAGE" in skip.details


# ---------------------------------------------------------------------------
# gather_risk_context + node factory (against a fake store)
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal SignalStore surface the risk gate reads."""

    def __init__(
        self,
        *,
        open_setups: list[StoredActiveSetup] | None = None,
        signals_by_id: dict[UUID, StoredSignal] | None = None,
        recent: list[StoredSignal] | None = None,
    ) -> None:
        self._open = open_setups or []
        self._by_id = signals_by_id or {}
        self._recent = recent or []

    async def list_open_active_setups(self) -> list[StoredActiveSetup]:
        return list(self._open)

    async def get_signal(self, signal_id: UUID) -> StoredSignal | None:
        return self._by_id.get(signal_id)

    async def list_recent_signals(
        self, *, limit: int = 50, symbol: str | None = None
    ) -> list[StoredSignal]:
        return list(self._recent)


def _snapshot(funding_rate: float | None = None) -> MarketSnapshot:
    candle = Kline(
        open_time=_NOW - timedelta(hours=4),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=10.0,
    )
    return MarketSnapshot(
        symbol="BTCUSDT",
        venue="binance",
        fetched_at=_NOW,
        klines={Timeframe.H4: [candle]},
        funding_rate=funding_rate,
    )


async def test_gather_builds_exposure_and_counts() -> None:
    eth_setup = StoredActiveSetup(id=uuid4(), signal_id=uuid4(), opened_at=_NOW, status="OPEN")
    eth_signal = make_stored_signal(
        created_at=_NOW - timedelta(hours=3), symbol="ETHUSDT", direction=SignalDirection.LONG
    )
    store = _FakeStore(
        open_setups=[eth_setup],
        signals_by_id={eth_setup.signal_id: eth_signal},
        recent=[
            make_stored_signal(created_at=_NOW - timedelta(hours=2)),
            make_stored_signal(outcome=SignalOutcome.LOSS, created_at=_NOW - timedelta(hours=6)),
        ],
    )
    ctx = await gather_risk_context(store=store, snapshot=_snapshot(0.0002), now=_NOW)  # type: ignore[arg-type]
    assert ctx.open_setup_count == 1
    assert ctx.open_exposure == (("ETHUSDT", SignalDirection.LONG),)
    assert ctx.published_last_24h == 2
    assert ctx.funding_rate == 0.0002


async def test_node_passes_compliant_proposal_through() -> None:
    node = make_risk_gate_node(_FakeStore(), clock=lambda: _NOW)  # type: ignore[arg-type]
    state = {
        "scan_context": make_scan_context(),
        "snapshot": _snapshot(),
        "proposal": make_proposal(),
        "decision": JudgeRuling.PUBLISH,
    }
    out = await node(state)  # type: ignore[arg-type]
    # On pass the node returns only its report; `proposal`/`decision` are left
    # untouched in the merged state (LangGraph merges the partial return).
    assert out["risk_gate_report"].passed
    assert "proposal" not in out  # not overwritten
    assert "decision" not in out  # the gate does not change a passing decision
    assert "rejected_proposal" not in out


async def test_node_force_skips_violating_proposal() -> None:
    # Three open correlated setups would also trip rule 4; use a blocked session
    # for a clean single-rule trigger.
    node = make_risk_gate_node(_FakeStore(), clock=lambda: _NOW)  # type: ignore[arg-type]
    proposal = make_proposal()
    state = {
        "scan_context": make_scan_context(ScanSession.ASIAN),
        "snapshot": _snapshot(),
        "proposal": proposal,
        "decision": JudgeRuling.PUBLISH,
    }
    out = await node(state)  # type: ignore[arg-type]
    assert isinstance(out["proposal"], SkipDecision)
    assert out["proposal"].violated_rule == "RULE_7_SESSION_BLOCK"
    assert out["decision"] is JudgeRuling.SKIP
    assert out["rejected_proposal"] is proposal  # original preserved for FR-1.7
    assert not out["risk_gate_report"].passed


async def test_node_ignores_non_proposal_state() -> None:
    node = make_risk_gate_node(_FakeStore(), clock=lambda: _NOW)  # type: ignore[arg-type]
    skip = SkipDecision(
        scan_id=uuid4(),
        strategy="smc",
        symbol="BTCUSDT",
        reason=SkipReason.NO_CLEAR_BIAS,
        details="No actionable bias.",
    )
    state = {"proposal": skip, "snapshot": _snapshot(), "scan_context": make_scan_context()}
    out = await node(state)  # type: ignore[arg-type]
    assert out == {}
