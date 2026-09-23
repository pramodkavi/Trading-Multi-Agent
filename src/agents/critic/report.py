"""Critic v0 period report: aggregates, the ``CriticReport`` call and markdown (design §6.3).

Layer 2 of Critic v0, run from GitHub Actions (``scripts/run_critic.py report``)
once the review queue for the period is empty. The flow is:

    fetch_all_decisions (keyset-paged DecisionSummary rows)
      -> compute_aggregates (pure)            -> recent_lessons (pure)
      -> structured_completion(CriticReport)  -> store.log_agent_run(critic)
      -> evaluate_exit_gate (optional, pure)  -> render_markdown (pure)
      -> store.save_critic_report

Everything except the three ``fetch_all_*`` helpers and ``CriticReporter`` is a
pure function so the numbers and the markdown are unit-testable without a
store or the network. The statistics are computed here, in Python, and handed
to Claude as JSON: the model interprets, it never counts (design §6.3).

Decision kinds. ``DecisionSummary.decision_kind`` is filled by the
``OutcomeResolver`` and is therefore NULL for rows that are not replayed yet
and for ``NO_SETUP`` skips, which are never replayed. ``decision_kind_of``
derives the kind from the row shape in those cases and labels a proposal
that still awaits its replay ``UNRESOLVED_PROPOSAL`` so it is counted but
never mistaken for a resolved one.

Rates. Every rate is ``None`` (never ``0.0``) when its denominator is zero,
so a tiny period reads as "no sample" rather than "0%". ``UNRESOLVABLE``
outcomes say nothing about the market and are excluded from every rate.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID, uuid4

from src.agents.critic.exit_gate import evaluate_exit_gate
from src.agents.critic.models import (
    CriticReport,
    DecisionKind,
    ExitGateReport,
    ResolutionOutcome,
    SkepticVerdict,
)
from src.agents.critic.prompts import REPORT_SYSTEM_PROMPT, build_report_user_prompt
from src.common.llm import DEFAULT_MODEL, structured_completion
from src.common.models import AgentRole
from src.persistence.models import (
    DecisionSummary,
    StoredActiveSetup,
    StoredCriticReport,
    StoredScanRun,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from anthropic import AsyncAnthropic

    from src.persistence.store import SignalStore

__all__ = [
    "EMPTY_PERIOD_REPORT",
    "KIND_UNRESOLVED_PROPOSAL",
    "Aggregates",
    "CriticReporter",
    "Lesson",
    "ReportBuild",
    "compute_aggregates",
    "decision_kind_of",
    "fetch_all_decisions",
    "fetch_all_scan_runs",
    "fetch_all_setups",
    "recent_lessons",
    "render_markdown",
]

logger = logging.getLogger(__name__)

KIND_UNRESOLVED_PROPOSAL: Final[str] = "UNRESOLVED_PROPOSAL"
"""Pseudo-kind for a proposal (``direction`` set) whose replay has not run yet."""

DEFAULT_PAGE: Final[int] = 500
"""Rows per keyset page for the ``fetch_all_*`` helpers (Data API caps a response at 1 MiB)."""

DEFAULT_LESSON_LIMIT: Final[int] = 25
"""Most recent per-decision lessons handed to the report call and rendered."""

REPORT_MAX_OUTPUT_TOKENS: Final[int] = 8192
"""``CriticReport`` can carry a 4000-char summary plus ten 2000-char changes; the
``structured_completion`` default (4096) is sized for per-decision agents."""

EMPTY_PERIOD_REPORT: Final[CriticReport] = CriticReport(
    summary="No decisions in period.",
    key_patterns=[],
    skeptic_assessment="n/a",
    judge_assessment="n/a",
    gate_assessment="n/a",
    proposed_changes=[],
)
"""Used instead of a Claude call when the period holds zero decisions."""

_TOOL_NAME: Final[str] = "emit_critic_report"
_TOOL_DESCRIPTION: Final[str] = "Record the period Critic report with its proposed rule changes."

# Canonical row order for the ``## Numbers`` table; unknown kinds follow, sorted.
_KIND_ORDER: Final[tuple[str, ...]] = (
    DecisionKind.PUBLISHED.value,
    DecisionKind.VETOED.value,
    DecisionKind.GATE_REJECTED.value,
    DecisionKind.NO_SETUP.value,
    KIND_UNRESOLVED_PROPOSAL,
)

_WIN: Final[str] = ResolutionOutcome.WIN.value
_LOSS: Final[str] = ResolutionOutcome.LOSS.value
_EXPIRED: Final[str] = ResolutionOutcome.EXPIRED.value

# Counterfactual outcomes that mean "not taking the trade avoided pain".
_AVOIDED: Final[frozenset[str]] = frozenset({_LOSS, _EXPIRED})
# Review verdicts that prove a Skeptic actually ran on the decision.
_SKEPTIC_RAN: Final[frozenset[str]] = frozenset({SkepticVerdict.YES.value, SkepticVerdict.NO.value})


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Kinds and value objects
# ---------------------------------------------------------------------------


def decision_kind_of(decision: DecisionSummary) -> str:
    """Kind label for one summary row.

    The resolver's ``decision_kind`` wins when present. Otherwise a skip row is
    ``GATE_REJECTED`` when it names a violated rule and ``NO_SETUP`` when it
    does not; a proposal row without a resolution is ``UNRESOLVED_PROPOSAL``.
    """
    if decision.decision_kind:
        return decision.decision_kind
    if decision.direction is None:
        if decision.violated_rule:
            return DecisionKind.GATE_REJECTED.value
        return DecisionKind.NO_SETUP.value
    return KIND_UNRESOLVED_PROPOSAL


@dataclass(frozen=True)
class Lesson:
    """One per-decision reviewer lesson, keyed by the signal it came from.

    The ``signal_id`` travels with the text so the report call can cite it in
    ``ProposedRuleChange.evidence_signal_ids`` and the markdown can show it.
    """

    signal_id: UUID
    symbol: str
    kind: str
    lesson: str


@dataclass(frozen=True)
class Aggregates:
    """Period statistics over ``DecisionSummary`` rows (design §6.3).

    Outcome dicts map ``WIN / LOSS / EXPIRED / UNRESOLVABLE`` to counts and only
    contain keys that occurred. Every rate is ``WIN / (WIN + LOSS)`` or, for the
    Judge / gate values, ``(LOSS + EXPIRED) / (WIN + LOSS + EXPIRED)`` over the
    counterfactual replays; ``None`` when the denominator is zero.
    """

    period_start: datetime
    period_end: datetime
    total_decisions: int
    by_kind: dict[str, int]
    outcomes_by_kind: dict[str, dict[str, int]]
    win_rate_by_kind: dict[str, float | None]
    total_r_published: float
    by_symbol: dict[str, dict[str, int]]
    by_hour_utc: dict[int, dict[str, int]]
    by_rule: dict[str, dict[str, int]]
    skeptic_value: dict[str, float | None]
    judge_value: float | None
    gate_value: float | None
    failure_categories: dict[str, int]
    reviews_present: int
    reviews_failed: int


# ---------------------------------------------------------------------------
# Pure aggregation
# ---------------------------------------------------------------------------


def _win_rate(outcomes: Counter[str]) -> float | None:
    wins, losses = outcomes[_WIN], outcomes[_LOSS]
    return None if wins + losses == 0 else wins / (wins + losses)


def _avoided_share(outcomes: Counter[str]) -> float | None:
    """Share of counterfactual replays that ended LOSS / EXPIRED (skipping paid off)."""
    resolved = outcomes[_WIN] + outcomes[_LOSS] + outcomes[_EXPIRED]
    if resolved == 0:
        return None
    return sum(outcomes[o] for o in _AVOIDED) / resolved


def _plain(counters: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
    return {key: dict(counter) for key, counter in counters.items()}


def _skeptic_verdict(decision: DecisionSummary) -> str | None:
    if decision.review is None:
        return None
    verdict = decision.review.get("skeptic_vindicated")
    return verdict if isinstance(verdict, str) else None


def compute_aggregates(
    decisions: Sequence[DecisionSummary], *, period_start: datetime, period_end: datetime
) -> Aggregates:
    """Fold ``DecisionSummary`` rows into the period statistics. Pure.

    ``by_symbol`` / ``by_hour_utc`` cover ``PUBLISHED`` decisions only (the hour
    is the decision hour in UTC); ``by_rule`` covers ``GATE_REJECTED``
    counterfactuals keyed by ``violated_rule``. ``skeptic_value`` compares the
    win rate of ``PUBLISHED`` decisions on which a Skeptic ran (the Judge
    *overruled* its objection) with the counterfactual win rate of ``VETOED``
    decisions on which a Skeptic ran (the Judge *respected* it); "a Skeptic
    ran" is read from the reviewer's ``skeptic_vindicated`` being YES or NO.
    ``failure_categories`` counts every reviewer verdict, ``NONE`` included, so
    the reader sees the denominator.
    """
    by_kind: Counter[str] = Counter()
    outcomes_by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    by_symbol: dict[str, Counter[str]] = defaultdict(Counter)
    by_hour: dict[int, Counter[str]] = defaultdict(Counter)
    by_rule: dict[str, Counter[str]] = defaultdict(Counter)
    overruled: Counter[str] = Counter()
    respected: Counter[str] = Counter()
    failure_categories: Counter[str] = Counter()
    total_r_published = 0.0
    reviews_present = reviews_failed = 0

    for decision in decisions:
        kind = decision_kind_of(decision)
        by_kind[kind] += 1
        outcome = decision.resolution_outcome
        if outcome is not None:
            outcomes_by_kind[kind][outcome] += 1
        published = kind == DecisionKind.PUBLISHED.value
        if published:
            total_r_published += decision.realized_r or 0.0
            if outcome is not None:
                by_symbol[decision.symbol][outcome] += 1
                by_hour[decision.created_at.astimezone(UTC).hour][outcome] += 1
        if (
            kind == DecisionKind.GATE_REJECTED.value
            and outcome is not None
            and decision.violated_rule
        ):
            by_rule[decision.violated_rule][outcome] += 1

        if decision.review is not None:
            reviews_present += 1
            category = decision.review.get("failure_category")
            if isinstance(category, str):
                failure_categories[category] += 1
            verdict = _skeptic_verdict(decision)
            if verdict in _SKEPTIC_RAN and outcome is not None:
                if published:
                    overruled[outcome] += 1
                elif kind == DecisionKind.VETOED.value:
                    respected[outcome] += 1
        elif decision.review_error is not None:
            reviews_failed += 1

    return Aggregates(
        period_start=period_start,
        period_end=period_end,
        total_decisions=len(decisions),
        by_kind=dict(by_kind),
        outcomes_by_kind=_plain(outcomes_by_kind),
        win_rate_by_kind={kind: _win_rate(outcomes_by_kind[kind]) for kind in by_kind},
        total_r_published=total_r_published,
        by_symbol=_plain(by_symbol),
        by_hour_utc={hour: dict(counter) for hour, counter in by_hour.items()},
        by_rule=_plain(by_rule),
        skeptic_value={
            "overruled_win_rate": _win_rate(overruled),
            "respected_win_rate": _win_rate(respected),
        },
        judge_value=_avoided_share(outcomes_by_kind[DecisionKind.VETOED.value]),
        gate_value=_avoided_share(outcomes_by_kind[DecisionKind.GATE_REJECTED.value]),
        failure_categories=dict(failure_categories),
        reviews_present=reviews_present,
        reviews_failed=reviews_failed,
    )


def recent_lessons(
    decisions: Iterable[DecisionSummary], *, limit: int = DEFAULT_LESSON_LIMIT
) -> list[Lesson]:
    """The newest ``limit`` reviewer lessons, most recent decision first. Pure.

    Rows without a successful review, or whose review carries no non-empty
    string ``lesson``, are skipped.
    """
    reviewed = (d for d in decisions if d.review is not None)
    newest_first = sorted(reviewed, key=lambda d: (d.created_at, d.id), reverse=True)
    lessons: list[Lesson] = []
    for decision in newest_first:
        text = decision.review.get("lesson") if decision.review is not None else None
        if not isinstance(text, str) or not text.strip():
            continue
        lessons.append(
            Lesson(
                signal_id=decision.id,
                symbol=decision.symbol,
                kind=decision_kind_of(decision),
                lesson=text.strip(),
            )
        )
        if len(lessons) == limit:
            break
    return lessons


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _cell(text: str) -> str:
    """Escape a markdown table cell: pipes would split the row, newlines the table."""
    return text.replace("|", "\\|").replace("\n", " ")


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return "_none_"
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return "\n".join(lines)


def _outcome_row(label: str, outcomes: dict[str, int]) -> list[str]:
    counter = Counter(outcomes)
    return [
        label,
        str(counter[_WIN]),
        str(counter[_LOSS]),
        str(counter[_EXPIRED]),
        _pct(_win_rate(counter)),
    ]


def _outcome_table(label: str, groups: dict[str, dict[str, int]]) -> str:
    header = (label, _WIN, _LOSS, _EXPIRED, "Win rate")
    return _table(header, [_outcome_row(key, groups[key]) for key in sorted(groups)])


def _ordered_kinds(by_kind: dict[str, int]) -> list[str]:
    known = [kind for kind in _KIND_ORDER if kind in by_kind]
    return known + sorted(kind for kind in by_kind if kind not in _KIND_ORDER)


def _gate_section(exit_gate: ExitGateReport) -> str:
    rows = [
        [str(c.number), c.title, "PASS" if c.passed else "FAIL", c.evidence]
        for c in exit_gate.criteria
    ]
    verdict = "PASS" if exit_gate.all_passed else "FAIL"
    table = _table(("#", "Criterion", "Result", "Evidence"), rows)
    return f"## Slice 2 exit gate\n{table}\n\n**Verdict:** {verdict}"


def _numbers_section(aggregates: Aggregates) -> str:
    rows = []
    for kind in _ordered_kinds(aggregates.by_kind):
        outcomes = Counter(aggregates.outcomes_by_kind.get(kind, {}))
        rows.append(
            [
                kind,
                str(aggregates.by_kind[kind]),
                str(outcomes[_WIN]),
                str(outcomes[_LOSS]),
                str(outcomes[_EXPIRED]),
                _pct(aggregates.win_rate_by_kind.get(kind)),
            ]
        )
    table = _table(("Kind", "Decisions", _WIN, _LOSS, _EXPIRED, "Win rate"), rows)
    skeptic = aggregates.skeptic_value
    judge = aggregates.judge_value
    gate = aggregates.gate_value
    lines = [
        "## Numbers",
        table,
        "",
        f"Total realised R (published): {aggregates.total_r_published:+.2f}",
        "Skeptic value: overruled win-rate "
        f"{_pct(skeptic.get('overruled_win_rate'))} vs respected "
        f"{_pct(skeptic.get('respected_win_rate'))}",
        (
            f"Judge value: {_pct(judge)} of vetoes avoided a loss/expiry"
            if judge is not None
            else "Judge value: n/a (no resolved vetoes)"
        ),
        (
            f"Gate value: {_pct(gate)} of rejections avoided a loss/expiry"
            if gate is not None
            else "Gate value: n/a (no resolved gate rejections)"
        ),
        "",
        "### By symbol (published)",
        _outcome_table("Symbol", aggregates.by_symbol),
        "",
        "### By hour UTC (published)",
        _outcome_table(
            "Hour (UTC)",
            {f"{hour:02d}:00": outcomes for hour, outcomes in aggregates.by_hour_utc.items()},
        ),
        "",
        "### By rule (counterfactual)",
        _outcome_table("Rule", aggregates.by_rule),
        "",
        "### Failure categories",
        _table(
            ("Category", "Count"),
            [
                [category, str(count)]
                for category, count in sorted(
                    aggregates.failure_categories.items(), key=lambda kv: (-kv[1], kv[0])
                )
            ],
        ),
    ]
    return "\n".join(lines)


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "_none_"


def _proposed_changes_section(report: CriticReport) -> str:
    if not report.proposed_changes:
        return "## Proposed changes\n_No changes proposed._"
    blocks = []
    for change in report.proposed_changes:
        evidence = ", ".join(f"`{sid}`" for sid in change.evidence_signal_ids) or "_none_"
        blocks.append(
            f"- [ ] **{change.target}** — {change.change}\n"
            f"  _Why:_ {change.rationale}  _Evidence:_ {evidence}"
        )
    return "## Proposed changes\n" + "\n".join(blocks)


def _lessons_section(lessons: Sequence[Lesson]) -> str:
    items = [
        f"**{lesson.symbol}** ({lesson.kind}): {lesson.lesson} `{lesson.signal_id}`"
        for lesson in lessons
    ]
    return "## Recent lessons\n" + _bullets(items)


def render_markdown(
    *,
    aggregates: Aggregates,
    report: CriticReport,
    exit_gate: ExitGateReport | None,
    lessons: Sequence[Lesson],
) -> str:
    """Render the report body posted to the proposed-rules PR. Pure.

    Layout (plan Task 12): title, optional ``## Slice 2 exit gate`` (only when
    ``exit_gate`` is given), ``## Summary``, ``## Numbers`` with its
    sub-tables, ``## Key patterns``, ``## Agent assessments``, ``## Proposed
    changes`` as a checklist, ``## Recent lessons`` and a footer with the
    review success / failure counts.
    """
    title = (
        f"# Critic report — {aggregates.period_start:%Y-%m-%d} → {aggregates.period_end:%Y-%m-%d}"
    )
    sections = [title]
    if exit_gate is not None:
        sections.append(_gate_section(exit_gate))
    sections.extend(
        [
            f"## Summary\n{report.summary}",
            _numbers_section(aggregates),
            "## Key patterns\n" + _bullets(report.key_patterns),
            "## Agent assessments\n"
            f"**Skeptic.** {report.skeptic_assessment}\n\n"
            f"**Judge.** {report.judge_assessment}\n\n"
            f"**Gates.** {report.gate_assessment}",
            _proposed_changes_section(report),
            _lessons_section(lessons),
            "_Generated by Critic v0. "
            f"Reviews: {aggregates.reviews_present} ok / {aggregates.reviews_failed} failed._",
        ]
    )
    return "\n\n".join(sections) + "\n"


# ---------------------------------------------------------------------------
# Keyset pagination helpers
# ---------------------------------------------------------------------------


def _check_page(page: int) -> None:
    if page < 1:
        raise ValueError(f"page must be >= 1, got {page}")


async def fetch_all_decisions(
    store: SignalStore, *, since: datetime, until: datetime, page: int = DEFAULT_PAGE
) -> list[DecisionSummary]:
    """Every ``DecisionSummary`` in ``[since, until)``, following the ``(created_at, id)`` cursor.

    Stops after the first page shorter than ``page`` rows (an exactly-full last
    page costs one extra empty call).
    """
    _check_page(page)
    rows: list[DecisionSummary] = []
    after: tuple[datetime, UUID] | None = None
    while True:
        batch = await store.list_decision_summaries(
            since=since, until=until, after=after, limit=page
        )
        rows.extend(batch)
        if len(batch) < page:
            return rows
        after = (batch[-1].created_at, batch[-1].id)


async def fetch_all_scan_runs(
    store: SignalStore, *, since: datetime, until: datetime, page: int = DEFAULT_PAGE
) -> list[StoredScanRun]:
    """Every ``scan_runs`` row started in ``[since, until)``; cursor ``(started_at, id)``."""
    _check_page(page)
    rows: list[StoredScanRun] = []
    after: tuple[datetime, UUID] | None = None
    while True:
        batch = await store.list_scan_runs_between(
            since=since, until=until, after=after, limit=page
        )
        rows.extend(batch)
        if len(batch) < page:
            return rows
        after = (batch[-1].started_at, batch[-1].id)


async def fetch_all_setups(
    store: SignalStore, *, page: int = DEFAULT_PAGE
) -> list[StoredActiveSetup]:
    """Every ``active_setups`` row (any status); cursor ``(opened_at, id)``."""
    _check_page(page)
    rows: list[StoredActiveSetup] = []
    after: tuple[datetime, UUID] | None = None
    while True:
        batch = await store.list_active_setups(after=after, limit=page)
        rows.extend(batch)
        if len(batch) < page:
            return rows
        after = (batch[-1].opened_at, batch[-1].id)


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportBuild:
    """What one ``CriticReporter.build`` produced.

    ``decision_count`` is the number of decisions the report covers -- the CLI
    prints it in its ``REPORT ... decisions=<n>`` contract line -- surfaced here
    so the caller does not have to page the decisions a second time.
    """

    stored: StoredCriticReport
    exit_gate: ExitGateReport | None
    decision_count: int


def _input_hash(since: datetime, until: datetime, decision_count: int) -> str:
    """Stable SHA-256 for the critic's ``agent_runs.input_hash`` (one report per period)."""
    key = f"critic-report:{since.isoformat()}:{until.isoformat()}:{decision_count}"
    return hashlib.sha256(key.encode()).hexdigest()


