"""Dashboard API app factory and Lambda entry point.

Design: docs/superpowers/specs/2026-10-18-serverless-dashboard-design.md
(§3.2 components, §6.1 endpoints, §6.2 module layout).

One FastAPI app is built per Lambda container: `handler` is constructed at
import time from `create_app()` and reused across invocations. Mangum
translates API Gateway HTTP API (payload format 2.0) events into ASGI calls;
the API is deployed on the `$default` stage, so `rawPath` carries no stage
prefix and routes are declared with their full `/api/...` paths.

`lifespan="off"`: nothing is opened at startup in Step 4.1. Step 4.2 adds the
store in `deps.py`, created lazily on first use, so startup stays I/O-free and
the unauthenticated `/api/health` probe can never wake Aurora.

`APP_VERSION` is the short git SHA injected by CDK; it falls back to `"dev"`
when unset (local runs, tests).
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from mangum import Mangum

from src.dashboard.api.routers import health


def create_app(*, version: str | None = None) -> FastAPI:
    """Build the dashboard FastAPI app.

    Args:
        version: Build identifier reported by `/api/health`. Defaults to the
            `APP_VERSION` environment variable, then `"dev"`.
    """
    app = FastAPI(
        title="crypto-signals dashboard API",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.version = version if version is not None else os.environ.get("APP_VERSION", "dev")
    app.include_router(health.router)
    return app


# Lambda entry point: `src.dashboard.api.app.handler`.
handler = Mangum(create_app(), lifespan="off")
