"""Prompts for the Critic v0 per-decision review and period report (design §6.2 / §6.3).

``REVIEW_SYSTEM_PROMPT`` frames the per-decision task: grade the *reasoning*
behind one past decision with the benefit of a deterministic price-replay
resolution, never the luck of the outcome. ``build_review_user_prompt`` renders
one ``DecisionRecord`` plus its ``Resolution`` into the sectioned user message
that goes with it.

``REPORT_SYSTEM_PROMPT`` frames the aggregate task: turn pre-computed period
statistics (``report.Aggregates``) plus the per-decision lessons into a
``CriticReport`` with evidence-backed proposed rule changes.
``build_report_user_prompt`` renders those inputs.

Token bounds (design §6.2 / §6.3): the review prompt carries the proposal /
skip payload, the Historian / Skeptic / Judge outputs, and the resolution row
with a one-line human summary -- never raw candles. The report prompt carries
only the aggregate numbers and at most 25 one-sentence lessons, never the
per-decision payloads.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from src.agents.critic.models import DecisionKind, DecisionRecord, Resolution

if TYPE_CHECKING:  # pragma: no cover - typing only; report.py imports this module at runtime
    from src.agents.critic.report import Aggregates, Lesson

REVIEW_SYSTEM_PROMPT: Final[str] = """\
You are the Critic for a signal-only Smart Money Concepts (SMC) crypto analyzer.
You grade ONE past trading decision with the benefit of hindsight: you are given
the Analyzer's proposal (or skip), the Historian / Skeptic / Judge outputs that
produced the decision, and the RESOLUTION -- what price actually did afterwards,
computed deterministically from 5-minute candles (WIN = target 1 hit before stop,
LOSS = stop hit first, EXPIRED = entry never triggered within 72h or nothing hit
within 7 days, UNRESOLVABLE = data missing).

Judge the REASONING, not the luck. A well-read setup can lose; a sloppy one can win.
- smc_read_valid: was the structure / zone / premium-discount / liquidity read
  defensible on its own terms? Ignore the outcome for this field.
- skeptic_vindicated: did the market bear out the Skeptic's objection? YES only
  when the objection's mechanism visibly played out; NOT_APPLICABLE if no Skeptic ran.
- judge_correct: given the outcome AND the information available at the time,
  was PUBLISH / PUBLISH_WITH_CAVEAT / SKIP the right ruling?
- gate_correct (GATE_REJECTED only): counterfactually, did the hard rule save a loss
  (true) or block a winner (false)? Leave null otherwise.
- failure_category: the single dominant error, or NONE.
- lesson: one concrete sentence a prompt author could act on. Cite the symbol and
  the specific element (e.g. "OB was in premium; discount rule should have fired").
