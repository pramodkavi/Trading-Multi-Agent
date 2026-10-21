"""Decision classification for the dashboard (Slice 4 Step 4.2, design §5.1 / §14 D2).

`classify.py` re-exports the Critic's classifier and adds a column-level variant plus
its SQL `CASE` twin. The parity test below pins all eight (direction, violated_rule,
judge_ruling) combinations against the Critic's `classify` (plan Review Focus #4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from src.agents.critic import inventory
from src.agents.critic import models as critic_models
from src.common.models import AgentRole, SignalDirection, SignalStatus
from src.dashboard.api import classify as classify_module
from src.dashboard.api.classify import (
    DECISION_KIND_SQL,
    DecisionKind,
    DecisionRecord,
    classify,
    decision_kind_from_columns,
)
from src.persistence.models import StoredAgentRun, StoredSignal

NOW = datetime(2026, 10, 20, 8, 3, tzinfo=UTC)
NARRATIVE = "Synthetic fixture for the dashboard classify parity tests."


def _proposal_payload(scan_id: Any, direction: str) -> dict[str, Any]:
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
# Re-exports (design §14 D2: thin re-export, no second classifier)
# ---------------------------------------------------------------------------


def test_reexports_are_the_critic_objects() -> None:
    assert DecisionKind is critic_models.DecisionKind
    assert DecisionRecord is critic_models.DecisionRecord
    assert classify is inventory.classify
    assert set(classify_module.__all__) >= {
        "DECISION_KIND_SQL",
        "DecisionKind",
        "DecisionRecord",
        "classify",
        "decision_kind_from_columns",
    }


# ---------------------------------------------------------------------------
# Column variant == Critic classify, for every combination
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("direction", "rule", "ruling", "kind"),
    [
        ("LONG", None, "PUBLISH", DecisionKind.PUBLISHED),
        ("LONG", None, "PUBLISH_WITH_CAVEAT", DecisionKind.PUBLISHED),
        ("LONG", None, None, DecisionKind.PUBLISHED),  # Slice-1-era row: no Judge run
        ("SHORT", None, "SKIP", DecisionKind.VETOED),
        (None, "RULE_3_PREMIUM_DISCOUNT", None, DecisionKind.GATE_REJECTED),
        (None, "", None, DecisionKind.NO_SETUP),  # empty rule string is "no rule"
        (None, None, None, DecisionKind.NO_SETUP),
        (None, None, "SKIP", DecisionKind.NO_SETUP),  # Judge SKIP on a skip row is noise
    ],
)
def test_decision_kind_from_columns_matches_classify(
    direction: str | None, rule: str | None, ruling: str | None, kind: DecisionKind
) -> None:
    scan_id = uuid4()
    payload = (
        _proposal_payload(scan_id, direction)
        if direction is not None
        else _skip_payload(scan_id, rule)
    )
    signal = _signal(payload, scan_id)
    runs = [_run(scan_id, AgentRole.JUDGE, _judge(ruling))] if ruling is not None else []

    # The Critic's stitched classification (the authority)...
    assert classify(signal, runs).kind is kind
    # ...and the column-level variant the SQL CASE mirrors agree.
    assert (
        decision_kind_from_columns(direction=direction, violated_rule=rule, judge_ruling=ruling)
        is kind
    )


def test_decision_kind_sql_mentions_every_kind() -> None:
    assert all(k.value in DECISION_KIND_SQL for k in DecisionKind)


def test_decision_kind_sql_is_the_agreed_literal() -> None:
    # Aliases are part of the contract: `s` = signals, `j.ruling` = the LATERAL judge
    # subquery in queries.py. Whitespace-insensitive so the formatter cannot break it.
    expected = (
        "CASE WHEN s.direction IS NOT NULL THEN CASE WHEN j.ruling = 'SKIP' THEN 'VETOED' "
        "ELSE 'PUBLISHED' END WHEN COALESCE(s.payload->>'violated_rule', '') <> '' "
        "THEN 'GATE_REJECTED' ELSE 'NO_SETUP' END"
    )
    assert " ".join(DECISION_KIND_SQL.split()) == expected
    assert ":" not in DECISION_KIND_SQL  # no bind parameters inside the expression
