"""Enums shared across signal, scan, and judgment models.

These are foundation types referenced by every agent's I/O. Keeping them
in one module avoids circular imports and makes the vocabulary discoverable.
"""

from enum import StrEnum


class SignalDirection(StrEnum):
    """Direction of a proposed trade."""

    LONG = "LONG"
    SHORT = "SHORT"


class JudgeRuling(StrEnum):
    """Terminal decision of the Judge agent for a SignalProposal.

    PUBLISH               -- send to Telegram as a high-confidence signal.
    PUBLISH_WITH_CAVEAT   -- send to Telegram, prefixed with the Skeptic's
                             objection; recipient takes reduced size.
    SKIP                  -- do not publish; reasoning still journaled.
    """

    PUBLISH = "PUBLISH"
    PUBLISH_WITH_CAVEAT = "PUBLISH_WITH_CAVEAT"
    SKIP = "SKIP"


class ScanSession(StrEnum):
    """UTC trading session windows used by the scheduler and risk gates.

    Derived from SPEC.md §1.7 and §1.6 rule 7. The ASIAN and COOLDOWN
    windows are hard-blocked for new signals; the others are scan triggers.
    """

    LONDON = "LONDON"  # cron 3 8 * * *
    NY = "NY"  # cron 3 13 * * *
    OVERLAP = "OVERLAP"  # cron 3 15 * * * (London-NY)
    DAILY_WRAP = "DAILY_WRAP"  # cron 3 22 * * *
    ASIAN = "ASIAN"  # 00:00-08:00 UTC (no new signals)
    COOLDOWN = "COOLDOWN"  # 21:00-00:00 UTC (no new signals)
    AD_HOC = "AD_HOC"  # manual / dev triggers
