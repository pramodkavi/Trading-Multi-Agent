"""P6 performance analytics (design §4.2 P6, §5.2a) -- pure aggregation over `OutcomeRow`s.

**Outcome rule (design §5.2a).** A decision's effective outcome is the Critic's
replay (``decision_resolutions.outcome``, source ``replayed``) when a row exists,
else ``signals.outcome`` (the Forecaster's verdict, source ``forecaster``), else
it is unresolved and omitted. A resolution row always wins -- even an
``UNRESOLVABLE`` one: there is no fallback to the Forecaster when a replay exists.
Decisive = WIN / LOSS; ``win_rate = wins / (wins + losses)``, ``None`` (never
``0.0``) when nothing was decisive.

- **Win rate** counts PUBLISHED decisions only. Buckets: ``tag`` (a row counts
  once under each of its tags; untagged rows have no tag bucket), ``session``
  (``UNKNOWN`` when the scan had none), ``strategy``, ``time`` (ISO week of
  ``created_at`` in UTC, ``YYYY-Www``). ``n`` is every published decision in the
  bucket, resolved or not.
- **Skeptic value**: an objection counts when ``recommends_against`` is true or
  severity is ``HIGH``; Judge SKIP -> ``respected``, PUBLISH_WITH_CAVEAT ->
  ``overruled_with_caveat``, PUBLISH -> ``overruled``. NoMacroData runs are
  excluded and counted in ``no_macro_data_excluded``; a counted objection with no
  Judge ruling recorded is counted in ``unruled`` (no outcome). A respected objection is a
  veto, so its outcomes are the vetoed **counterfactuals, from replays only**;
  the overruled buckets use the effective outcome.
- **Equity curves** (1 R risk per decision), points ordered by ``created_at``:
  R = ``realized_r`` when replayed (an ``UNRESOLVABLE`` replay is omitted), else
  WIN -> ``+risk_reward_ratio`` (omitted when the payload has no R:R), LOSS ->
  ``-1``, BREAKEVEN / INVALIDATED / EXPIRED -> ``0``; unresolved decisions are
  omitted. The vetoed and gate-rejected counterfactual curves use replays only.

Every response carries ``source_counts`` -- how many of the outcomes it used came
from each source -- so the UI can draw the "replayed vs forecaster-judged" legend.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.common.models import JudgeRuling
from src.dashboard.api.classify import DecisionKind
from src.dashboard.api.queries import OutcomeRow

__all__ = [
    "EquityCurves",
    "EquityPoint",
    "OutcomeSource",
    "SkepticBucket",
    "SkepticValue",
    "SourceCounts",
    "WinRateBucket",
    "WinRateBy",
    "effective_outcome",
    "equity_curves",
    "skeptic_value",
    "source_counts",
    "win_rate_by",
]

OutcomeSource = Literal["replayed", "forecaster"]
WinRateBy = Literal["tag", "session", "strategy", "time"]

_WIN = "WIN"
_LOSS = "LOSS"
_ZERO_R = frozenset({"BREAKEVEN", "INVALIDATED", "EXPIRED"})
_UNRESOLVABLE = "UNRESOLVABLE"
_UNKNOWN_SESSION = "UNKNOWN"


class SourceCounts(BaseModel):
    """How many outcomes behind a figure came from each source (design §5.2a legend)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    replayed: int = 0
    forecaster: int = 0


class WinRateBucket(BaseModel):
    """Win rate of the published decisions in one bucket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    wins: int
    losses: int
    decisive: int = Field(description="wins + losses.")
    win_rate: float | None = Field(description="wins / decisive; null when decisive is 0.")
    n: int = Field(description="Published decisions in the bucket, resolved or not.")


class SkepticBucket(BaseModel):
    """Counted objections under one Judge response, and how those decisions ended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n: int
    wins: int
    losses: int
    win_rate: float | None


