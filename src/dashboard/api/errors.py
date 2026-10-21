"""Error mapping for the dashboard's data routes (design §3.4).

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

Two more app-wide mappings keep the routers thin and the bodies uniform
(`ErrorResponse`): `NotFound` -> **404** `{"reason": "not_found"}` and the
journal cursor's `BadCursor` -> **400** `{"reason": "bad_cursor"}`.
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.dashboard.api.cursor import BadCursor
from src.dashboard.api.schemas import ErrorResponse

logger = logging.getLogger(__name__)

# Aurora Serverless v2 waking from auto-pause; mirrors
# `src.persistence.store._RESUME_RETRY_CODES` (the only code the store retries).
_RESUMING_CODE = "DatabaseResumingException"

RETRY_AFTER_SECONDS = "10"

# `responses=` entry every data router declares so the 503 / 502 bodies are in OpenAPI.
DATA_ROUTE_RESPONSES: dict[int | str, dict[str, Any]] = {
    503: {
        "model": ErrorResponse,
        "description": "Aurora is resuming; retry after `Retry-After` seconds.",
    },
    502: {
        "model": ErrorResponse,
        "description": "Data API error (details in the Lambda log, never echoed).",
    },
}


class NotFound(Exception):  # noqa: N818 - the name is the route contract, not an "Error"
    """Raised by a data route when the addressed row does not exist -> 404 `not_found`."""


def error_code(exc: ClientError) -> str:
    """The Data API error code carried by a botocore `ClientError` ('' if absent)."""
    code = exc.response.get("Error", {}).get("Code", "")
    return str(code)


def is_resuming(exc: ClientError) -> bool:
    """True when `exc` is Aurora's scale-from-zero resume error."""
    return error_code(exc) == _RESUMING_CODE


def register_error_handlers(app: FastAPI) -> None:
    """Install the app-wide `ClientError` / `NotFound` / `BadCursor` mappings on `app`."""

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

    @app.exception_handler(NotFound)
    async def _not_found_handler(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"reason": "not_found"})

    @app.exception_handler(BadCursor)
    async def _bad_cursor_handler(request: Request, exc: BadCursor) -> JSONResponse:
        return JSONResponse(status_code=400, content={"reason": "bad_cursor"})
