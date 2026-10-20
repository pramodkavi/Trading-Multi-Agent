"""asyncpg-backed repositories for scan_runs, signals, agent_runs, active_setups
and the Critic v0 tables (decision_resolutions, decision_reviews, critic_reports).

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
    SetupEvaluationStatus,
    SignalDirection,
    SignalOutcome,
    SignalProposal,
    SignalStatus,
    SkipDecision,
)
from src.persistence.models import (
    DecisionSummary,
    StoredActiveSetup,
    StoredAgentRun,
    StoredCriticReport,
    StoredScanRun,
    StoredSetupEvaluation,
    StoredSignal,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    import asyncpg

    # Typing-only: ``src.agents.critic.models`` imports ``src.persistence.models``
    # (-> this package's __init__ -> this module), so a runtime import here would
    # be a cycle whenever the critic package loads first. ``get_resolution``
    # imports it lazily at call time.
    from src.agents.critic.models import Resolution

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
        return self._record_to_stored(record)

    @staticmethod
    def _record_to_stored(record: asyncpg.Record) -> StoredScanRun:
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

    @staticmethod
    def _record_to_stored(record: asyncpg.Record) -> StoredSignal:
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
        return self._record_to_stored(record)

    @staticmethod
    def _record_to_stored(record: asyncpg.Record) -> StoredAgentRun:
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


# ---------------------------------------------------------------------------
# SetupEvaluationRepository (Slice 4 Step 4.0, dashboard design §5.2)
# ---------------------------------------------------------------------------

_SETUP_EVALUATION_COLUMNS = (
    "id, setup_id, signal_id, evaluated_at, status, outcome, reasoning, "
    "error, latency_ms, token_usage, cost_usd"
)


class SetupEvaluationRepository:
    """Append-only journal of Forecaster evaluations: the `setup_evaluations` table.

    One row per look at a setup -- a STILL_VALID / AT_RISK / INVALIDATED verdict,
    or an ERROR row when the per-setup evaluation raised (design §5.2: failures
    must leave a trace). ``active_setups.latest_evaluation`` remains the
    "last one wins" cache; this is the full history the dashboard reads.
    """

    def __init__(self, conn: asyncpg.Connection[Any]) -> None:
        self._conn = conn

    async def record(
        self,
        *,
        setup_id: UUID,
        signal_id: UUID,
        status: SetupEvaluationStatus,
        outcome: SignalOutcome | None = None,
        reasoning: str | None = None,
        error: str | None = None,
        latency_ms: int = 0,
        token_usage: Mapping[str, Any] | None = None,
        cost_usd: float | None = None,
        evaluated_at: datetime | None = None,
    ) -> UUID:
        """Insert one evaluation row; returns the new id.

        ``evaluated_at`` defaults to ``NOW()`` DB-side (as ``AgentRunRepository
        .log_run`` does for ``created_at``); ``token_usage`` defaults to ``{}``.
        """
        evaluation_id = uuid4()
        await self._conn.execute(
            """
            INSERT INTO setup_evaluations
                (id, setup_id, signal_id, evaluated_at, status, outcome, reasoning,
                 error, latency_ms, token_usage, cost_usd)
            VALUES
                ($1, $2, $3, COALESCE($4, NOW()), $5, $6, $7, $8, $9, $10::jsonb, $11)
            """,
            evaluation_id,
            setup_id,
            signal_id,
            evaluated_at,
            status.value,
            outcome.value if outcome is not None else None,
            reasoning,
            error,
            latency_ms,
            _to_jsonb(dict(token_usage) if token_usage is not None else {}),
            cost_usd,
        )
        return evaluation_id

    async def list_for_setup(
        self, setup_id: UUID, *, limit: int = 50
    ) -> list[StoredSetupEvaluation]:
        """A setup's evaluations newest first; ``limit`` is clamped to 1..200.

        Served by the §5.2 ``(setup_id, evaluated_at DESC)`` index.
        """
        capped = max(1, min(limit, 200))
        rows = await self._conn.fetch(
            f"""
            SELECT {_SETUP_EVALUATION_COLUMNS}
            FROM setup_evaluations
            WHERE setup_id = $1
            ORDER BY evaluated_at DESC, id DESC
            LIMIT $2
            """,
            setup_id,
            capped,
        )
        return [self._record_to_stored(r) for r in rows]

    @staticmethod
    def _record_to_stored(record: asyncpg.Record) -> StoredSetupEvaluation:
        data = _record_to_dict(record)
        data["token_usage"] = _parse_jsonb_field(data["token_usage"])
        if data.get("cost_usd") is not None:
            # asyncpg returns NUMERIC as Decimal; coerce as AgentRunRepository does.
            data["cost_usd"] = float(data["cost_usd"])
        return StoredSetupEvaluation.model_validate(data)


# ---------------------------------------------------------------------------
# DecisionRepository (Critic v0, Step 2.14)
# ---------------------------------------------------------------------------

# Column lists shared with the sibling repositories' mappers. ``_prefixed`` is
# for queries that alias ``signals`` as ``s`` (joins against the Critic tables).
_SIGNAL_COLUMNS = (
    "id, scan_id, symbol, strategy, direction, status, "
    "created_at, payload, tags, features, outcome, outcome_metadata"
)
_AGENT_RUN_COLUMNS = (
    "id, scan_id, agent_role, strategy, input_hash, output, "
    "latency_ms, token_usage, cost_usd, created_at"
)
_SCAN_RUN_COLUMNS = (
    "id, started_at, completed_at, status, error_message, session, strategy, symbols"
)


def _prefixed(columns: str, alias: str) -> str:
    return ", ".join(f"{alias}.{c}" for c in columns.split(", "))


_SIGNAL_COLUMNS_S = _prefixed(_SIGNAL_COLUMNS, "s")

# The list_* / count_* pairs below must agree on their predicate, so each pair
# is built from one shared SQL fragment rather than two hand-copied strings.
#
# Unresolved decision ($1 = decided_before): a proposal (direction set) or a
# gate-rejected skip (payload.violated_rule set) that has no resolution row yet.
# NO_SETUP skips never get one -- there is nothing to price-replay.
_UNRESOLVED_FROM_WHERE = """
    FROM signals s
    LEFT JOIN decision_resolutions r ON r.signal_id = s.id
    WHERE r.signal_id IS NULL
      AND (s.direction IS NOT NULL OR s.payload->>'violated_rule' IS NOT NULL)
      AND s.created_at < $1