Traditional indicators (RSI, MACD, moving averages) are forbidden in this system;
never recommend them. Never recommend order execution or position management.
"""

# Literal marker for a section whose agent did not run / whose data does not exist.
NOT_RUN: Final[str] = "(not run)"


def _dump(obj: Any) -> str:
    """Pretty JSON for a dict or model; ``default=str`` covers UUIDs / datetimes / enums."""
    return json.dumps(obj, indent=2, default=str, sort_keys=False)


def _section(title: str, body: str) -> str:
    return f"## {title}\n{body}"


def _format_duration(start: datetime, end: datetime) -> str:
    """``6h12m`` / ``2d03h05m`` style; clamps negative spans to zero."""
    total_minutes = max(0, int((end - start).total_seconds() // 60))
    days, rem = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days}d{hours:02d}h{minutes:02d}m"
    return f"{hours}h{minutes:02d}m"


def _format_r(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}R"


def resolution_summary(resolution: Resolution) -> str:
    """One human-readable line, e.g.

    ``Outcome: WIN after 6h12m in trade; realized 3.00R; MAE -0.40R; MFE 3.20R;
    TP2 not reached`` -- or ``Outcome: EXPIRED never entered; ...`` when the entry
    was never triggered.
    """
    if resolution.entered_at is None:
        when = "never entered"
    else:
        held = _format_duration(resolution.entered_at, resolution.resolved_at)
        when = f"after {held} in trade"
    tp2 = "reached" if resolution.tp2_reached else "not reached"
    return (
        f"Outcome: {resolution.outcome.value} {when}; "
        f"realized {resolution.realized_r:.2f}R; "
        f"MAE {_format_r(resolution.max_adverse_excursion_r)}; "
        f"MFE {_format_r(resolution.max_favorable_excursion_r)}; "
        f"TP2 {tp2}"
    )


def _decision_section(record: DecisionRecord) -> str:
    signal = record.signal
    return _section(
        "Decision",
        _dump(
            {
                "signal_id": signal.id,
                "scan_id": signal.scan_id,
                "symbol": signal.symbol,
                "strategy": signal.strategy,
                "decision_kind": record.kind.value,
                "decided_at": signal.created_at,
            }
        ),
    )


def _analyzer_sections(record: DecisionRecord) -> list[str]:
    """``## Analyzer proposal`` and/or ``## Analyzer skip``.

    GATE_REJECTED decisions carry both: the SkipDecision that names the violated
    rule and the proposal the gate blocked. When the typed re-parse failed
    (``inventory.classify`` degrades corrupt payloads to None) the raw journal
    payload is shown instead so the Critic still sees what was recorded.
    """
    sections: list[str] = []
    if record.skip is not None:
        sections.append(_section("Analyzer skip", _dump(record.skip.model_dump(mode="json"))))
    if record.proposal is not None:
        title = "Analyzer proposal"
        if record.kind is DecisionKind.GATE_REJECTED:
            title = "Analyzer proposal (rejected by hard rule)"
        sections.append(_section(title, _dump(record.proposal.model_dump(mode="json"))))
    if not sections:
        payload = record.signal.payload
        title = "Analyzer proposal" if "direction" in payload else "Analyzer skip"
        note = "(payload failed schema validation; raw journal row shown)\n"
        sections.append(_section(title, note + _dump(payload)))
    return sections


def _agent_section(title: str, output: dict[str, Any] | None) -> str:
    return _section(title, NOT_RUN if output is None else _dump(output))


def _resolution_section(record: DecisionRecord, resolution: Resolution | None) -> str:
    if resolution is None:
        body = NOT_RUN
        if record.kind is DecisionKind.NO_SETUP:
            body += (
                "\nNo proposal existed, so nothing was replayed. Grade whether declining "
                "to trade was the defensible read of the market at the time."
            )
        return _section("Resolution (ground truth)", body)
    return _section(
        "Resolution (ground truth)",
        resolution_summary(resolution) + "\n" + _dump(resolution.model_dump(mode="json")),
    )


def build_review_user_prompt(record: DecisionRecord, resolution: Resolution | None) -> str:
    """Render one decision + its resolution as the review call's user message.

    Sections, in order: ``## Decision``, ``## Analyzer proposal`` / ``## Analyzer
    skip``, ``## Historian``, ``## Skeptic``, ``## Judge``, ``## Resolution (ground
    truth)``. Agents that did not run are shown as the literal ``(not run)`` line.
    """
    sections = [
        _decision_section(record),
        *_analyzer_sections(record),
        _agent_section("Historian", record.historian),
        _agent_section("Skeptic", record.skeptic),
        _agent_section("Judge", record.judge),
        _resolution_section(record, resolution),
    ]
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Period report (design §6.3)
# ---------------------------------------------------------------------------

REPORT_SYSTEM_PROMPT: Final[str] = """\
You are the weekly Critic for a signal-only SMC crypto analyzer. You receive
pre-computed statistics over a period (win rates by decision kind, symbol, hour,
rule; Skeptic / Judge / gate value; failure-category counts) plus the per-decision
lessons written by the per-decision reviewer. Produce:
- summary: what worked, what did not, in plain language with the numbers.
- key_patterns: recurring, evidence-backed patterns (max 20).
- skeptic_assessment / judge_assessment / gate_assessment: is each agent adding
  value? Quote the relevant rates.
- proposed_changes: at most 10 concrete, minimal changes. Each `target` must be one
  of SKEPTIC_SYSTEM_PROMPT, JUDGE_SYSTEM_PROMPT, FORECASTER_SYSTEM_PROMPT,
  risk_gates.<CONSTANT>, or "SPEC §1.6 rule N". `change` is the exact wording or
  value to adopt. `rationale` cites the statistic. `evidence_signal_ids` lists the
  signal ids from the lessons that support it.
Never propose RSI/MACD/moving averages/Bollinger (forbidden). Never propose order
execution, position sizing changes above SPEC §1.6 limits, or removing a hard rule.
Prefer "no change" over speculative change when the sample is small (say so).
"""

# Fields of ``Aggregates`` already rendered in the ``## Period`` section.
_PERIOD_FIELDS: Final[frozenset[str]] = frozenset({"period_start", "period_end"})


def _lessons_section(lessons: Sequence[Lesson]) -> str:
    if not lessons:
        return _section("Per-decision lessons (most recent first)", "(none)")
    lines = [
        f"- signal_id={lesson.signal_id} symbol={lesson.symbol} kind={lesson.kind}: {lesson.lesson}"
        for lesson in lessons
    ]
    return _section("Per-decision lessons (most recent first)", "\n".join(lines))


def build_report_user_prompt(aggregates: Aggregates, lessons: Sequence[Lesson]) -> str:
    """Render the period statistics + recent lessons as the report call's user message.

    Sections: ``## Period`` (ISO bounds, UTC), ``## Statistics`` (the
    ``Aggregates`` dataclass as JSON; ``null`` marks a rate with no sample) and
    ``## Per-decision lessons`` (one line each, carrying the ``signal_id`` the
    model must quote in ``evidence_signal_ids``).
    """
    stats = {
        name: value
        for name, value in dataclasses.asdict(aggregates).items()
        if name not in _PERIOD_FIELDS
    }
    period = (
        f"{aggregates.period_start.isoformat()} -> {aggregates.period_end.isoformat()} (UTC)\n"
        "Rates are fractions in [0, 1]; null means no resolved sample. "
        "Treat small samples with caution and say so."
    )
    sections = [
        _section("Period", period),
        _section("Statistics", _dump(stats)),
        _lessons_section(lessons),
    ]
    return "\n\n".join(sections)
