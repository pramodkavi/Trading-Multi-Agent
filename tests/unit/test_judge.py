"""Unit tests for the Judge agent (Slice 2 Step 2.6).

All offline (mocked Anthropic client). Covers:
- JudgeDecision schema validation (caveat required iff PUBLISH_WITH_CAVEAT)
- the three SPEC Step 2.6 scenarios (PUBLISH / SKIP / PUBLISH_WITH_CAVEAT): the
  ruling plumbs through and the prompt encodes the distinguishing facts
- FR-4.3 macro-unavailable confidence cap (deterministic, post-LLM)
- the LangGraph node: skip for non-proposals (no LLM call); sets both
  judge_decision and the decision enum for proposals
- prompt rendering of None / NoMacroData inputs and system-prompt guards

The ruling itself is produced by the (mocked) LLM, so these tests verify the
plumbing + prompt content, not the model's judgement.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.agents.historian import HistorianReport
from src.agents.judge import Judge, JudgeDecision, make_judge_node
from src.agents.judge.judge import JUDGE_SYSTEM_PROMPT, _build_user_prompt, _render_skeptic
from src.agents.skeptic import SkepticObjection
from src.common.models import (
    JudgeConfidence,
    JudgeRuling,
    ObjectionSeverity,
    SignalDirection,
    SignalProposal,
    SkipDecision,
    SkipReason,
)
from src.providers import NoMacroData

# ---------------------------------------------------------------------------
# Mock helpers / builders
# ---------------------------------------------------------------------------


def _fake_tool_response(
    payload: dict[str, Any],
    *,
    tool_name: str = "emit_ruling",
    tokens_in: int = 200,
    tokens_out: int = 80,
) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name=tool_name, id="toolu_j", input=payload)
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out),
    )


def _mock_client(responses: list[Any]) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    return client


def make_proposal(**overrides: Any) -> SignalProposal:
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
        "tags": ["smc", "bullish-ob", "liquidity-sweep"],
        "confluence_narrative": "Bullish OB in discount with a liquidity sweep below equal lows.",
        "features": {"primary_poi_type": "order_block", "confluence_score": 4},
    }
    base.update(overrides)
    return SignalProposal(**base)


def make_report(
    *,
    win_rate: float | None,
    sample_size: int,
    wins: int,
    losses: int,
    breakeven: int = 0,
    inconclusive: int = 0,
    summary: str = "Historian summary of similar setups.",
) -> HistorianReport:
    return HistorianReport(
        query_proposal_id=uuid4(),
        strategy="smc",
        direction=SignalDirection.LONG,
        sample_size=sample_size,
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        inconclusive=inconclusive,
        win_rate=win_rate,
        summary=summary,
    )


def make_objection(
    severity: ObjectionSeverity, *, recommends_against: bool = False
) -> SkepticObjection:
    return SkepticObjection(
        severity=severity,
        recommends_against=recommends_against,
        headline="Macro context noted for this setup.",
        reasoning="Reasoning that cites the supplied macro data points in adequate detail.",
        cited_macro=["broad USD index 104.2"],
    )


_PUBLISH: dict[str, Any] = {
    "ruling": "PUBLISH",
    "confidence": "HIGH",
    "reasoning": "Clean structure, supportive history, only a weak macro objection.",
    "caveat": None,
}
_SKIP: dict[str, Any] = {
    "ruling": "SKIP",
    "confidence": "HIGH",
    "reasoning": "A strong macro objection outweighs a thin, poor track record.",
    "caveat": None,
}
_WITH_CAVEAT: dict[str, Any] = {
    "ruling": "PUBLISH_WITH_CAVEAT",
    "confidence": "MEDIUM",
    "reasoning": "Worth sending but the medium macro objection warrants reduced size.",
    "caveat": "Reduce size: dollar-strength headwind.",
}


# ---------------------------------------------------------------------------
# JudgeDecision schema
# ---------------------------------------------------------------------------


def test_with_caveat_requires_caveat() -> None:
    with pytest.raises(ValidationError):
        JudgeDecision.model_validate({**_WITH_CAVEAT, "caveat": None})
    # whitespace-only is also rejected
    with pytest.raises(ValidationError):
        JudgeDecision.model_validate({**_WITH_CAVEAT, "caveat": "   "})


def test_with_caveat_accepts_caveat() -> None:
    decision = JudgeDecision.model_validate(_WITH_CAVEAT)
    assert decision.ruling is JudgeRuling.PUBLISH_WITH_CAVEAT
    assert decision.caveat == "Reduce size: dollar-strength headwind."


def test_publish_and_skip_need_no_caveat() -> None:
    assert JudgeDecision.model_validate(_PUBLISH).caveat is None
    assert JudgeDecision.model_validate(_SKIP).caveat is None


# ---------------------------------------------------------------------------
# The three SPEC Step 2.6 scenarios (ruling plumbs through; prompt encodes facts)
# ---------------------------------------------------------------------------


async def test_strong_proposal_supportive_history_weak_objection_publishes() -> None:
    client = _mock_client([_fake_tool_response(_PUBLISH)])
    judge = Judge(client=client)
    report = make_report(win_rate=0.78, sample_size=9, wins=7, losses=2)
    objection = make_objection(ObjectionSeverity.LOW)

    decision = await judge.evaluate(make_proposal(), report, objection)
    assert decision.ruling is JudgeRuling.PUBLISH

    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "78%" in prompt
    assert "Severity: LOW" in prompt


async def test_strong_proposal_weak_history_strong_objection_skips() -> None:
    client = _mock_client([_fake_tool_response(_SKIP)])
    judge = Judge(client=client)
    report = make_report(win_rate=0.25, sample_size=8, wins=2, losses=6)
    objection = make_objection(ObjectionSeverity.HIGH, recommends_against=True)

    decision = await judge.evaluate(make_proposal(), report, objection)
    assert decision.ruling is JudgeRuling.SKIP

    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "25%" in prompt
    assert "Severity: HIGH" in prompt


async def test_borderline_publishes_with_caveat() -> None:
    client = _mock_client([_fake_tool_response(_WITH_CAVEAT)])
    judge = Judge(client=client)
    report = make_report(win_rate=0.55, sample_size=6, wins=3, losses=2, breakeven=1)
    objection = make_objection(ObjectionSeverity.MEDIUM, recommends_against=True)

    decision = await judge.evaluate(make_proposal(), report, objection)
    assert decision.ruling is JudgeRuling.PUBLISH_WITH_CAVEAT
    assert decision.caveat == "Reduce size: dollar-strength headwind."


# ---------------------------------------------------------------------------
# FR-4.3 macro-unavailable confidence cap
# ---------------------------------------------------------------------------


async def test_macro_unavailable_caps_confidence_to_medium() -> None:
    client = _mock_client([_fake_tool_response(_PUBLISH)])  # LLM claims HIGH confidence
    judge = Judge(client=client)
    nomacro = NoMacroData(provider="skeptic", reason="all providers down")

    decision = await judge.evaluate(
        make_proposal(), make_report(win_rate=0.7, sample_size=10, wins=7, losses=3), nomacro
    )
    assert decision.ruling is JudgeRuling.PUBLISH
    assert decision.confidence is JudgeConfidence.MEDIUM  # capped from HIGH


async def test_macro_available_does_not_cap_confidence() -> None:
    client = _mock_client([_fake_tool_response(_PUBLISH)])
    judge = Judge(client=client)
    decision = await judge.evaluate(
        make_proposal(),
        make_report(win_rate=0.7, sample_size=10, wins=7, losses=3),
        make_objection(ObjectionSeverity.LOW),
    )
    assert decision.confidence is JudgeConfidence.HIGH  # untouched


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------


async def test_node_skips_non_proposal_without_llm() -> None:
    client = _mock_client([_fake_tool_response(_PUBLISH)])
    node = make_judge_node(Judge(client=client))

    skip = SkipDecision(
        scan_id=uuid4(),
        strategy="smc",
        symbol="BTCUSDT",
        reason=SkipReason.NO_CLEAR_BIAS,
        details="no setup",
    )
    for proposal in (None, skip):
        out = await node({"proposal": proposal})
        assert out == {"judge_decision": None, "decision": JudgeRuling.SKIP}
    client.messages.create.assert_not_awaited()


async def test_node_proposal_sets_decision_and_judge_decision() -> None:
    client = _mock_client([_fake_tool_response(_WITH_CAVEAT)])
    node = make_judge_node(Judge(client=client))
    out = await node(
        {
            "proposal": make_proposal(),
            "historian_report": make_report(win_rate=0.6, sample_size=5, wins=3, losses=2),
            "skeptic_objection": make_objection(ObjectionSeverity.MEDIUM),
        }
    )
    assert isinstance(out["judge_decision"], JudgeDecision)
    assert out["judge_decision"].ruling is JudgeRuling.PUBLISH_WITH_CAVEAT
    assert out["decision"] is JudgeRuling.PUBLISH_WITH_CAVEAT


async def test_node_tolerates_missing_historian_and_skeptic() -> None:
    client = _mock_client([_fake_tool_response(_PUBLISH)])
    node = make_judge_node(Judge(client=client))
    out = await node({"proposal": make_proposal()})  # no historian / skeptic keys
    assert out["decision"] is JudgeRuling.PUBLISH


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_prompt_handles_none_inputs() -> None:
    prompt = _build_user_prompt(make_proposal(), None, None)
    assert "No historian report available" in prompt
    assert "No skeptic objection available" in prompt
    assert "BTCUSDT" in prompt


def test_render_skeptic_nomacrodata_flags_caution() -> None:
    rendered = _render_skeptic(NoMacroData(provider="skeptic", reason="down"))
    assert "MACRO DATA UNAVAILABLE" in rendered
    assert "CAUTION" in rendered


def test_prompt_renders_no_decisive_history() -> None:
    report = make_report(win_rate=None, sample_size=3, wins=0, losses=0, inconclusive=3)
    prompt = _build_user_prompt(make_proposal(), report, None)
    assert "no decisive" in prompt


def test_system_prompt_guards() -> None:
    assert "signal-only" in JUDGE_SYSTEM_PROMPT
    assert "RSI" in JUDGE_SYSTEM_PROMPT
    assert "PUBLISH_WITH_CAVEAT" in JUDGE_SYSTEM_PROMPT
    assert "missed signal costs nothing" in JUDGE_SYSTEM_PROMPT
