"""Derive the ScanSession from the UTC clock (Slice 4 Step 4.0, design §14 D1).

SPEC §1.6 rule 7 forbids new signals during the Asian session (00:00-08:00 UTC)
and the Cooldown (21:00-00:00 UTC). The risk gate enforces that rule from
``ScanContext.session``, so the session recorded at scan start must reflect the
real clock -- not a hard-coded ``AD_HOC`` (the Step 4.0 D1 fix; see
docs/superpowers/specs/2026-10-18-serverless-dashboard-design.md §14).

Windows (UTC, start-inclusive / end-exclusive):

    00:00-08:00  ASIAN
    08:00-13:00  LONDON
    13:00-15:00  NY
    15:00-21:00  OVERLAP
    21:00-24:00  COOLDOWN

``DAILY_WRAP`` (the 22:03 cron label) and ``AD_HOC`` (an explicit operator
override) are never derived from the clock: the 22:03 scan falls inside
COOLDOWN and is therefore review-only under rule 7.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.common.models.enums import ScanSession

# (start_hour_inclusive, end_hour_exclusive, session), in UTC, covering all 24h.
_WINDOWS: tuple[tuple[int, int, ScanSession], ...] = (
    (0, 8, ScanSession.ASIAN),
    (8, 13, ScanSession.LONDON),
    (13, 15, ScanSession.NY),
    (15, 21, ScanSession.OVERLAP),
    (21, 24, ScanSession.COOLDOWN),
)


def session_for(now: datetime) -> ScanSession:
    """Return the SPEC §1.6 session window that contains ``now``.

    ``now`` must be timezone-aware; it is converted to UTC before the hour
    lookup, so a non-UTC instant is classified by its UTC wall clock, not its
    local one. Raises ``ValueError`` on a naive datetime rather than guessing.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")
    hour = now.astimezone(UTC).hour
    for start, end, session in _WINDOWS:
        if start <= hour < end:
            return session
    raise AssertionError(f"hour {hour} not covered by session windows")  # pragma: no cover