class CriticReporter:
    """Build, persist and return one period ``StoredCriticReport``.

    A failing Claude call propagates (after ``structured_completion``'s own
    retries): a report without the model's judgement is not a report, and the
    CLI turns the exception into a non-zero exit so the workflow can retry.
    Nothing is written to ``critic_reports`` in that case.
    """

    def __init__(
        self,
        *,
        store: SignalStore,
        client: AsyncAnthropic,
        model: str = DEFAULT_MODEL,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._client = client
        self._model = model
        self._now = now

    async def build(
        self, *, since: datetime, until: datetime, include_exit_gate: bool
    ) -> ReportBuild:
        """Report over decisions in ``[since, until)``; optionally with the Slice 2 gate.

        Zero decisions skip the Claude call (``EMPTY_PERIOD_REPORT``) and the
        ``agent_runs`` write, but the report row is still saved so the period
        is recorded as reviewed.
        """
        decisions = await fetch_all_decisions(self._store, since=since, until=until)
        aggregates = compute_aggregates(decisions, period_start=since, period_end=until)
        lessons = recent_lessons(decisions)

        if decisions:
            report = await self._call_claude(aggregates, lessons, decisions)
        else:
            logger.info("critic report %s..%s: no decisions; skipping Claude call", since, until)
            report = EMPTY_PERIOD_REPORT

        exit_gate: ExitGateReport | None = None
        if include_exit_gate:
            scan_runs = await fetch_all_scan_runs(self._store, since=since, until=until)
            setups = await fetch_all_setups(self._store)
            exit_gate = evaluate_exit_gate(scan_runs=scan_runs, decisions=decisions, setups=setups)

        markdown = render_markdown(
            aggregates=aggregates, report=report, exit_gate=exit_gate, lessons=lessons
        )
        stored = StoredCriticReport(
            id=uuid4(),
            period_start=since,
            period_end=until,
            report=report.model_dump(mode="json"),
            markdown=markdown,
            pr_url=None,
            created_at=self._now(),
        )
        await self._store.save_critic_report(stored)
        logger.info(
            "critic report %s saved: decisions=%d proposed_changes=%d gate=%s",
            stored.id,
            len(decisions),
            len(report.proposed_changes),
            "n/a" if exit_gate is None else ("PASS" if exit_gate.all_passed else "FAIL"),
        )
        return ReportBuild(stored=stored, exit_gate=exit_gate, decision_count=len(decisions))

    async def _call_claude(
        self,
        aggregates: Aggregates,
        lessons: Sequence[Lesson],
        decisions: Sequence[DecisionSummary],
    ) -> CriticReport:
        """One ``CriticReport`` call, logged to ``agent_runs`` under the critic role (NFR-2.3).

        ``agent_runs.scan_id`` is a foreign key, so the row borrows the
        ``scan_id`` of the most recent decision in the period.
        """
        result = await structured_completion(
            output_schema=CriticReport,
            system=REPORT_SYSTEM_PROMPT,
            user=build_report_user_prompt(aggregates, lessons),
            model=self._model,
            max_output_tokens=REPORT_MAX_OUTPUT_TOKENS,
            client=self._client,
            tool_name=_TOOL_NAME,
            tool_description=_TOOL_DESCRIPTION,
        )
        newest = max(decisions, key=lambda d: (d.created_at, d.id))
        output: dict[str, Any] = result.output.model_dump(mode="json")
        await self._store.log_agent_run(
            scan_id=newest.scan_id,
            agent_role=AgentRole.CRITIC,
            strategy=None,
            input_hash=_input_hash(aggregates.period_start, aggregates.period_end, len(decisions)),
            output=output,
            latency_ms=result.latency_ms,
            token_usage={"tokens_in": result.tokens_in, "tokens_out": result.tokens_out},
            cost_usd=result.cost_usd,
        )
        logger.debug(
            "critic report call: model=%s tokens_in=%d tokens_out=%d cost_usd=%s attempts=%d",
            result.model,
            result.tokens_in,
            result.tokens_out,
            result.cost_usd,
            result.attempts,
        )
        return result.output
