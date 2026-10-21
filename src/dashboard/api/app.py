"""Dashboard API app factory and Lambda entry point.

Design: docs/superpowers/specs/2026-10-18-serverless-dashboard-design.md
(§3.2 components, §6.1 endpoints, §6.2 module layout).

One FastAPI app is built per Lambda container: `handler` is constructed at
import time from `create_app()` and reused across invocations. Mangum
translates API Gateway HTTP API (payload format 2.0) events into ASGI calls;
the API is deployed on the `$default` stage, so `rawPath` carries no stage
prefix and routes are declared with their full `/api/...` paths.

`lifespan="off"`: nothing is opened at startup. The store lives in `deps.py`
(Step 4.2) and is created lazily on the first request that depends on it, so
startup stays I/O-free and the unauthenticated `/api/health` probe can never
wake Aurora. `errors.py` installs the app-wide error mapping for the data
routes (503 `db_resuming` / 502 `db_error` / 404 `not_found` / 400 `bad_cursor`)
and, since Step 4.3, the market routes (502 `market_unavailable` / 400
`unknown_symbol`). The market provider is also built lazily, per request
(`market.get_provider_factory`), so startup imports no ccxt.

`APP_VERSION` is the short git SHA injected by CDK; it falls back to `"dev"`
when unset (local runs, tests).
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from mangum import Mangum

from src.dashboard.api.errors import register_error_handlers
from src.dashboard.api.routers import (
    funnel,
    health,
    market,
    overview,
    setups,
    signals,
    system,
)


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
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(system.router)
    # Step 4.2 data routes. They exist in the FastAPI app only: the HTTP API routes
    # them (behind the authorizer) in Step 4.4.
    app.include_router(overview.router)
    app.include_router(setups.router)
    app.include_router(signals.router)
    # Step 4.3 market data (candle proxy; the chart route lives in `signals`).
    app.include_router(market.router)
    # Step 4.3 aggregates.
    app.include_router(funnel.router)
    return app


# Lambda entry point: `src.dashboard.api.app.handler`.
handler = Mangum(create_app(), lifespan="off")
