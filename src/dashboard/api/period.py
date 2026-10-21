"""Period parameters of the aggregate routes (funnel, analytics): defaults and the cap.

The Data API caps a response at 1 MB, so every aggregate route bounds its range
(plan Global Constraints): at most :data:`MAX_PERIOD_DAYS` days; a longer, empty
or inverted range is **422** (rejected, never clamped -- the same ruling as the
journal's ``limit``). Naive query timestamps are read as UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

__all__ = ["MAX_PERIOD_DAYS", "as_utc", "resolve_period"]

MAX_PERIOD_DAYS = 365


def as_utc(value: datetime | None) -> datetime | None:
    """Read a naive query timestamp as UTC; normalise aware ones to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def resolve_period(
    since: datetime | None,
    until: datetime | None,
    *,
    now: datetime,
    default_days: int,
    max_days: int = MAX_PERIOD_DAYS,
) -> tuple[datetime, datetime]:
    """``(since, until)`` with defaults applied; 422 when empty, inverted or too long.

    ``until`` defaults to ``now``; ``since`` to ``until - default_days``.
    """
    end = as_utc(until) or now
    start = as_utc(since) or end - timedelta(days=default_days)
    if start >= end:
        raise HTTPException(status_code=422, detail="since must be before until")
    if end - start > timedelta(days=max_days):
        raise HTTPException(status_code=422, detail=f"period exceeds {max_days} days")
    return start, end
