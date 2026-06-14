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
import re
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from src.common.models import (
    ActiveSetupStatus,
    AgentRole,
    ScanStatus,
    SignalDirection,
    SignalOutcome,
    SignalProposal,
    SignalStatus,
    SkipDecision,
)
from src.persistence.models import (
    StoredActiveSetup,
    StoredAgentRun,
    StoredScanRun,
    StoredSignal,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    import asyncpg

# Historian stage-3 builds the L2-distance SQL by interpolating feature KEY names
# (the values are always bound parameters). Keys originate from a code constant
# (historian.L2_FEATURE_KEYS), never user input -- but we whitelist their shape
# anyway as defence-in-depth against SQL injection through a JSONB key.
_SAFE_FEATURE_KEY = re.compile(r"^[A-Za-z0-9_]+$")


def _assert_safe_feature_key(key: str) -> None:
    if not _SAFE_FEATURE_KEY.match(key):
        raise ValueError(f"unsafe feature key for SQL interpolation: {key!r}")


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
            tags = list(payload.tags)
            features: dict[str, Any] = dict(payload.features)
        else:
            status = SignalStatus.SKIPPED
            direction = None
            tags = []
            features = {}
        symbol = payload.symbol
        strategy = payload.strategy
        scan_id = payload.scan_id

        # asyncpg binds Python lists to text[] natively, so tags goes as $8 directly
        # (no string_to_array workaround needed here -- that is only for the Data API).
        await self._conn.execute(
            """
            INSERT INTO signals
                (id, scan_id, symbol, strategy, direction, status, payload, tags, features)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::jsonb)
            """,
            signal_id,
            scan_id,
            symbol,
            strategy,
            direction.value if direction is not None else None,
            status.value,
            _to_jsonb(payload.model_dump(mode="json")),
            tags,
            _to_jsonb(features),
        )
        return signal_id

    async def get_by_id(self, signal_id: UUID) -> StoredSignal | None:
        record = await self._conn.fetchrow(
            """
            SELECT id, scan_id, symbol, strategy, direction, status,
                   created_at, payload, tags, features, outcome, outcome_metadata
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
                       created_at, payload, tags, features, outcome, outcome_metadata
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
                       created_at, payload, tags, features, outcome, outcome_metadata
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

    async def set_outcome(
        self,
        *,
        signal_id: UUID,
        outcome: SignalOutcome,
        outcome_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Stamp a terminal outcome on a signal (Forecaster write side, Step 2.9).

        Used now by the Step 2.4b seed fixture so the journal has outcome-bearing
        rows for the Historian to retrieve; the Forecaster reuses it unchanged.
        """
        await self._conn.execute(
            """
            UPDATE signals
            SET outcome = $1, outcome_metadata = $2::jsonb
            WHERE id = $3
            """,
            outcome.value,
            _to_jsonb(dict(outcome_metadata)) if outcome_metadata is not None else None,
            signal_id,
        )

    async def find_similar(
        self,
        *,
        direction: str,
        session: str | None,
        primary_poi_type: str | None,
        query_tags: list[str],
        l2_features: Sequence[tuple[str, float]],
        limit: int = 10,
        tag_pool: int = 50,
        exclude_signal_id: UUID | None = None,
    ) -> list[StoredSignal]:
        """Historian three-stage retrieval (asyncpg backend; SPEC FR-1.4).

        Stage 1 = the WHERE clause (direction / session / primary_poi_type, plus
        PUBLISHED + known-outcome). Stage 2 = ``tag_overlap`` via the array
        INTERSECT, narrowing to ``tag_pool`` rows. Stage 3 = ``l2_distance`` over
        the scale-free numeric feature vector, returning the top ``limit``.
        """
        capped_limit = max(1, min(limit, 1000))
        capped_pool = max(capped_limit, min(tag_pool, 1000))

        args: list[Any] = []

        def add(value: Any) -> str:
            args.append(value)
            return f"${len(args)}"

        # --- stage 1: hard categorical filters ---
        where = [
            "s.status = 'PUBLISHED'",
            "s.outcome IS NOT NULL",
            f"s.direction = {add(direction)}",
        ]
        if session is not None:
            where.append(f"r.session = {add(session)}")
        if primary_poi_type is not None:
            where.append(f"s.features->>'primary_poi_type' = {add(primary_poi_type)}")
        if exclude_signal_id is not None:
            where.append(f"s.id <> {add(exclude_signal_id)}")

        # --- stage 2: tag-overlap count via PostgreSQL array operators ---
        tags_placeholder = add(query_tags)
        overlap_expr = (
            "COALESCE(cardinality(ARRAY(SELECT unnest(s.tags) "
            f"INTERSECT SELECT unnest({tags_placeholder}::text[]))), 0)"
        )

        # --- stage 3: Euclidean distance over the numeric feature vector ---
        l2_expr = self._l2_expr(l2_features, add)

        pool_placeholder = add(capped_pool)
        limit_placeholder = add(capped_limit)

        sql = f"""
            WITH filtered AS (
                SELECT s.id, s.scan_id, s.symbol, s.strategy, s.direction, s.status,
                       s.created_at, s.payload, s.tags, s.features, s.outcome,
                       s.outcome_metadata,
                       {overlap_expr} AS tag_overlap,
                       {l2_expr} AS l2_distance
                FROM signals s
                JOIN scan_runs r ON s.scan_id = r.id
                WHERE {" AND ".join(where)}
            ),
            tag_ranked AS (
                SELECT * FROM filtered
                ORDER BY tag_overlap DESC, l2_distance ASC
                LIMIT {pool_placeholder}
            )
            SELECT * FROM tag_ranked
            ORDER BY l2_distance ASC, tag_overlap DESC
            LIMIT {limit_placeholder}
        """
        rows = await self._conn.fetch(sql, *args)
        return [self._record_to_stored(r) for r in rows]

    @staticmethod
    def _l2_expr(l2_features: Sequence[tuple[str, float]], add: Any) -> str:
        """Build the L2-distance SQL expression (asyncpg placeholders).

        Each missing-or-non-numeric feature on a candidate contributes nothing
        (``COALESCE(value, query_value) - query_value``), so rows are never
        penalised for features they predate.
        """
        if not l2_features:
            return "0::double precision"
        terms: list[str] = []
        for key, value in l2_features:
            _assert_safe_feature_key(key)
            placeholder = add(value)
            terms.append(
                f"power(COALESCE((s.features->>'{key}')::double precision, "
                f"{placeholder}) - {placeholder}, 2)"
            )
        return f"sqrt({' + '.join(terms)})"

    def _record_to_stored(self, record: asyncpg.Record) -> StoredSignal:
        data = _record_to_dict(record)
        # find_similar adds ranking columns the StoredSignal model forbids; the
        # Historian recomputes them in Python, so drop them here.
        data.pop("tag_overlap", None)
        data.pop("l2_distance", None)
        data["payload"] = _parse_jsonb_field(data["payload"])
        data["features"] = _parse_jsonb_field(data["features"])
        if data.get("outcome_metadata") is not None:
            data["outcome_metadata"] = _parse_jsonb_field(data["outcome_metadata"])
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


# ---------------------------------------------------------------------------
# ActiveSetupRepository
# ---------------------------------------------------------------------------

_ACTIVE_SETUP_COLUMNS = "id, signal_id, opened_at, status, last_evaluated_at, latest_evaluation"


class ActiveSetupRepository:
    """CRUD for the `active_setups` table (Step 2.8).

    A setup is opened (status OPEN) when the Judge publishes a signal. The
    Forecaster (Step 2.9) lists the OPEN setups each scan, records its
    evaluation, and updates the status to a terminal value when one resolves.
    """

    def __init__(self, conn: asyncpg.Connection[Any]) -> None:
        self._conn = conn

    async def open_setup(self, *, signal_id: UUID) -> UUID:
        """Insert a new OPEN setup for ``signal_id``; returns the new id."""
        setup_id = uuid4()
        await self._conn.execute(
            """
            INSERT INTO active_setups (id, signal_id, status)
            VALUES ($1, $2, $3)
            """,
            setup_id,
            signal_id,
            ActiveSetupStatus.OPEN.value,
        )
        return setup_id

    async def list_open(self) -> list[StoredActiveSetup]:
        """All OPEN setups, oldest first (the Forecaster's work queue)."""
        rows = await self._conn.fetch(
            f"""
            SELECT {_ACTIVE_SETUP_COLUMNS}
            FROM active_setups
            WHERE status = $1
            ORDER BY opened_at ASC
            """,
            ActiveSetupStatus.OPEN.value,
        )
        return [self._record_to_stored(r) for r in rows]

    async def get_by_id(self, setup_id: UUID) -> StoredActiveSetup | None:
        record = await self._conn.fetchrow(
            f"""
            SELECT {_ACTIVE_SETUP_COLUMNS}
            FROM active_setups
            WHERE id = $1
            """,
            setup_id,
        )
        if record is None:
            return None
        return self._record_to_stored(record)

    async def update_status(
        self,
        *,
        setup_id: UUID,
        status: ActiveSetupStatus,
        evaluation: Mapping[str, Any] | None = None,
        evaluated_at: datetime | None = None,
    ) -> None:
        """Update a setup's status + evaluation.

        Used for both a non-terminal touch (status stays OPEN, e.g. STILL_VALID /
        AT_RISK) and a close (a terminal status). ``evaluation`` is COALESCEd so
        passing None preserves the previous evaluation; ``last_evaluated_at``
        defaults to NOW() when not given.
        """
        await self._conn.execute(
            """
            UPDATE active_setups
            SET status = $1,
                latest_evaluation = COALESCE($2::jsonb, latest_evaluation),
                last_evaluated_at = COALESCE($3, NOW())
            WHERE id = $4
            """,
            status.value,
            _to_jsonb(dict(evaluation)) if evaluation is not None else None,
            evaluated_at,
            setup_id,
        )

    @staticmethod
    def _record_to_stored(record: asyncpg.Record) -> StoredActiveSetup:
        data = _record_to_dict(record)
        if data.get("latest_evaluation") is not None:
            data["latest_evaluation"] = _parse_jsonb_field(data["latest_evaluation"])
        return StoredActiveSetup.model_validate(data)