"""

# Pending review ($1 = no_setup_before, $2 = retry_before). A decision needs a
# review when it has no review row, or its only row errored and is still under
# the retry cap (attempts < 2) and old enough to retry. Branch (a): resolved
# decisions. Branch (b): NO_SETUP skips, which skip resolution entirely and are
# reviewed once they are ``no_setup_before`` old (design §6.1).
_REVIEW_MISSING_OR_RETRYABLE = """(v.signal_id IS NULL
         OR (v.error IS NOT NULL AND v.attempts < 2 AND v.created_at < $2))"""

_PENDING_REVIEWS_UNION = f"""
    SELECT {_SIGNAL_COLUMNS_S}
    FROM signals s
    JOIN decision_resolutions r ON r.signal_id = s.id
    LEFT JOIN decision_reviews v ON v.signal_id = s.id
    WHERE {_REVIEW_MISSING_OR_RETRYABLE}
    UNION ALL
    SELECT {_SIGNAL_COLUMNS_S}
    FROM signals s
    LEFT JOIN decision_reviews v ON v.signal_id = s.id
    WHERE s.direction IS NULL AND s.payload->>'violated_rule' IS NULL
      AND s.created_at < $1
      AND {_REVIEW_MISSING_OR_RETRYABLE}
"""

_RESOLUTION_COLUMNS = (
    "signal_id, decision_kind, outcome, decided_at, entered_at, resolved_at, realized_r, "
    "max_adverse_excursion_r, max_favorable_excursion_r, tp2_reached, candles_used, "
    "engine_version"
)


def _split_cursor(
    after: tuple[datetime, UUID] | None,
) -> tuple[datetime | None, UUID | None]:
    """Unpack a keyset cursor into its two bind params (both NULL == first page)."""
    if after is None:
        return None, None
    return after[0], after[1]


class DecisionRepository:
    """Critic v0 persistence: resolver / reviewer queues and report reads (Step 2.14).

    The queues are *derived from the data* -- an unresolved decision is simply
    a ``signals`` row with no ``decision_resolutions`` row -- so both layers are
    idempotent and restartable without any job table. Row mapping for the
    existing tables reuses the sibling repositories' ``_record_to_stored``
    mappers so JSONB / NUMERIC handling lives in exactly one place per table.

    Report reads are keyset-paginated on ``(timestamp, id)``: ``after`` is that
    tuple from the last row of the previous page (``None`` for the first page)
    and pages are ordered by the same tuple ascending, so a page never skips or
    repeats rows while new ones are being inserted.
    """

    def __init__(self, conn: asyncpg.Connection[Any]) -> None:
        self._conn = conn

    # ---- Layer 1: OutcomeResolver queue --------------------------------------

    async def list_unresolved_decisions(
        self, *, limit: int, decided_before: datetime
    ) -> list[StoredSignal]:
        """Oldest-first chunk of decisions that still need a price replay."""
        rows = await self._conn.fetch(
            f"""
            SELECT {_SIGNAL_COLUMNS_S}
            {_UNRESOLVED_FROM_WHERE}
            ORDER BY s.created_at ASC, s.id ASC
            LIMIT $2
            """,
            decided_before,
            limit,
        )
        return [SignalRepository._record_to_stored(r) for r in rows]

    async def count_unresolved_decisions(self, *, decided_before: datetime) -> int:
        n = await self._conn.fetchval(
            f"""
            SELECT count(*)
            {_UNRESOLVED_FROM_WHERE}
            """,
            decided_before,
        )
        return int(n) if n is not None else 0

    async def list_agent_runs_for_scan(self, scan_id: UUID) -> list[StoredAgentRun]:
        """Every agent run of one scan (== one decision), in execution order."""
        rows = await self._conn.fetch(
            f"""
            SELECT {_AGENT_RUN_COLUMNS}
            FROM agent_runs
            WHERE scan_id = $1
            ORDER BY created_at ASC
            """,
            scan_id,
        )
        return [AgentRunRepository._record_to_stored(r) for r in rows]

    async def save_resolution(self, resolution: Resolution) -> None:
        """Upsert on ``signal_id`` so re-running the resolver is idempotent."""
        await self._conn.execute(
            f"""
            INSERT INTO decision_resolutions
                ({_RESOLUTION_COLUMNS})
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (signal_id) DO UPDATE SET
                decision_kind = EXCLUDED.decision_kind,
                outcome = EXCLUDED.outcome,
                decided_at = EXCLUDED.decided_at,
                entered_at = EXCLUDED.entered_at,
                resolved_at = EXCLUDED.resolved_at,
                realized_r = EXCLUDED.realized_r,
                max_adverse_excursion_r = EXCLUDED.max_adverse_excursion_r,
                max_favorable_excursion_r = EXCLUDED.max_favorable_excursion_r,
                tp2_reached = EXCLUDED.tp2_reached,
                candles_used = EXCLUDED.candles_used,
                engine_version = EXCLUDED.engine_version,
                created_at = NOW()
            """,
            resolution.signal_id,
            resolution.decision_kind.value,
            resolution.outcome.value,
            resolution.decided_at,
            resolution.entered_at,
            resolution.resolved_at,
            resolution.realized_r,
            resolution.max_adverse_excursion_r,
            resolution.max_favorable_excursion_r,
            resolution.tp2_reached,
            resolution.candles_used,
            resolution.engine_version,
        )

    async def get_resolution(self, signal_id: UUID) -> Resolution | None:
        # Runtime import: ``src.agents.critic.models`` imports this package's
        # models (-> __init__ -> this module), so a top-level import would be a
        # cycle whenever the critic package loads first. Mirrors store.py.
        from src.agents.critic.models import Resolution

        record = await self._conn.fetchrow(
            f"""
            SELECT {_RESOLUTION_COLUMNS}
            FROM decision_resolutions
            WHERE signal_id = $1
            """,
            signal_id,
        )
        if record is None:
            return None
        return Resolution.model_validate(_record_to_dict(record))

    async def close_active_setup_for_signal(
        self, *, signal_id: UUID, status: ActiveSetupStatus, evaluation: Mapping[str, Any]
    ) -> None:
        """Close a signal's OPEN setup with the resolver's verdict; no-op if none is open.

        Guarding on ``status = 'OPEN'`` keeps the Forecaster's own terminal
        status (and evaluation) intact when it already closed the setup.
        """
        await self._conn.execute(
            """
            UPDATE active_setups
            SET status = $2,
                latest_evaluation = $3::jsonb,
                last_evaluated_at = NOW()
            WHERE signal_id = $1 AND status = 'OPEN'
            """,
            signal_id,
            status.value,
            _to_jsonb(dict(evaluation)),
        )

    # ---- Layer 2: DecisionReviewer queue -------------------------------------

    async def list_pending_reviews(
        self, *, limit: int, no_setup_before: datetime, retry_before: datetime
    ) -> list[StoredSignal]:
        """Oldest-first chunk of decisions lacking a successful Claude review."""
        rows = await self._conn.fetch(
            f"""
            SELECT * FROM (
            {_PENDING_REVIEWS_UNION}
            ) q
            ORDER BY q.created_at ASC, q.id ASC
            LIMIT $3
            """,
            no_setup_before,
            retry_before,
            limit,
        )
        return [SignalRepository._record_to_stored(r) for r in rows]

    async def count_pending_reviews(
        self, *, no_setup_before: datetime, retry_before: datetime
    ) -> int:
        n = await self._conn.fetchval(
            f"""
            SELECT count(*) FROM (
            {_PENDING_REVIEWS_UNION}
            ) q
            """,
            no_setup_before,
            retry_before,
        )
        return int(n) if n is not None else 0

    async def save_review(
        self,
        *,
        signal_id: UUID,
        decision_kind: str,
        review: Mapping[str, Any],
        model: str,
        token_usage: Mapping[str, Any],
        cost_usd: float | None,
        error: str | None,
    ) -> None:
        """Upsert a review; a conflict (retry) bumps ``attempts`` and refreshes ``created_at``.

        ``attempts`` is what caps retries in the pending-review predicate, so
        it must count *every* write, including the failed ones -- hence the
        increment lives in the upsert rather than in Python.
        """
        await self._conn.execute(
            """
            INSERT INTO decision_reviews
                (signal_id, decision_kind, review, model, token_usage, cost_usd, error, attempts)
            VALUES ($1, $2, $3::jsonb, $4, $5::jsonb, $6, $7, 1)
            ON CONFLICT (signal_id) DO UPDATE SET
                decision_kind = EXCLUDED.decision_kind,
                review = EXCLUDED.review,
                model = EXCLUDED.model,
                token_usage = EXCLUDED.token_usage,
                cost_usd = EXCLUDED.cost_usd,
                error = EXCLUDED.error,
                attempts = decision_reviews.attempts + 1,
                created_at = NOW()
            """,
            signal_id,
            decision_kind,
            _to_jsonb(dict(review)),
            model,
            _to_jsonb(dict(token_usage)),
            cost_usd,
            error,
        )

    # ---- Report reads (keyset-paginated) -------------------------------------

    async def list_decision_summaries(
        self,
        *,
        since: datetime,
        until: datetime,
        after: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[DecisionSummary]:
        """One light row per decision in ``[since, until)`` with its resolution/review."""
        after_ts, after_id = _split_cursor(after)
        rows = await self._conn.fetch(
            """
            SELECT s.id, s.scan_id, s.symbol, s.direction, s.status, s.created_at,
                   s.payload->>'violated_rule' AS violated_rule, s.outcome,
                   r.decision_kind, r.outcome AS resolution_outcome, r.realized_r, r.entered_at,
                   CASE WHEN v.error IS NULL THEN v.review END AS review,
                   v.error AS review_error
            FROM signals s
            LEFT JOIN decision_resolutions r ON r.signal_id = s.id
            LEFT JOIN decision_reviews v ON v.signal_id = s.id
            WHERE s.created_at >= $1 AND s.created_at < $2
              AND ($3::timestamptz IS NULL OR (s.created_at, s.id) > ($3, $4))
            ORDER BY s.created_at ASC, s.id ASC
            LIMIT $5
            """,
            since,
            until,
            after_ts,
            after_id,
            limit,
        )
        return [self._record_to_summary(r) for r in rows]

    async def list_scan_runs_between(
        self,
        *,
        since: datetime,
        until: datetime,
        after: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[StoredScanRun]:
        after_ts, after_id = _split_cursor(after)
        rows = await self._conn.fetch(
            f"""
            SELECT {_SCAN_RUN_COLUMNS}
            FROM scan_runs
            WHERE started_at >= $1 AND started_at < $2
              AND ($3::timestamptz IS NULL OR (started_at, id) > ($3, $4))
            ORDER BY started_at ASC, id ASC
            LIMIT $5
            """,
            since,
            until,
            after_ts,
            after_id,
            limit,
        )
        return [ScanRunRepository._record_to_stored(r) for r in rows]

    async def list_active_setups(
        self, *, after: tuple[datetime, UUID] | None, limit: int
    ) -> list[StoredActiveSetup]:
        """Every setup regardless of status (the exit gate counts closed ones too)."""
        after_ts, after_id = _split_cursor(after)
        rows = await self._conn.fetch(
            f"""
            SELECT {_ACTIVE_SETUP_COLUMNS}
            FROM active_setups
            WHERE ($1::timestamptz IS NULL OR (opened_at, id) > ($1, $2))
            ORDER BY opened_at ASC, id ASC
            LIMIT $3
            """,
            after_ts,
            after_id,
            limit,
        )
        return [ActiveSetupRepository._record_to_stored(r) for r in rows]

    async def save_critic_report(self, report: StoredCriticReport) -> None:
        await self._conn.execute(
            """
            INSERT INTO critic_reports
                (id, period_start, period_end, report, markdown, pr_url, created_at)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
            """,
            report.id,
            report.period_start,
            report.period_end,
            _to_jsonb(dict(report.report)),
            report.markdown,
            report.pr_url,
            report.created_at,
        )

    @staticmethod
    def _record_to_summary(record: asyncpg.Record) -> DecisionSummary:
        data = _record_to_dict(record)
        # The SQL CASE already NULLs `review` for errored rows; only parse real JSONB.
        data["review"] = (
            _parse_jsonb_field(data["review"]) if data.get("review") is not None else None
        )
        if data.get("realized_r") is not None:
            data["realized_r"] = float(data["realized_r"])
        return DecisionSummary.model_validate(data)
