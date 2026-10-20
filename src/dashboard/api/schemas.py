"""Response models for the dashboard API (design §6.1).

Every route returns a Pydantic model so the OpenAPI document at
`/api/openapi.json` is the contract the UI codegens against.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """`GET /api/health` body — deploy smoke test only, no I/O behind it."""

    status: Literal["ok"]
    version: str
