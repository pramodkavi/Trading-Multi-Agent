"""Unit tests for the Critic v0 DecisionReviewer (design §6.1 / §6.2) -- fully offline.

Fake store + fake Anthropic client (the ``test_forecaster.py`` pattern); no
network, no database. Covers the queue -> classify -> resolution -> Claude ->
persist loop, the double write (``decision_reviews`` + ``agent_runs`` with
``agent_role='critic'`` for NFR-2.3 cost tracking), per-decision fault
isolation with an error row, the cost sum, and the user-prompt rendering.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from src.agents.critic.inventory import classify
from src.agents.critic.models import DecisionKind, Resolution, ResolutionOutcome
from src.agents.critic.prompts import REVIEW_SYSTEM_PROMPT, build_review_user_prompt
from src.agents.critic.reviewer import DecisionReviewer, ReviewSummary
from src.common.llm import DEFAULT_MODEL
from src.common.models import AgentRole, SignalDirection, SignalStatus
from src.persistence.models import StoredAgentRun, StoredSignal

T0 = datetime(2026, 7, 1, 8, 3, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _proposal_payload(scan_id: UUID) -> dict[str, Any]:
    return {
        "proposal_id": str(uuid4()),
        "scan_id": str(scan_id),
        "strategy": "smc",
        "created_at": T0.isoformat(),
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit_1": 106.0,
        "take_profit_2": None,
        "risk_reward_ratio": 3.0,
        "leverage": 3.0,
        "risk_percent": 1.0,
        "tags": ["fvg"],
        "confluence_narrative": "Bullish discount FVG after a liquidity sweep.",
        "features": {},
    }


def _skip_payload(scan_id: UUID, *, violated_rule: str | None) -> dict[str, Any]:
    return {
        "decision_id": str(uuid4()),
        "scan_id": str(scan_id),
        "strategy": "smc",
        "symbol": "SOLUSDT",
        "reason": "PREMIUM_DISCOUNT_VIOLATION" if violated_rule else "GATE_FAILED",
        "details": "OB sat in premium; long blocked" if violated_rule else "nothing at a POI",
        "violated_rule": violated_rule,
        "created_at": T0.isoformat(),
    }


def _signal(scan_id: UUID) -> StoredSignal:
    return StoredSignal(
        id=uuid4(),
        scan_id=scan_id,
        symbol="BTCUSDT",
        strategy="smc",
        direction=SignalDirection.LONG,
        status=SignalStatus.PUBLISHED,
        created_at=T0,
        payload=_proposal_payload(scan_id),
        tags=[],
        features={},
    )


def _skip_signal(scan_id: UUID, *, violated_rule: str | None) -> StoredSignal:
    return StoredSignal(
        id=uuid4(),
        scan_id=scan_id,
        symbol="SOLUSDT",
        strategy="smc",
        direction=None,
        status=SignalStatus.SKIPPED,
        created_at=T0,
        payload=_skip_payload(scan_id, violated_rule=violated_rule),
        tags=[],
        features={},
    )


def _run(scan_id: UUID, role: AgentRole, output: dict[str, Any]) -> StoredAgentRun:
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
        created_at=T0,
    )


def _resolution(signal_id: UUID) -> Resolution:
    return Resolution(
        signal_id=signal_id,
        decision_kind=DecisionKind.PUBLISHED,
        outcome=ResolutionOutcome.WIN,
        decided_at=T0,
        entered_at=T0,
        resolved_at=T0.replace(hour=14, minute=15),
        realized_r=3.0,
        max_adverse_excursion_r=-0.4,
        max_favorable_excursion_r=3.2,
        tp2_reached=False,
        candles_used=72,
        engine_version="replay-v1",
    )


REVIEW_OK: dict[str, Any] = {
    "smc_read_valid": True,
    "skeptic_vindicated": "NO",
    "judge_correct": True,
    "gate_correct": None,
    "failure_category": "NONE",
    "lesson": "BTCUSDT discount FVG long was textbook; keep.",
    "confidence": "HIGH",
}


def _response(payload: dict[str, Any]) -> SimpleNamespace:
    block = SimpleNamespace(
        type="tool_use", name="emit_decision_review", id="toolu_1", input=payload
    )
    return SimpleNamespace(
        content=[block], usage=SimpleNamespace(input_tokens=1200, output_tokens=90)
    )


def _client(responses: list[Any]) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    return client


class FakeStore:
    def __init__(
        self,
        signals: list[StoredSignal],
        runs: dict[UUID, list[StoredAgentRun]],
        resolutions: dict[UUID, Resolution],
        remaining: int = 0,
    ) -> None:
        self._signals, self._runs, self._res, self._remaining = (
            signals,
            runs,
            resolutions,
            remaining,
        )
        self.reviews: list[dict[str, Any]] = []
        self.agent_runs: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.count_calls: list[dict[str, Any]] = []

    async def list_pending_reviews(
        self, *, limit: int, no_setup_before: datetime, retry_before: datetime
    ) -> list[StoredSignal]:
        self.list_calls.append(
            {"limit": limit, "no_setup_before": no_setup_before, "retry_before": retry_before}
        )
        return self._signals[:limit]

    async def count_pending_reviews(
        self, *, no_setup_before: datetime, retry_before: datetime
    ) -> int:
        self.count_calls.append({"no_setup_before": no_setup_before, "retry_before": retry_before})
        return self._remaining

    async def list_agent_runs_for_scan(self, scan_id: UUID) -> list[StoredAgentRun]:
        return self._runs.get(scan_id, [])

    async def get_resolution(self, signal_id: UUID) -> Resolution | None:
        return self._res.get(signal_id)

    async def save_review(self, **kwargs: Any) -> None:
        self.reviews.append(kwargs)

    async def log_agent_run(self, **kwargs: Any) -> UUID:
        self.agent_runs.append(kwargs)
        return uuid4()


# ---------------------------------------------------------------------------
# run(): persistence contract
# ---------------------------------------------------------------------------


async def test_review_saved_and_logged_to_agent_runs() -> None:
    scan = uuid4()
    sig = _signal(scan)
    store = FakeStore(
        [sig],
        {scan: [_run(scan, AgentRole.JUDGE, {"ruling": "PUBLISH"})]},
        {sig.id: _resolution(sig.id)},
    )
    reviewer = DecisionReviewer(store=store, client=_client([_response(REVIEW_OK)]))
    summary = await reviewer.run(chunk_size=10)
    assert summary.reviewed == 1 and summary.failed == 0
    saved = store.reviews[0]
    assert saved["signal_id"] == sig.id and saved["decision_kind"] == "PUBLISHED"
    assert saved["error"] is None
    assert saved["review"]["failure_category"] == "NONE"
    assert saved["model"] == DEFAULT_MODEL
    assert saved["token_usage"] == {"tokens_in": 1200, "tokens_out": 90}
    assert saved["cost_usd"] is not None and saved["cost_usd"] > 0
    logged = store.agent_runs[0]
    assert logged["agent_role"] is AgentRole.CRITIC and logged["scan_id"] == scan
    assert logged["strategy"] == "smc"
    assert logged["input_hash"] == sha256(f"critic:{sig.id}".encode()).hexdigest()
    assert logged["output"] == saved["review"]
    assert logged["token_usage"] == {"tokens_in": 1200, "tokens_out": 90}
    assert logged["cost_usd"] == saved["cost_usd"]


async def test_failed_call_records_error_row_and_continues() -> None:
    scan1, scan2 = uuid4(), uuid4()
    s1, s2 = _signal(scan1), _signal(scan2)
    store = FakeStore(
        [s1, s2], {}, {s1.id: _resolution(s1.id), s2.id: _resolution(s2.id)}, remaining=3
    )
    client = _client([RuntimeError("api down"), _response(REVIEW_OK)])
    summary = await DecisionReviewer(store=store, client=client, concurrency=1).run()
    assert summary == ReviewSummary(reviewed=1, failed=1, remaining=3, cost_usd=summary.cost_usd)
    errors = [r for r in store.reviews if r["error"]]
    assert len(errors) == 1 and errors[0]["error"].startswith("RuntimeError")
    assert errors[0]["review"] == {}
    assert errors[0]["signal_id"] == s1.id
    assert errors[0]["decision_kind"] == "PUBLISHED"
    assert errors[0]["model"] == DEFAULT_MODEL
    assert errors[0]["token_usage"] == {} and errors[0]["cost_usd"] is None
    # A failed call never lands in agent_runs: there is no cost to account for.
    assert len(store.agent_runs) == 1 and store.agent_runs[0]["scan_id"] == scan2


async def test_cost_is_summed_over_successful_calls_only() -> None:
    scans = [uuid4() for _ in range(3)]
    sigs = [_signal(s) for s in scans]
    store = FakeStore(sigs, {}, {s.id: _resolution(s.id) for s in sigs})
    client = _client([_response(REVIEW_OK), RuntimeError("boom"), _response(REVIEW_OK)])
    summary = await DecisionReviewer(store=store, client=client, concurrency=1).run()
    assert (summary.reviewed, summary.failed, summary.remaining) == (2, 1, 0)
    expected = sum(r["cost_usd"] for r in store.reviews if r["error"] is None)
    assert summary.cost_usd == expected and expected > 0


async def test_queue_windows_derive_from_injected_clock() -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    store = FakeStore([], {}, {}, remaining=0)
    summary = await DecisionReviewer(store=store, client=_client([]), now=lambda: now).run(
        chunk_size=7
    )
    assert summary == ReviewSummary(reviewed=0, failed=0, remaining=0, cost_usd=0.0)
    assert store.list_calls == [
        {
            "limit": 7,
            "no_setup_before": now - timedelta(days=7),
            "retry_before": now - timedelta(days=1),
        }
    ]
    assert store.count_calls == [
        {"no_setup_before": now - timedelta(days=7), "retry_before": now - timedelta(days=1)}
    ]


async def test_no_setup_decision_is_reviewed_without_resolution() -> None:
    scan = uuid4()
    sig = _skip_signal(scan, violated_rule=None)
    store = FakeStore([sig], {}, {})
    reviewer = DecisionReviewer(store=store, client=_client([_response(REVIEW_OK)]))
    summary = await reviewer.run()
    assert summary.reviewed == 1
    assert store.reviews[0]["decision_kind"] == "NO_SETUP"
    # The prompt that went to Claude must say so explicitly.
    sent = reviewer._client.messages.create.await_args.kwargs
    user_text = sent["messages"][0]["content"]
    assert "## Analyzer skip" in user_text
    assert "## Resolution (ground truth)" in user_text and "(not run)" in user_text


async def test_concurrency_is_bounded_by_semaphore() -> None:
    scans = [uuid4() for _ in range(6)]
    sigs = [_signal(s) for s in scans]
    store = FakeStore(sigs, {}, {s.id: _resolution(s.id) for s in sigs})
    in_flight = 0
    peak = 0

    async def create(**_: Any) -> SimpleNamespace:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return _response(REVIEW_OK)

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=create)
    summary = await DecisionReviewer(store=store, client=client, concurrency=2).run()
    assert summary.reviewed == 6 and summary.failed == 0
    assert 1 <= peak <= 2


async def test_review_one_returns_result_and_none_on_failure() -> None:
    scan = uuid4()
    sig = _signal(scan)
    store = FakeStore([sig], {}, {sig.id: _resolution(sig.id)})
    ok = await DecisionReviewer(store=store, client=_client([_response(REVIEW_OK)])).review_one(sig)
    assert ok is not None and ok.output.failure_category.value == "NONE"
    failed = await DecisionReviewer(store=store, client=_client([ValueError("x")])).review_one(sig)
    assert failed is None
    assert [r["error"] is None for r in store.reviews] == [True, False]


async def test_structured_completion_called_with_review_tool_and_model() -> None:
    scan = uuid4()
    sig = _signal(scan)
    store = FakeStore([sig], {}, {sig.id: _resolution(sig.id)})
    client = _client([_response(REVIEW_OK)])
    await DecisionReviewer(store=store, client=client, model="claude-sonnet-4-5").run()
    kwargs = client.messages.create.await_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-5"
    assert kwargs["system"] == REVIEW_SYSTEM_PROMPT
    assert kwargs["tools"][0]["name"] == "emit_decision_review"
    assert kwargs["tool_choice"] == {"type": "tool", "name": "emit_decision_review"}


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def test_user_prompt_includes_resolution_summary_and_sections() -> None:
    scan = uuid4()
    sig = _signal(scan)
    record = classify(
        sig, [_run(scan, AgentRole.SKEPTIC, {"severity": "HIGH", "headline": "DXY up"})]
    )
    text = build_review_user_prompt(record, _resolution(sig.id))
    assert "## Resolution (ground truth)" in text and "Outcome: WIN" in text and "3.00R" in text
    assert "## Skeptic" in text and "DXY up" in text
    assert "## Judge" in text and "(not run)" in text
    assert "## Analyzer proposal" in text and "discount FVG" in text
    assert "## Historian" in text
    assert "## Decision" in text and "PUBLISHED" in text


def test_user_prompt_resolution_summary_line_is_human_readable() -> None:
    scan = uuid4()
    sig = _signal(scan)
    record = classify(sig, [])
    text = build_review_user_prompt(record, _resolution(sig.id))
    # entered 08:03 -> resolved 14:15 = 6h12m in trade
    assert (
        "Outcome: WIN after 6h12m in trade; realized 3.00R; MAE -0.40R; MFE 3.20R; TP2 not reached"
    ) in text


def test_user_prompt_handles_never_entered_and_missing_excursions() -> None:
    scan = uuid4()
    sig = _signal(scan)
    record = classify(sig, [])
    res = Resolution(
        signal_id=sig.id,
        decision_kind=DecisionKind.PUBLISHED,
        outcome=ResolutionOutcome.EXPIRED,
        decided_at=T0,
        entered_at=None,
        resolved_at=T0 + timedelta(hours=72),
        realized_r=0.0,
        candles_used=864,
        engine_version="replay-v1",
    )
    text = build_review_user_prompt(record, res)
    assert "Outcome: EXPIRED never entered; realized 0.00R" in text
    assert "MAE n/a" in text and "MFE n/a" in text


def test_user_prompt_gate_rejected_shows_skip_and_rejected_proposal() -> None:
    scan = uuid4()
    sig = _skip_signal(scan, violated_rule="premium_discount")
    rejected = _proposal_payload(scan) | {"symbol": "SOLUSDT"}
    record = classify(sig, [_run(scan, AgentRole.ANALYZER, rejected)])
    assert record.kind is DecisionKind.GATE_REJECTED
    text = build_review_user_prompt(record, None)
    assert "GATE_REJECTED" in text
    assert "## Analyzer skip" in text and "premium_discount" in text
    assert "## Analyzer proposal" in text and "SOLUSDT" in text
    assert "## Resolution (ground truth)" in text


def test_user_prompt_falls_back_to_raw_payload_when_proposal_is_corrupt() -> None:
    scan = uuid4()
    sig = _signal(scan)
    # Too-short narrative fails SignalProposal validation -> classify() yields proposal=None.
    corrupt = sig.model_copy(update={"payload": sig.payload | {"confluence_narrative": "x"}})
    record = classify(corrupt, [])
    assert record.kind is DecisionKind.PUBLISHED and record.proposal is None
    text = build_review_user_prompt(record, _resolution(sig.id))
    assert "## Analyzer proposal" in text
    assert "raw journal row shown" in text and '"entry_price": 100.0' in text


def test_system_prompt_forbids_indicators() -> None:
    assert "RSI" in REVIEW_SYSTEM_PROMPT and "forbidden" in REVIEW_SYSTEM_PROMPT
    assert "gate_correct" in REVIEW_SYSTEM_PROMPT and "NOT_APPLICABLE" in REVIEW_SYSTEM_PROMPT
