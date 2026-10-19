"""Unit tests for src.common.sessions (Slice 4 Step 4.0, design §14 D1).

``session_for`` maps a timezone-aware instant onto the SPEC §1.6 rule-7 UTC
session windows. The boundaries are start-inclusive / end-exclusive, so the
08:00:00 instant is LONDON (not ASIAN) and 20:59:59 is OVERLAP (not COOLDOWN).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.common.models import ScanSession
from src.common.sessions import session_for


@pytest.mark.parametrize(
    ("hh", "mm", "ss", "expected"),
    [
        (0, 0, 0, ScanSession.ASIAN),
        (7, 59, 59, ScanSession.ASIAN),
        (8, 0, 0, ScanSession.LONDON),
        (8, 3, 0, ScanSession.LONDON),
        (12, 59, 59, ScanSession.LONDON),
        (13, 0, 0, ScanSession.NY),
        (13, 3, 0, ScanSession.NY),
        (14, 59, 59, ScanSession.NY),
        (15, 0, 0, ScanSession.OVERLAP),
        (15, 3, 0, ScanSession.OVERLAP),
        (20, 59, 59, ScanSession.OVERLAP),
        (21, 0, 0, ScanSession.COOLDOWN),
        (22, 3, 0, ScanSession.COOLDOWN),
        (23, 59, 59, ScanSession.COOLDOWN),
    ],
)
def test_session_for_boundaries(hh: int, mm: int, ss: int, expected: ScanSession) -> None:
    assert session_for(datetime(2026, 10, 19, hh, mm, ss, tzinfo=UTC)) is expected


def test_session_for_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        session_for(datetime(2026, 10, 19, 8, 3))


def test_session_for_converts_non_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    # 05:30 IST == 00:00 UTC -> ASIAN, not LONDON-ish by local hour.
    assert session_for(datetime(2026, 10, 19, 5, 30, tzinfo=ist)) is ScanSession.ASIAN


def test_session_for_never_returns_ad_hoc_or_daily_wrap() -> None:
    seen = {session_for(datetime(2026, 10, 19, h, tzinfo=UTC)) for h in range(24)}
    assert ScanSession.AD_HOC not in seen and ScanSession.DAILY_WRAP not in seen
