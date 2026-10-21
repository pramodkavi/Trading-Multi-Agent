"""`GET /api/system/db` -- the database probe (design §6.1, behind the authorizer).

Runs `SELECT 1 AS n` through the container's store and reports
`{db: ok | resuming | error, latency_ms}`. Unlike the data routes, this route
catches the Data API `ClientError` itself and answers **200**: the state *is*
the payload the health strip renders, and a 503 here would be indistinguishable
from the API being down. The app-wide 503 / 502 mapping in `errors.py` is for
routes that cannot serve data without the database.

Not DB-free by design -- it sits behind the authorizer (Step 4.4), unlike
`/api/health`, so it cannot be used by anonymous scanners to keep Aurora awake.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter

from src.dashboard.api.deps import StoreDep
from src.dashboard.api.errors import error_code, is_resuming
from src.dashboard.api.schemas import DbProbeResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])

_PROBE_SQL = "SELECT 1 AS n"


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
