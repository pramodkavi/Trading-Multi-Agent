"""Response models for the dashboard API (design §6.1).

Every route returns a Pydantic model so the OpenAPI document at
`/api/openapi.json` is the contract the UI codegens against.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """`GET /api/health` body — deploy smoke test only, no I/O behind it."""

    status: Literal["ok"]
    version: str


class DbProbeResponse(BaseModel):
    """`GET /api/system/db` body — one `SELECT 1` round trip through the Data API."""

    db: Literal["ok", "resuming", "error"]
    latency_ms: int = Field(ge=0, description="Wall-clock time of the probe statement.")


class ErrorResponse(BaseModel):
    """Body of the app-wide error replies (design §3.4).

    `reason` is a stable machine token the UI switches on: `db_resuming` (503,
    with `Retry-After`), `db_error` (502), `not_found` (404). Data routers list
    this model under `responses={503: ..., 502: ...}` so it lands in OpenAPI.
    """

    reason: str
