"""Pure assembly for the P7 system-health routes (design §4.2 P7, §8; Step 4.3).

No I/O and no clock reads: the router (``routers/system.py``) does the reads
through ``queries`` / ``cloudwatch`` and hands the results here.

* :func:`scan_history_out` -- one scan-history line per ``scan_runs`` row, with
  its schedule window (``schedule.window_for``) and duration.
* :func:`cost_per_day` -- LLM spend per UTC day, total and per role, summed over
  the three sources ``queries.agent_usage_by_day`` reads (``agent_runs``,
  ``setup_evaluations`` as ``forecaster``, ``decision_reviews`` as ``critic``).
* :func:`forecaster_sweeps` -- Forecaster evaluations bucketed per sweep with the
  ``overview.last_sweep_at`` semantics: a sweep starts at fire time +
  :data:`overview.SWEEP_OFFSET` and an evaluation belongs to the **latest sweep
  start <= it** (a console re-run at 03:00 counts toward yesterday's 22:05).
  Every scheduled sweep started in ``[since, now]`` is listed, empty ones as 0 /
  0, so a sweep that never ran is visible rather than silently missing.
* :func:`inferred_config` -- the config panel. The dashboard cannot read the
  scan Lambda's configuration (no ``lambda:*``, no SSM -- design §8), so every
  value but the schedule is **inferred** and says from where.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta

from src.common.schedule import SCAN_WINDOWS, window_for, windows_for_day
from src.dashboard.api.overview import SWEEP_OFFSET
from src.dashboard.api.queries import ConfigFacts, EvaluationStamp, UsageDay
from src.dashboard.api.schemas import (
    ConfigResponse,
    CostDay,
    Inferred,
    ScanRowOut,
    ScheduleOut,
    ScheduleWindowOut,
    SweepRow,
)
from src.persistence.models import StoredScanRun

__all__ = [
    "cost_per_day",
    "day_window_start",
    "forecaster_sweeps",
    "inferred_config",
    "scan_history_out",
    "sweep_start_for",
]


def day_window_start(now: datetime, days: int) -> datetime:
    """Midnight UTC of the date ``days`` days before ``now`` (the P7 range start)."""
    day = (now.astimezone(UTC) - timedelta(days=days)).date()
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Scan history
# ---------------------------------------------------------------------------


def scan_history_out(scans: Sequence[StoredScanRun]) -> list[ScanRowOut]:
    """One line per scan row, order preserved; ``duration_s`` null until completed."""
    out: list[ScanRowOut] = []
    for scan in scans:
        duration = (
            None
            if scan.completed_at is None
            else max(0.0, (scan.completed_at - scan.started_at).total_seconds())
        )
        out.append(
            ScanRowOut(
                id=scan.id,
                window=window_for(scan.started_at),
                started_at=scan.started_at,
                completed_at=scan.completed_at,
                duration_s=duration,
                status=scan.status,
                symbols=list(scan.symbols or []),
                error_message=scan.error_message,
            )
        )
    return out


# ---------------------------------------------------------------------------
# LLM cost per day
# ---------------------------------------------------------------------------


def cost_per_day(usage: Iterable[UsageDay]) -> list[CostDay]:
    """Spend per UTC day (ascending): the total and the per-role split."""
    by_day: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for line in usage:
        by_day[line.date][line.role] += line.cost_usd
    return [
        CostDay(date=day, total_usd=sum(roles.values()), by_role=dict(roles))
        for day, roles in sorted(by_day.items())
    ]


# ---------------------------------------------------------------------------
# Forecaster sweeps
# ---------------------------------------------------------------------------


def _sweep_starts(day: date) -> list[datetime]:
    return [fire + SWEEP_OFFSET for fire in windows_for_day(day)]


def sweep_start_for(at: datetime) -> datetime:
    """The latest sweep start <= ``at`` (yesterday's 22:05 before today's 08:05)."""
    at_utc = at.astimezone(UTC)
    day = at_utc.date()
    starts = [*_sweep_starts(day - timedelta(days=1)), *_sweep_starts(day)]
    return [start for start in starts if start <= at_utc][-1]


def forecaster_sweeps(
    stamps: Iterable[EvaluationStamp], *, since: datetime, now: datetime
) -> list[SweepRow]:
    """Evaluations / errors per sweep, oldest first.

    Keys: every scheduled sweep start in ``[since, now]`` plus the sweep any
    evaluation buckets to (which may start before ``since``).
    """
    counts: dict[datetime, list[int]] = {}
    day = since.astimezone(UTC).date()
    while day <= now.astimezone(UTC).date():
        for start in _sweep_starts(day):
            if since <= start <= now:
                counts[start] = [0, 0]
        day += timedelta(days=1)
    for stamp in stamps:
        bucket = counts.setdefault(sweep_start_for(stamp.evaluated_at), [0, 0])
        bucket[0] += 1
        bucket[1] += int(stamp.is_error)
    return [
        SweepRow(sweep_start=start, evaluations=total, errors=errors)
        for start, (total, errors) in sorted(counts.items())
    ]


# ---------------------------------------------------------------------------
# Config panel
# ---------------------------------------------------------------------------

_SCHEDULE = ScheduleOut(
    windows=[
        ScheduleWindowOut(label=w.label, time_utc=f"{w.hour:02d}:{w.minute:02d}")
        for w in SCAN_WINDOWS
    ],
    source="src.common.schedule.SCAN_WINDOWS (the SPEC §5 cron times)",
)


def inferred_config(facts: ConfigFacts, *, langfuse_configured: bool) -> ConfigResponse:
    """The config panel from the database facts plus the dashboard's own Langfuse setting."""
    macro = None if facts.skeptic_outputs == 0 else facts.real_objections > 0
    return ConfigResponse(
        watchlist=Inferred[list[str]](
            value=list(facts.watchlist),
            source="union of scan_runs.symbols over the last 7 days",
        ),
        model=Inferred[str | None](
            value=facts.model,
            source="latest agent_runs.token_usage->>'model'",
        ),
        macro_providers_configured=Inferred[bool | None](
            value=macro,
            source="any of the last 10 Skeptic outputs is a real objection (not NoMacroData)",
        ),
        langfuse_configured=Inferred[bool](
            value=langfuse_configured,
            source="the dashboard's own LANGFUSE_BASE_URL setting (not the scan Lambda's)",
        ),
        schedule=_SCHEDULE,
    )
