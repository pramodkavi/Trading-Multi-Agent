"""Shared Lambda-path helpers for the dashboard API tests.

`_v2_event` builds an API Gateway HTTP API payload-2.0 event for the `$default`
stage; `_Ctx` is a structural `LambdaContext`. Pass `claims=` to add the JWT
authorizer block a request routed through the Step 4.4 Cognito authorizer
carries (`requestContext.authorizer.jwt.claims`); without it the event looks
like a direct invoke / unauthenticated route, which the Lambda-side defence
(`src.dashboard.api.auth`) rejects for every path but `/api/health`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mangum.types import LambdaCognitoIdentity, LambdaMobileClientContext

# Claims the authorizer forwards for a valid Cognito access token (trimmed).
CLAIMS: dict[str, Any] = {
    "sub": "u-1",
    "client_id": "c-1",
    "token_use": "access",
    "exp": "1792494000",
}


def _v2_event(
    path: str = "/api/health",
    method: str = "GET",
    *,
    claims: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Minimal API Gateway HTTP API payload-2.0 event for the $default stage."""
    request_context: dict[str, Any] = {
        "accountId": "123456789012",
        "apiId": "abc",
        "domainName": "abc.execute-api.ap-south-1.amazonaws.com",
        "domainPrefix": "abc",
        "requestId": "req-1",
        "routeKey": f"{method} {path}",
        "stage": "$default",
        "time": "20/Oct/2026:10:00:00 +0000",
        "timeEpoch": 1792490400000,
        "http": {
            "method": method,
            "path": path,
            "protocol": "HTTP/1.1",
            "sourceIp": "203.0.113.1",
            "userAgent": "pytest",
        },
    }
    if claims is not None:
        request_context["authorizer"] = {"jwt": {"claims": dict(claims), "scopes": None}}
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "abc.execute-api.ap-south-1.amazonaws.com", "accept": "*/*"},
        "requestContext": request_context,
        "isBase64Encoded": False,
    }


class _Ctx:
    """Structural stand-in for `mangum.types.LambdaContext`."""

    aws_request_id = "req-1"
    function_name = "dashboard-api"
    function_version = "$LATEST"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:ap-south-1:123456789012:function:dashboard-api"
    log_group_name = "/aws/lambda/dashboard-api"
    log_stream_name = "2026/10/20/[$LATEST]abc"
    identity: LambdaCognitoIdentity | None = None
    client_context: LambdaMobileClientContext | None = None

    def get_remaining_time_in_millis(self) -> int:
        return 25_000
