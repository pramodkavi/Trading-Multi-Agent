"""Unit tests for AsyncpgSignalStore (the local SignalStore facade).

The facade's job is narrow: own a connection *pool*'s lifetime (Step 2.13) and
forward each backend-neutral method to the matching Step 1.9 repository (renaming
``get_by_id`` -> ``get_scan_run`` etc.) on a connection acquired from the pool.
The repositories themselves are covered by test_repositories.py, so here we only
assert the wiring: that calls reach a pooled connection and that ``aclose``
closes the pool. A MagicMock pool + connection stand in for asyncpg -- no
database required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.common.models import AgentRole
from src.persistence.store import AsyncpgSignalStore


class _AcquireCtx:
    """Async context manager standing in for ``pool.acquire()``.

    ``AsyncpgSignalStore._acquire`` does ``async with self._pool.acquire() as
    conn``; this yields the shared mock connection so per-call assertions on it
    still work.
    """

    def __init__(self, conn: MagicMock) -> None:
        self._conn = conn

    async def __aenter__(self) -> MagicMock:
        return self._conn

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False


def _conn() -> MagicMock:
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    return conn


def _pool(conn: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCtx(conn))
    pool.close = AsyncMock()
    return pool


def _store() -> tuple[AsyncpgSignalStore, MagicMock]:
    conn = _conn()
    return AsyncpgSignalStore(_pool(conn)), conn


async def test_start_scan_executes_insert() -> None:
    store, conn = _store()
    await store.start_scan(
        scan_id=uuid4(),
        started_at=datetime(2026, 6, 1, 8, tzinfo=UTC),
        session="LONDON",
        strategy="smc",
        symbols=["BTCUSDT"],
    )
    conn.execute.assert_awaited_once()


async def test_complete_and_fail_scan_execute() -> None:
    store, conn = _store()
    scan_id = uuid4()
    now = datetime(2026, 6, 1, 8, tzinfo=UTC)
    await store.complete_scan(scan_id=scan_id, completed_at=now)
    await store.fail_scan(scan_id=scan_id, completed_at=now, error_message="boom")
    assert conn.execute.await_count == 2


async def test_get_scan_run_returns_none_when_absent() -> None:
    store, conn = _store()
    result = await store.get_scan_run(uuid4())
    assert result is None
    conn.fetchrow.assert_awaited_once()


async def test_create_signal_returns_uuid() -> None:
    from uuid import UUID

    from src.common.models import SignalProposal

    store, conn = _store()
    payload = SignalProposal(
        scan_id=uuid4(),
        strategy="smc",
        symbol="BTCUSDT",
        direction="LONG",  # type: ignore[arg-type]
        entry_price=100.0,
        stop_loss=95.0,
        take_profit_1=115.0,
        risk_reward_ratio=3.0,
        leverage=5.0,
        risk_percent=1.0,
        confluence_narrative="Bullish OB tap with a liquidity sweep below equal lows.",
    )
    signal_id = await store.create_signal(payload)
    assert isinstance(signal_id, UUID)
    conn.execute.assert_awaited_once()


async def test_get_signal_and_list_recent_query_connection() -> None:
    store, conn = _store()
    assert await store.get_signal(uuid4()) is None
    assert await store.list_recent_signals(limit=10, symbol="BTCUSDT") == []
    conn.fetchrow.assert_awaited_once()
    conn.fetch.assert_awaited_once()


async def test_log_agent_run_returns_uuid() -> None:
    from uuid import UUID

    store, conn = _store()
    run_id = await store.log_agent_run(
        scan_id=uuid4(),
        agent_role=AgentRole.ANALYZER,
        strategy="smc",
        input_hash="abc",
        output={"k": "v"},
        latency_ms=10,
    )
    assert isinstance(run_id, UUID)
    conn.execute.assert_awaited_once()


async def test_get_agent_run_returns_none_when_absent() -> None:
    store, conn = _store()
    assert await store.get_agent_run(uuid4()) is None
    conn.fetchrow.assert_awaited_once()


async def test_aclose_closes_pool() -> None:
    conn = _conn()
    pool = _pool(conn)
    store = AsyncpgSignalStore(pool)
    await store.aclose()
    pool.close.assert_awaited_once()
