"""Live-Postgres integration tests for the asyncpg repositories.

Marked `integration` and *skipped by default*. Run with:

    docker compose up -d db
    export DATABASE_URL="postgresql://signals:signals@localhost:5432/signals"
    pytest -m integration tests/integration/test_repositories_integration.py

Skips cleanly when DATABASE_URL is unset so a stray `pytest -m integration`
on an empty environment is a clean skip rather than a noisy fail.

Coverage per SPEC §5.2 persistence checkpoints:
- All queries use parameterized statements (verified by the repositories
  themselves; here we round-trip real data).
- Round-trip a SignalProposal and a SkipDecision through SignalRepository.
- Round-trip an AgentRun through AgentRunRepository.
- ScanRunRepository lifecycle: start -> complete and start -> fail.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from itertools import pairwise
from uuid import uuid4

import asyncpg
import psycopg
import pytest

from scripts.migrate import run_migration
from src.common.models import (
    ActiveSetupStatus,
    AgentRole,
    ScanStatus,
    SignalDirection,
    SignalProposal,
    SignalStatus,
    SkipDecision,
    SkipReason,
)
from src.persistence import (
    ActiveSetupRepository,
    AgentRunRepository,
    ScanRunRepository,
    SignalRepository,
)

pytestmark = pytest.mark.integration

DATABASE_URL_ENV = "DATABASE_URL"


def _require_database_url() -> str:
    url = os.getenv(DATABASE_URL_ENV)
    if not url:
        pytest.skip(
            f"{DATABASE_URL_ENV} not set; start `docker compose up -d db` "
            "and export DATABASE_URL to run integration tests"
        )
    return url


def _reset_database(url: str) -> None:
    """Drop everything from the public schema, re-create, re-migrate.

    psycopg (sync) is fine here -- one-shot reset between tests.
    """
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
        conn.commit()
    run_migration(database_url=url)


@pytest.fixture
async def conn() -> asyncpg.Connection[asyncpg.Record]:  # type: ignore[type-arg]
    url = _require_database_url()
    _reset_database(url)
    connection = await asyncpg.connect(url)
    try:
        yield connection
    finally:
        await connection.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proposal(scan_id_param: object) -> SignalProposal:
    return SignalProposal(
        scan_id=scan_id_param,  # type: ignore[arg-type]
        strategy="smc",
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit_1=115.0,
        risk_reward_ratio=3.0,
        leverage=5.0,
        risk_percent=1.0,
        confluence_narrative="Bullish OB tap with liquidity sweep below equal lows.",
    )


def _make_skip(scan_id_param: object) -> SkipDecision:
    return SkipDecision(
        scan_id=scan_id_param,  # type: ignore[arg-type]
        strategy="smc",
        symbol="ETHUSDT",
        reason=SkipReason.NO_CLEAR_BIAS,
        details="Consolidation; bias unclear within freshness window.",
    )


async def _seed_scan(
    conn_obj: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> object:
    """Insert one running scan_runs row; returns its id (UUID)."""
    scan_id = uuid4()
    await ScanRunRepository(conn_obj).start_scan(
        scan_id=scan_id,
        started_at=datetime.now(UTC),
        session="LONDON",
        strategy="smc",
        symbols=["BTCUSDT", "ETHUSDT"],
    )
    return scan_id


# ---------------------------------------------------------------------------
# ScanRunRepository lifecycle
# ---------------------------------------------------------------------------


async def test_scan_run_start_then_complete(
    conn: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> None:
    repo = ScanRunRepository(conn)
    scan_id = uuid4()
    started_at = datetime.now(UTC)
    await repo.start_scan(
        scan_id=scan_id,
        started_at=started_at,
        session="LONDON",
        strategy="smc",
        symbols=["BTCUSDT"],
    )
    stored = await repo.get_by_id(scan_id)
    assert stored is not None
    assert stored.status is ScanStatus.RUNNING

    completed_at = datetime.now(UTC)
    await repo.complete_scan(scan_id=scan_id, completed_at=completed_at)

    stored2 = await repo.get_by_id(scan_id)
    assert stored2 is not None
    assert stored2.status is ScanStatus.SUCCESS
    assert stored2.completed_at is not None


async def test_scan_run_start_then_fail(
    conn: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> None:
    repo = ScanRunRepository(conn)
    scan_id = uuid4()
    await repo.start_scan(
        scan_id=scan_id,
        started_at=datetime.now(UTC),
    )
    await repo.fail_scan(
        scan_id=scan_id,
        completed_at=datetime.now(UTC),
        error_message="Binance timeout after 3 retries",
    )
    stored = await repo.get_by_id(scan_id)
    assert stored is not None
    assert stored.status is ScanStatus.FAILED
    assert stored.error_message == "Binance timeout after 3 retries"


async def test_complete_scan_is_noop_on_already_terminal(
    conn: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> None:
    repo = ScanRunRepository(conn)
    scan_id = uuid4()
    await repo.start_scan(scan_id=scan_id, started_at=datetime.now(UTC))
    await repo.fail_scan(
        scan_id=scan_id,
        completed_at=datetime.now(UTC),
        error_message="boom",
    )
    # Trying to complete an already-FAILED scan should not flip it.
    await repo.complete_scan(scan_id=scan_id, completed_at=datetime.now(UTC))
    stored = await repo.get_by_id(scan_id)
    assert stored is not None
    assert stored.status is ScanStatus.FAILED


# ---------------------------------------------------------------------------
# SignalRepository round-trips
# ---------------------------------------------------------------------------


async def test_signal_repo_round_trips_proposal(
    conn: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> None:
    scan_id = await _seed_scan(conn)
    repo = SignalRepository(conn)
    proposal = _make_proposal(scan_id)

    signal_id = await repo.create_signal(proposal)
    stored = await repo.get_by_id(signal_id)
    assert stored is not None
    assert stored.status is SignalStatus.PUBLISHED
    assert stored.direction is SignalDirection.LONG
    assert stored.symbol == "BTCUSDT"

    round_tripped = stored.as_proposal()
    assert round_tripped.entry_price == proposal.entry_price
    assert round_tripped.confluence_narrative == proposal.confluence_narrative


async def test_signal_repo_round_trips_skip(
    conn: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> None:
    scan_id = await _seed_scan(conn)
    repo = SignalRepository(conn)
    skip = _make_skip(scan_id)

    signal_id = await repo.create_signal(skip)
    stored = await repo.get_by_id(signal_id)
    assert stored is not None
    assert stored.status is SignalStatus.SKIPPED
    assert stored.direction is None

    round_tripped = stored.as_skip()
    assert round_tripped.reason is SkipReason.NO_CLEAR_BIAS


async def test_list_recent_orders_by_created_at_desc(
    conn: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> None:
    scan_id = await _seed_scan(conn)
    repo = SignalRepository(conn)
    for _ in range(3):
        await repo.create_signal(_make_proposal(scan_id))
    rows = await repo.list_recent(limit=5)
    assert len(rows) == 3
    # DESC: created_at[i] >= created_at[i+1]
    for prev, curr in pairwise(rows):
        assert prev.created_at >= curr.created_at


async def test_list_recent_filters_by_symbol(
    conn: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> None:
    scan_id = await _seed_scan(conn)
    repo = SignalRepository(conn)
    await repo.create_signal(_make_proposal(scan_id))
    await repo.create_signal(_make_skip(scan_id))  # symbol = ETHUSDT
    rows = await repo.list_recent(symbol="ETHUSDT")
    assert len(rows) == 1
    assert rows[0].symbol == "ETHUSDT"


# ---------------------------------------------------------------------------
# AgentRunRepository round-trip
# ---------------------------------------------------------------------------


async def test_agent_run_round_trip(
    conn: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> None:
    scan_id = await _seed_scan(conn)
    repo = AgentRunRepository(conn)
    run_id = await repo.log_run(
        scan_id=scan_id,  # type: ignore[arg-type]
        agent_role=AgentRole.ANALYZER,
        strategy="smc",
        input_hash="hash-of-snapshot-abc",
        output={"decision": "publish", "n_candles": 30, "bias": "UPTREND"},
        latency_ms=42,
        token_usage={"input_tokens": 100, "output_tokens": 50, "model": "claude-sonnet-4-5"},
        cost_usd=0.0105,
    )
    stored = await repo.get_by_id(run_id)
    assert stored is not None
    assert stored.scan_id == scan_id
    assert stored.agent_role is AgentRole.ANALYZER
    assert stored.input_hash == "hash-of-snapshot-abc"
    assert stored.output["decision"] == "publish"
    assert stored.latency_ms == 42
    assert stored.token_usage["input_tokens"] == 100
    assert stored.cost_usd == pytest.approx(0.0105)


async def test_agent_run_log_without_cost_or_tokens(
    conn: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> None:
    """Slice 1's analyzer doesn't call the LLM; cost / tokens may be unset."""
    scan_id = await _seed_scan(conn)
    repo = AgentRunRepository(conn)
    run_id = await repo.log_run(
        scan_id=scan_id,  # type: ignore[arg-type]
        agent_role=AgentRole.ANALYZER,
        strategy="smc",
        input_hash="hash-xyz",
        output={"decision": "skip", "reason": "NO_CLEAR_BIAS"},
        latency_ms=8,
    )
    stored = await repo.get_by_id(run_id)
    assert stored is not None
    assert stored.token_usage == {}
    assert stored.cost_usd is None


# ---------------------------------------------------------------------------
# Cross-table integrity
# ---------------------------------------------------------------------------


async def test_signals_cascade_delete_on_scan_run_removal(
    conn: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> None:
    """Deleting a scan_run should cascade to its signals (FK ON DELETE CASCADE)."""
    scan_id = await _seed_scan(conn)
    repo = SignalRepository(conn)
    await repo.create_signal(_make_proposal(scan_id))
    await repo.create_signal(_make_skip(scan_id))

    # Verify rows exist, then delete the parent.
    rows_before = await repo.list_recent(limit=10)
    assert len(rows_before) == 2

    await conn.execute("DELETE FROM scan_runs WHERE id = $1", scan_id)

    rows_after = await repo.list_recent(limit=10)
    assert len(rows_after) == 0


# ---------------------------------------------------------------------------
# ActiveSetupRepository (Step 2.8)
# ---------------------------------------------------------------------------


async def test_active_setup_open_list_update_lifecycle(
    conn: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> None:
    scan_id = await _seed_scan(conn)
    signal_id = await SignalRepository(conn).create_signal(_make_proposal(scan_id))

    repo = ActiveSetupRepository(conn)
    setup_id = await repo.open_setup(signal_id=signal_id)

    open_setups = await repo.list_open()
    assert len(open_setups) == 1
    assert open_setups[0].id == setup_id
    assert open_setups[0].signal_id == signal_id
    assert open_setups[0].is_open
    assert open_setups[0].last_evaluated_at is None

    await repo.update_status(
        setup_id=setup_id,
        status=ActiveSetupStatus.INVALIDATED,
        evaluation={"reason": "premise broke", "outcome": "INVALIDATED"},
    )

    # Resolved setups leave the open queue.
    assert await repo.list_open() == []

    stored = await repo.get_by_id(setup_id)
    assert stored is not None
    assert stored.status is ActiveSetupStatus.INVALIDATED
    assert not stored.is_open
    assert stored.latest_evaluation == {"reason": "premise broke", "outcome": "INVALIDATED"}
    assert stored.last_evaluated_at is not None


async def test_active_setup_cascades_on_signal_delete(
    conn: asyncpg.Connection[asyncpg.Record],  # type: ignore[type-arg]
) -> None:
    """Deleting a signal cascades to its active_setups row (FK ON DELETE CASCADE)."""
    scan_id = await _seed_scan(conn)
    signal_id = await SignalRepository(conn).create_signal(_make_proposal(scan_id))
    repo = ActiveSetupRepository(conn)
    await repo.open_setup(signal_id=signal_id)
    assert len(await repo.list_open()) == 1

    await conn.execute("DELETE FROM signals WHERE id = $1", signal_id)

    assert await repo.list_open() == []
