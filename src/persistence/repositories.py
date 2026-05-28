"""asyncpg-backed repositories for scan_runs, signals, and agent_runs.

Each repository takes an `asyncpg.Connection` in its constructor and operates
on that connection. The caller is responsible for connection lifecycle
(opening, transactions, closing). This keeps the repositories simple and
testable; pool-aware variants can wrap these in Slice 2 Step 2.13 when
multi-symbol parallelism arrives.

All methods are async. All SQL uses parameterized statements -- string
interpolation of user-controlled values would be a SQL-injection footgun and
violates the SPEC §5.2 persistence checkpoint.

asyncpg notes:
- $1, $2, ... for parameters (not %s like psycopg).
- INSERT ... RETURNING id with conn.fetchval to get the new PK as a scalar.
- conn.fetchrow / conn.fetch for one / many rows; we convert asyncpg.Record
  to dict at the boundary so the Pydantic wrappers see a clean dict.
- JSONB is sent as a JSON string via json.dumps to avoid relying on a
  per-connection codec registration. simpler and explicit.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from src.common.models import (
    AgentRole,
    ScanStatus,
    SignalDirection,
    SignalProposal,
    SignalStatus,
    SkipDecision,
)
from src.persistence.models import StoredAgentRun, StoredScanRun, StoredSignal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping
    from datetime import datetime

    import asyncpg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_jsonb(payload: Mapping[str, Any]) -> str:
    """Serialise a mapping to a JSON string for INSERT into a JSONB column.

    Using a JSON string lets us avoid asyncpg's per-connection codec setup --
    each connection would otherwise need `await conn.set_type_codec('jsonb', ...)`
    which is one more thing for callers to remember. The DB does the same
    parse on either path; this is the simpler boundary.
    """
    return json.dumps(payload, default=str)


def _record_to_dict(record: asyncpg.Record) -> dict[str, Any]:
    """Convert an asyncpg.Record into a plain dict.

    Records are read-only mapping-like objects; Pydantic's model_validate
    wants a real dict. We pay one allocation per row -- negligible compared
    to the network round-trip cost.
    """
    return dict(record)


def _parse_jsonb_field(value: Any) -> dict[str, Any]:
    """Normalise a JSONB column value into a dict[str, Any].

    asyncpg may return JSONB as a Python str (if no codec is registered) or
    as a parsed dict / list (if one is). We accept both shapes so the
    repositories work regardless of caller-side codec configuration.
    """
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSONB to deserialise to dict, got {type(parsed).__name__}")
    # The cast below is safe because we just asserted dict-ness.
    return dict(parsed)


# ---------------------------------------------------------------------------
# ScanRunRepository
# ---------------------------------------------------------------------------


class ScanRunRepository:
    """Lifecycle operations on the `scan_runs` table.

    Pattern: caller invokes `start_scan` at scan kickoff, then either
    `complete_scan` on success or `fail_scan` on exception. The row is
    immutable in `id`, `started_at`, `session`, `strategy`, `symbols`;
    only the status / completed_at / error_message change.
    """

    def __init__(self, conn: asyncpg.Connection[Any]) -> None:
        self._conn = conn

    async def start_scan(
        self,
        *,
        scan_id: UUID,
        started_at: datetime,
        session: str | None = None,
        strategy: str | None = None,
        symbols: list[str] | None = None,
    ) -> None:
        """Insert a RUNNING row. Idempotency is the caller's concern."""
        await self._conn.execute(
            """
            INSERT INTO scan_runs
                (id, started_at, status, session, strategy, symbols)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            scan_id,
            started_at,
            ScanStatus.RUNNING.value,
            session,
            strategy,
            symbols,
        )

    async def complete_scan(
        self,
        *,
        scan_id: UUID,
        completed_at: datetime,
    ) -> None:
        """Mark the scan SUCCESS. No-op if the row is already terminal."""
        await self._conn.execute(
            """
            UPDATE scan_runs
            SET status = $1, completed_at = $2
            WHERE id = $3 AND status = $4
            """,
            ScanStatus.SUCCESS.value,
            completed_at,
            scan_id,
            ScanStatus.RUNNING.value,
        )

    async def fail_scan(
        self,
        *,
        scan_id: UUID,
        completed_at: datetime,
        error_message: str,
    ) -> None:
        """Mark the scan FAILED with an error message."""
        await self._conn.execute(
            """
            UPDATE scan_runs
            SET status = $1, completed_at = $2, error_message = $3
            WHERE id = $4 AND status = $5
            """,
            ScanStatus.FAILED.value,
            completed_at,
            error_message,
            scan_id,
            ScanStatus.RUNNING.value,
        )

    async def get_by_id(self, scan_id: UUID) -> StoredScanRun | None:
        """Fetch a single row or None."""
        record = await self._conn.fetchrow(
            """
            SELECT id, started_at, completed_at, status, error_message,
                   session, strategy, symbols
            FROM scan_runs
            WHERE id = $1
            """,
            scan_id,
        )
        if record is None:
            return None
        return StoredScanRun.model_validate(_record_to_dict(record))


# ---------------------------------------------------------------------------
# SignalRepository
# ---------------------------------------------------------------------------


class SignalRepository:
    """CRUD for the `signals` table.

    `create_signal` accepts either a SignalProposal (-> status=PUBLISHED) or a
    SkipDecision (-> status=SKIPPED) and discriminates internally so the
    caller doesn't have to branch. The model is serialised to JSONB via
    model_dump(mode='json') so timestamps and UUIDs land as strings the way
    Postgres expects.
    """

    def __init__(self, conn: asyncpg.Connection[Any]) -> None:
        self._conn = conn

    async def create_signal(
        self,
        payload: SignalProposal | SkipDecision,
    ) -> UUID:
        """Insert a row and return its assigned id.

        We generate a UUID here rather than relying on a DB default so the
        caller can join immediately on the returned id without an extra
        round-trip. The DB still validates uniqueness via PK.
        """
        signal_id = uuid4()
        if isinstance(payload, SignalProposal):
            status = SignalStatus.PUBLISHED
            direction: SignalDirection | None = payload.direction
            symbol = payload.symbol
            strategy = payload.strategy
            scan_id = payload.scan_id
        else:
            status = SignalStatus.SKIPPED
            direction = None
            symbol = payload.symbol
            strategy = payload.strategy
            scan_id = payload.scan_id

        await self._conn.execute(
            """
            INSERT INTO signals
                (id, scan_id, symbol, strategy, direction, status, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            """,
            signal_id,
            scan_id,
            symbol,
            strategy,
            direction.value if direction is not None else None,
            status.value,
            _to_jsonb(payload.model_dump(mode="json")),
        )
        return signal_id

    async def get_by_id(self, signal_id: UUID) -> StoredSignal | None:
        record = await self._conn.fetchrow(
            """
            SELECT id, scan_id, symbol, strategy, direction, status,
                   created_at, payload
            FROM signals
            WHERE id = $1
            """,
            signal_id,
        )
        if record is None:
            return None
        return self._record_to_stored(record)

    async def list_recent(
        self,
        *,
        limit: int = 50,
        symbol: str | None = None,
    ) -> list[StoredSignal]:
        """Return the most-recent rows in DESC created_at order.

        Optional `symbol` filter exploits the (symbol, created_at DESC) index
        added in Step 1.8. Caller decides limit; we cap at 1000 to guard
        against a runaway query.
        """
        capped = max(1, min(limit, 1000))
        if symbol is None:
            rows = await self._conn.fetch(
                """
                SELECT id, scan_id, symbol, strategy, direction, status,
                       created_at, payload
                FROM signals
                ORDER BY created_at DESC
                LIMIT $1
                """,
                capped,
            )
        else:
            rows = await self._conn.fetch(
                """
                SELECT id, scan_id, symbol, strategy, direction, status,
                       created_at, payload
                FROM signals
                WHERE symbol = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                symbol,
                capped,
            )
        # asyncpg.fetch returns list[Record], never with None entries.
        return [self._record_to_stored(r) for r in rows]

    def _record_to_stored(self, record: asyncpg.Record) -> StoredSignal:
        data = _record_to_dict(record)
        data["payload"] = _parse_jsonb_field(data["payload"])
        return StoredSignal.model_validate(data)


# ---------------------------------------------------------------------------
# AgentRunRepository
# ---------------------------------------------------------------------------


class AgentRunRepository:
    """Append-only log of agent executions per SPEC §3.1.6 FR-6.2.

    Slice 1 only has the analyzer logging here. Slice 2's Skeptic / Judge /
    Historian / Forecaster will write rows through this same interface;
    StructuredCompletionResult from src/common/llm.py provides the
    latency_ms / token_usage / cost_usd fields directly.
    """

    def __init__(self, conn: asyncpg.Connection[Any]) -> None:
        self._conn = conn

    async def log_run(
        self,
        *,
        scan_id: UUID,
        agent_role: AgentRole,
        strategy: str | None,
        input_hash: str,
        output: Mapping[str, Any],
        latency_ms: int,
        token_usage: Mapping[str, Any] | None = None,
        cost_usd: float | None = None,
        created_at: datetime | None = None,
    ) -> UUID:
        """Insert one agent execution record; returns the new id."""
        run_id = uuid4()
        await self._conn.execute(
            """
            INSERT INTO agent_runs
                (id, scan_id, agent_role, strategy, input_hash, output,
                 latency_ms, token_usage, cost_usd, created_at)
            VALUES
                ($1, $2, $3, $4, $5, $6::jsonb, $7, $8::jsonb, $9,
                 COALESCE($10, NOW()))
            """,
            run_id,
            scan_id,
            agent_role.value,
            strategy,
            input_hash,
            _to_jsonb(dict(output)),
            latency_ms,
            _to_jsonb(dict(token_usage) if token_usage is not None else {}),
            cost_usd,
            created_at,
        )
        return run_id

    async def get_by_id(self, run_id: UUID) -> StoredAgentRun | None:
        record = await self._conn.fetchrow(
            """
            SELECT id, scan_id, agent_role, strategy, input_hash, output,
                   latency_ms, token_usage, cost_usd, created_at
            FROM agent_runs
            WHERE id = $1
            """,
            run_id,
        )
        if record is None:
            return None
        data = _record_to_dict(record)
        data["output"] = _parse_jsonb_field(data["output"])
        data["token_usage"] = _parse_jsonb_field(data["token_usage"])
        if data.get("cost_usd") is not None:
            # asyncpg returns NUMERIC as Decimal; pydantic float field
            # accepts a Decimal but mypy is happier with explicit coercion.
            data["cost_usd"] = float(data["cost_usd"])
        return StoredAgentRun.model_validate(data)
