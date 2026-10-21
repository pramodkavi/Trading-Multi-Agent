"""FastAPI dependencies: the per-container `DataApiSignalStore` (design §3.4, §6.2).

One store (one boto3 `rds-data` client) is built per Lambda container on the
first request that needs it and reused for every later invocation -- the same
lifetime the scan Lambda gives its client. Nothing is created at import, so
`/api/health` (unauthenticated, must never wake Aurora) and the Mangum handler
construction stay I/O-free; `boto3` is only imported inside
`DataApiSignalStore.from_arns`.

The store is built with `resume_attempts=1`: behind API Gateway's 30 s cap the
dashboard cannot wait out an Aurora scale-from-zero resume (~75 s worst case in
the scan Lambda's default budget), so the first `DatabaseResumingException`
propagates immediately and `errors.py` turns it into HTTP 503 `db_resuming`
for the UI to retry.

The boto3 client itself is bounded too (`_DASHBOARD_CLIENT_CONFIG`): botocore's
defaults (60 s connect / 60 s read, several retries) could sit past the Lambda's
own timeout, in which case API Gateway answers a bare 504 and the app-wide
error handler never runs. With a 3 s connect, a 15 s read and a single attempt
the slowest Data API call fails well inside the function's 25 s budget.
"""

from __future__ import annotations

from typing import Annotated

from botocore.config import Config
from fastapi import Depends

from src.dashboard.api.settings import DashboardSettings, get_dashboard_settings
from src.persistence.store import DataApiSignalStore

# Dashboard store resume budget (design §3.4): fail fast, let the client retry.
_DASHBOARD_RESUME_ATTEMPTS = 1

# Bounded botocore timeouts. Lambda timeout is 25 s and API Gateway's cap is 30 s:
# the Lambda must give up FIRST so `errors.py` turns the failure into 503 / 502
# (a body the UI understands) and the gateway never has to answer 504 for us.
# 3 s connect + 15 s read + no botocore retry (`max_attempts=1`) keeps one call
# under 18 s; the store's own resume budget (above) is the only retry layer.
_DASHBOARD_CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=15,
    retries={"max_attempts": 1},
)

_STORE: DataApiSignalStore | None = None


def get_store() -> DataApiSignalStore:
    """Return the container's `DataApiSignalStore`, building it on first use."""
    global _STORE
    if _STORE is None:
        settings = get_dashboard_settings()
        _STORE = DataApiSignalStore.from_arns(
            cluster_arn=settings.db_cluster_arn,
            secret_arn=settings.db_secret_arn,
            database=settings.db_name,
            region_name=settings.aws_region,
            resume_attempts=_DASHBOARD_RESUME_ATTEMPTS,
            client_config=_DASHBOARD_CLIENT_CONFIG,
        )
    return _STORE


def reset_store() -> None:
    """Drop the cached store so the next `get_store()` builds a fresh one (tests)."""
    global _STORE
    _STORE = None


# Route parameter annotation: `async def route(store: StoreDep)`. Tests swap the
# store with `app.dependency_overrides[get_store] = lambda: fake`.
StoreDep = Annotated[DataApiSignalStore, Depends(get_store)]

# The per-container settings (watchlist for the market routes). Tests override
# `get_dashboard_settings` the same way.
SettingsDep = Annotated[DashboardSettings, Depends(get_dashboard_settings)]
