"""The scan schedule as data: fire times, window bucketing, next-scan lookup.

SPEC §5 (and CLAUDE.md §5) schedule four EventBridge cron scans per UTC day,
each at ``:03`` past the hour to avoid clock-jitter on ``:00``:

    08:03  LONDON       London open
    13:03  NY           New York open
    15:03  OVERLAP      London-NY overlap
    22:03  DAILY_WRAP   Daily wrap + active setups review

The Slice 4 dashboard treats "a scan" as an invocation bucketed by window
(dashboard design §5.1): a run that starts within :data:`WINDOW_TOLERANCE` of a
fire time belongs to that window; anything else (a console test invoke, a retry
hours later) is ``AD_HOC``. These four times are the dashboard's single source
of truth. ``SchedulingStack`` (infra) keeps its own cron literals; the unit test
``test_scan_windows_match_scheduling_stack`` pins the two equal by value.

This module is pure and clock-free: callers pass the instant in. Unlike
:mod:`src.common.sessions` (which derives the SPEC §1.6 rule-7 *session* from
the clock), the window labels here are schedule labels, not risk-rule sessions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple


class ScanWindow(NamedTuple):
    """One scheduled scan: its dashboard label and UTC wall-clock fire time."""

    label: str
    hour: int
    minute: int


SCAN_WINDOWS: tuple[ScanWindow, ...] = (
    ScanWindow("LONDON", 8, 3),
    ScanWindow("NY", 13, 3),
    ScanWindow("OVERLAP", 15, 3),
    ScanWindow("DAILY_WRAP", 22, 3),
)

# A run starting in [fire, fire + WINDOW_TOLERANCE) is attributed to that window.
WINDOW_TOLERANCE = timedelta(minutes=30)

AD_HOC = "AD_HOC"


def _require_aware(value: datetime, name: str) -> datetime:
    """Reject naive datetimes (same rule as ``sessions.session_for``) and normalise to UTC."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _fire_time(day: date, window: ScanWindow) -> datetime:
    return datetime(day.year, day.month, day.day, window.hour, window.minute, tzinfo=UTC)


def windows_for_day(day: date) -> list[datetime]:
    """The four scheduled fire times on UTC date ``day``, in chronological order."""
    return [_fire_time(day, window) for window in SCAN_WINDOWS]


def next_scan_at(now: datetime) -> datetime:
    """The first scheduled fire time strictly after ``now`` (tz-aware, UTC).

    Wraps to the next day's first window after the daily wrap. Raises
    ``ValueError`` on a naive datetime rather than guessing.
    """
    now_utc = _require_aware(now, "now")
    today = now_utc.date()
    for fire in windows_for_day(today):
        if fire > now_utc:
            return fire
    return windows_for_day(today + timedelta(days=1))[0]


def window_for(started_at: datetime) -> str:
    """Bucket a scan start instant into a window label, or ``"AD_HOC"``.

    Returns the label of the latest fire time ``f`` on that UTC date with
    ``f <= started_at < f + WINDOW_TOLERANCE``. A run that starts even a second
    before its fire time, or 30 minutes or more after it, is ``AD_HOC``. Raises
    ``ValueError`` on a naive datetime.
    """
    started_utc = _require_aware(started_at, "started_at")
    for window in reversed(SCAN_WINDOWS):
        fire = _fire_time(started_utc.date(), window)
        if fire <= started_utc < fire + WINDOW_TOLERANCE:
            return window.label
    return AD_HOC
