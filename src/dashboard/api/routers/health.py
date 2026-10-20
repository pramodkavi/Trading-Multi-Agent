"""`GET /api/health` — unauthenticated deploy smoke test (design §6.1).

This route must never touch the database or any AWS API: it sits outside the
authorizer, so a probe that woke Aurora would let any scanner keep the cluster
awake (~$25/month). It only echoes the version stamped on the app at startup.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from src.dashboard.api.schemas import HealthResponse

router = APIRouter(tags=["system"])


@router.get("/api/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Return `{status: "ok", version}` without performing any I/O."""
    return HealthResponse(status="ok", version=request.app.state.version)
