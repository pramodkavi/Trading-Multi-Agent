"""Slice 4 Step 4.1 — dashboard API skeleton: app factory, /api/health, Mangum handler.

The `lambda_event_loop` fixture used by the direct `handler(...)` tests lives in
`tests/unit/conftest.py` (shared with the Step 4.2 route tests).
"""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest
from fastapi.testclient import TestClient
from mangum.types import LambdaCognitoIdentity, LambdaMobileClientContext

from src.dashboard.api.app import create_app, handler


def _v2_event(path: str = "/api/health", method: str = "GET") -> dict[str, Any]:
    """Minimal API Gateway HTTP API payload-2.0 event for the $default stage."""
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "abc.execute-api.ap-south-1.amazonaws.com", "accept": "*/*"},
        "requestContext": {
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
        },
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


def test_health_returns_ok_and_version() -> None:
    client = TestClient(create_app(version="abc1234"))
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "abc1234"}


def test_version_defaults_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_VERSION", "deadbee")
    assert TestClient(create_app()).get("/api/health").json()["version"] == "deadbee"
    monkeypatch.delenv("APP_VERSION")
    assert TestClient(create_app()).get("/api/health").json()["version"] == "dev"


def test_health_does_not_touch_aws_or_db(monkeypatch: pytest.MonkeyPatch) -> None:
    # Design §6.1: an unauthenticated probe must never wake Aurora or call AWS.
    def _forbidden(*_a: Any, **_k: Any) -> None:
        pytest.fail("health endpoint must not construct an AWS client")

    monkeypatch.setattr(boto3, "client", _forbidden)
    monkeypatch.setattr(boto3, "Session", _forbidden)
    assert TestClient(create_app(version="x")).get("/api/health").status_code == 200


def test_openapi_lives_under_api_prefix() -> None:
    client = TestClient(create_app(version="x"))
    spec = client.get("/api/openapi.json")
    assert spec.status_code == 200 and "/api/health" in spec.json()["paths"]
    assert client.get("/api/docs").status_code == 200
    assert client.get("/docs").status_code == 404


def test_unknown_route_is_json_404() -> None:
    resp = TestClient(create_app(version="x")).get("/api/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


@pytest.mark.usefixtures("lambda_event_loop")
def test_lambda_handler_serves_http_api_v2_event(monkeypatch: pytest.MonkeyPatch) -> None:
    # no effect: handler built at import; version asserted via create_app() tests
    monkeypatch.setenv("APP_VERSION", "cafe123")
    result = handler(_v2_event(), _Ctx())
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["status"] == "ok"
    assert any(
        k.lower() == "content-type" and "application/json" in v
        for k, v in result["headers"].items()
    )


@pytest.mark.usefixtures("lambda_event_loop")
def test_lambda_handler_404_for_unknown_path() -> None:
    assert handler(_v2_event("/api/nope"), _Ctx())["statusCode"] == 404
