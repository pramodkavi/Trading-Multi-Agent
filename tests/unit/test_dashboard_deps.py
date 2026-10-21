"""Slice 4 Step 4.2 — `deps.py`: one `DataApiSignalStore` per Lambda container, built lazily.

Design §3.4: the dashboard store is constructed with `resume_attempts=1` so a cold Aurora
cluster fails fast (-> HTTP 503) instead of sleeping past API Gateway's 30 s cap.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

import boto3
import pytest

from src.dashboard.api import deps
from src.dashboard.api.settings import get_dashboard_settings
from src.persistence.store import DataApiSignalStore

_CLUSTER = "arn:aws:rds:ap-south-1:123456789012:cluster:signals"
_SECRET = "arn:aws:secretsmanager:ap-south-1:123456789012:secret:signals-db-abc123"


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts (and ends) with no cached settings and no cached store."""
    monkeypatch.setenv("DB_CLUSTER_ARN", _CLUSTER)
    monkeypatch.setenv("DB_SECRET_ARN", _SECRET)
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    monkeypatch.delenv("DB_NAME", raising=False)
    get_dashboard_settings.cache_clear()
    deps.reset_store()
    yield
    deps.reset_store()
    get_dashboard_settings.cache_clear()


def _forbid_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*_a: Any, **_k: Any) -> None:
        pytest.fail("no boto3 client may be created here")

    monkeypatch.setattr(boto3, "client", _forbidden)
    monkeypatch.setattr(boto3, "Session", _forbidden)


def test_importing_deps_creates_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_boto3(monkeypatch)
    # `importlib.reload` re-executes the module body in the same module dict, so the
    # original function objects (which `routers/*.py` captured via `StoreDep`) keep
    # working; put them back afterwards so later modules override the same `get_store`.
    original = {name: getattr(deps, name) for name in ("get_store", "reset_store", "StoreDep")}
    try:
        reloaded = importlib.reload(deps)
        assert reloaded._STORE is None
    finally:
        for name, value in original.items():
            setattr(deps, name, value)


def test_store_is_built_with_one_resume_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_boto3(monkeypatch)
    captured: dict[str, Any] = {}
    sentinel = object()

    def _fake_from_arns(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(DataApiSignalStore, "from_arns", _fake_from_arns)

    assert deps.get_store() is sentinel
    assert captured == {
        "cluster_arn": _CLUSTER,
        "secret_arn": _SECRET,
        "database": "signals",
        "region_name": "ap-south-1",
        "resume_attempts": 1,
    }


def test_store_is_cached_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_boto3(monkeypatch)
    calls: list[dict[str, Any]] = []

    def _fake_from_arns(**kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(DataApiSignalStore, "from_arns", _fake_from_arns)

    first = deps.get_store()
    assert deps.get_store() is first
    assert len(calls) == 1
    deps.reset_store()
    assert deps.get_store() is not first
    assert len(calls) == 2


def test_store_dep_resolves_to_get_store() -> None:
    # `StoreDep` is the Annotated alias routers declare; its Depends must point at
    # `get_store` so `app.dependency_overrides[get_store]` reaches every data route.
    dep = deps.StoreDep.__metadata__[0]
    assert dep.dependency is deps.get_store
