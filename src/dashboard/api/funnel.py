"""The decision funnel (design §4.2 P4, §5.1) -- pure counting over `DecisionRow`s.

Stages, per group and for the totals:

- ``symbols_scanned`` -- one decision row per symbol per scan, so: rows.
- ``scans`` -- distinct invocations, bucketed by window (design §5.1): the pairs
  ``(UTC date of scan_started_at, window_for(scan_started_at))``. One scan writes
  one ``scan_runs`` row per symbol, milliseconds apart; they are one scan.
- ``proposals`` = PUBLISHED + VETOED + GATE_REJECTED (the Analyzer proposed);
  ``gate_passed`` = proposals - gate_rejected.
- ``judge_publish`` / ``judge_caveat`` / ``judge_skip`` -- the Judge ruling on
  gate-passed rows (a gate-passed row with no Judge run counts in none).
- ``published`` -- an ``active_setups`` row exists (``has_setup``).
- ``rejections_by_rule`` -- ``violated_rule`` of GATE_REJECTED rows.

Group keys: ``day`` -> the UTC ISO date of ``created_at`` (the same clock the
period filter uses); ``window`` -> the window label of the row's scan, ``AD_HOC``
included as its own group (console invokes are never dropped or folded into a
scheduled window); ``symbol`` -> the symbol. Only non-empty groups are returned:
days ascending, windows in schedule order then ``AD_HOC``, symbols alphabetical.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.common.models import JudgeRuling
from src.common.schedule import AD_HOC, SCAN_WINDOWS, window_for
from src.dashboard.api.classify import DecisionKind
from src.dashboard.api.queries import DecisionRow

__all__ = ["FunnelGroup", "GroupBy", "build_funnel", "summarise", "top_rejections"]

GroupBy = Literal["day", "window", "symbol"]

_PROPOSAL_KINDS = frozenset(
    {DecisionKind.PUBLISHED, DecisionKind.VETOED, DecisionKind.GATE_REJECTED}
)
_WINDOW_ORDER = {label: index for index, label in enumerate([w.label for w in SCAN_WINDOWS])}


class FunnelGroup(BaseModel):
    """The funnel counts for one group (or the totals, key ``"total"``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    scans: int
    symbols_scanned: int
    proposals: int
    gate_rejected: int
    gate_passed: int
    judge_publish: int
    judge_caveat: int
    judge_skip: int
    published: int
    rejections_by_rule: dict[str, int]


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _scan_key(row: DecisionRow) -> tuple[str, str]:
    started = _utc(row.scan_started_at)
    return started.date().isoformat(), window_for(started)


def _rule_counts(rows: Iterable[DecisionRow]) -> Counter[str]:
    return Counter(
        row.violated_rule
        for row in rows
        if row.decision_kind is DecisionKind.GATE_REJECTED and row.violated_rule
    )


def summarise(rows: Sequence[DecisionRow], *, key: str) -> FunnelGroup:
    """Every funnel stage counted over ``rows`` (one group, or all rows for the totals)."""
    proposals = [r for r in rows if r.decision_kind in _PROPOSAL_KINDS]
    gate_rejected = sum(1 for r in proposals if r.decision_kind is DecisionKind.GATE_REJECTED)
    rulings = Counter(
        r.judge_ruling for r in proposals if r.decision_kind is not DecisionKind.GATE_REJECTED
    )
    rules = _rule_counts(rows)
    return FunnelGroup(
        key=key,
        scans=len({_scan_key(r) for r in rows}),
        symbols_scanned=len(rows),
        proposals=len(proposals),
        gate_rejected=gate_rejected,
        gate_passed=len(proposals) - gate_rejected,
        judge_publish=rulings[JudgeRuling.PUBLISH.value],
        judge_caveat=rulings[JudgeRuling.PUBLISH_WITH_CAVEAT.value],
        judge_skip=rulings[JudgeRuling.SKIP.value],
        published=sum(1 for r in rows if r.has_setup),
        rejections_by_rule=dict(sorted(rules.items(), key=lambda item: (-item[1], item[0]))),
    )


def _day_key(row: DecisionRow) -> str:
    return _utc(row.created_at).date().isoformat()


def _window_key(row: DecisionRow) -> str:
    return window_for(_utc(row.scan_started_at))


def _symbol_key(row: DecisionRow) -> str:
    return row.symbol


_KEY_FUNCS: dict[str, Callable[[DecisionRow], str]] = {
    "day": _day_key,
    "window": _window_key,
    "symbol": _symbol_key,
}


def _window_sort(label: str) -> tuple[int, str]:
    # Scheduled windows in fire-time order, then AD_HOC (and anything unknown) last.
    return _WINDOW_ORDER.get(label, len(_WINDOW_ORDER) + (label != AD_HOC)), label


def build_funnel(rows: Sequence[DecisionRow], *, group_by: GroupBy) -> list[FunnelGroup]:
    """The funnel per non-empty group, in the module docstring's order."""
    key_of = _KEY_FUNCS[group_by]
    grouped: dict[str, list[DecisionRow]] = {}
    for row in rows:
        grouped.setdefault(key_of(row), []).append(row)
    keys = sorted(grouped, key=_window_sort) if group_by == "window" else sorted(grouped)
    return [summarise(grouped[key], key=key) for key in keys]


def top_rejections(rows: Sequence[DecisionRow], *, since: datetime) -> list[tuple[str, int]]:
    """Gate rules by rejection count for rows created at/after ``since``.

    Descending by count, ties broken by rule id so the table is stable.
    """
    counts = _rule_counts(r for r in rows if r.created_at >= since)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))
