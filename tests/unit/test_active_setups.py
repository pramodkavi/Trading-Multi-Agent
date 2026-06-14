"""Unit tests for active-setups persistence (Slice 2 Step 2.8).

Offline contract tests:
- StoredActiveSetup model (is_open, tz-aware validators)
- ActiveSetupRepository (asyncpg) -- SQL shape + positional params + parsing,
  against a mocked connection
- DataApiSignalStore active-setup methods -- SQL shape + typed :name params +
  parsing, against a mocked rds-data client

The live ranking/round-trip is covered by the opt-in integration tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from src.common.models import ActiveSetupStatus
from src.persistence.models import StoredActiveSetup
from src.persistence.repositories import ActiveSetupRepository
from src.persistence.store import DataApiSignalStore

# ---------------------------------------------------------------------------
# StoredActiveSetup model
# ---------------------------------------------------------------------------


def _setup_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": uuid4(),
        "signal_id": uuid4(),
        "opened_at": datetime(2026, 6, 1, 8, tzinfo=UTC),
        "status": ActiveSetupStatus.OPEN,
    }
    base.update(overrides)
    return base


def test_is_open_true_for_open_status() -> None:
    assert StoredActiveSetup(**_setup_kwargs()).is_open is True


def test_is_open_false_for_terminal_status() -> None:
    assert StoredActiveSetup(**_setup_kwargs(status=ActiveSetupStatus.WIN)).is_open is False


def test_opened_at_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        StoredActiveSetup(**_setup_kwargs(opened_at=datetime(2026, 6, 1, 8)))  # naive


def test_last_evaluated_at_must_be_timezone_aware_when_set() -> None:
    with pytest.raises(ValidationError):
        StoredActiveSetup(**_setup_kwargs(last_evaluated_at=datetime(2026, 6, 1, 9)))  # naive


# ---------------------------------------------------------------------------
# ActiveSetupRepository (asyncpg)
# ---------------------------------------------------------------------------


def _conn() -> MagicMock:
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    return conn


async def test_open_setup_inserts_open_row() -> None:
    conn = _conn()
    repo = ActiveSetupRepository(conn)
    signal_id = uuid4()

    setup_id = await repo.open_setup(signal_id=signal_id)

    assert isinstance(setup_id, UUID)
    sql, *args = conn.execute.call_args.args
    assert "INSERT INTO active_setups" in sql
    assert args[0] == setup_id
    assert args[1] == signal_id
    assert args[2] == "OPEN"


async def test_list_open_filters_and_parses() -> None:
    conn = _conn()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": uuid4(),
                "signal_id": uuid4(),
                "opened_at": datetime(2026, 6, 1, 8, tzinfo=UTC),
                "status": "OPEN",
                "last_evaluated_at": None,
                "latest_evaluation": None,
            }
        ]
    )
    repo = ActiveSetupRepository(conn)

    setups = await repo.list_open()

    sql, *args = conn.fetch.call_args.args
    assert "WHERE status = $1" in sql
    assert "ORDER BY opened_at ASC" in sql
    assert args[0] == "OPEN"
    assert len(setups) == 1
    assert setups[0].is_open


async def test_get_by_id_parses_latest_evaluation() -> None:
    conn = _conn()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": uuid4(),
            "signal_id": uuid4(),
            "opened_at": datetime(2026, 6, 1, 8, tzinfo=UTC),
            "status": "INVALIDATED",
            "last_evaluated_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
            "latest_evaluation": '{"reason": "premise broke"}',  # JSONB as str
        }
    )
    repo = ActiveSetupRepository(conn)

    setup = await repo.get_by_id(uuid4())

    assert setup is not None
    assert not setup.is_open
    assert setup.latest_evaluation == {"reason": "premise broke"}


async def test_get_by_id_returns_none_when_absent() -> None:
    repo = ActiveSetupRepository(_conn())
    assert await repo.get_by_id(uuid4()) is None


async def test_update_status_sets_status_evaluation_and_timestamp() -> None:
    conn = _conn()
    repo = ActiveSetupRepository(conn)
    setup_id = uuid4()

    await repo.update_status(
        setup_id=setup_id,
        status=ActiveSetupStatus.INVALIDATED,
        evaluation={"reason": "premise broke"},
    )

    sql, *args = conn.execute.call_args.args
    assert "UPDATE active_setups" in sql
    assert "status = $1" in sql
    assert "COALESCE($2::jsonb, latest_evaluation)" in sql
    assert "COALESCE($3, NOW())" in sql
    assert args[0] == "INVALIDATED"
    assert "premise broke" in args[1]  # serialized JSON string
    assert args[2] is None  # evaluated_at -> NOW()
    assert args[3] == setup_id


async def test_update_status_none_evaluation_passes_null() -> None:
    conn = _conn()
    repo = ActiveSetupRepository(conn)
    await repo.update_status(setup_id=uuid4(), status=ActiveSetupStatus.OPEN)
    _, *args = conn.execute.call_args.args
    assert args[1] is None  # COALESCE keeps the previous evaluation


# ---------------------------------------------------------------------------
# DataApiSignalStore active-setup methods
# ---------------------------------------------------------------------------

CLUSTER_ARN = "arn:aws:rds:ap-south-1:123456789012:cluster:crypto"
SECRET_ARN = "arn:aws:secretsmanager:ap-south-1:123456789012:secret:crypto-signals/db"
DATABASE = "signals"


class FakeRdsDataClient:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response if response is not None else {}

    def execute_statement(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._response


def make_store(
    response: dict[str, Any] | None = None,
) -> tuple[DataApiSignalStore, FakeRdsDataClient]:
    client = FakeRdsDataClient(response)
    store = DataApiSignalStore(
        client=client, cluster_arn=CLUSTER_ARN, secret_arn=SECRET_ARN, database=DATABASE
    )
    return store, client


def params_by_name(call: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["name"]: p["value"] for p in call["parameters"]}


def sv(value: str) -> dict[str, Any]:
    return {"stringValue": value}


def null() -> dict[str, Any]:
    return {"isNull": True}


def _rows(columns: list[str], record: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "columnMetadata": [{"name": c, "label": c} for c in columns],
        "records": [record],
    }


_COLUMNS = ["id", "signal_id", "opened_at", "status", "last_evaluated_at", "latest_evaluation"]


async def test_dataapi_open_active_setup_inserts() -> None:
    store, client = make_store()
    signal_id = uuid4()

    setup_id = await store.open_active_setup(signal_id=signal_id)

    sql = client.calls[0]["sql"]
    assert "INSERT INTO active_setups" in sql
    params = params_by_name(client.calls[0])
    assert params["id"] == sv(str(setup_id))
    assert params["signal_id"] == sv(str(signal_id))
    assert params["status"] == sv("OPEN")


async def test_dataapi_list_open_sql_and_parse() -> None:
    record = [
        sv(str(uuid4())),
        sv(str(uuid4())),
        sv("2026-06-01T08:03:00.000000+00:00"),
        sv("OPEN"),
        null(),
        null(),
    ]
    store, client = make_store(_rows(_COLUMNS, record))

    setups = await store.list_open_active_setups()

    sql = client.calls[0]["sql"]
    assert "FROM active_setups" in sql
    assert "WHERE status = :status" in sql
    assert "ORDER BY opened_at ASC" in sql
    assert params_by_name(client.calls[0])["status"] == sv("OPEN")
    assert len(setups) == 1
    assert setups[0].is_open
    assert setups[0].last_evaluated_at is None


async def test_dataapi_get_active_setup_parses_evaluation() -> None:
    record = [
        sv(str(uuid4())),
        sv(str(uuid4())),
        sv("2026-06-01T08:03:00.000000+00:00"),
        sv("LOSS"),
        sv("2026-06-01T12:00:00.000000+00:00"),
        sv('{"realized_r": -1.0}'),
    ]
    store, _ = make_store(_rows(_COLUMNS, record))

    setup = await store.get_active_setup(uuid4())

    assert setup is not None
    assert not setup.is_open
    assert setup.latest_evaluation == {"realized_r": -1.0}


async def test_dataapi_get_active_setup_returns_none_when_absent() -> None:
    store, _ = make_store({"columnMetadata": [], "records": []})
    assert await store.get_active_setup(uuid4()) is None


async def test_dataapi_update_active_setup_sql_and_params() -> None:
    store, client = make_store()
    setup_id = uuid4()

    await store.update_active_setup(
        setup_id=setup_id,
        status=ActiveSetupStatus.WIN,
        evaluation={"realized_r": 3.0},
    )

    sql = client.calls[0]["sql"]
    assert "UPDATE active_setups" in sql
    assert "COALESCE(:latest_evaluation::jsonb, latest_evaluation)" in sql
    assert "COALESCE(:evaluated_at::timestamptz, NOW())" in sql
    params = params_by_name(client.calls[0])
    assert params["status"] == sv("WIN")
    assert params["id"] == sv(str(setup_id))
    assert "realized_r" in params["latest_evaluation"]["stringValue"]


async def test_dataapi_update_active_setup_null_evaluation() -> None:
    store, client = make_store()
    await store.update_active_setup(setup_id=uuid4(), status=ActiveSetupStatus.OPEN)
    params = params_by_name(client.calls[0])
    assert params["latest_evaluation"] == null()
    assert params["evaluated_at"] == null()
