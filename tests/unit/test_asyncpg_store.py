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


# ---------------------------------------------------------------------------
# Critic v0 (Step 2.14) wiring -- each new method must reach a pooled connection
# ---------------------------------------------------------------------------


def _critic_store() -> tuple[AsyncpgSignalStore, MagicMock]:
    store, conn = _store()
    conn.fetchval = AsyncMock(return_value=0)
    return store, conn


async def test_list_and_count_unresolved_decisions_query_connection() -> None:
    store, conn = _critic_store()
    cutoff = datetime(2026, 7, 1, tzinfo=UTC)
    assert await store.list_unresolved_decisions(limit=10, decided_before=cutoff) == []
    assert await store.count_unresolved_decisions(decided_before=cutoff) == 0
    conn.fetch.assert_awaited_once()
    conn.fetchval.assert_awaited_once()


async def test_list_agent_runs_for_scan_queries_connection() -> None:
    store, conn = _critic_store()
    assert await store.list_agent_runs_for_scan(uuid4()) == []
    conn.fetch.assert_awaited_once()


async def test_save_and_get_resolution_reach_connection() -> None:
    from src.agents.critic.models import DecisionKind, Resolution, ResolutionOutcome

    store, conn = _critic_store()
    res = Resolution(
        signal_id=uuid4(),
        decision_kind=DecisionKind.PUBLISHED,
        outcome=ResolutionOutcome.LOSS,
        decided_at=datetime(2026, 7, 1, tzinfo=UTC),
        resolved_at=datetime(2026, 7, 2, tzinfo=UTC),
        realized_r=-1.0,
        candles_used=3,
        engine_version="replay-v1",
    )
    await store.save_resolution(res)
    assert await store.get_resolution(res.signal_id) is None
    conn.execute.assert_awaited_once()
    conn.fetchrow.assert_awaited_once()


async def test_close_active_setup_for_signal_executes() -> None:
    from src.common.models import ActiveSetupStatus

    store, conn = _critic_store()
    await store.close_active_setup_for_signal(
        signal_id=uuid4(), status=ActiveSetupStatus.LOSS, evaluation={"source": "resolver"}
    )
    conn.execute.assert_awaited_once()


async def test_list_and_count_pending_reviews_query_connection() -> None:
    store, conn = _critic_store()
    t = datetime(2026, 7, 1, tzinfo=UTC)
    assert await store.list_pending_reviews(limit=5, no_setup_before=t, retry_before=t) == []
    assert await store.count_pending_reviews(no_setup_before=t, retry_before=t) == 0
    conn.fetch.assert_awaited_once()
    conn.fetchval.assert_awaited_once()


async def test_save_review_executes() -> None:
    store, conn = _critic_store()
    await store.save_review(
        signal_id=uuid4(),
        decision_kind="PUBLISHED",
        review={"lesson": "x"},
        model="m",
        token_usage={},
        cost_usd=None,
        error=None,
    )
    conn.execute.assert_awaited_once()


async def test_report_reads_query_connection() -> None:
    store, conn = _critic_store()
    t = datetime(2026, 7, 1, tzinfo=UTC)
    assert await store.list_decision_summaries(since=t, until=t, after=None, limit=10) == []
    assert await store.list_scan_runs_between(since=t, until=t, after=None, limit=10) == []
    assert await store.list_active_setups(after=None, limit=10) == []
    assert conn.fetch.await_count == 3


async def test_save_critic_report_executes() -> None:
    from src.persistence.models import StoredCriticReport

    store, conn = _critic_store()
    t = datetime(2026, 7, 1, tzinfo=UTC)
    await store.save_critic_report(
        StoredCriticReport(
            id=uuid4(),
            period_start=t,
            period_end=t,
            report={"summary": "s"},
            markdown="# r",
            pr_url=None,
            created_at=t,
        )
    )
    conn.execute.assert_awaited_once()
