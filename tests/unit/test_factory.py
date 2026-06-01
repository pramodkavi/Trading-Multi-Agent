"""Unit tests for src.persistence.factory.create_store.

The factory turns Settings into a live SignalStore. We avoid real connections
by patching the two backend constructors:

- ``AsyncpgSignalStore.connect`` (would open a socket) -> AsyncMock
- ``DataApiSignalStore.from_arns`` (would build a boto3 client) -> MagicMock

The happy paths use real Settings (so the cross-field validator is exercised
end to end); the guard branches use a SimpleNamespace fake to reach the
factory's own defensive checks, which a validated Settings can't trigger.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.persistence import factory

CLUSTER_ARN = "arn:aws:rds:us-east-1:123456789012:cluster:crypto"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:crypto-signals/db"


def _asyncpg_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "anthropic_api_key": "sk-ant-test",
        "telegram_bot_token": "123:ABC",
        "telegram_chat_id": "111",
        "database_url": "postgresql://u:p@localhost:5433/db",
        "persistence_backend": "asyncpg",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def _dataapi_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "anthropic_api_key": "sk-ant-test",
        "telegram_bot_token": "123:ABC",
        "telegram_chat_id": "111",
        "persistence_backend": "dataapi",
        "db_cluster_arn": CLUSTER_ARN,
        "db_secret_arn": SECRET_ARN,
        "db_name": "signals",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


async def test_asyncpg_backend_opens_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    connect = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(factory.AsyncpgSignalStore, "connect", connect)

    store = await factory.create_store(_asyncpg_settings())

    assert store is sentinel
    connect.assert_awaited_once_with("postgresql://u:p@localhost:5433/db")


async def test_dataapi_backend_builds_from_arns(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    from_arns = MagicMock(return_value=sentinel)
    monkeypatch.setattr(factory.DataApiSignalStore, "from_arns", from_arns)

    store = await factory.create_store(_dataapi_settings())

    assert store is sentinel
    from_arns.assert_called_once_with(
        cluster_arn=CLUSTER_ARN,
        secret_arn=SECRET_ARN,
        database="signals",
    )


async def test_asyncpg_without_url_raises() -> None:
    fake = SimpleNamespace(persistence_backend="asyncpg", database_url=None)
    with pytest.raises(ValueError, match="requires database_url"):
        await factory.create_store(fake)  # type: ignore[arg-type]


async def test_dataapi_without_arns_raises() -> None:
    fake = SimpleNamespace(
        persistence_backend="dataapi",
        db_cluster_arn=None,
        db_secret_arn=None,
    )
    with pytest.raises(ValueError, match="requires db_cluster_arn"):
        await factory.create_store(fake)  # type: ignore[arg-type]


async def test_unknown_backend_raises() -> None:
    fake = SimpleNamespace(persistence_backend="redis")
    with pytest.raises(ValueError, match="unknown persistence backend"):
        await factory.create_store(fake)  # type: ignore[arg-type]
