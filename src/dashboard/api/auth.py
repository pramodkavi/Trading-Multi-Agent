"""Lambda-side defence behind the HTTP API JWT authorizer (design §8, Step 4.4).

The HTTP API validates the Cognito access token (issuer, `client_id`, expiry) on
`ANY /api/{proxy+}` before invoking the Lambda; `GET /api/health` has no
authorizer. This middleware is the second layer: a request that reaches the
function some other way -- a console test event, a direct `lambda:InvokeFunction`,
a route mis-wired without the authorizer -- must not run a data route.

Rule: when the request runs under Lambda (Mangum puts the raw event in
`scope["aws.event"]`) and the path is not in `PUBLIC_PATHS`, the event must
carry `requestContext.authorizer.jwt.claims` as a non-empty mapping with a
non-empty `sub`. Otherwise the reply is **401** `{"reason": "unauthorized"}` and
the inner app is never called, so no route, store, AWS or provider call happens.

Outside Lambda (TestClient, local uvicorn) there is no `aws.event` and the
middleware is a deliberate no-op: there is no authorizer to have run.

It is a pure ASGI middleware rather than Starlette's `BaseHTTPMiddleware` (no
response-streaming / exception-propagation quirks). Neither the claims nor the
`Authorization` header are ever logged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from src.dashboard.api.schemas import ErrorResponse

PUBLIC_PATHS: frozenset[str] = frozenset({"/api/health"})

_UNAUTHORIZED_BODY = ErrorResponse(reason="unauthorized").model_dump_json().encode()


def jwt_claims(scope: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The JWT authorizer claims of the Lambda event in `scope`, or None.

    Reads `scope["aws.event"]["requestContext"]["authorizer"]["jwt"]["claims"]`;
    any missing or non-mapping level yields None.
    """
    node: Any = scope.get("aws.event")
    for key in ("requestContext", "authorizer", "jwt", "claims"):
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node if isinstance(node, Mapping) else None


def _authorized(scope: Mapping[str, Any]) -> bool:
    claims = jwt_claims(scope)
    if not claims:
        return False
    sub = claims.get("sub")
    return isinstance(sub, str) and bool(sub)


class AuthorizerClaimsMiddleware:
    """Reject non-public Lambda requests that lack JWT authorizer claims (401)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and "aws.event" in scope
            and scope["path"] not in PUBLIC_PATHS
            and not _authorized(scope)
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
            return
        await self.app(scope, receive, send)
