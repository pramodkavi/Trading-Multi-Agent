"""Unit tests for DataApiSignalStore against a mocked rds-data client.

These exercise the boto3 ``rds-data`` contract entirely offline: a fake client
records every ``execute_statement`` call and returns canned, Data-API-shaped
responses. We assert two things per method:

1. **Outbound SQL + typed parameters** -- the right statement, the right
   ``{"stringValue"|"longValue"|...}`` tagging, ``None`` -> ``{"isNull": True}``.
2. **Inbound parsing** -- ``columnMetadata``/``records`` decode back into the
   Stored* Pydantic models with tz-aware datetimes and JSONB re-parsed to dicts.

No AWS, no network. The real Data API round-trip is covered later by an
integration test once the cluster is deployed (Step 1.22).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.common.models import (
    AgentRole,
    SignalDirection,
    SignalProposal,
    SignalStatus,
)
from src.common.models.skip_decision import SkipDecision, SkipReason
from src.persistence.models import StoredAgentRun, StoredScanRun, StoredSignal
from src.persistence.store import (
    DataApiSignalStore,
    _parse_field,
    _parse_records,
)

CLUSTER_ARN = "arn:aws:rds:us-east-1:123456789012:cluster:crypto"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:crypto-signals/db"
DATABASE = "signals"


# ---------------------------------------------------------------------------
# Fakes / builders
# ---------------------------------------------------------------------------


class FakeRdsDataClient:
    """Records execute_statement calls; returns a pre-seeded response."""

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
        client=client,
        cluster_arn=CLUSTER_ARN,
        secret_arn=SECRET_ARN,
        database=DATABASE,
    )
    return store, client


def params_by_name(call: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["name"]: p["value"] for p in call["parameters"]}


def response(columns: list[str], *records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "columnMetadata": [{"name": c, "label": c} for c in columns],
        "records": list(records),
    }


def make_proposal(**overrides: Any) -> SignalProposal:
    base: dict[str, Any] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "direction": SignalDirection.LONG,
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit_1": 115.0,
        "risk_reward_ratio": 3.0,
        "leverage": 5.0,
        "risk_percent": 1.0,
        "confluence_narrative": "Bullish order block tapped in discount with a liquidity sweep.",
    }
    base.update(overrides)
    return SignalProposal(**base)


def make_skip(**overrides: Any) -> SkipDecision:
    base: dict[str, Any] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "ETHUSDT",
        "reason": SkipReason.NO_CLEAR_BIAS,
        "details": "HTF state machine ambiguous; no clean BOS.",
    }
    base.update(overrides)
    return SkipDecision(**base)


# ---------------------------------------------------------------------------
# Plumbing: client wiring
# ---------------------------------------------------------------------------


async def test_execute_passes_cluster_secret_and_database() -> None:
    store, client = make_store()
    scan_id = uuid4()
    await store.start_scan(scan_id=scan_id, started_at=datetime(2026, 6, 1, 8, tzinfo=UTC))

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["resourceArn"] == CLUSTER_ARN
    assert call["secretArn"] == SECRET_ARN
    assert call["database"] == DATABASE


# ---------------------------------------------------------------------------
# scan_runs writes
# ---------------------------------------------------------------------------


async def test_start_scan_builds_typed_parameters() -> None:
    store, client = make_store()
    scan_id = uuid4()
    started = datetime(2026, 6, 1, 8, 3, tzinfo=UTC)

    await store.start_scan(
        scan_id=scan_id,
        started_at=started,
        session="LONDON",
        strategy="smc",
        symbols=["BTCUSDT", "ETHUSDT"],
    )

    call = client.calls[0]
    assert "INSERT INTO scan_runs" in call["sql"]
    params = params_by_name(call)
    assert params["id"] == {"stringValue": str(scan_id)}
    assert params["started_at"] == {"stringValue": started.isoformat()}
    assert params["status"] == {"stringValue": "RUNNING"}
    assert params["session"] == {"stringValue": "LONDON"}
    assert params["symbols"] == {"arrayValue": {"stringValues": ["BTCUSDT", "ETHUSDT"]}}


async def test_start_scan_nulls_optional_fields() -> None:
    store, client = make_store()
    await store.start_scan(scan_id=uuid4(), started_at=datetime(2026, 6, 1, tzinfo=UTC))

    params = params_by_name(client.calls[0])
    assert params["session"] == {"isNull": True}
    assert params["strategy"] == {"isNull": True}
    assert params["symbols"] == {"isNull": True}


async def test_complete_scan_sets_success() -> None:
    store, client = make_store()
    scan_id = uuid4()
    completed = datetime(2026, 6, 1, 8, 4, tzinfo=UTC)

    await store.complete_scan(scan_id=scan_id, completed_at=completed)

    call = client.calls[0]
    assert "UPDATE scan_runs" in call["sql"]
    params = params_by_name(call)
    assert params["status"] == {"stringValue": "SUCCESS"}
    assert params["completed_at"] == {"stringValue": completed.isoformat()}
    assert params["id"] == {"stringValue": str(scan_id)}
    assert params["running"] == {"stringValue": "RUNNING"}


async def test_fail_scan_records_error() -> None:
    store, client = make_store()
    await store.fail_scan(
        scan_id=uuid4(),
        completed_at=datetime(2026, 6, 1, 8, 4, tzinfo=UTC),
        error_message="binance timeout",
    )

    params = params_by_name(client.calls[0])
    assert params["status"] == {"stringValue": "FAILED"}
    assert params["error_message"] == {"stringValue": "binance timeout"}


# ---------------------------------------------------------------------------
# scan_runs reads
# ---------------------------------------------------------------------------


async def test_get_scan_run_parses_row() -> None:
    columns = [
        "id",
        "started_at",
        "completed_at",
        "status",
        "error_message",
        "session",
        "strategy",
        "symbols",
    ]
    scan_id = uuid4()
    record: list[dict[str, Any]] = [
        {"stringValue": str(scan_id)},
        {"stringValue": "2026-06-01T08:03:00.000000+00:00"},
        {"stringValue": "2026-06-01T08:04:00.000000+00:00"},
        {"stringValue": "SUCCESS"},
        {"isNull": True},
        {"stringValue": "LONDON"},
        {"stringValue": "smc"},
        {"arrayValue": {"stringValues": ["BTCUSDT", "ETHUSDT"]}},
    ]
    store, _ = make_store(response(columns, record))

    result = await store.get_scan_run(scan_id)

    assert isinstance(result, StoredScanRun)
    assert result.id == scan_id
    assert result.status.value == "SUCCESS"
    assert result.started_at.tzinfo is not None
    assert result.completed_at is not None
    assert result.error_message is None
    assert result.symbols == ["BTCUSDT", "ETHUSDT"]


async def test_get_scan_run_handles_null_completed_at() -> None:
    columns = [
        "id",
        "started_at",
        "completed_at",
        "status",
        "error_message",
        "session",
        "strategy",
        "symbols",
    ]
    scan_id = uuid4()
    record: list[dict[str, Any]] = [
        {"stringValue": str(scan_id)},
        {"stringValue": "2026-06-01T08:03:00.000000+00:00"},
        {"isNull": True},
        {"stringValue": "RUNNING"},
        {"isNull": True},
        {"isNull": True},
        {"isNull": True},
        {"isNull": True},
    ]
    store, _ = make_store(response(columns, record))

    result = await store.get_scan_run(scan_id)

    assert result is not None
    assert result.completed_at is None
    assert result.symbols is None


async def test_get_scan_run_returns_none_when_empty() -> None:
    store, _ = make_store(response(["id"]))
    assert await store.get_scan_run(uuid4()) is None


# ---------------------------------------------------------------------------
# signals writes
# ---------------------------------------------------------------------------


async def test_create_signal_proposal_publishes() -> None:
    store, client = make_store()
    proposal = make_proposal()

    signal_id = await store.create_signal(proposal)

    assert isinstance(signal_id, UUID)
    call = client.calls[0]
    assert "INSERT INTO signals" in call["sql"]
    params = params_by_name(call)
    assert params["status"] == {"stringValue": SignalStatus.PUBLISHED.value}
    assert params["direction"] == {"stringValue": "LONG"}
    assert params["scan_id"] == {"stringValue": str(proposal.scan_id)}
    payload = json.loads(params["payload"]["stringValue"])
    assert payload["symbol"] == "BTCUSDT"


async def test_create_signal_skip_has_null_direction() -> None:
    store, client = make_store()
    skip = make_skip()

    await store.create_signal(skip)

    params = params_by_name(client.calls[0])
    assert params["status"] == {"stringValue": SignalStatus.SKIPPED.value}
    assert params["direction"] == {"isNull": True}


# ---------------------------------------------------------------------------
# signals reads
# ---------------------------------------------------------------------------


def _signal_columns() -> list[str]:
    return [
        "id",
        "scan_id",
        "symbol",
        "strategy",
        "direction",
        "status",
        "created_at",
        "payload",
    ]


def _signal_record(proposal: SignalProposal, signal_id: UUID) -> list[dict[str, Any]]:
    return [
        {"stringValue": str(signal_id)},
        {"stringValue": str(proposal.scan_id)},
        {"stringValue": proposal.symbol},
        {"stringValue": proposal.strategy},
        {"stringValue": "LONG"},
        {"stringValue": "PUBLISHED"},
        {"stringValue": "2026-06-01T08:03:00.000000+00:00"},
        {"stringValue": json.dumps(proposal.model_dump(mode="json"))},
    ]


async def test_get_signal_parses_payload_into_dict() -> None:
    proposal = make_proposal()
    signal_id = uuid4()
    store, _ = make_store(response(_signal_columns(), _signal_record(proposal, signal_id)))

    result = await store.get_signal(signal_id)

    assert isinstance(result, StoredSignal)
    assert isinstance(result.payload, dict)
    assert result.status is SignalStatus.PUBLISHED
    # JSONB round-trips back into a typed proposal.
    assert result.as_proposal().symbol == "BTCUSDT"


async def test_get_signal_returns_none_when_empty() -> None:
    store, _ = make_store(response(_signal_columns()))
    assert await store.get_signal(uuid4()) is None


async def test_list_recent_signals_filters_by_symbol_and_caps_limit() -> None:
    proposal = make_proposal()
    rows = [_signal_record(proposal, uuid4()), _signal_record(proposal, uuid4())]
    store, client = make_store(response(_signal_columns(), *rows))

    result = await store.list_recent_signals(limit=5000, symbol="BTCUSDT")

    assert len(result) == 2
    call = client.calls[0]
    assert "WHERE symbol = :symbol" in call["sql"]
    params = params_by_name(call)
    assert params["symbol"] == {"stringValue": "BTCUSDT"}
    assert params["limit"] == {"longValue": 1000}  # 5000 capped to 1000


async def test_list_recent_signals_without_symbol_floors_limit() -> None:
    store, client = make_store(response(_signal_columns()))

    result = await store.list_recent_signals(limit=0)

    assert result == []
    call = client.calls[0]
    assert "WHERE symbol" not in call["sql"]
    assert params_by_name(call)["limit"] == {"longValue": 1}  # 0 floored to 1


# ---------------------------------------------------------------------------
# agent_runs
# ---------------------------------------------------------------------------


async def test_log_agent_run_builds_typed_parameters() -> None:
    store, client = make_store()
    scan_id = uuid4()

    run_id = await store.log_agent_run(
        scan_id=scan_id,
        agent_role=AgentRole.ANALYZER,
        strategy="smc",
        input_hash="abc123",
        output={"decision": "skip"},
        latency_ms=42,
        token_usage={"input": 10, "output": 5},
        cost_usd=0.0012,
    )

    assert isinstance(run_id, UUID)
    call = client.calls[0]
    assert "INSERT INTO agent_runs" in call["sql"]
    params = params_by_name(call)
    assert params["agent_role"] == {"stringValue": "analyzer"}
    assert params["latency_ms"] == {"longValue": 42}
    assert params["cost_usd"] == {"doubleValue": 0.0012}
    assert json.loads(params["output"]["stringValue"]) == {"decision": "skip"}
    assert json.loads(params["token_usage"]["stringValue"]) == {"input": 10, "output": 5}
    assert params["created_at"] == {"isNull": True}


async def test_log_agent_run_nulls_cost_and_defaults_token_usage() -> None:
    store, client = make_store()

    await store.log_agent_run(
        scan_id=uuid4(),
        agent_role=AgentRole.ANALYZER,
        strategy=None,
        input_hash="h",
        output={},
        latency_ms=0,
    )

    params = params_by_name(client.calls[0])
    assert params["cost_usd"] == {"isNull": True}
    assert params["strategy"] == {"isNull": True}
    assert json.loads(params["token_usage"]["stringValue"]) == {}


async def test_get_agent_run_parses_jsonb_and_cost() -> None:
    columns = [
        "id",
        "scan_id",
        "agent_role",
        "strategy",
        "input_hash",
        "output",
        "latency_ms",
        "token_usage",
        "cost_usd",
        "created_at",
    ]
    run_id = uuid4()
    scan_id = uuid4()
    record: list[dict[str, Any]] = [
        {"stringValue": str(run_id)},
        {"stringValue": str(scan_id)},
        {"stringValue": "analyzer"},
        {"stringValue": "smc"},
        {"stringValue": "abc123"},
        {"stringValue": json.dumps({"decision": "skip"})},
        {"longValue": 42},
        {"stringValue": json.dumps({"input": 10})},
        {"doubleValue": 0.0012},
        {"stringValue": "2026-06-01T08:03:00.000000+00:00"},
    ]
    store, _ = make_store(response(columns, record))

    result = await store.get_agent_run(run_id)

    assert isinstance(result, StoredAgentRun)
    assert result.output == {"decision": "skip"}
    assert result.token_usage == {"input": 10}
    assert result.cost_usd == pytest.approx(0.0012)
    assert result.latency_ms == 42
    assert result.created_at.tzinfo is not None


async def test_get_agent_run_handles_null_cost() -> None:
    columns = [
        "id",
        "scan_id",
        "agent_role",
        "strategy",
        "input_hash",
        "output",
        "latency_ms",
        "token_usage",
        "cost_usd",
        "created_at",
    ]
    record: list[dict[str, Any]] = [
        {"stringValue": str(uuid4())},
        {"stringValue": str(uuid4())},
        {"stringValue": "analyzer"},
        {"isNull": True},
        {"stringValue": "h"},
        {"stringValue": "{}"},
        {"longValue": 0},
        {"stringValue": "{}"},
        {"isNull": True},
        {"stringValue": "2026-06-01T08:03:00.000000+00:00"},
    ]
    store, _ = make_store(response(columns, record))

    result = await store.get_agent_run(uuid4())

    assert result is not None
    assert result.cost_usd is None
    assert result.strategy is None


# ---------------------------------------------------------------------------
# field / record parsing primitives
# ---------------------------------------------------------------------------


def test_parse_field_variants() -> None:
    assert _parse_field({"isNull": True}) is None
    assert _parse_field({"stringValue": "x"}) == "x"
    assert _parse_field({"longValue": 7}) == 7
    assert _parse_field({"doubleValue": 1.5}) == pytest.approx(1.5)
    assert _parse_field({"booleanValue": True}) is True
    assert _parse_field({"arrayValue": {"stringValues": ["a", "b"]}}) == ["a", "b"]


def test_parse_field_rejects_unknown_shape() -> None:
    with pytest.raises(ValueError, match="unsupported Data API field"):
        _parse_field({"blobValue": b"x"})


def test_parse_records_zips_columns_to_values() -> None:
    resp = response(["a", "b"], [{"longValue": 1}, {"stringValue": "two"}])
    assert _parse_records(resp) == [{"a": 1, "b": "two"}]


def test_parse_records_empty() -> None:
    assert _parse_records(response(["a"])) == []


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


async def test_aclose_is_noop() -> None:
    store, client = make_store()
    await store.aclose()
    assert client.calls == []
