"""Shared, framework-neutral building blocks used across agents and scripts.

Public API:
    session_for -- map a timezone-aware instant onto the SPEC §1.6 rule-7 UTC
        session windows (Slice 4 Step 4.0, design §14 D1)
    SCAN_WINDOWS / ScanWindow / WINDOW_TOLERANCE / next_scan_at / window_for /
        windows_for_day -- the SPEC §5 scan schedule as data: fire times, window
        bucketing of a scan start, next scheduled scan (Slice 4 Step 4.2,
        design §5.1)
"""

from src.common.schedule import (
    SCAN_WINDOWS,
    WINDOW_TOLERANCE,
    ScanWindow,
    next_scan_at,
    window_for,
    windows_for_day,
)
from src.common.sessions import session_for

__all__ = [
    "SCAN_WINDOWS",
    "WINDOW_TOLERANCE",
    "ScanWindow",
    "next_scan_at",
    "session_for",
    "window_for",
    "windows_for_day",
]
