"""Deep links from Telegram messages to the dashboard (Step 4.7 Part B, design §7).

``signal_url`` turns the dashboard's base URL (published by ``DashboardStack`` in SSM) and a
signal id into the page that shows that decision. It accepts only an ``https`` URL with a host:
anything else (missing, empty, ``http``, ``javascript:``) yields ``None`` -- "no link" -- so a bad
parameter can never put a broken or unsafe link into an alert.
"""

from __future__ import annotations

from urllib.parse import urlsplit
from uuid import UUID


def signal_url(base_url: str | None, signal_id: UUID) -> str | None:
    """``<base>/signals/<id>`` for a usable https ``base_url``, else ``None``."""
    if not base_url or not base_url.strip():
        return None
    base = base_url.strip()
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.netloc:
        return None
    return f"{base.rstrip('/')}/signals/{signal_id}"
