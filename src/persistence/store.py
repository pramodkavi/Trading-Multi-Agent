"""Signal-store abstraction with a cloud (RDS Data API) backend.

Step 1.17 (serverless pivot, SPEC §2.2 / §2.4): the agent pipeline now runs on
Lambda *outside* any VPC, so it cannot open a Postgres socket to Aurora. Instead
it talks to the cluster over the **RDS Data API** (HTTPS, IAM-authenticated). The
local Docker dev loop still uses asyncpg, so persistence is expressed once as a
`SignalStore` Protocol with two interchangeable backends:

- ``DataApiSignalStore`` (this module) -- boto3 ``rds-data`` for the cloud.
- ``AsyncpgSignalStore`` (Step 1.17 Part B) -- thin facade over the existing
  asyncpg repositories for local dev / integration tests.

Both expose the same nine async methods (mirroring ScanRunRepository /
SignalRepository / AgentRunRepository) plus ``aclose``. A ``create_store``
factory (Part B) selects the backend from settings.

Why the Data API surface looks the way it does
-----------------------------------------------
* **Typed parameters.** The Data API does not infer types from the SQL the way
  asyncpg's binary protocol does -- every parameter is a tagged JSON value
  (``{"stringValue": ...}`` / ``{"longValue": ...}`` / ``{"isNull": true}``).
  We send UUIDs, timestamps and JSONB all as ``stringValue`` and let an explicit
  SQL cast (``:id::uuid``, ``:payload::jsonb``) coerce them DB-side. This keeps
  the parameter builder tiny and the type contract visible in the SQL.
* **Timestamps.** A ``timestamptz`` read back through the Data API arrives as a
  bare string without an offset, which Pydantic would reject (our Stored* models
  require tz-aware datetimes). We therefore format every timestamp column with
  ``to_char(col AT TIME ZONE 'UTC', ...)`` so it comes back as an explicit
  offset-bearing ISO-8601 string -- unambiguous regardless of the API/session
  timezone.
* **Sync client, async surface.** ``boto3`` is synchronous; we wrap each call in
  ``asyncio.to_thread`` so the store stays awaitable and never blocks the loop.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

import asyncpg

from src.common.models import (
    AgentRole,
    SignalDirection,
    SignalProposal,
    SignalStatus,
    SkipDecision,
)
from src.persistence.models import StoredAgentRun, StoredScanRun, StoredSignal
from src.persistence.repositories import (
    AgentRunRepository,
    ScanRunRepository,
    SignalRepository,
    _parse_jsonb_field,
    _to_jsonb,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence
    from datetime import datetime

# A boto3 ``rds-data`` client. botocore ships no usable static types under
# --strict, so it is opaque here; the store's only contact surface is
# ``execute_statement`` (see the pyproject mypy override for boto3).
RdsDataClient = Any


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

# to_char template that renders a UTC wall-clock as an offset-bearing ISO-8601
# string, e.g. "2026-06-01T08:03:00.123456+00:00". The doubled-quoted "T" and
# "+00:00" are literal text in a to_char template.
_TS_FORMAT = 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'


def _utc_iso(column: str, *, alias: str) -> str:
    """SQL snippet selecting a ``timestamptz`` column as an ISO-8601 UTC string."""
    return f"to_char({column} AT TIME ZONE 'UTC', '{_TS_FORMAT}') AS {alias}"


# ---------------------------------------------------------------------------
# Data API parameter builders
# ---------------------------------------------------------------------------
#
# Each returns one ``{"name": ..., "value": {...}}`` entry for the Data API
# ``parameters`` list. ``None`` always maps to ``{"isNull": True}``.


def _str_param(name: str, value: str | None) -> dict[str, Any]:
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    return {"name": name, "value": {"stringValue": value}}


def _long_param(name: str, value: int | None) -> dict[str, Any]:
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    return {"name": name, "value": {"longValue": value}}


def _double_param(name: str, value: float | None) -> dict[str, Any]:
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    return {"name": name, "value": {"doubleValue": value}}


def _array_param(name: str, values: Sequence[str] | None) -> dict[str, Any]:
    """Build a string-array parameter for a ``text[]`` column."""
    if values is None:
        return {"name": name, "value": {"isNull": True}}
    return {"name": name, "value": {"arrayValue": {"stringValues": list(values)}}}


# ---------------------------------------------------------------------------
# Data API result parsing
# ---------------------------------------------------------------------------


def _parse_field(field: Mapping[str, Any]) -> Any:
    """Reduce one Data API typed field to a plain Python scalar / list / None.

    The API returns each cell as a single-key dict tagging its type. We only
    encounter the variants our schema produces (string / long / double /
    boolean / null / string-array); anything else is a contract surprise worth
    failing loudly on.
    """
    if field.get("isNull"):
        return None
    if "stringValue" in field:
        return field["stringValue"]
    if "longValue" in field:
        return field["longValue"]
    if "doubleValue" in field:
        return field["doubleValue"]
    if "booleanValue" in field:
        return field["booleanValue"]
    if "arrayValue" in field:
        array_value = field["arrayValue"]
        # Our only array columns are text[] (e.g. scan_runs.symbols).
        values = array_value.get("stringValues", [])
        return list(values)
    raise ValueError(f"unsupported Data API field shape: {sorted(field)}")


def _parse_records(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Zip ``columnMetadata`` names against ``records`` into a list of dicts.

    Requires the statement to have been executed with
    ``includeResultMetadata=True`` so column labels are present.
    """
    metadata = response.get("columnMetadata") or []
    columns = [col.get("label") or col["name"] for col in metadata]
    rows: list[dict[str, Any]] = []
    for record in response.get("records", []):
        rows.append({col: _parse_field(field) for col, field in zip(columns, record, strict=True)})
    return rows


