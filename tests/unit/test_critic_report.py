"""Unit tests for the Critic v0 period report (design §6.3, plan Task 12) -- offline.

``compute_aggregates`` / ``render_markdown`` are pure functions over
``DecisionSummary`` rows; the ``fetch_all_*`` helpers and ``CriticReporter``
are exercised with a fake store and the fake Anthropic client pattern from
``test_forecaster.py`` / ``test_critic_reviewer.py``. No network, no database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.agents.critic.exit_gate import evaluate_exit_gate
from src.agents.critic.models import CriticReport, ExitGateReport
from src.agents.critic.prompts import REPORT_SYSTEM_PROMPT, build_report_user_prompt
from src.agents.critic.report import (
    EMPTY_PERIOD_REPORT,
    KIND_UNRESOLVED_PROPOSAL,
    Aggregates,
    CriticReporter,
    Lesson,
    compute_aggregates,
    decision_kind_of,
    fetch_all_decisions,
    fetch_all_scan_runs,
    fetch_all_setups,
    recent_lessons,
    render_markdown,
)
from src.common.llm import DEFAULT_MODEL
from src.common.models import (
    ActiveSetupStatus,
    AgentRole,
    ScanStatus,
    SignalDirection,
    SignalStatus,
)
from src.persistence.models import (
    DecisionSummary,
    StoredActiveSetup,
    StoredCriticReport,
    StoredScanRun,
)

SINCE = datetime(2026, 7, 1, tzinfo=UTC)
UNTIL = datetime(2026, 7, 8, tzinfo=UTC)
D0 = datetime(2026, 7, 1, 8, 3, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _decision(
    *,
    kind: str | None,
    outcome: str | None = None,
    realized_r: float | None = None,
    direction: SignalDirection | None = SignalDirection.LONG,
    violated_rule: str | None = None,
    symbol: str = "BTCUSDT",
    created_at: datetime = D0,
    review: dict[str, Any] | None = None,
    review_error: str | None = None,
) -> DecisionSummary:
    return DecisionSummary(
        id=uuid4(),
        scan_id=uuid4(),
        symbol=symbol,
        direction=direction,
        status=SignalStatus.PUBLISHED if direction else SignalStatus.SKIPPED,
        created_at=created_at,
        violated_rule=violated_rule,
        outcome=None,
        decision_kind=kind,
        resolution_outcome=outcome,
        realized_r=realized_r,
        entered_at=None,
        review=review,
        review_error=review_error,
    )


def _review(
    *,
    failure_category: str = "NONE",
    skeptic_vindicated: str = "NOT_APPLICABLE",
    lesson: str = "Keep the discount FVG rule; it worked.",
) -> dict[str, Any]:
    return {
        "smc_read_valid": True,
        "skeptic_vindicated": skeptic_vindicated,
        "judge_correct": True,
        "gate_correct": None,
        "failure_category": failure_category,
        "lesson": lesson,
        "confidence": "HIGH",
    }


def _fixture() -> list[DecisionSummary]:
    """The plan's 8-row fixture: 3 PUBLISHED, 2 VETOED, 2 GATE_REJECTED, 1 NO_SETUP."""
    return [
        _decision(kind="PUBLISHED", outcome="WIN", realized_r=3.0, created_at=D0),
        _decision(
            kind="PUBLISHED",
            outcome="WIN",
            realized_r=3.0,
            symbol="ETHUSDT",
            created_at=D0.replace(hour=13),
        ),
        _decision(
            kind="PUBLISHED",
            outcome="LOSS",
            realized_r=-1.0,
            created_at=D0 + timedelta(days=1),
            review=_review(
                failure_category="WRONG_ZONE",
                skeptic_vindicated="YES",
                lesson="BTCUSDT OB sat in premium; the long should not have been published.",
            ),
        ),
        _decision(
            kind="VETOED",
            outcome="LOSS",
            realized_r=-1.0,
            direction=SignalDirection.SHORT,
            review=None,
            review_error="RuntimeError: api down",
        ),
        _decision(
            kind="VETOED",
            outcome="WIN",
            realized_r=3.0,
            direction=SignalDirection.SHORT,
            created_at=D0 + timedelta(days=2),
            review=_review(
                skeptic_vindicated="NO",
                lesson="ETHUSDT veto blocked a clean winner; Judge over-weighted the Skeptic.",
            ),
        ),
        _decision(
            kind="GATE_REJECTED",
            outcome="LOSS",
            realized_r=-1.0,
            direction=None,
            violated_rule="RULE_3_PREMIUM_DISCOUNT",
        ),
        _decision(
            kind="GATE_REJECTED",
            outcome="EXPIRED",
            realized_r=0.0,
            direction=None,
            violated_rule="RULE_2_MIN_RR",
        ),
        _decision(kind=None, direction=None),
    ]


