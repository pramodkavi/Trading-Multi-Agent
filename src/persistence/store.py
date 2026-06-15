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
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID, uuid4

import asyncpg
from botocore.exceptions import ClientError

from src.common.models import (
    ActiveSetupStatus,
    AgentRole,
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
from src.persistence.repositories import (
    ActiveSetupRepository,
    AgentRunRepository,
    ScanRunRepository,
    SignalRepository,
    _assert_safe_feature_key,
    _parse_jsonb_field,
    _to_jsonb,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Mapping, Sequence
    from datetime import datetime

# A boto3 ``rds-data`` client. botocore ships no usable static types under
# --strict, so it is opaque here; the store's only contact surface is
# ``execute_statement`` (see the pyproject mypy override for boto3).
RdsDataClient = Any

logger = logging.getLogger(__name__)

# Aurora Serverless v2 scale-to-zero (min 0 ACU auto-pause, SPEC §2.2): the first
# RDS Data API call after the cluster idles raises DatabaseResumingException while
# it wakes (~20-30s). Without a retry the scheduled scan dies on its very first DB
# call (start_scan) every time -- the failure mode seen in production. Retry the
# transient resume error with linear backoff so a scan rides out the wake; the CD
# migration step retries for the same reason. Worst-case wait
# (5+10+15+20+25 = 75s) is well under the 10-minute Lambda timeout. Only the Data
# API backend needs this -- local asyncpg / Docker Postgres never auto-pauses.
_RESUME_RETRY_CODES: frozenset[str] = frozenset({"DatabaseResumingException"})
_RESUME_MAX_ATTEMPTS: int = 6
_RESUME_BACKOFF_SECONDS: float = 5.0


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

    async def set_signal_outcome(
        self,
        *,
        signal_id: UUID,
        outcome: SignalOutcome,
        outcome_metadata: Mapping[str, Any] | None = None,
    ) -> None: ...

    async def find_similar_signals(
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
        """Historian three-stage retrieval (SPEC FR-1.4): hard filters -> tag
        overlap -> L2 distance. Returns up to ``limit`` precedents, most-similar
        first, restricted to PUBLISHED rows with a known outcome."""
        ...

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

    async def open_active_setup(self, *, signal_id: UUID) -> UUID:
        """Open a tracked setup for a published signal (Step 2.8). Returns its id."""
        ...

    async def list_open_active_setups(self) -> list[StoredActiveSetup]:
        """All OPEN setups, oldest first -- the Forecaster's per-scan work queue."""
        ...

    async def get_active_setup(self, setup_id: UUID) -> StoredActiveSetup | None: ...

    async def update_active_setup(
        self,
        *,
        setup_id: UUID,
        status: ActiveSetupStatus,
        evaluation: Mapping[str, Any] | None = None,
        evaluated_at: datetime | None = None,
    ) -> None:
        """Record an evaluation + status on a setup (keeps OPEN, or closes it)."""
        ...

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
        """Run one statement off the event loop (boto3 is synchronous).

        Retries ``DatabaseResumingException`` -- Aurora Serverless v2 waking from
        its scale-to-zero auto-pause -- with linear backoff, so a scan that lands
        on a paused cluster rides out the ~20-30s resume instead of failing on its
        first DB call. Any other error (including a non-resume ``ClientError``)
        propagates immediately.
        """

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

        for attempt in range(1, _RESUME_MAX_ATTEMPTS + 1):
            try:
                return await asyncio.to_thread(_call)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in _RESUME_RETRY_CODES or attempt == _RESUME_MAX_ATTEMPTS:
                    raise
                delay = _RESUME_BACKOFF_SECONDS * attempt
                logger.warning(
                    "Aurora resuming (%s); retry %d/%d in %.0fs",
                    code,
                    attempt,
                    _RESUME_MAX_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
        # The loop returns on success or re-raises on the final attempt; this line
        # is unreachable but satisfies the type checker's return-path analysis.
        raise AssertionError("unreachable: _execute retry loop exited")  # pragma: no cover

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
        # RDS Data API does not support array parameters, so convert symbols to
        # a comma-separated string. Use string_to_array() to parse back to array
        # on the server side.
        symbols_str = ",".join(symbols) if symbols else None
        await self._execute(
            """
            INSERT INTO scan_runs
                (id, started_at, status, session, strategy, symbols)
            VALUES
                (:id::uuid, :started_at::timestamptz, :status, :session,
                 :strategy, CASE WHEN :symbols::text IS NOT NULL
                              THEN string_to_array(:symbols::text, ',')
                              ELSE NULL
                           END)
            """,
            [
                _str_param("id", str(scan_id)),
                _str_param("started_at", started_at.isoformat()),
                _str_param("status", "RUNNING"),
                _str_param("session", session),
                _str_param("strategy", strategy),
                _str_param("symbols", symbols_str),
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
            tags = list(payload.tags)
            features: dict[str, Any] = dict(payload.features)
        else:
            status = SignalStatus.SKIPPED
            direction = None
            tags = []
            features = {}

        # The Data API rejects array parameters, so tags (text[]) goes as a
        # comma-joined string and is rebuilt server-side with string_to_array --
        # the same workaround scan_runs.symbols uses. Tags are kebab-case and
        # never contain commas, so the join is unambiguous. None -> empty array.
        # The :tags::text cast in the SQL is REQUIRED: a skip sends tags as a
        # typeless NULL, and inside string_to_array()/IS NOT NULL Postgres has no
        # column context to infer the type, so an uncast NULL fails with
        # "could not determine data type of parameter $N" (42P18).
        tags_str = ",".join(tags) if tags else None

        await self._execute(
            """
            INSERT INTO signals
                (id, scan_id, symbol, strategy, direction, status, payload, tags, features)
            VALUES
                (:id::uuid, :scan_id::uuid, :symbol, :strategy, :direction,
                 :status, :payload::jsonb,
                 CASE WHEN :tags::text IS NOT NULL
                      THEN string_to_array(:tags::text, ',')
                      ELSE '{}'::text[]
                 END,
                 :features::jsonb)
            """,
            [
                _str_param("id", str(signal_id)),
                _str_param("scan_id", str(payload.scan_id)),
                _str_param("symbol", payload.symbol),
                _str_param("strategy", payload.strategy),
                _str_param("direction", direction.value if direction is not None else None),
                _str_param("status", status.value),
                _str_param("payload", _to_jsonb(payload.model_dump(mode="json"))),
                _str_param("tags", tags_str),
                _str_param("features", _to_jsonb(features)),
            ],
        )
        return signal_id

    async def get_signal(self, signal_id: UUID) -> StoredSignal | None:
        response = await self._execute(
            f"""
            SELECT id::text AS id, scan_id::text AS scan_id, symbol, strategy,
                   direction, status,
                   {_utc_iso("created_at", alias="created_at")},
                   payload::text AS payload,
                   tags, features::text AS features, outcome,
                   outcome_metadata::text AS outcome_metadata
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
                       payload::text AS payload,
                       tags, features::text AS features, outcome,
                       outcome_metadata::text AS outcome_metadata
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
                       payload::text AS payload,
                       tags, features::text AS features, outcome,
                       outcome_metadata::text AS outcome_metadata
                FROM signals
                WHERE symbol = :symbol
                ORDER BY created_at DESC
                LIMIT :limit
                """,
                [_str_param("symbol", symbol), _long_param("limit", capped)],
                with_metadata=True,
            )
        return [self._row_to_signal(row) for row in _parse_records(response)]

    async def set_signal_outcome(
        self,
        *,
        signal_id: UUID,
        outcome: SignalOutcome,
        outcome_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        await self._execute(
            """
            UPDATE signals
            SET outcome = :outcome, outcome_metadata = :outcome_metadata::jsonb
            WHERE id = :id::uuid
            """,
            [
                _str_param("outcome", outcome.value),
                _str_param(
                    "outcome_metadata",
                    _to_jsonb(dict(outcome_metadata)) if outcome_metadata is not None else None,
                ),
                _str_param("id", str(signal_id)),
            ],
        )

    async def find_similar_signals(
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
        """Historian three-stage retrieval over the Data API (SPEC FR-1.4)."""
        capped_limit = max(1, min(limit, 1000))
        capped_pool = max(capped_limit, min(tag_pool, 1000))

        params: list[dict[str, Any]] = [_str_param("direction", direction)]

        # --- stage 1: hard categorical filters ---
        where = [
            "s.status = 'PUBLISHED'",
            "s.outcome IS NOT NULL",
            "s.direction = :direction",
        ]
        if session is not None:
            where.append("r.session = :session")
            params.append(_str_param("session", session))
        if primary_poi_type is not None:
            where.append("s.features->>'primary_poi_type' = :primary_poi_type")
            params.append(_str_param("primary_poi_type", primary_poi_type))
        if exclude_signal_id is not None:
            where.append("s.id <> :exclude_id::uuid")
            params.append(_str_param("exclude_id", str(exclude_signal_id)))

        # --- stage 2: tag-overlap via PG array operators. The Data API rejects
        # array params, so tags cross the wire as a comma string and are rebuilt
        # with string_to_array (mirrors create_signal). None -> NULL -> 0 overlap.
        params.append(_str_param("qtags", ",".join(query_tags) if query_tags else None))
        overlap_expr = (
            "COALESCE(cardinality(ARRAY(SELECT unnest(s.tags) "
            "INTERSECT SELECT unnest(string_to_array(:qtags::text, ',')))), 0)"
        )

        # --- stage 3: L2 distance over the numeric feature vector ---
        l2_expr = self._l2_expr_named(l2_features, params)

        params.append(_long_param("tag_pool", capped_pool))
        params.append(_long_param("lim", capped_limit))

        sql = f"""
            WITH filtered AS (
                SELECT s.id::text AS id, s.scan_id::text AS scan_id, s.symbol, s.strategy,
                       s.direction, s.status,
                       {_utc_iso("s.created_at", alias="created_at")},
                       s.payload::text AS payload, s.tags, s.features::text AS features,
                       s.outcome, s.outcome_metadata::text AS outcome_metadata,
                       {overlap_expr} AS tag_overlap,
                       {l2_expr} AS l2_distance
                FROM signals s
                JOIN scan_runs r ON s.scan_id = r.id
                WHERE {" AND ".join(where)}
            ),
            tag_ranked AS (
                SELECT * FROM filtered
                ORDER BY tag_overlap DESC, l2_distance ASC
                LIMIT :tag_pool
            )
            SELECT * FROM tag_ranked
            ORDER BY l2_distance ASC, tag_overlap DESC
            LIMIT :lim
        """
        response = await self._execute(sql, params, with_metadata=True)
        return [self._row_to_signal(row) for row in _parse_records(response)]

    @staticmethod
    def _l2_expr_named(
        l2_features: Sequence[tuple[str, float]],
        params: list[dict[str, Any]],
    ) -> str:
        """Build the L2-distance SQL expression with named params (appends to ``params``)."""
        if not l2_features:
            return "0::double precision"
        terms: list[str] = []
        for index, (key, value) in enumerate(l2_features):
            _assert_safe_feature_key(key)
            name = f"l2_{index}"
            params.append(_double_param(name, value))
            terms.append(
                f"power(COALESCE((s.features->>'{key}')::double precision, :{name}) - :{name}, 2)"
            )
        return f"sqrt({' + '.join(terms)})"

    @staticmethod
    def _row_to_signal(row: dict[str, Any]) -> StoredSignal:
        # tags (text[]) already parsed to list[str] by _parse_field; features and
        # outcome_metadata come back as ::text JSONB strings.
        # find_similar adds ranking columns the StoredSignal model forbids; the
        # Historian recomputes them in Python, so drop them here.
        row.pop("tag_overlap", None)
        row.pop("l2_distance", None)
        row["payload"] = _parse_jsonb_field(row["payload"])
        row["features"] = _parse_jsonb_field(row["features"])
        if row.get("outcome_metadata") is not None:
            row["outcome_metadata"] = _parse_jsonb_field(row["outcome_metadata"])
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

    # ---- active_setups ----------------------------------------------------

    async def open_active_setup(self, *, signal_id: UUID) -> UUID:
        setup_id = uuid4()
        await self._execute(
            """
            INSERT INTO active_setups (id, signal_id, status)
            VALUES (:id::uuid, :signal_id::uuid, :status)
            """,
            [
                _str_param("id", str(setup_id)),
                _str_param("signal_id", str(signal_id)),
                _str_param("status", ActiveSetupStatus.OPEN.value),
            ],
        )
        return setup_id

    def _select_active_setups(self, where: str) -> str:
        return f"""
            SELECT id::text AS id, signal_id::text AS signal_id,
                   {_utc_iso("opened_at", alias="opened_at")},
                   status,
                   {_utc_iso("last_evaluated_at", alias="last_evaluated_at")},
                   latest_evaluation::text AS latest_evaluation
            FROM active_setups
            {where}
        """

    async def list_open_active_setups(self) -> list[StoredActiveSetup]:
        response = await self._execute(
            self._select_active_setups("WHERE status = :status ORDER BY opened_at ASC"),
            [_str_param("status", ActiveSetupStatus.OPEN.value)],
            with_metadata=True,
        )
        return [self._row_to_active_setup(row) for row in _parse_records(response)]

    async def get_active_setup(self, setup_id: UUID) -> StoredActiveSetup | None:
        response = await self._execute(
            self._select_active_setups("WHERE id = :id::uuid"),
            [_str_param("id", str(setup_id))],
            with_metadata=True,
        )
        rows = _parse_records(response)
        if not rows:
            return None
        return self._row_to_active_setup(rows[0])

    async def update_active_setup(
        self,
        *,
        setup_id: UUID,
        status: ActiveSetupStatus,
        evaluation: Mapping[str, Any] | None = None,
        evaluated_at: datetime | None = None,
    ) -> None:
        await self._execute(
            """
            UPDATE active_setups
            SET status = :status,
                latest_evaluation = COALESCE(:latest_evaluation::jsonb, latest_evaluation),
                last_evaluated_at = COALESCE(:evaluated_at::timestamptz, NOW())
            WHERE id = :id::uuid
            """,
            [
                _str_param("status", status.value),
                _str_param(
                    "latest_evaluation",
                    _to_jsonb(dict(evaluation)) if evaluation is not None else None,
                ),
                _str_param(
                    "evaluated_at",
                    evaluated_at.isoformat() if evaluated_at is not None else None,
                ),
                _str_param("id", str(setup_id)),
            ],
        )

    @staticmethod
    def _row_to_active_setup(row: dict[str, Any]) -> StoredActiveSetup:
        if row.get("latest_evaluation") is not None:
            row["latest_evaluation"] = _parse_jsonb_field(row["latest_evaluation"])
        return StoredActiveSetup.model_validate(row)

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
    """``SignalStore`` backed by an asyncpg connection **pool** (local dev / tests).

    Thin facade over the Step 1.9 repositories: it owns the pool's lifetime
    (``aclose`` closes it) and forwards each call to the matching repository
    method. The only adaptation is naming -- the repositories expose
    ``get_by_id`` / ``list_recent`` / ``log_run``; the store renames them to the
    backend-neutral ``get_scan_run`` / ``get_signal`` / ``list_recent_signals``
    / ``log_agent_run`` so both backends present one identical surface.

    Why a pool (Step 2.13): multi-symbol scans run the per-symbol pipelines
    concurrently (``scripts.run_scan._run_symbols``), and a *single* asyncpg
    connection cannot serve concurrent operations -- it raises "another operation
    is in progress". Each method therefore acquires a fresh connection from the
    pool for the duration of the call, so concurrent symbol tasks never share a
    connection. The cloud runtime uses the stateless Data API backend instead;
    this pool path is the local-dev / integration one.
    """

    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self._pool = pool

    @classmethod
    async def connect(
        cls, dsn: str, *, min_size: int = 1, max_size: int = 10
    ) -> AsyncpgSignalStore:
        """Open a connection pool to ``dsn`` and wrap it in a store.

        ``max_size`` (default 10) comfortably covers the 4-symbol watchlist run
        concurrently -- each symbol's pipeline holds at most one connection at a
        time (acquire-per-call), with headroom for the gate + historian overlap.
        ``min_size`` defaults to 1 so a test / single scan does not eagerly open
        ten sockets.
        """
        pool: asyncpg.Pool[Any] = await asyncpg.create_pool(
            dsn, min_size=min_size, max_size=max_size
        )
        return cls(pool)

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[asyncpg.Connection[Any]]:
        """Yield a pooled connection for one operation, then return it to the pool.

        ``pool.acquire()`` yields a ``PoolConnectionProxy`` that forwards every
        ``Connection`` method; the cast tells the type checker so (the proxy is
        not a ``Connection`` subclass in the stubs).
        """
        async with self._pool.acquire() as conn:
            yield cast("asyncpg.Connection[Any]", conn)

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
        async with self._acquire() as conn:
            await ScanRunRepository(conn).start_scan(
                scan_id=scan_id,
                started_at=started_at,
                session=session,
                strategy=strategy,
                symbols=symbols,
            )

    async def complete_scan(self, *, scan_id: UUID, completed_at: datetime) -> None:
        async with self._acquire() as conn:
            await ScanRunRepository(conn).complete_scan(scan_id=scan_id, completed_at=completed_at)

    async def fail_scan(self, *, scan_id: UUID, completed_at: datetime, error_message: str) -> None:
        async with self._acquire() as conn:
            await ScanRunRepository(conn).fail_scan(
                scan_id=scan_id,
                completed_at=completed_at,
                error_message=error_message,
            )

    async def get_scan_run(self, scan_id: UUID) -> StoredScanRun | None:
        async with self._acquire() as conn:
            return await ScanRunRepository(conn).get_by_id(scan_id)

    # ---- signals ----------------------------------------------------------

    async def create_signal(self, payload: SignalProposal | SkipDecision) -> UUID:
        async with self._acquire() as conn:
            return await SignalRepository(conn).create_signal(payload)

    async def get_signal(self, signal_id: UUID) -> StoredSignal | None:
        async with self._acquire() as conn:
            return await SignalRepository(conn).get_by_id(signal_id)

    async def list_recent_signals(
        self, *, limit: int = 50, symbol: str | None = None
    ) -> list[StoredSignal]:
        async with self._acquire() as conn:
            return await SignalRepository(conn).list_recent(limit=limit, symbol=symbol)

    async def set_signal_outcome(
        self,
        *,
        signal_id: UUID,
        outcome: SignalOutcome,
        outcome_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        async with self._acquire() as conn:
            await SignalRepository(conn).set_outcome(
                signal_id=signal_id,
                outcome=outcome,
                outcome_metadata=outcome_metadata,
            )

    async def find_similar_signals(
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
        async with self._acquire() as conn:
            return await SignalRepository(conn).find_similar(
                direction=direction,
                session=session,
                primary_poi_type=primary_poi_type,
                query_tags=query_tags,
                l2_features=l2_features,
                limit=limit,
                tag_pool=tag_pool,
                exclude_signal_id=exclude_signal_id,
            )

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
        async with self._acquire() as conn:
            return await AgentRunRepository(conn).log_run(
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
        async with self._acquire() as conn:
            return await AgentRunRepository(conn).get_by_id(run_id)

    # ---- active_setups ----------------------------------------------------

    async def open_active_setup(self, *, signal_id: UUID) -> UUID:
        async with self._acquire() as conn:
            return await ActiveSetupRepository(conn).open_setup(signal_id=signal_id)

    async def list_open_active_setups(self) -> list[StoredActiveSetup]:
        async with self._acquire() as conn:
            return await ActiveSetupRepository(conn).list_open()

    async def get_active_setup(self, setup_id: UUID) -> StoredActiveSetup | None:
        async with self._acquire() as conn:
            return await ActiveSetupRepository(conn).get_by_id(setup_id)

    async def update_active_setup(
        self,
        *,
        setup_id: UUID,
        status: ActiveSetupStatus,
        evaluation: Mapping[str, Any] | None = None,
        evaluated_at: datetime | None = None,
    ) -> None:
        async with self._acquire() as conn:
            await ActiveSetupRepository(conn).update_status(
                setup_id=setup_id,
                status=status,
                evaluation=evaluation,
                evaluated_at=evaluated_at,
            )

    # ---- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying connection pool (releases every connection)."""
        await self._pool.close()
