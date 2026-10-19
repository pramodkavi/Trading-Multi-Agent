"""Tests for the decision inventory classifier (design §3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from src.agents.critic.inventory import classify, is_scoreable
from src.agents.critic.models import DecisionKind
from src.common.models import AgentRole, SignalDirection, SignalStatus, SkipReason
from src.persistence.models import StoredAgentRun, StoredSignal

NOW = datetime(2026, 7, 1, 8, 3, tzinfo=UTC)
NARRATIVE = "Synthetic fixture for the inventory classifier unit tests."


def _proposal_payload(scan_id: Any, direction: str = "LONG") -> dict[str, Any]:
    if direction == "LONG":
        entry, stop, tp1 = 100.0, 98.0, 106.0
    else:
        entry, stop, tp1 = 100.0, 102.0, 94.0
    return {
        "proposal_id": str(uuid4()),
        "scan_id": str(scan_id),
        "strategy": "smc",
        "created_at": NOW.isoformat(),
        "symbol": "BTCUSDT",
        "direction": direction,
        "entry_price": entry,
        "stop_loss": stop,
        "take_profit_1": tp1,
        "take_profit_2": None,
        "risk_reward_ratio": 3.0,
        "leverage": 3.0,
        "risk_percent": 1.0,
        "tags": ["fvg"],
        "confluence_narrative": NARRATIVE,
        "features": {},
    }


def _skip_payload(scan_id: Any, violated_rule: str | None) -> dict[str, Any]:
    return {
        "decision_id": str(uuid4()),
        "scan_id": str(scan_id),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "reason": "INSUFFICIENT_RR" if violated_rule else "NO_CLEAR_BIAS",
        "details": "gate rejected the proposal" if violated_rule else "no valid structure",
        "violated_rule": violated_rule,
        "created_at": NOW.isoformat(),
    }


def _signal(payload: dict[str, Any], scan_id: Any) -> StoredSignal:
    is_proposal = "direction" in payload
    return StoredSignal(
        id=uuid4(),
        scan_id=scan_id,
        symbol="BTCUSDT",
        strategy="smc",
        direction=SignalDirection(payload["direction"]) if is_proposal else None,
        status=SignalStatus.PUBLISHED if is_proposal else SignalStatus.SKIPPED,
        created_at=NOW,
        payload=payload,
        tags=[],
        features={},
    )


def _run(scan_id: Any, role: AgentRole, output: dict[str, Any]) -> StoredAgentRun:
    return StoredAgentRun(
        id=uuid4(),
        scan_id=scan_id,
        agent_role=role,
        strategy="smc",
        input_hash="h",
        output=output,
        latency_ms=0,
        token_usage={},
        cost_usd=None,
        created_at=NOW,
    )


def _judge(ruling: str) -> dict[str, Any]:
    return {"ruling": ruling, "confidence": "MEDIUM", "reasoning": "ok", "caveat": None}


# ---------------------------------------------------------------------------
# Proposal payloads: PUBLISHED vs VETOED
# ---------------------------------------------------------------------------


def test_published_when_judge_publishes() -> None:
    sid = uuid4()
    sig = _signal(_proposal_payload(sid), sid)
    judge_out = {
        "ruling": "PUBLISH_WITH_CAVEAT",
        "confidence": "MEDIUM",
        "reasoning": "ok",
        "caveat": "c",
    }
    rec = classify(sig, [_run(sid, AgentRole.JUDGE, judge_out)])
    assert rec.kind is DecisionKind.PUBLISHED
    assert rec.signal is sig
    assert rec.proposal is not None and rec.proposal.entry_price == 100.0
    assert rec.proposal.direction is SignalDirection.LONG
    assert rec.skip is None
    assert rec.judge is not None and rec.judge["ruling"] == "PUBLISH_WITH_CAVEAT"


def test_published_when_judge_ruling_is_plain_publish() -> None:
    sid = uuid4()
    rec = classify(
        _signal(_proposal_payload(sid), sid),
        [_run(sid, AgentRole.JUDGE, _judge("PUBLISH"))],
    )
    assert rec.kind is DecisionKind.PUBLISHED


def test_vetoed_when_judge_skips_even_though_status_is_published() -> None:
    sid = uuid4()
    sig = _signal(_proposal_payload(sid), sid)
    assert sig.status is SignalStatus.PUBLISHED  # the known status bug
    rec = classify(sig, [_run(sid, AgentRole.JUDGE, _judge("SKIP"))])
    assert rec.kind is DecisionKind.VETOED
    assert rec.proposal is not None  # counterfactual replay still needs the proposal
    assert rec.skip is None


def test_published_when_no_judge_run_slice1_era() -> None:
    sid = uuid4()
    rec = classify(_signal(_proposal_payload(sid), sid), [])
    assert rec.kind is DecisionKind.PUBLISHED
    assert rec.judge is None
    assert rec.historian is None and rec.skeptic is None


def test_published_when_judge_output_lacks_ruling_key() -> None:
    sid = uuid4()
    rec = classify(
        _signal(_proposal_payload(sid), sid),
        [_run(sid, AgentRole.JUDGE, {"reasoning": "?"})],
    )
    assert rec.kind is DecisionKind.PUBLISHED
    assert rec.judge == {"reasoning": "?"}


def test_short_proposal_classifies_and_parses() -> None:
    sid = uuid4()
    rec = classify(
        _signal(_proposal_payload(sid, "SHORT"), sid),
        [_run(sid, AgentRole.JUDGE, _judge("SKIP"))],
    )
    assert rec.kind is DecisionKind.VETOED
    assert rec.proposal is not None and rec.proposal.direction is SignalDirection.SHORT
    assert rec.proposal.stop_loss == 102.0


def test_corrupt_proposal_payload_yields_none_proposal() -> None:
    sid = uuid4()
    bad = _proposal_payload(sid)
    bad["stop_loss"] = "not-a-number"
    rec = classify(_signal(bad, sid), [])
    assert rec.kind is DecisionKind.PUBLISHED
    assert rec.proposal is None


def test_corrupt_proposal_geometry_yields_none_proposal_but_keeps_veto_kind() -> None:
    sid = uuid4()
    bad = _proposal_payload(sid)
    bad["stop_loss"] = 150.0  # LONG with stop above entry fails the geometry validator
    rec = classify(_signal(bad, sid), [_run(sid, AgentRole.JUDGE, _judge("SKIP"))])
    assert rec.kind is DecisionKind.VETOED
    assert rec.proposal is None


# ---------------------------------------------------------------------------
# Skip payloads: GATE_REJECTED vs NO_SETUP
# ---------------------------------------------------------------------------


def test_gate_rejected_pulls_proposal_from_analyzer_run() -> None:
    sid = uuid4()
    rec = classify(
        _signal(_skip_payload(sid, "RULE_2_MIN_RR"), sid),
        [_run(sid, AgentRole.ANALYZER, _proposal_payload(sid))],
    )
    assert rec.kind is DecisionKind.GATE_REJECTED
    assert rec.proposal is not None and rec.proposal.entry_price == 100.0
    assert rec.skip is not None and rec.skip.violated_rule == "RULE_2_MIN_RR"
    assert rec.skip.reason is SkipReason.INSUFFICIENT_RR


def test_gate_rejected_without_analyzer_proposal_has_none() -> None:
    sid = uuid4()
    rec = classify(_signal(_skip_payload(sid, "RULE_7_SESSION_BLOCK"), sid), [])
    assert rec.kind is DecisionKind.GATE_REJECTED
    assert rec.proposal is None
    assert rec.skip is not None


def test_gate_rejected_ignores_analyzer_output_without_direction() -> None:
    """An Analyzer run that recorded a SkipDecision (no 'direction') is not a proposal."""
    sid = uuid4()
    rec = classify(
        _signal(_skip_payload(sid, "RULE_2_MIN_RR"), sid),
        [_run(sid, AgentRole.ANALYZER, _skip_payload(sid, "RULE_2_MIN_RR"))],
    )
    assert rec.kind is DecisionKind.GATE_REJECTED
    assert rec.proposal is None


def test_gate_rejected_with_corrupt_analyzer_proposal_has_none() -> None:
    sid = uuid4()
    bad = _proposal_payload(sid)
    bad["entry_price"] = -1.0
    rec = classify(
        _signal(_skip_payload(sid, "RULE_2_MIN_RR"), sid),
        [_run(sid, AgentRole.ANALYZER, bad)],
    )
    assert rec.kind is DecisionKind.GATE_REJECTED
    assert rec.proposal is None


def test_no_setup() -> None:
    sid = uuid4()
    rec = classify(_signal(_skip_payload(sid, None), sid), [])
    assert rec.kind is DecisionKind.NO_SETUP
    assert rec.proposal is None and rec.skip is not None
    assert rec.skip.reason is SkipReason.NO_CLEAR_BIAS


def test_no_setup_ignores_analyzer_run_that_is_the_same_skip() -> None:
    """On an Analyzer skip the ANALYZER agent_run is the skip itself; no proposal exists."""
    sid = uuid4()
    skip = _skip_payload(sid, None)
    rec = classify(_signal(skip, sid), [_run(sid, AgentRole.ANALYZER, skip)])
    assert rec.kind is DecisionKind.NO_SETUP
    assert rec.proposal is None


def test_corrupt_skip_payload_still_classifies_from_raw_violated_rule() -> None:
    sid = uuid4()
    bad = _skip_payload(sid, "RULE_2_MIN_RR")
    bad["reason"] = "NOT_A_REAL_REASON"
    rec = classify(_signal(bad, sid), [])
    assert rec.kind is DecisionKind.GATE_REJECTED
    assert rec.skip is None


def test_corrupt_skip_payload_without_violated_rule_is_no_setup() -> None:
    sid = uuid4()
    bad = _skip_payload(sid, None)
    del bad["details"]
    rec = classify(_signal(bad, sid), [])
    assert rec.kind is DecisionKind.NO_SETUP
    assert rec.skip is None


def test_empty_string_violated_rule_is_no_setup() -> None:
    sid = uuid4()
    rec = classify(_signal(_skip_payload(sid, ""), sid), [])
    assert rec.kind is DecisionKind.NO_SETUP


# ---------------------------------------------------------------------------
# Agent output attachment
# ---------------------------------------------------------------------------


def test_attaches_historian_and_skeptic() -> None:
    sid = uuid4()
    rec = classify(
        _signal(_proposal_payload(sid), sid),
        [
            _run(sid, AgentRole.HISTORIAN, {"sample_size": 3, "win_rate": 0.66}),
            _run(sid, AgentRole.SKEPTIC, {"severity": "HIGH", "headline": "DXY breakout"}),
            _run(sid, AgentRole.JUDGE, {"ruling": "PUBLISH"}),
        ],
    )
    assert rec.historian == {"sample_size": 3, "win_rate": 0.66}
    assert rec.skeptic is not None and rec.skeptic["severity"] == "HIGH"
    assert rec.judge == {"ruling": "PUBLISH"}


def test_attached_outputs_are_copies_not_aliases() -> None:
    sid = uuid4()
    run = _run(sid, AgentRole.HISTORIAN, {"sample_size": 3})
    rec = classify(_signal(_proposal_payload(sid), sid), [run])
    assert rec.historian == run.output
    assert rec.historian is not run.output


def test_ignores_runs_from_other_roles() -> None:
    sid = uuid4()
    rec = classify(
        _signal(_proposal_payload(sid), sid),
        [
            _run(sid, AgentRole.FORECASTER, {"status": "STILL_VALID"}),
            _run(sid, AgentRole.CRITIC, {"summary": "n/a"}),
        ],
    )
    assert rec.kind is DecisionKind.PUBLISHED
    assert rec.historian is None and rec.skeptic is None and rec.judge is None


def test_first_run_wins_when_a_role_appears_twice() -> None:
    sid = uuid4()
    rec = classify(
        _signal(_proposal_payload(sid), sid),
        [
            _run(sid, AgentRole.JUDGE, _judge("SKIP")),
            _run(sid, AgentRole.JUDGE, _judge("PUBLISH")),
        ],
    )
    assert rec.kind is DecisionKind.VETOED
    assert rec.judge is not None and rec.judge["ruling"] == "SKIP"


# ---------------------------------------------------------------------------
# is_scoreable
# ---------------------------------------------------------------------------


def test_is_scoreable() -> None:
    assert is_scoreable(DecisionKind.PUBLISHED)
    assert is_scoreable(DecisionKind.VETOED)
    assert is_scoreable(DecisionKind.GATE_REJECTED)
    assert not is_scoreable(DecisionKind.NO_SETUP)


@pytest.mark.parametrize("kind", list(DecisionKind))
def test_is_scoreable_covers_every_kind(kind: DecisionKind) -> None:
    assert is_scoreable(kind) is (kind is not DecisionKind.NO_SETUP)
