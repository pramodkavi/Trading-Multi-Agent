"""Unit tests for src/common/schedule.py (Slice 4 Step 4.2).

The four scan fire times are the dashboard's single source of truth for
bucketing scan invocations into windows (design §5.1); ``SchedulingStack``
keeps its own cron literals, pinned equal by value in
``test_scan_windows_match_scheduling_stack``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from src.common.schedule import SCAN_WINDOWS, next_scan_at, window_for, windows_for_day


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 20, 7, 59, tzinfo=UTC), datetime(2026, 10, 20, 8, 3, tzinfo=UTC)),
        # strictly after
        (datetime(2026, 10, 20, 8, 3, tzinfo=UTC), datetime(2026, 10, 20, 13, 3, tzinfo=UTC)),
        # wraps
        (datetime(2026, 10, 20, 22, 3, 1, tzinfo=UTC), datetime(2026, 10, 21, 8, 3, tzinfo=UTC)),
    ],
)
def test_next_scan_at(now: datetime, expected: datetime) -> None:
    assert next_scan_at(now) == expected


@pytest.mark.parametrize(
    ("started", "label"),
    [
        (datetime(2026, 10, 20, 8, 3, 52, tzinfo=UTC), "LONDON"),
        (datetime(2026, 10, 20, 22, 32, 59, tzinfo=UTC), "DAILY_WRAP"),
        (datetime(2026, 10, 20, 22, 33, 0, tzinfo=UTC), "AD_HOC"),  # > 30 min after
        (datetime(2026, 10, 20, 8, 2, 59, tzinfo=UTC), "AD_HOC"),  # before the fire time
        (datetime(2026, 10, 20, 15, 4, 21, tzinfo=UTC), "OVERLAP"),
    ],
)
def test_window_for(started: datetime, label: str) -> None:
    assert window_for(started) == label


def test_windows_for_day_lists_four_fire_times_in_order() -> None:
    times = windows_for_day(date(2026, 10, 20))
    assert [t.hour for t in times] == [8, 13, 15, 22]
    assert all(t.minute == 3 and t.tzinfo is UTC for t in times)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_scan_at(datetime(2026, 10, 20, 8))
    with pytest.raises(ValueError, match="timezone-aware"):
        window_for(datetime(2026, 10, 20, 8))


def test_scan_windows_match_scheduling_stack() -> None:
    assert [(w.label, w.hour, w.minute) for w in SCAN_WINDOWS] == [
        ("LONDON", 8, 3),
        ("NY", 13, 3),
        ("OVERLAP", 15, 3),
        ("DAILY_WRAP", 22, 3),
    ]
