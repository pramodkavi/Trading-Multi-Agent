"""Unit tests for DataApiSignalStore.find_similar_signals / set_signal_outcome.

Offline contract tests against a mocked rds-data client (mirrors
test_data_api_store.py): we assert the three-stage SQL shape + typed parameters
on the way out, and StoredSignal decoding (with the ranking columns stripped) on
the way back. The real Postgres ranking is covered by the opt-in integration
test.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from src.common.models import SignalOutcome
from src.persistence.models import StoredSignal
from src.persistence.store import DataApiSignalStore

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


# Data-API typed-field builders
def sv(value: str) -> dict[str, Any]:
    return {"stringValue": value}


def lv(value: int) -> dict[str, Any]:
    return {"longValue": value}


def dv(value: float) -> dict[str, Any]:
    return {"doubleValue": value}


def av(values: list[str]) -> dict[str, Any]:
    return {"arrayValue": {"stringValues": values}}


def null() -> dict[str, Any]:
    return {"isNull": True}


# ---------------------------------------------------------------------------
# find_similar_signals -- outbound SQL + parameters
# ---------------------------------------------------------------------------


async def test_find_similar_builds_three_stage_sql() -> None:
    store, client = make_store()
    await store.find_similar_signals(
        direction="LONG",
        session="LONDON",
        primary_poi_type="order_block",
        query_tags=["smc", "long", "bullish-ob"],
        l2_features=[("confluence_score", 4.0), ("ob_confluence_count", 2.0)],
        limit=10,
        tag_pool=50,
    )

    assert len(client.calls) == 1
    sql = client.calls[0]["sql"]
    # Stage 1: hard filters
    assert "s.status = 'PUBLISHED'" in sql
    assert "s.outcome IS NOT NULL" in sql
    assert "s.direction = :direction" in sql
    assert "r.session = :session" in sql
    assert "s.features->>'primary_poi_type' = :primary_poi_type" in sql
    # Stage 2: tag overlap via array operators
    assert "INTERSECT SELECT unnest(string_to_array(:qtags, ','))" in sql
    assert "AS tag_overlap" in sql
    # Stage 3: L2 distance via sqrt/power over the numeric vector
    assert "sqrt(" in sql
    assert "power(COALESCE((s.features->>'confluence_score')::double precision, :l2_0)" in sql
    assert "power(COALESCE((s.features->>'ob_confluence_count')::double precision, :l2_1)" in sql
    assert "AS l2_distance" in sql
    # ranking + limits
    assert "ORDER BY tag_overlap DESC, l2_distance ASC" in sql
    assert "LIMIT :tag_pool" in sql
    assert "ORDER BY l2_distance ASC, tag_overlap DESC" in sql
    assert "LIMIT :lim" in sql

    params = params_by_name(client.calls[0])
    assert params["direction"] == sv("LONG")
    assert params["session"] == sv("LONDON")
    assert params["primary_poi_type"] == sv("order_block")
    assert params["qtags"] == sv("smc,long,bullish-ob")
    assert params["l2_0"] == dv(4.0)
    assert params["l2_1"] == dv(2.0)
    assert params["tag_pool"] == lv(50)
    assert params["lim"] == lv(10)


async def test_find_similar_omits_optional_filters() -> None:
    store, client = make_store()
    await store.find_similar_signals(
        direction="SHORT",
        session=None,
        primary_poi_type=None,
        query_tags=[],
        l2_features=[],
        limit=5,
        tag_pool=20,
    )
    sql = client.calls[0]["sql"]
    assert "r.session" not in sql
    assert "primary_poi_type" not in sql
    assert "s.id <>" not in sql
    # empty l2 vector degrades to a constant distance (ranking falls to overlap)
    assert "0::double precision AS l2_distance" in sql
    params = params_by_name(client.calls[0])
    assert "session" not in params
    assert "primary_poi_type" not in params
    assert params["qtags"] == null()  # empty tags -> NULL


async def test_find_similar_includes_exclude_clause() -> None:
    store, client = make_store()
    exclude = uuid4()
    await store.find_similar_signals(
        direction="LONG",
        session=None,
        primary_poi_type=None,
        query_tags=["smc"],
        l2_features=[],
        exclude_signal_id=exclude,
    )
    sql = client.calls[0]["sql"]
    assert "s.id <> :exclude_id::uuid" in sql
    assert params_by_name(client.calls[0])["exclude_id"] == sv(str(exclude))


# ---------------------------------------------------------------------------
# find_similar_signals -- inbound parsing (ranking columns stripped)
# ---------------------------------------------------------------------------


async def test_find_similar_parses_rows_and_strips_ranking_columns() -> None:
    signal_id = uuid4()
    scan_id = uuid4()
    columns = [
        "id",
        "scan_id",
        "symbol",
        "strategy",
        "direction",
        "status",
        "created_at",
        "payload",
        "tags",
        "features",
        "outcome",
        "outcome_metadata",
        "tag_overlap",
        "l2_distance",
    ]
    record = [
        sv(str(signal_id)),
        sv(str(scan_id)),
        sv("BTCUSDT"),
        sv("smc"),
        sv("LONG"),
        sv("PUBLISHED"),
        sv("2026-06-01T08:03:00.000000+00:00"),
        sv("{}"),
        av(["smc", "long", "bullish-ob"]),
        sv('{"confluence_score": 4, "ob_confluence_count": 2}'),
        sv("WIN"),
        null(),
        lv(3),
        dv(1.5),
    ]
    response = {
        "columnMetadata": [{"name": c, "label": c} for c in columns],
        "records": [record],
    }
    store, _ = make_store(response)
    rows = await store.find_similar_signals(
        direction="LONG",
        session="LONDON",
        primary_poi_type="order_block",
        query_tags=["smc"],
        l2_features=[("confluence_score", 4.0)],
    )
    assert len(rows) == 1
    sig = rows[0]
    assert isinstance(sig, StoredSignal)
    assert sig.id == signal_id
    assert sig.outcome is SignalOutcome.WIN
    assert sig.tags == ["smc", "long", "bullish-ob"]
    assert sig.features == {"confluence_score": 4, "ob_confluence_count": 2}


# ---------------------------------------------------------------------------
# set_signal_outcome
# ---------------------------------------------------------------------------


async def test_set_signal_outcome_sql_and_params() -> None:
    store, client = make_store()
    signal_id = uuid4()
    await store.set_signal_outcome(
        signal_id=signal_id,
        outcome=SignalOutcome.WIN,
        outcome_metadata={"exit_price": 109.0, "realized_r": 3.0},
    )
    call = client.calls[0]
    sql = call["sql"]
    assert "UPDATE signals" in sql
    assert "outcome = :outcome" in sql
    assert "outcome_metadata = :outcome_metadata::jsonb" in sql
    assert "WHERE id = :id::uuid" in sql
    params = params_by_name(call)
    assert params["outcome"] == sv("WIN")
    assert params["id"] == sv(str(signal_id))
    assert "exit_price" in params["outcome_metadata"]["stringValue"]


async def test_set_signal_outcome_null_metadata() -> None:
    store, client = make_store()
    await store.set_signal_outcome(signal_id=uuid4(), outcome=SignalOutcome.LOSS)
    params = params_by_name(client.calls[0])
    assert params["outcome"] == sv("LOSS")
    assert params["outcome_metadata"] == null()
