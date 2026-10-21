"""Slice 4 Step 4.4 -- Lambda-side defence behind the JWT authorizer (design §8).

Under Lambda (Mangum puts the raw event in `scope["aws.event"]`) every path but
`/api/health` must carry the HTTP API JWT authorizer's claims with a `sub`;
otherwise the middleware answers 401 `{"reason": "unauthorized"}` and the route
(and so the store) never runs. Outside Lambda (TestClient, local) it is a no-op.
The Lambda cases go through the real import-time Mangum `handler`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api.app import create_app, handler
from src.dashboard.api.auth import PUBLIC_PATHS, AuthorizerClaimsMiddleware, jwt_claims
from src.dashboard.api.deps import get_store
from tests.unit.dashboard_fakes import CLAIMS, _Ctx, _v2_event


class _RecordingStore:
    """Store stand-in for `/api/system/db`: records every statement it is asked to run."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def read_rows(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        self.calls.append(sql)
        return [{"one": 1}]


@pytest.fixture
def store() -> Iterator[_RecordingStore]:
    """Point the import-time handler app's `get_store` at a recording fake."""
    fake = _RecordingStore()
    overrides = handler.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[get_store] = lambda: fake
    try:
        yield fake
    finally:
        overrides.pop(get_store, None)


def _body(result: dict[str, Any]) -> Any:
    return json.loads(result["body"])


def _content_type(result: dict[str, Any]) -> str:
    return str(next(v for k, v in result["headers"].items() if k.lower() == "content-type"))


@pytest.mark.usefixtures("lambda_event_loop")
def test_lambda_event_without_claims_is_401(store: _RecordingStore) -> None:
    result = handler(_v2_event("/api/system/db"), _Ctx())
    assert result["statusCode"] == 401
    assert _body(result) == {"reason": "unauthorized"}
    assert store.calls == []


@pytest.mark.usefixtures("lambda_event_loop")
def test_lambda_event_with_claims_reaches_route(store: _RecordingStore) -> None:
    result = handler(_v2_event("/api/system/db", claims=CLAIMS), _Ctx())
    assert result["statusCode"] == 200
    assert _body(result)["db"] == "ok"
    assert len(store.calls) == 1


@pytest.mark.usefixtures("lambda_event_loop")
def test_health_open_without_claims_under_lambda() -> None:
    assert sorted(PUBLIC_PATHS) == ["/api/health"]
    result = handler(_v2_event("/api/health"), _Ctx())
    assert result["statusCode"] == 200
    assert _body(result)["status"] == "ok"


@pytest.mark.usefixtures("lambda_event_loop")
@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"client_id": "c-1", "token_use": "access"},
        {"sub": "", "client_id": "c-1"},
        {"sub": None},
    ],
    ids=["empty", "no-sub", "blank-sub", "null-sub"],
)
def test_claims_without_sub_is_401(store: _RecordingStore, claims: dict[str, Any]) -> None:
    result = handler(_v2_event("/api/system/db", claims=claims), _Ctx())
    assert result["statusCode"] == 401
    assert _body(result) == {"reason": "unauthorized"}
    assert store.calls == []


def test_local_testclient_unaffected() -> None:
    # No `aws.event` in the scope -> the middleware is a no-op (local runs, TestClient).
    fake = _RecordingStore()
    app = create_app(version="x")
    app.dependency_overrides[get_store] = lambda: fake
    resp = TestClient(app).get("/api/system/db")
    assert resp.status_code == 200
    assert fake.calls


@pytest.mark.usefixtures("lambda_event_loop")
def test_401_body_and_no_store_call(store: _RecordingStore) -> None:
    for path in ("/api/overview", "/api/system/db", "/api/nope", "/api/health/"):
        result = handler(_v2_event(path), _Ctx())
        assert result["statusCode"] == 401, path
        assert _body(result) == {"reason": "unauthorized"}
        assert _content_type(result).startswith("application/json")
    assert store.calls == []


def test_jwt_claims_extraction() -> None:
    assert jwt_claims({"type": "http"}) is None
    assert jwt_claims({"aws.event": {}}) is None
    assert jwt_claims({"aws.event": {"requestContext": {"authorizer": None}}}) is None
    assert jwt_claims({"aws.event": {"requestContext": {"authorizer": {"jwt": "x"}}}}) is None
    event = _v2_event("/api/overview", claims=CLAIMS)
    assert jwt_claims({"aws.event": event}) == CLAIMS


def test_middleware_registered_on_app() -> None:
    classes: list[object] = [m.cls for m in create_app().user_middleware]
    assert AuthorizerClaimsMiddleware in classes