# ---------------------------------------------------------------------------
# SignalStore protocol
# ---------------------------------------------------------------------------


class SignalStore(Protocol):
    """Backend-agnostic persistence surface used by the scan pipeline.

    Implemented by ``DataApiSignalStore`` (cloud) and ``AsyncpgSignalStore``
    (local, Part B). Method semantics mirror the asyncpg repositories so the
    runner code is identical regardless of backend.
    """

    async def start_scan(
        self,
        *,
        scan_id: UUID,
        started_at: datetime,
        session: str | None = None,
        strategy: str | None = None,
        symbols: list[str] | None = None,
    ) -> None: ...

    async def complete_scan(self, *, scan_id: UUID, completed_at: datetime) -> None: ...

    async def fail_scan(
        self, *, scan_id: UUID, completed_at: datetime, error_message: str
    ) -> None: ...

    async def get_scan_run(self, scan_id: UUID) -> StoredScanRun | None: ...

    async def create_signal(self, payload: SignalProposal | SkipDecision) -> UUID: ...

    async def get_signal(self, signal_id: UUID) -> StoredSignal | None: ...

    async def list_recent_signals(
        self, *, limit: int = 50, symbol: str | None = None
    ) -> list[StoredSignal]: ...

    async def log_agent_run(
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
    ) -> UUID: ...

    async def get_agent_run(self, run_id: UUID) -> StoredAgentRun | None: ...

    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# DataApiSignalStore
# ---------------------------------------------------------------------------


