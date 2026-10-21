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

Step 4.3 market data adds two: any `ProviderError` (Binance down, rate limited,
timed out, malformed) -> **502** `{"reason": "market_unavailable"}`, the
provider message logged at WARNING and never echoed; and `UnknownSymbol` (a
klines request for a symbol outside the watchlist, raised before any provider
is built) -> **400** `{"reason": "unknown_symbol"}`. The setup cards' unrealised
R never reaches the `ProviderError` handler: `market.latest_prices` swallows
provider errors and timeouts by design.
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.dashboard.api.cursor import BadCursor
from src.dashboard.api.schemas import ErrorResponse
from src.persistence.store import RESUME_RETRY_CODES
from src.providers.base import ProviderError

logger = logging.getLogger(__name__)

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


# `responses=` entry of the routes that call the market provider.
MARKET_ROUTE_RESPONSES: dict[int | str, dict[str, Any]] = {
    502: {
        "model": ErrorResponse,
        "description": "`db_error`, or `market_unavailable` when the market data provider "
        "failed (details in the Lambda log, never echoed).",
    },
}


class NotFound(Exception):  # noqa: N818 - the name is the route contract, not an "Error"
    """Raised by a data route when the addressed row does not exist -> 404 `not_found`."""


class UnknownSymbol(Exception):  # noqa: N818 - the name is the route contract, not an "Error"
    """Raised when a market route is asked for a non-watchlist symbol -> 400 `unknown_symbol`."""


def error_code(exc: ClientError) -> str:
    """The Data API error code carried by a botocore `ClientError` ('' if absent)."""
    code = exc.response.get("Error", {}).get("Code", "")
    return str(code)


def is_resuming(exc: ClientError) -> bool:
    """True when `exc` is Aurora's scale-from-zero resume error.

    The codes are the store's own `RESUME_RETRY_CODES` (the only ones its
    `_execute` retries), so the 503 mapping and the retry loop cannot drift apart.
    """
    return error_code(exc) in RESUME_RETRY_CODES


def register_error_handlers(app: FastAPI) -> None:
    """Install the app-wide `ClientError` / `NotFound` / `BadCursor` / market mappings."""

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

    @app.exception_handler(UnknownSymbol)
    async def _unknown_symbol_handler(request: Request, exc: UnknownSymbol) -> JSONResponse:
        return JSONResponse(status_code=400, content={"reason": "unknown_symbol"})

    @app.exception_handler(ProviderError)
    async def _provider_error_handler(request: Request, exc: ProviderError) -> JSONResponse:
        logger.warning(
            "Market provider %s failed (%s) on %s %s: %s; replying 502 market_unavailable",
            exc.provider,
            type(exc).__name__,
            request.method,
            request.url.path,
            exc,
        )
        return JSONResponse(status_code=502, content={"reason": "market_unavailable"})