def _critic_report_payload() -> dict[str, Any]:
    return {
        "summary": "Two of three published signals won; the gates saved two losses.",
        "key_patterns": ["Premium longs lose", "Vetoes are 50/50"],
        "skeptic_assessment": "Skeptic objection was vindicated once; sample too small.",
        "judge_assessment": "Vetoes avoided one loss and blocked one winner.",
        "gate_assessment": "Both gate rejections avoided a loss or expiry.",
        "proposed_changes": [
            {
                "target": "JUDGE_SYSTEM_PROMPT",
                "change": "Weight HIGH-severity Skeptic objections only when macro data is fresh.",
                "rationale": "Respected win rate 100% vs overruled 0% on n=2; do not over-fit.",
                "evidence_signal_ids": [str(uuid4())],
            }
        ],
    }


def _response(payload: dict[str, Any]) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name="emit_critic_report", id="toolu_1", input=payload)
    return SimpleNamespace(
        content=[block], usage=SimpleNamespace(input_tokens=3000, output_tokens=400)
    )


def _client(responses: list[Any]) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    return client


class FakeStore:
    """Duck-typed ``SignalStore`` subset: paginated reads + report/agent-run writes."""

    def __init__(
        self,
        *,
        decision_pages: list[list[DecisionSummary]] | None = None,
        scan_run_pages: list[list[StoredScanRun]] | None = None,
        setup_pages: list[list[StoredActiveSetup]] | None = None,
    ) -> None:
        self._decision_pages = list(decision_pages or [[]])
        self._scan_run_pages = list(scan_run_pages or [[]])
        self._setup_pages = list(setup_pages or [[]])
        self.decision_calls: list[dict[str, Any]] = []
        self.scan_run_calls: list[dict[str, Any]] = []
        self.setup_calls: list[dict[str, Any]] = []
        self.saved_reports: list[StoredCriticReport] = []
        self.agent_runs: list[dict[str, Any]] = []

    async def list_decision_summaries(
        self,
        *,
        since: datetime,
        until: datetime,
        after: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[DecisionSummary]:
        self.decision_calls.append({"since": since, "until": until, "after": after, "limit": limit})
        return self._decision_pages.pop(0) if self._decision_pages else []

    async def list_scan_runs_between(
        self,
        *,
        since: datetime,
        until: datetime,
        after: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[StoredScanRun]:
        self.scan_run_calls.append({"since": since, "until": until, "after": after, "limit": limit})
        return self._scan_run_pages.pop(0) if self._scan_run_pages else []

    async def list_active_setups(
        self, *, after: tuple[datetime, UUID] | None, limit: int
    ) -> list[StoredActiveSetup]:
        self.setup_calls.append({"after": after, "limit": limit})
        return self._setup_pages.pop(0) if self._setup_pages else []

    async def save_critic_report(self, report: StoredCriticReport) -> None:
        self.saved_reports.append(report)

    async def log_agent_run(self, **kwargs: Any) -> UUID:
        self.agent_runs.append(kwargs)
        return uuid4()


# ---------------------------------------------------------------------------
# decision_kind_of
# ---------------------------------------------------------------------------


def test_kind_prefers_resolution_then_falls_back_to_row_shape() -> None:
    assert decision_kind_of(_decision(kind="VETOED", outcome="WIN")) == "VETOED"
    assert decision_kind_of(_decision(kind=None, direction=None)) == "NO_SETUP"
    assert (
        decision_kind_of(_decision(kind=None, direction=None, violated_rule="RULE_2_MIN_RR"))
        == "GATE_REJECTED"
    )
    assert decision_kind_of(_decision(kind=None)) == KIND_UNRESOLVED_PROPOSAL


# ---------------------------------------------------------------------------
# compute_aggregates
# ---------------------------------------------------------------------------


def test_aggregates_on_plan_fixture() -> None:
    agg = compute_aggregates(_fixture(), period_start=SINCE, period_end=UNTIL)
    assert isinstance(agg, Aggregates)
    assert (agg.period_start, agg.period_end) == (SINCE, UNTIL)
    assert agg.total_decisions == 8
    assert agg.by_kind == {"PUBLISHED": 3, "VETOED": 2, "GATE_REJECTED": 2, "NO_SETUP": 1}
    assert agg.outcomes_by_kind == {
        "PUBLISHED": {"WIN": 2, "LOSS": 1},
        "VETOED": {"LOSS": 1, "WIN": 1},
        "GATE_REJECTED": {"LOSS": 1, "EXPIRED": 1},
    }
    assert agg.win_rate_by_kind["PUBLISHED"] == 2 / 3
    assert agg.win_rate_by_kind["VETOED"] == 0.5
    assert agg.win_rate_by_kind["GATE_REJECTED"] == 0.0
    assert agg.win_rate_by_kind["NO_SETUP"] is None
    assert agg.total_r_published == 5.0
    assert agg.judge_value == 0.5
    assert agg.gate_value == 1.0
    assert agg.failure_categories == {"WRONG_ZONE": 1, "NONE": 1}
    assert agg.reviews_present == 2 and agg.reviews_failed == 1


def test_aggregates_published_only_breakdowns() -> None:
    agg = compute_aggregates(_fixture(), period_start=SINCE, period_end=UNTIL)
    # By symbol / hour count PUBLISHED decisions only: the VETOED SHORTs must not appear.
    assert agg.by_symbol == {"BTCUSDT": {"WIN": 1, "LOSS": 1}, "ETHUSDT": {"WIN": 1}}
    assert agg.by_hour_utc == {8: {"WIN": 1, "LOSS": 1}, 13: {"WIN": 1}}
    assert agg.by_rule == {
        "RULE_3_PREMIUM_DISCOUNT": {"LOSS": 1},
        "RULE_2_MIN_RR": {"EXPIRED": 1},
    }


def test_aggregates_skeptic_value_from_reviews() -> None:
    agg = compute_aggregates(_fixture(), period_start=SINCE, period_end=UNTIL)
    # Overruled = PUBLISHED with a Skeptic verdict (the LOSS with YES) -> 0/1.
    # Respected = VETOED with a Skeptic verdict (the counterfactual WIN with NO) -> 1/1.
    assert agg.skeptic_value == {"overruled_win_rate": 0.0, "respected_win_rate": 1.0}


def test_aggregates_buckets_hours_in_utc() -> None:
    from datetime import timezone

    plus_five = timezone(timedelta(hours=5))
    row = _decision(
        kind="PUBLISHED",
        outcome="WIN",
        realized_r=3.0,
        created_at=datetime(2026, 7, 2, 2, 0, tzinfo=plus_five),  # 21:00 UTC previous day
    )
    agg = compute_aggregates([row], period_start=SINCE, period_end=UNTIL)
    assert agg.by_hour_utc == {21: {"WIN": 1}}


def test_aggregates_unresolved_and_unresolvable_rows() -> None:
    rows = [
        _decision(kind=None),  # proposal awaiting replay
        _decision(
            kind=None, direction=None, violated_rule="RULE_2_MIN_RR"
        ),  # gate skip, unreplayed
        _decision(kind="PUBLISHED", outcome="UNRESOLVABLE", realized_r=0.0),
        _decision(kind="VETOED", outcome="UNRESOLVABLE", realized_r=0.0),
    ]
    agg = compute_aggregates(rows, period_start=SINCE, period_end=UNTIL)
    assert agg.by_kind == {
        KIND_UNRESOLVED_PROPOSAL: 1,
        "GATE_REJECTED": 1,
        "PUBLISHED": 1,
        "VETOED": 1,
    }
    assert agg.outcomes_by_kind == {"PUBLISHED": {"UNRESOLVABLE": 1}, "VETOED": {"UNRESOLVABLE": 1}}
    # UNRESOLVABLE says nothing about the market: no win rate, no judge / gate value.
    assert agg.win_rate_by_kind == {
        KIND_UNRESOLVED_PROPOSAL: None,
        "GATE_REJECTED": None,
        "PUBLISHED": None,
        "VETOED": None,
    }
    assert agg.judge_value is None and agg.gate_value is None
    assert agg.total_r_published == 0.0
    assert agg.skeptic_value == {"overruled_win_rate": None, "respected_win_rate": None}


def test_aggregates_empty_period() -> None:
    agg = compute_aggregates([], period_start=SINCE, period_end=UNTIL)
    assert agg.total_decisions == 0 and agg.by_kind == {} and agg.outcomes_by_kind == {}
    assert agg.total_r_published == 0.0
    assert agg.judge_value is None and agg.gate_value is None
    assert agg.failure_categories == {} and agg.reviews_present == 0 and agg.reviews_failed == 0


# ---------------------------------------------------------------------------
# recent_lessons
# ---------------------------------------------------------------------------


def test_recent_lessons_newest_first_with_ids_and_limit() -> None:
    rows = _fixture()
    lessons = recent_lessons(rows)
    assert [lesson.kind for lesson in lessons] == ["VETOED", "PUBLISHED"]
    assert lessons[0].symbol == "BTCUSDT" and lessons[0].signal_id == rows[4].id
    assert lessons[1].lesson.startswith("BTCUSDT OB sat in premium")
    assert recent_lessons(rows, limit=1) == lessons[:1]


def test_recent_lessons_skips_reviews_without_a_lesson() -> None:
    bad = _decision(kind="PUBLISHED", outcome="WIN", review={"failure_category": "NONE"})
    assert recent_lessons([bad]) == []


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


def _report() -> CriticReport:
    return CriticReport.model_validate(_critic_report_payload())


def _gate(passed: bool) -> ExitGateReport:
    if passed:
        published = [_decision(kind="PUBLISHED", outcome="WIN") for _ in range(5)]
        skips = [_decision(kind=None, direction=None) for _ in range(4)]
        skips.append(_decision(kind=None, direction=None, violated_rule="RULE_2_MIN_RR"))
        setups = [
            StoredActiveSetup(
                id=uuid4(),
                signal_id=p.id,
                opened_at=D0,
                status=ActiveSetupStatus.OPEN,
                last_evaluated_at=D0,
                latest_evaluation={"status": "STILL_VALID"},
            )
            for p in published
        ]
        runs = [
            StoredScanRun(
                id=uuid4(), started_at=D0 + timedelta(days=d, minutes=i), status=ScanStatus.SUCCESS
            )
            for d in range(7)
            for i in range(16)
        ]
        return evaluate_exit_gate(scan_runs=runs, decisions=published + skips, setups=setups)
    return evaluate_exit_gate(scan_runs=[], decisions=[], setups=[])


def test_markdown_full_layout_with_gate() -> None:
    agg = compute_aggregates(_fixture(), period_start=SINCE, period_end=UNTIL)
    lessons = recent_lessons(_fixture())
    md = render_markdown(aggregates=agg, report=_report(), exit_gate=_gate(True), lessons=lessons)
    assert md.startswith("# Critic report — 2026-07-01 → 2026-07-08\n")
    assert "## Slice 2 exit gate" in md
    assert "| # | Criterion | Result | Evidence |" in md
    assert "| 1 | 7 consecutive days autonomous | PASS |" in md
    assert "**Verdict:** PASS" in md
    assert "## Summary\nTwo of three published signals won" in md
    assert "| Kind | Decisions | WIN | LOSS | EXPIRED | Win rate |" in md
    assert "| PUBLISHED | 3 | 2 | 1 | 0 | 67% |" in md
    assert "| VETOED | 2 | 1 | 1 | 0 | 50% |" in md
    assert "| GATE_REJECTED | 2 | 0 | 1 | 1 | 0% |" in md
    assert "| NO_SETUP | 1 | 0 | 0 | 0 | n/a |" in md
    assert "Total realised R (published): +5.00" in md
    assert "Skeptic value: overruled win-rate 0% vs respected 100%" in md
    assert "Judge value: 50% of vetoes avoided a loss/expiry" in md
    assert "Gate value: 100% of rejections avoided a loss/expiry" in md
    assert "### By symbol (published)" in md and "| ETHUSDT | 1 | 0 | 0 | 100% |" in md
    assert "### By hour UTC (published)" in md and "| 13:00 | 1 | 0 | 0 | 100% |" in md
    assert "### By rule (counterfactual)" in md and "| RULE_2_MIN_RR | 0 | 0 | 1 | n/a |" in md
    assert "### Failure categories" in md and "| WRONG_ZONE | 1 |" in md
    assert "## Key patterns\n- Premium longs lose\n- Vetoes are 50/50" in md
    assert "## Agent assessments" in md
    assert "**Skeptic.** Skeptic objection was vindicated once" in md
    assert "**Judge.** Vetoes avoided" in md and "**Gates.** Both gate rejections" in md
    assert "## Recent lessons" in md
    assert "- **BTCUSDT** (VETOED): ETHUSDT veto blocked a clean winner" in md
    assert f"`{lessons[0].signal_id}`" in md
    assert md.rstrip().endswith("_Generated by Critic v0. Reviews: 2 ok / 1 failed._")


def test_markdown_proposed_changes_render_as_checklist() -> None:
    agg = compute_aggregates(_fixture(), period_start=SINCE, period_end=UNTIL)
    report = _report()
    md = render_markdown(aggregates=agg, report=report, exit_gate=None, lessons=[])
    change = report.proposed_changes[0]
    assert "## Proposed changes\n" in md
    assert f"- [ ] **JUDGE_SYSTEM_PROMPT** — {change.change}" in md
    assert f"  _Why:_ {change.rationale}  _Evidence:_ `{change.evidence_signal_ids[0]}`" in md


def test_markdown_omits_gate_section_when_none_and_marks_fail() -> None:
    agg = compute_aggregates(_fixture(), period_start=SINCE, period_end=UNTIL)
    without = render_markdown(aggregates=agg, report=_report(), exit_gate=None, lessons=[])
    assert "## Slice 2 exit gate" not in without and "**Verdict:**" not in without
    failing = render_markdown(aggregates=agg, report=_report(), exit_gate=_gate(False), lessons=[])
    assert "**Verdict:** FAIL" in failing and "| FAIL |" in failing


def test_markdown_empty_period_placeholders() -> None:
    agg = compute_aggregates([], period_start=SINCE, period_end=UNTIL)
    md = render_markdown(aggregates=agg, report=EMPTY_PERIOD_REPORT, exit_gate=None, lessons=[])
    assert "No decisions in period." in md
    assert "Skeptic value: overruled win-rate n/a vs respected n/a" in md
    assert "Judge value: n/a" in md and "Gate value: n/a" in md
    assert "## Proposed changes\n_No changes proposed._" in md
    assert "## Recent lessons\n_none_" in md
    assert "## Key patterns\n_none_" in md
    assert "Reviews: 0 ok / 0 failed." in md


def test_markdown_escapes_pipes_in_free_text_cells() -> None:
    agg = compute_aggregates([], period_start=SINCE, period_end=UNTIL)
    gate = ExitGateReport(
        criteria=[
            *(c for c in _gate(False).criteria[:3]),
            _gate(False).criteria[3].model_copy(update={"evidence": "rules: a | b"}),
        ],
        all_passed=False,
    )
    md = render_markdown(aggregates=agg, report=EMPTY_PERIOD_REPORT, exit_gate=gate, lessons=[])
    assert "rules: a \\| b" in md


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def test_report_system_prompt_names_targets_and_forbids_indicators() -> None:
    for target in ("SKEPTIC_SYSTEM_PROMPT", "JUDGE_SYSTEM_PROMPT", "FORECASTER_SYSTEM_PROMPT"):
        assert target in REPORT_SYSTEM_PROMPT
    assert "risk_gates.<CONSTANT>" in REPORT_SYSTEM_PROMPT
    assert "RSI/MACD" in REPORT_SYSTEM_PROMPT and "at most 10" in REPORT_SYSTEM_PROMPT


def test_report_user_prompt_carries_statistics_and_lesson_ids() -> None:
    rows = _fixture()
    agg = compute_aggregates(rows, period_start=SINCE, period_end=UNTIL)
    lessons = recent_lessons(rows)
    text = build_report_user_prompt(agg, lessons)
    assert "## Period" in text and "2026-07-01" in text and "2026-07-08" in text
    assert "## Statistics" in text and '"total_r_published": 5.0' in text
    assert '"judge_value": 0.5' in text and '"WRONG_ZONE": 1' in text
    assert "## Per-decision lessons" in text
    for lesson in lessons:
        assert str(lesson.signal_id) in text and lesson.lesson in text


def test_report_user_prompt_with_no_lessons_says_so() -> None:
    agg = compute_aggregates([], period_start=SINCE, period_end=UNTIL)
    text = build_report_user_prompt(agg, [])
    assert "## Per-decision lessons" in text and "(none)" in text


# ---------------------------------------------------------------------------
# fetch_all_* pagination
# ---------------------------------------------------------------------------


async def test_fetch_all_decisions_pages_until_short_page() -> None:
    first = [
        _decision(kind="PUBLISHED", outcome="WIN", created_at=D0 + timedelta(minutes=i))
        for i in range(500)
    ]
    second = [_decision(kind=None) for _ in range(3)]
    store = FakeStore(decision_pages=[first, second])
    rows = await fetch_all_decisions(store, since=SINCE, until=UNTIL)
    assert len(rows) == 503 and rows[:500] == first and rows[500:] == second
    assert len(store.decision_calls) == 2
    assert store.decision_calls[0] == {"since": SINCE, "until": UNTIL, "after": None, "limit": 500}
    assert store.decision_calls[1]["after"] == (first[-1].created_at, first[-1].id)


async def test_fetch_all_decisions_single_short_page_makes_one_call() -> None:
    store = FakeStore(decision_pages=[[_decision(kind=None)]])
    rows = await fetch_all_decisions(store, since=SINCE, until=UNTIL, page=10)
    assert len(rows) == 1 and len(store.decision_calls) == 1


async def test_fetch_all_decisions_exact_page_then_empty() -> None:
    page = [_decision(kind=None) for _ in range(4)]
    store = FakeStore(decision_pages=[page, []])
    rows = await fetch_all_decisions(store, since=SINCE, until=UNTIL, page=4)
    assert rows == page and len(store.decision_calls) == 2


async def test_fetch_all_rejects_non_positive_page() -> None:
    store = FakeStore()
    with pytest.raises(ValueError, match="page"):
        await fetch_all_decisions(store, since=SINCE, until=UNTIL, page=0)


async def test_fetch_all_scan_runs_cursor_is_started_at_and_id() -> None:
    first = [
        StoredScanRun(id=uuid4(), started_at=D0 + timedelta(minutes=i), status=ScanStatus.SUCCESS)
        for i in range(2)
    ]
    store = FakeStore(scan_run_pages=[first, []])
    rows = await fetch_all_scan_runs(store, since=SINCE, until=UNTIL, page=2)
    assert rows == first
    assert store.scan_run_calls[0]["after"] is None
    assert store.scan_run_calls[1]["after"] == (first[-1].started_at, first[-1].id)
    assert store.scan_run_calls[1]["since"] == SINCE and store.scan_run_calls[1]["until"] == UNTIL


async def test_fetch_all_setups_cursor_is_opened_at_and_id() -> None:
    first = [
        StoredActiveSetup(
            id=uuid4(),
            signal_id=uuid4(),
            opened_at=D0 + timedelta(minutes=i),
            status=ActiveSetupStatus.OPEN,
        )
        for i in range(2)
    ]
    store = FakeStore(setup_pages=[first, [first[0]]])
    rows = await fetch_all_setups(store, page=2)
    assert len(rows) == 3
    assert store.setup_calls == [
        {"after": None, "limit": 2},
        {"after": (first[-1].opened_at, first[-1].id), "limit": 2},
    ]


# ---------------------------------------------------------------------------
# CriticReporter.build
# ---------------------------------------------------------------------------


async def test_build_calls_claude_saves_report_and_logs_agent_run() -> None:
    rows = _fixture()
    store = FakeStore(decision_pages=[rows])
    client = _client([_response(_critic_report_payload())])
    now = datetime(2026, 7, 9, 12, tzinfo=UTC)
    reporter = CriticReporter(store=store, client=client, now=lambda: now)

    stored, gate = await reporter.build(since=SINCE, until=UNTIL, include_exit_gate=False)

    assert gate is None
    assert isinstance(stored, StoredCriticReport)
    assert (stored.period_start, stored.period_end, stored.created_at) == (SINCE, UNTIL, now)
    assert stored.pr_url is None
    assert store.saved_reports == [stored]
    # The JSONB report round-trips through the typed model.
    rebuilt = CriticReport.model_validate(stored.report)
    assert rebuilt.proposed_changes[0].target == "JUDGE_SYSTEM_PROMPT"
    # JSON mode: UUIDs are stored as strings so the row is plain JSONB.
    assert stored.report["proposed_changes"][0]["evidence_signal_ids"] == [
        str(u) for u in rebuilt.proposed_changes[0].evidence_signal_ids
    ]
    assert stored.markdown.startswith("# Critic report — 2026-07-01 → 2026-07-08")
    assert "## Slice 2 exit gate" not in stored.markdown
    # Exactly one Claude call, with the report tool and the shared model.
    kwargs = client.messages.create.await_args.kwargs
    assert client.messages.create.await_count == 1
    assert kwargs["model"] == DEFAULT_MODEL
    assert kwargs["system"] == REPORT_SYSTEM_PROMPT
    assert kwargs["tools"][0]["name"] == "emit_critic_report"
    assert kwargs["tool_choice"] == {"type": "tool", "name": "emit_critic_report"}
    assert '"total_r_published": 5.0' in kwargs["messages"][0]["content"]
    # NFR-2.3: the call cost lands in agent_runs under the critic role.
    assert len(store.agent_runs) == 1
    logged = store.agent_runs[0]
    assert logged["agent_role"] is AgentRole.CRITIC
    newest = max(rows, key=lambda d: (d.created_at, d.id))
    assert logged["scan_id"] == newest.scan_id
    assert logged["output"] == stored.report
    assert logged["token_usage"] == {"tokens_in": 3000, "tokens_out": 400}
    assert logged["cost_usd"] is not None and logged["cost_usd"] > 0
    assert logged["input_hash"]
    # No exit-gate reads were made.
    assert store.scan_run_calls == [] and store.setup_calls == []


async def test_build_with_exit_gate_reads_runs_and_setups() -> None:
    rows = _fixture()
    runs = [
        StoredScanRun(
            id=uuid4(), started_at=D0 + timedelta(days=d, minutes=i), status=ScanStatus.SUCCESS
        )
        for d in range(7)
        for i in range(16)
    ]
    store = FakeStore(decision_pages=[rows], scan_run_pages=[runs], setup_pages=[[]])
    client = _client([_response(_critic_report_payload())])
    stored, gate = await CriticReporter(store=store, client=client).build(
        since=SINCE, until=UNTIL, include_exit_gate=True
    )
    assert gate is not None and len(gate.criteria) == 4
    assert gate.criteria[0].passed is True  # 7 healthy days
    assert gate.all_passed is False  # only 3 published (no setups rows) -> criterion 2 fails
    assert "## Slice 2 exit gate" in stored.markdown and "**Verdict:** FAIL" in stored.markdown
    assert store.scan_run_calls[0]["since"] == SINCE and store.scan_run_calls[0]["until"] == UNTIL
    assert len(store.setup_calls) == 1


async def test_build_empty_period_skips_claude_and_agent_run() -> None:
    store = FakeStore(decision_pages=[[]])
    client = _client([])
    stored, gate = await CriticReporter(store=store, client=client).build(
        since=SINCE, until=UNTIL, include_exit_gate=False
    )
    assert gate is None
    client.messages.create.assert_not_awaited()
    assert store.agent_runs == []
    assert store.saved_reports == [stored]
    assert stored.report == EMPTY_PERIOD_REPORT.model_dump(mode="json")
    assert stored.report["summary"] == "No decisions in period."
    assert "No decisions in period." in stored.markdown


async def test_build_propagates_claude_failure_without_saving() -> None:
    store = FakeStore(decision_pages=[_fixture()])
    client = _client([RuntimeError("api down")])
    with pytest.raises(RuntimeError, match="api down"):
        await CriticReporter(store=store, client=client).build(
            since=SINCE, until=UNTIL, include_exit_gate=False
        )
    assert store.saved_reports == [] and store.agent_runs == []


def test_lesson_is_frozen_value_object() -> None:
    lesson = Lesson(signal_id=uuid4(), symbol="BTCUSDT", kind="PUBLISHED", lesson="x" * 5)
    with pytest.raises(AttributeError):
        lesson.symbol = "ETHUSDT"  # type: ignore[misc]