class DataApiSignalStore:
    """``SignalStore`` backed by Aurora's RDS Data API via boto3 ``rds-data``.

    The client is injected (not created here) so the unit tests can pass a
    mock and so the Lambda handler controls client lifetime/reuse across
    invocations. Use :meth:`from_arns` to build one with a real boto3 client.
    """

    def __init__(
        self,
        *,
        client: RdsDataClient,
        cluster_arn: str,
        secret_arn: str,
        database: str,
    ) -> None:
        self._client = client
        self._cluster_arn = cluster_arn
        self._secret_arn = secret_arn
        self._database = database

    @classmethod
    def from_arns(
        cls,
        *,
        cluster_arn: str,
        secret_arn: str,
        database: str,
        region_name: str | None = None,
    ) -> DataApiSignalStore:
        """Construct with a fresh boto3 ``rds-data`` client.

        ``boto3`` is imported lazily so importing this module (and the unit
        tests, which inject a mock) never depends on AWS credentials or region
        resolution.
        """
        import boto3

        client = boto3.client("rds-data", region_name=region_name)
        return cls(
            client=client,
            cluster_arn=cluster_arn,
            secret_arn=secret_arn,
            database=database,
        )

    # ---- low-level execute ------------------------------------------------

    async def _execute(
        self,
        sql: str,
        parameters: list[dict[str, Any]],
        *,
        with_metadata: bool = False,
    ) -> dict[str, Any]:
        """Run one statement off the event loop (boto3 is synchronous)."""

        def _call() -> dict[str, Any]:
            result: dict[str, Any] = self._client.execute_statement(
                resourceArn=self._cluster_arn,
                secretArn=self._secret_arn,
                database=self._database,
                sql=sql,
                parameters=parameters,
                includeResultMetadata=with_metadata,
            )
            return result

        return await asyncio.to_thread(_call)

    # ---- scan_runs --------------------------------------------------------

    async def start_scan(
        self,
        *,
        scan_id: UUID,
        started_at: datetime,
        session: str | None = None,
        strategy: str | None = None,
        symbols: list[str] | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO scan_runs
                (id, started_at, status, session, strategy, symbols)
            VALUES
                (:id::uuid, :started_at::timestamptz, :status, :session,
                 :strategy, :symbols)
            """,
            [
                _str_param("id", str(scan_id)),
                _str_param("started_at", started_at.isoformat()),
                _str_param("status", "RUNNING"),
                _str_param("session", session),
                _str_param("strategy", strategy),
                _array_param("symbols", symbols),
            ],
        )

    async def complete_scan(self, *, scan_id: UUID, completed_at: datetime) -> None:
        await self._execute(
            """
            UPDATE scan_runs
            SET status = :status, completed_at = :completed_at::timestamptz
            WHERE id = :id::uuid AND status = :running
            """,
            [
                _str_param("status", "SUCCESS"),
                _str_param("completed_at", completed_at.isoformat()),
                _str_param("id", str(scan_id)),
                _str_param("running", "RUNNING"),
            ],
        )

    async def fail_scan(self, *, scan_id: UUID, completed_at: datetime, error_message: str) -> None:
        await self._execute(
            """
            UPDATE scan_runs
            SET status = :status,
                completed_at = :completed_at::timestamptz,
                error_message = :error_message
            WHERE id = :id::uuid AND status = :running
            """,
            [
                _str_param("status", "FAILED"),
                _str_param("completed_at", completed_at.isoformat()),
                _str_param("error_message", error_message),
                _str_param("id", str(scan_id)),
                _str_param("running", "RUNNING"),
            ],
        )

    async def get_scan_run(self, scan_id: UUID) -> StoredScanRun | None:
        response = await self._execute(
            f"""
            SELECT id::text AS id,
                   {_utc_iso("started_at", alias="started_at")},
                   {_utc_iso("completed_at", alias="completed_at")},
                   status, error_message, session, strategy, symbols
            FROM scan_runs
            WHERE id = :id::uuid
            """,
            [_str_param("id", str(scan_id))],
            with_metadata=True,
        )
        rows = _parse_records(response)
        if not rows:
            return None
        return StoredScanRun.model_validate(rows[0])

    # ---- signals ----------------------------------------------------------

    async def create_signal(self, payload: SignalProposal | SkipDecision) -> UUID:
        signal_id = uuid4()
        if isinstance(payload, SignalProposal):
            status = SignalStatus.PUBLISHED
            direction: SignalDirection | None = payload.direction
        else:
            status = SignalStatus.SKIPPED
            direction = None

        await self._execute(
            """
            INSERT INTO signals
                (id, scan_id, symbol, strategy, direction, status, payload)
            VALUES
                (:id::uuid, :scan_id::uuid, :symbol, :strategy, :direction,
                 :status, :payload::jsonb)
            """,
            [
                _str_param("id", str(signal_id)),
                _str_param("scan_id", str(payload.scan_id)),
                _str_param("symbol", payload.symbol),
                _str_param("strategy", payload.strategy),
                _str_param("direction", direction.value if direction is not None else None),
                _str_param("status", status.value),
                _str_param("payload", _to_jsonb(payload.model_dump(mode="json"))),
            ],
        )
        return signal_id

    async def get_signal(self, signal_id: UUID) -> StoredSignal | None:
        response = await self._execute(
            f"""
            SELECT id::text AS id, scan_id::text AS scan_id, symbol, strategy,
                   direction, status,
                   {_utc_iso("created_at", alias="created_at")},
                   payload::text AS payload
            FROM signals
            WHERE id = :id::uuid
            """,
            [_str_param("id", str(signal_id))],
            with_metadata=True,
        )
        rows = _parse_records(response)
        if not rows:
            return None
        return self._row_to_signal(rows[0])

    async def list_recent_signals(
        self, *, limit: int = 50, symbol: str | None = None
    ) -> list[StoredSignal]:
        capped = max(1, min(limit, 1000))
        if symbol is None:
            response = await self._execute(
                f"""
                SELECT id::text AS id, scan_id::text AS scan_id, symbol, strategy,
                       direction, status,
                       {_utc_iso("created_at", alias="created_at")},
                       payload::text AS payload
                FROM signals
                ORDER BY created_at DESC
                LIMIT :limit
                """,
                [_long_param("limit", capped)],
                with_metadata=True,
            )
        else:
            response = await self._execute(
                f"""
                SELECT id::text AS id, scan_id::text AS scan_id, symbol, strategy,
                       direction, status,
                       {_utc_iso("created_at", alias="created_at")},
                       payload::text AS payload
                FROM signals
                WHERE symbol = :symbol
                ORDER BY created_at DESC
                LIMIT :limit
                """,
                [_str_param("symbol", symbol), _long_param("limit", capped)],
                with_metadata=True,
            )
        return [self._row_to_signal(row) for row in _parse_records(response)]

    @staticmethod
    def _row_to_signal(row: dict[str, Any]) -> StoredSignal:
        row["payload"] = _parse_jsonb_field(row["payload"])
        return StoredSignal.model_validate(row)

    # ---- agent_runs -------------------------------------------------------

    async def log_agent_run(
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
        run_id = uuid4()
        await self._execute(
            """
            INSERT INTO agent_runs
                (id, scan_id, agent_role, strategy, input_hash, output,
                 latency_ms, token_usage, cost_usd, created_at)
            VALUES
                (:id::uuid, :scan_id::uuid, :agent_role, :strategy, :input_hash,
                 :output::jsonb, :latency_ms, :token_usage::jsonb, :cost_usd,
                 COALESCE(:created_at::timestamptz, NOW()))
            """,
            [
                _str_param("id", str(run_id)),
                _str_param("scan_id", str(scan_id)),
                _str_param("agent_role", agent_role.value),
                _str_param("strategy", strategy),
                _str_param("input_hash", input_hash),
                _str_param("output", _to_jsonb(dict(output))),
                _long_param("latency_ms", latency_ms),
                _str_param(
                    "token_usage",
                    _to_jsonb(dict(token_usage) if token_usage is not None else {}),
                ),
                _double_param("cost_usd", cost_usd),
                _str_param(
                    "created_at",
                    created_at.isoformat() if created_at is not None else None,
                ),
            ],
        )
        return run_id

    async def get_agent_run(self, run_id: UUID) -> StoredAgentRun | None:
        response = await self._execute(
            f"""
            SELECT id::text AS id, scan_id::text AS scan_id, agent_role, strategy,
                   input_hash, output::text AS output, latency_ms,
                   token_usage::text AS token_usage,
                   cost_usd::double precision AS cost_usd,
                   {_utc_iso("created_at", alias="created_at")}
            FROM agent_runs
            WHERE id = :id::uuid
            """,
            [_str_param("id", str(run_id))],
            with_metadata=True,
        )
        rows = _parse_records(response)
        if not rows:
            return None
        row = rows[0]
        row["output"] = _parse_jsonb_field(row["output"])
        row["token_usage"] = _parse_jsonb_field(row["token_usage"])
        return StoredAgentRun.model_validate(row)

    # ---- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """No-op: the Data API is stateless (no pool/socket to release).

        Present so callers can treat every ``SignalStore`` uniformly with
        ``async with``-style teardown regardless of backend.
        """
        return None


# ---------------------------------------------------------------------------
# AsyncpgSignalStore
# ---------------------------------------------------------------------------


class AsyncpgSignalStore:
    """``SignalStore`` backed by a single asyncpg connection (local dev / tests).

    Thin facade over the Step 1.9 repositories: it owns the connection's
    lifetime (``aclose`` closes it) and forwards each call to the matching
    repository method. The only adaptation is naming -- the repositories expose
    ``get_by_id`` / ``list_recent`` / ``log_run``; the store renames them to the
    backend-neutral ``get_scan_run`` / ``get_signal`` / ``list_recent_signals``
    / ``log_agent_run`` so both backends present one identical surface.

    Scope (Slice 1): one connection, no pool. The pool-aware variant arrives in
    Slice 2 Step 2.13 with multi-symbol parallelism; the ``SignalStore``
    interface stays the same, so callers are unaffected.
    """

    def __init__(self, conn: asyncpg.Connection[Any]) -> None:
        self._conn = conn
        self._scans = ScanRunRepository(conn)
        self._signals = SignalRepository(conn)
        self._agent_runs = AgentRunRepository(conn)

    @classmethod
    async def connect(cls, dsn: str) -> AsyncpgSignalStore:
        """Open a connection to ``dsn`` and wrap it in a store."""
        conn: asyncpg.Connection[Any] = await asyncpg.connect(dsn)
        return cls(conn)

    # ---- scan_runs --------------------------------------------------------

    async def start_scan(
        self,
        *,
        scan_id: UUID,
        started_at: datetime,
        session: str | None = None,
        strategy: str | None = None,
        symbols: list[str] | None = None,
    ) -> None:
        await self._scans.start_scan(
            scan_id=scan_id,
            started_at=started_at,
            session=session,
            strategy=strategy,
            symbols=symbols,
        )

    async def complete_scan(self, *, scan_id: UUID, completed_at: datetime) -> None:
        await self._scans.complete_scan(scan_id=scan_id, completed_at=completed_at)

    async def fail_scan(self, *, scan_id: UUID, completed_at: datetime, error_message: str) -> None:
        await self._scans.fail_scan(
            scan_id=scan_id,
            completed_at=completed_at,
            error_message=error_message,
        )

    async def get_scan_run(self, scan_id: UUID) -> StoredScanRun | None:
        return await self._scans.get_by_id(scan_id)

    # ---- signals ----------------------------------------------------------

    async def create_signal(self, payload: SignalProposal | SkipDecision) -> UUID:
        return await self._signals.create_signal(payload)

    async def get_signal(self, signal_id: UUID) -> StoredSignal | None:
        return await self._signals.get_by_id(signal_id)

    async def list_recent_signals(
        self, *, limit: int = 50, symbol: str | None = None
    ) -> list[StoredSignal]:
        return await self._signals.list_recent(limit=limit, symbol=symbol)

    # ---- agent_runs -------------------------------------------------------

    async def log_agent_run(
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
        return await self._agent_runs.log_run(
            scan_id=scan_id,
            agent_role=agent_role,
            strategy=strategy,
            input_hash=input_hash,
            output=output,
            latency_ms=latency_ms,
            token_usage=token_usage,
            cost_usd=cost_usd,
            created_at=created_at,
        )

    async def get_agent_run(self, run_id: UUID) -> StoredAgentRun | None:
        return await self._agent_runs.get_by_id(run_id)

    # ---- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying connection."""
        await self._conn.close()
