"""Shared, framework-neutral building blocks used across agents and scripts.

Public API:
    session_for -- map a timezone-aware instant onto the SPEC §1.6 rule-7 UTC
        session windows (Slice 4 Step 4.0, design §14 D1)
"""

from src.common.sessions import session_for

__all__ = ["session_for"]
