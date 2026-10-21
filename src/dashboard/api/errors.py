"""Data API error mapping for the dashboard's data routes (design §3.4).

The store is built with a resume budget of 1 (`deps.py`), so Aurora's
scale-from-zero `DatabaseResumingException` reaches the route on the first
call. Rather than every router catching `ClientError`, one app-wide handler
maps it:

- resuming  -> **503** `{"reason": "db_resuming"}` + `Retry-After: 10`; the UI
  shows "waking the database..." and retries with backoff (design §3.4).
- otherwise -> **502** `{"reason": "db_error"}`.

The AWS error code and message are logged at WARNING for the operator and
never echoed in the body (the message can carry SQL fragments / ARNs).
`/api/system/db` (`routers/system.py`) deliberately catches `ClientError`
itself and answers 200 with the state, because it *is* the probe.
"""

from __future__ import annotations

import logging

from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Aurora Serverless v2 waking from auto-pause; mirrors
# `src.persistence.store._RESUME_RETRY_CODES` (the only code the store retries).
_RESUMING_CODE = "DatabaseResumingException"

RETRY_AFTER_SECONDS = "10"


def error_code(exc: ClientError) -> str:
    """The Data API error code carried by a botocore `ClientError` ('' if absent)."""
    code = exc.response.get("Error", {}).get("Code", "")
    return str(code)


def is_resuming(exc: ClientError) -> bool:
    """True when `exc` is Aurora's scale-from-zero resume error."""
    return error_code(exc) == _RESUMING_CODE


def register_error_handlers(app: FastAPI) -> None:
    """Install the app-wide `ClientError` -> 503 / 502 mapping on `app`."""

    @app.exception_handler(ClientError)
    async def _client_error_handler(request: Request, exc: ClientError) -> JSONResponse:
        code = error_code(exc)
        message = exc.response.get("Error", {}).get("Message", "")
        if is_resuming(exc):
            logger.warning(
                "Aurora resuming (%s) on %s %s; replying 503 db_resuming",
                code,
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=503,
                content={"reason": "db_resuming"},
                headers={"Retry-After": RETRY_AFTER_SECONDS},
            )
        logger.warning(
            "Data API error %s on %s %s: %s; replying 502 db_error",
            code,
            request.method,
            request.url.path,
            message,
        )
        return JSONResponse(status_code=502, content={"reason": "db_error"})
