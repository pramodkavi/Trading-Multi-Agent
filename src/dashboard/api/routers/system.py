"""`/api/system/*` -- the database probe and the P7 system-health page (design §4.2 P7, §6.1).

- `GET /api/system/db` -- runs `SELECT 1 AS n` through the container's store and
  reports `{db: ok | resuming | error, latency_ms}`. Unlike the data routes, this
  route catches the Data API `ClientError` itself and answers **200**: the state
  *is* the payload the health strip renders, and a 503 here would be
  indistinguishable from the API being down. Not DB-free by design -- it sits
  behind the authorizer (Step 4.4), unlike `/api/health`, so it cannot be used by
  anonymous scanners to keep Aurora awake.
- `GET /api/system/scans` -- the latest 50 `scan_runs` rows (window, status,
  duration, symbols, error).
- `GET /api/system/agents?days=7` -- per-role runs / failures / LLM cost per UTC
  day (`agent_runs` + `setup_evaluations` + `decision_reviews`) and the cost
  per day; `days` 1..30.
- `GET /api/system/sweeps?days=7` -- Forecaster evaluations / errors per sweep.
- `GET /api/system/alarms` -- the `CryptoSignals-Monitoring-*` alarm states.
- `GET /api/system/metrics?days=7` -- the scan Lambda's daily Invocations /
  Errors / p95 Duration (function discovered from its metrics; `days` 1..30).
- `GET /api/system/config` -- the inferred config panel (design §8).

The DB-backed routes map Data API failures app-wide (503 / 502, `errors.py`).
The CloudWatch routes never fail on CloudWatch: a `ClientError` /
`BotoCoreError` (AccessDenied, throttling, network) answers **200** with the
section `null` and `note = "cloudwatch unavailable"`. Ranges start at midnight
UTC `days` days ago and end now (`overview.utcnow()`).
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Annotated, Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Query

from src.dashboard.api import overview as assembly
from src.dashboard.api import system as health
from src.dashboard.api.cloudwatch import CLOUDWATCH_UNAVAILABLE, CloudWatchDep, read_alarms
from src.dashboard.api.deps import SettingsDep, StoreDep
from src.dashboard.api.errors import DATA_ROUTE_RESPONSES, error_code, is_resuming
from src.dashboard.api.queries import (
    agent_usage_by_day,
    config_inference,
    list_evaluation_stamps,
    list_recent_scan_runs,
)
from src.dashboard.api.schemas import (
    AgentsResponse,
    AlarmsResponse,
    ConfigResponse,
    DbProbeResponse,
    MetricsResponse,
    ScansResponse,
    SweepsResponse,
    UsageDayOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])

_PROBE_SQL = "SELECT 1 AS n"

SCAN_HISTORY_LIMIT = 50
MAX_SYSTEM_DAYS = 30
DEFAULT_SYSTEM_DAYS = 7
# The config panel's watchlist window (design §8: symbols scanned in the last 7 days).
WATCHLIST_WINDOW = timedelta(days=7)
SCAN_FUNCTION_NOT_FOUND = "scan function not found"

DaysQuery = Annotated[
    int, Query(ge=1, le=MAX_SYSTEM_DAYS, description="Days back from midnight UTC (1..30).")
]


@router.get("/api/system/db", response_model=DbProbeResponse)
async def db_probe(store: StoreDep) -> DbProbeResponse:
    """Round-trip one trivial statement and report the database state + latency."""
    state: Literal["ok", "resuming", "error"]
    started = time.perf_counter()
    try:
        await store.read_rows(_PROBE_SQL, [])
    except ClientError as exc:
        state = "resuming" if is_resuming(exc) else "error"
        logger.warning("DB probe: %s (%s)", state, error_code(exc))
    else:
        state = "ok"
    latency_ms = int((time.perf_counter() - started) * 1000)
    return DbProbeResponse(db=state, latency_ms=latency_ms)


@router.get("/api/system/scans", response_model=ScansResponse, responses=DATA_ROUTE_RESPONSES)
async def scans(store: StoreDep) -> ScansResponse:
    """The latest 50 scan runs, newest first."""
    rows = await list_recent_scan_runs(store, limit=SCAN_HISTORY_LIMIT)
    return ScansResponse(items=health.scan_history_out(rows))


@router.get("/api/system/agents", response_model=AgentsResponse, responses=DATA_ROUTE_RESPONSES)
async def agents(store: StoreDep, days: DaysQuery = DEFAULT_SYSTEM_DAYS) -> AgentsResponse:
    """Per-role runs / failures / LLM cost per day, plus the cost per day."""
    now = assembly.utcnow()
    since = health.day_window_start(now, days)
    usage = await agent_usage_by_day(store, since=since)
    return AgentsResponse(
        since=since,
        until=now,
        usage=[UsageDayOut.model_validate(line.model_dump()) for line in usage],
        cost_per_day=health.cost_per_day(usage),
    )


@router.get("/api/system/sweeps", response_model=SweepsResponse, responses=DATA_ROUTE_RESPONSES)
async def sweeps(store: StoreDep, days: DaysQuery = DEFAULT_SYSTEM_DAYS) -> SweepsResponse:
    """Forecaster evaluations / errors per sweep since midnight `days` days ago."""
    now = assembly.utcnow()
    since = health.day_window_start(now, days)
    stamps = await list_evaluation_stamps(store, since=since)
    return SweepsResponse(
        since=since,
        until=now,
        sweeps=health.forecaster_sweeps(stamps, since=since, now=now),
    )


@router.get("/api/system/alarms", response_model=AlarmsResponse)
async def alarms(cw: CloudWatchDep) -> AlarmsResponse:
    """The Monitoring alarms; `alarms: null` + note when CloudWatch failed (still 200)."""
    states, note = await read_alarms(cw)
    return AlarmsResponse(alarms=states, note=note)


@router.get("/api/system/metrics", response_model=MetricsResponse)
async def metrics(cw: CloudWatchDep, days: DaysQuery = DEFAULT_SYSTEM_DAYS) -> MetricsResponse:
    """The scan Lambda's daily metrics; `days: null` + note when unavailable (still 200)."""
    now = assembly.utcnow()
    try:
        function_name = await cw.scan_function_name()
        if function_name is None:
            return MetricsResponse(function_name=None, days=None, note=SCAN_FUNCTION_NOT_FOUND)
        series = await cw.lambda_daily_metrics(function_name, days=days, now=now)
    except (ClientError, BotoCoreError) as exc:
        logger.warning("CloudWatch metrics failed: %s", exc)
        return MetricsResponse(function_name=None, days=None, note=CLOUDWATCH_UNAVAILABLE)
    return MetricsResponse(function_name=function_name, days=series, note=None)


@router.get("/api/system/config", response_model=ConfigResponse, responses=DATA_ROUTE_RESPONSES)
async def config(store: StoreDep, settings: SettingsDep) -> ConfigResponse:
    """The config panel -- inferred from the database (design §8), schedule from SPEC."""
    now = assembly.utcnow()
    facts = await config_inference(store, since=now - WATCHLIST_WINDOW)
    return health.inferred_config(facts, langfuse_configured=settings.langfuse_base_url is not None)