class SkepticValue(BaseModel):
    """The three Skeptic-value bars (design §5.2a)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    respected: SkepticBucket = Field(
        description="Judge SKIP; outcomes are vetoed counterfactuals from replays only."
    )
    overruled_with_caveat: SkepticBucket
    overruled: SkepticBucket
    no_macro_data_excluded: int = Field(description="NoMacroData Skeptic runs left out.")
    unruled: int = Field(
        description="Counted objections with no Judge ruling recorded: counted, no outcome."
    )
    source_counts: SourceCounts


class EquityPoint(BaseModel):
    """One decision on an R-multiple equity curve."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: UUID
    at: datetime = Field(description="created_at of the decision.")
    r: float
    cumulative_r: float
    source: OutcomeSource


class EquityCurves(BaseModel):
    """Published curve plus the vetoed / gate-rejected counterfactual curves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    published: list[EquityPoint]
    vetoed: list[EquityPoint]
    gate_rejected: list[EquityPoint]
    source_counts: SourceCounts


# ---------------------------------------------------------------------------
# Outcome rule
# ---------------------------------------------------------------------------


def effective_outcome(row: OutcomeRow) -> tuple[str | None, OutcomeSource | None]:
    """``(outcome, source)``: the replay when present, else the Forecaster, else unresolved."""
    if row.resolution_outcome is not None:
        return row.resolution_outcome, "replayed"
    if row.signal_outcome is not None:
        return row.signal_outcome, "forecaster"
    return None, None


def _replayed_outcome(row: OutcomeRow) -> str | None:
    """Counterfactual outcome: the replay only (design §5.2a)."""
    return row.resolution_outcome


def _rate(wins: int, losses: int) -> float | None:
    decisive = wins + losses
    return None if decisive == 0 else wins / decisive


def _count(sources: Iterable[OutcomeSource | None]) -> SourceCounts:
    values = list(sources)
    return SourceCounts(
        replayed=sum(1 for s in values if s == "replayed"),
        forecaster=sum(1 for s in values if s == "forecaster"),
    )


def _published(rows: Iterable[OutcomeRow]) -> list[OutcomeRow]:
    return [r for r in rows if r.decision_kind is DecisionKind.PUBLISHED]


def source_counts(rows: Sequence[OutcomeRow]) -> SourceCounts:
    """Sources of the published outcomes a win-rate figure used (unresolved rows: none)."""
    return _count(effective_outcome(r)[1] for r in _published(rows))


# ---------------------------------------------------------------------------
# Win rate
# ---------------------------------------------------------------------------


def _iso_week(row: OutcomeRow) -> str:
    year, week, _ = row.created_at.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


_KEYS: dict[str, Callable[[OutcomeRow], list[str]]] = {
    "tag": lambda row: sorted(set(row.tags)),
    "session": lambda row: [row.session or _UNKNOWN_SESSION],
    "strategy": lambda row: [row.strategy],
    "time": lambda row: [_iso_week(row)],
}


def win_rate_by(rows: Sequence[OutcomeRow], *, by: WinRateBy) -> list[WinRateBucket]:
    """Published win rate per bucket, buckets sorted by key (weeks sort chronologically)."""
    keys_of = _KEYS[by]
    grouped: dict[str, list[OutcomeRow]] = {}
    for row in _published(rows):
        for key in keys_of(row):
            grouped.setdefault(key, []).append(row)
    buckets: list[WinRateBucket] = []
    for key in sorted(grouped):
        outcomes = [effective_outcome(r)[0] for r in grouped[key]]
        wins = outcomes.count(_WIN)
        losses = outcomes.count(_LOSS)
        buckets.append(
            WinRateBucket(
                key=key,
                wins=wins,
                losses=losses,
                decisive=wins + losses,
                win_rate=_rate(wins, losses),
                n=len(outcomes),
            )
        )
    return buckets


# ---------------------------------------------------------------------------
# Skeptic value
# ---------------------------------------------------------------------------


_RULINGS = frozenset(r.value for r in JudgeRuling)


def _objection_counts(row: OutcomeRow) -> bool:
    return row.skeptic_recommends_against is True or row.skeptic_severity == "HIGH"


def _bucket(outcomes: list[str | None]) -> SkepticBucket:
    wins = outcomes.count(_WIN)
    losses = outcomes.count(_LOSS)
    return SkepticBucket(n=len(outcomes), wins=wins, losses=losses, win_rate=_rate(wins, losses))


def skeptic_value(rows: Sequence[OutcomeRow]) -> SkepticValue:
    """The respected / overruled-with-caveat / overruled bars (module docstring)."""
    respected: list[str | None] = []
    caveat: list[str | None] = []
    overruled: list[str | None] = []
    sources: list[OutcomeSource | None] = []
    excluded = 0
    unruled = 0
    for row in rows:
        if row.skeptic_is_no_macro:
            excluded += 1
            continue
        if not _objection_counts(row):
            continue
        if row.judge_ruling not in _RULINGS:
            unruled += 1  # the Judge never ruled (failed / aborted run): no bar to put it in
        elif row.judge_ruling == JudgeRuling.SKIP.value:
            outcome = _replayed_outcome(row)
            respected.append(outcome)
            sources.append(None if outcome is None else "replayed")
        elif row.judge_ruling == JudgeRuling.PUBLISH_WITH_CAVEAT.value:
            outcome, source = effective_outcome(row)
            caveat.append(outcome)
            sources.append(source)
        elif row.judge_ruling == JudgeRuling.PUBLISH.value:
            outcome, source = effective_outcome(row)
            overruled.append(outcome)
            sources.append(source)
    return SkepticValue(
        respected=_bucket(respected),
        overruled_with_caveat=_bucket(caveat),
        overruled=_bucket(overruled),
        no_macro_data_excluded=excluded,
        unruled=unruled,
        source_counts=_count(sources),
    )


# ---------------------------------------------------------------------------
# Equity curves
# ---------------------------------------------------------------------------


def _published_r(row: OutcomeRow) -> tuple[float, OutcomeSource] | None:
    outcome, source = effective_outcome(row)
    if source == "replayed":
        if outcome == _UNRESOLVABLE or row.realized_r is None:
            return None
        return row.realized_r, "replayed"
    if outcome == _WIN:
        rr = row.risk_reward_ratio
        return None if rr is None else (rr, "forecaster")
    if outcome == _LOSS:
        return -1.0, "forecaster"
    if outcome in _ZERO_R:
        return 0.0, "forecaster"
    return None  # unresolved (or an unknown outcome string)


def _counterfactual_r(row: OutcomeRow) -> tuple[float, OutcomeSource] | None:
    outcome = _replayed_outcome(row)
    if outcome is None or outcome == _UNRESOLVABLE or row.realized_r is None:
        return None
    return row.realized_r, "replayed"


def _curve(
    rows: Iterable[OutcomeRow],
    kind: DecisionKind,
    r_of: Callable[[OutcomeRow], tuple[float, OutcomeSource] | None],
) -> list[EquityPoint]:
    points: list[EquityPoint] = []
    cumulative = 0.0
    selected = sorted(
        (r for r in rows if r.decision_kind is kind), key=lambda r: (r.created_at, str(r.id))
    )
    for row in selected:
        value = r_of(row)
        if value is None:
            continue
        r, source = value
        cumulative += r
        points.append(
            EquityPoint(
                signal_id=row.id,
                at=row.created_at,
                r=r,
                cumulative_r=round(cumulative, 6),
                source=source,
            )
        )
    return points


def equity_curves(rows: Sequence[OutcomeRow]) -> EquityCurves:
    """The published curve and the two replay-only counterfactual curves."""
    published = _curve(rows, DecisionKind.PUBLISHED, _published_r)
    vetoed = _curve(rows, DecisionKind.VETOED, _counterfactual_r)
    gate_rejected = _curve(rows, DecisionKind.GATE_REJECTED, _counterfactual_r)
    return EquityCurves(
        published=published,
        vetoed=vetoed,
        gate_rejected=gate_rejected,
        source_counts=_count(p.source for p in [*published, *vetoed, *gate_rejected]),
    )
