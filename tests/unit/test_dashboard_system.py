"""Slice 4 Step 4.2 — `/api/system/db` probe and the Data API error mapping (design §3.4, §6.1).

The probe reports `resuming` / `error` with HTTP 200 (it *is* the health signal); every
other data route gets the app-wide `ClientError` handler: 503 `db_resuming` (with
`Retry-After: 10`) or 502 `db_error`, never echoing the AWS message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dashboard.api.app import create_app
from src.dashboard.api.deps import StoreDep, get_store
from src.dashboard.api.errors import is_resuming
from src.persistence.store import DataApiSignalStore

_RESUMING = "DatabaseResumingException"
_OTHER = "BadRequestException"


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": f"{code} (simulated)"}},
        "ExecuteStatement",
    )


class _FakeStore:
    """Stand-in exposing only `read_rows`: returns `rows` or raises `error`."""

    def __init__(
        self, *, rows: list[dict[str, Any]] | None = None, error: Exception | None = None
    ) -> None:
        self.rows = rows if rows is not None else [{"n": 1}]
        self.error = error
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def read_rows(self, sql: str, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.calls.append((sql, parameters))
        if self.error is not None:
            raise self.error
        return self.rows


class _RaisingRdsClient:
    """boto3 `rds-data` stand-in whose every call raises `error`."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def execute_statement(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise self.error


def _client_with(store: object) -> tuple[FastAPI, TestClient]:
    app = create_app(version="t")
    app.dependency_overrides[get_store] = lambda: store
    return app, TestClient(app)


# --- /api/system/db probe -------------------------------------------------------------


def test_db_probe_ok() -> None:
    fake = _FakeStore(rows=[{"n": 1}])
    _, client = _client_with(fake)
    resp = client.get("/api/system/db")
    assert resp.status_code == 200
    body = resp.json()
    assert body["db"] == "ok"
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    assert fake.calls == [("SELECT 1 AS n", [])]


def test_db_probe_resuming(caplog: pytest.LogCaptureFixture) -> None:
    _, client = _client_with(_FakeStore(error=_client_error(_RESUMING)))
    with caplog.at_level(logging.WARNING, logger="src.dashboard.api"):
        resp = client.get("/api/system/db")
    assert resp.status_code == 200
    body = resp.json()
    assert body["db"] == "resuming"
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    assert _RESUMING in caplog.text


def test_db_probe_error(caplog: pytest.LogCaptureFixture) -> None:
    _, client = _client_with(_FakeStore(error=_client_error(_OTHER)))
    with caplog.at_level(logging.WARNING, logger="src.dashboard.api"):
        resp = client.get("/api/system/db")
    assert resp.status_code == 200
    assert resp.json()["db"] == "error"
    assert _OTHER in caplog.text
    assert "simulated" not in resp.text


def test_db_probe_is_in_openapi() -> None:
    spec = TestClient(create_app(version="t")).get("/api/openapi.json").json()
    assert "/api/system/db" in spec["paths"]


# --- app-wide ClientError mapping for data routes ---------------------------------------


def test_is_resuming_matches_only_the_resume_code() -> None:
    assert is_resuming(_client_error(_RESUMING))
    assert not is_resuming(_client_error(_OTHER))
    assert not is_resuming(ClientError({}, "ExecuteStatement"))  # no Error block at all


def test_resuming_maps_to_503_without_sleep(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A throwaway data route reading through a *real* DataApiSignalStore built the way
    # deps.py builds it (resume_attempts=1): the first resume error must propagate with
    # no backoff sleep and become 503 db_resuming.
    async def _no_sleep(_delay: float) -> None:
        pytest.fail("resume budget of 1 must not sleep")

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    rds = _RaisingRdsClient(_client_error(_RESUMING))
    store = DataApiSignalStore(
        client=rds, cluster_arn="c", secret_arn="s", database="signals", resume_attempts=1
    )
    app, client = _client_with(store)

    @app.get("/api/_throwaway")
    async def _throwaway(store: StoreDep) -> list[dict[str, Any]]:
        return await store.read_rows("SELECT 1 AS n", [])

    with caplog.at_level(logging.WARNING, logger="src.dashboard.api"):
        resp = client.get("/api/_throwaway")
    assert resp.status_code == 503
    assert resp.json() == {"reason": "db_resuming"}
    assert resp.headers["retry-after"] == "10"
    assert rds.calls == 1
    assert _RESUMING in caplog.text


def test_other_client_error_maps_to_502(caplog: pytest.LogCaptureFixture) -> None:
    app, client = _client_with(_FakeStore(error=_client_error(_OTHER)))

    @app.get("/api/_throwaway")
    async def _throwaway(store: StoreDep) -> list[dict[str, Any]]:
        return await store.read_rows("SELECT 1 AS n", [])

    with caplog.at_level(logging.WARNING, logger="src.dashboard.api"):
        resp = client.get("/api/_throwaway")
    assert resp.status_code == 502
    assert resp.json() == {"reason": "db_error"}
    assert "retry-after" not in resp.headers
    assert "simulated" not in resp.text  # the AWS message is logged, never echoed
    assert _OTHER in caplog.text and "simulated" in caplog.text


def test_health_still_db_free(monkeypatch: pytest.MonkeyPatch) -> None:
    # Step 4.1 invariant: the unauthenticated probe never builds a client or touches
    # the store, even now that the app owns a store dependency and error handlers.
    def _forbidden(*_a: Any, **_k: Any) -> None:
        pytest.fail("health endpoint must not construct an AWS client")

    monkeypatch.setattr(boto3, "client", _forbidden)
    monkeypatch.setattr(boto3, "Session", _forbidden)
    monkeypatch.setattr(DataApiSignalStore, "from_arns", _forbidden)
    resp = TestClient(create_app(version="x")).get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "x"}
