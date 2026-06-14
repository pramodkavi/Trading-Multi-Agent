"""Unit tests for SignalRepository.find_similar / set_outcome (asyncpg backend).

Mirror of test_find_similar_dataapi.py for the asyncpg path: a mocked connection
captures the outbound SQL + positional parameters (the three-stage shape), and
we verify row decoding strips the ranking columns. The live ranking is covered
by tests/integration/test_historian_integration.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.common.models import SignalOutcome
from src.persistence.models import StoredSignal
from src.persistence.repositories import SignalRepository


def _conn_mock() -> MagicMock:
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    return conn


async def test_find_similar_sql_shape_and_positional_params() -> None:
    conn = _conn_mock()
    repo = SignalRepository(conn)
    await repo.find_similar(
        direction="LONG",
        session="LONDON",
        primary_poi_type="order_block",
        query_tags=["smc", "long"],
        l2_features=[("confluence_score", 4.0), ("ob_confluence_count", 2.0)],
        limit=10,
        tag_pool=50,
    )

    sql, *args = conn.fetch.call_args.args
    # Stage 1: hard filters + the scan_runs JOIN (for session).
    assert "JOIN scan_runs r ON s.scan_id = r.id" in sql
    assert "s.status = 'PUBLISHED'" in sql
    assert "s.outcome IS NOT NULL" in sql
    assert "s.direction = $1" in sql
    assert "r.session = $2" in sql
    assert "s.features->>'primary_poi_type' = $3" in sql
    # Stage 2: tag overlap via array operators (query tags bound as text[]).
    assert "INTERSECT SELECT unnest($4::text[])" in sql
    # Stage 3: L2 distance over the numeric vector (value reused as fallback).
    assert "power(COALESCE((s.features->>'confluence_score')::double precision, $5) - $5, 2)" in sql
    assert (
        "power(COALESCE((s.features->>'ob_confluence_count')::double precision, $6) - $6, 2)" in sql
    )
    assert "sqrt(" in sql
    assert "ORDER BY tag_overlap DESC, l2_distance ASC" in sql
    assert "ORDER BY l2_distance ASC, tag_overlap DESC" in sql

    assert args == ["LONG", "LONDON", "order_block", ["smc", "long"], 4.0, 2.0, 50, 10]


async def test_find_similar_omits_optional_filters() -> None:
    conn = _conn_mock()
    repo = SignalRepository(conn)
    await repo.find_similar(
        direction="SHORT",
        session=None,
        primary_poi_type=None,
        query_tags=[],
        l2_features=[],
    )
    sql, *args = conn.fetch.call_args.args
    assert "r.session" not in sql
    assert "primary_poi_type" not in sql
    assert "s.id <>" not in sql
    assert "0::double precision AS l2_distance" in sql
    # direction ($1), query_tags ($2), tag_pool ($3), limit ($4)
    assert args == ["SHORT", [], 50, 10]


async def test_find_similar_includes_exclude() -> None:
    conn = _conn_mock()
    repo = SignalRepository(conn)
    exclude = uuid4()
    await repo.find_similar(
        direction="LONG",
        session=None,
        primary_poi_type=None,
        query_tags=["smc"],
        l2_features=[],
        exclude_signal_id=exclude,
    )
    sql, *args = conn.fetch.call_args.args
    assert "s.id <> $2" in sql
    assert args[1] == exclude


async def test_find_similar_strips_ranking_columns_on_parse() -> None:
    row: dict[str, Any] = {
        "id": uuid4(),
        "scan_id": uuid4(),
        "symbol": "BTCUSDT",
        "strategy": "smc",
        "direction": "LONG",
        "status": "PUBLISHED",
        "created_at": datetime(2026, 6, 1, 8, tzinfo=UTC),
        "payload": {},
        "tags": ["smc", "long"],
        "features": {"confluence_score": 4},
        "outcome": "WIN",
        "outcome_metadata": None,
        "tag_overlap": 2,  # extra ranking column -> must be dropped
        "l2_distance": 1.5,  # extra ranking column -> must be dropped
    }
    conn = _conn_mock()
    conn.fetch = AsyncMock(return_value=[row])
    repo = SignalRepository(conn)
    rows = await repo.find_similar(
        direction="LONG",
        session=None,
        primary_poi_type=None,
        query_tags=["smc"],
        l2_features=[],
    )
    assert len(rows) == 1
    assert isinstance(rows[0], StoredSignal)
    assert rows[0].outcome is SignalOutcome.WIN
    assert rows[0].features == {"confluence_score": 4}


async def test_set_outcome_sql_and_params() -> None:
    conn = _conn_mock()
    repo = SignalRepository(conn)
    signal_id = uuid4()
    await repo.set_outcome(
        signal_id=signal_id,
        outcome=SignalOutcome.LOSS,
        outcome_metadata={"realized_r": -1.0},
    )
    sql, *args = conn.execute.call_args.args
    assert "UPDATE signals" in sql
    assert "SET outcome = $1, outcome_metadata = $2::jsonb" in sql
    assert "WHERE id = $3" in sql
    assert args[0] == "LOSS"
    assert "realized_r" in args[1]  # serialized JSON string
    assert args[2] == signal_id


async def test_set_outcome_null_metadata() -> None:
    conn = _conn_mock()
    repo = SignalRepository(conn)
    await repo.set_outcome(signal_id=uuid4(), outcome=SignalOutcome.WIN)
    _, *args = conn.execute.call_args.args
    assert args[0] == "WIN"
    assert args[1] is None
