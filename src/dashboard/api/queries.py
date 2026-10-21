"""Read-only SQL for the dashboard API (design §5.1, §3.4, §6.2; FR-7.4).

Every function here takes a :class:`DashboardReader` -- anything with the
``read_rows(sql, parameters)`` seam ``DataApiSignalStore`` exposes -- and returns
frozen models defined in this module. The routers (Task 4) map them to API
schemas; nothing here knows about HTTP.

Rules every statement follows (plan Global Constraints, PROJECT_STATE §5):

* **Every value is bound by name** through the ``store.py`` parameter builders.
  Only fixed SQL fragments are ever concatenated; no filter value, id or
  timestamp is formatted into the SQL text.
* RDS Data API quirks: timestamps out via ``_utc_iso`` (ISO-8601 with offset),
  in as ISO strings with a ``::timestamptz`` cast; JSONB out as ``::text`` and
  parsed with ``_parse_jsonb_field``; NULL-able parameters carry an explicit
  ``::type`` cast; **no array parameters** -- a uuid list travels as one
  comma-joined string and is rebuilt with ``string_to_array(...)::uuid[]``.
* The journal pages by keyset on ``(created_at DESC, id DESC)`` and fetches
  ``limit + 1`` rows so the caller can tell whether a next page exists.
* Decision kind is derived in SQL by :data:`DECISION_KIND_SQL` (design §5.1), so
  the ``decision_kind`` filter is a server-side predicate, not a post-filter.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.common.models import ActiveSetupStatus, SignalDirection, SignalOutcome
from src.dashboard.api.classify import DECISION_KIND_SQL, DecisionKind
from src.persistence.models import StoredActiveSetup, StoredReview, StoredScanRun, StoredSignal
from src.persistence.repositories import _parse_jsonb_field
from src.persistence.store import (
    DataApiSignalStore,
    _cursor_params,
    _long_param,
    _str_param,
    _utc_iso,
    signal_projection,
)

__all__ = [
    "DashboardReader",
    "JournalRow",
    "SetupCard",
    "SignalFilters",
    "count_evaluations_since",
    "get_review",
    "get_setup_for_signal",
    "list_open_setup_cards",
    "list_scan_runs_since",
    "list_signals_for_scans",
    "list_signals_page",
]


class DashboardReader(Protocol):
    """The one read seam the dashboard uses (``DataApiSignalStore.read_rows``)."""

    async def read_rows(
        self, sql: str, parameters: list[dict[str, Any]]
    ) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Result models (frozen; the routers map them to API schemas)
# ---------------------------------------------------------------------------


class SignalFilters(BaseModel):
    """Optional journal filters; each present field adds one bound predicate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    since: datetime | None = None
    until: datetime | None = None
    symbol: str | None = None
    decision_kind: DecisionKind | None = None
    outcome: SignalOutcome | None = None
    tag: str | None = None
    session: str | None = None
    violated_rule: str | None = None


class JournalRow(BaseModel):
    """One journal line: the stored signal plus the columns derived around it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal: StoredSignal
    decision_kind: DecisionKind = Field(description="Derived in SQL by DECISION_KIND_SQL.")
    judge_ruling: str | None = Field(description="Latest Judge run's ruling for the scan.")
    session: str | None = Field(description="scan_runs.session (never from the payload).")
    has_setup: bool = Field(description="An active_setups row exists for the signal.")
    violated_rule: str | None = Field(description="payload->>'violated_rule' for gate skips.")
    resolution_outcome: str | None = Field(description="decision_resolutions.outcome.")
    realized_r: float | None = Field(description="decision_resolutions.realized_r.")


class SetupCard(BaseModel):
    """An OPEN setup with the prices the UI renders; ages are the router's job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    setup_id: UUID
    signal_id: UUID
    symbol: str
    direction: SignalDirection
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    opened_at: datetime
    last_evaluated_at: datetime | None
    latest_evaluation: dict[str, Any] | None


# ---------------------------------------------------------------------------
# Journal projection (shared by the page and the per-scan listing)
# ---------------------------------------------------------------------------

# Columns of the journal projection that are *not* ``signals`` columns; popped
# off the row before it is handed to ``row_to_signal`` (which forbids extras).
_JOURNAL_EXTRA_COLUMNS: tuple[str, ...] = (
    "decision_kind",
    "judge_ruling",
    "session",
    "has_setup",
    "violated_rule",
    "resolution_outcome",
    "realized_r",
)

_JOURNAL_SELECT = f"""SELECT {signal_projection("s")},
       ({DECISION_KIND_SQL}) AS decision_kind,
       j.ruling AS judge_ruling,
       r.session AS session,
       (t.id IS NOT NULL) AS has_setup,
       s.payload->>'violated_rule' AS violated_rule,
       d.outcome AS resolution_outcome,
       d.realized_r::double precision AS realized_r"""

# ``j`` is the latest Judge run sharing the signal's scan (one scan_runs row per
# symbol, design §5.1); ``t`` / ``d`` are the setup and the Critic's replay.
_JOURNAL_FROM = """FROM signals s
JOIN scan_runs r ON r.id = s.scan_id
LEFT JOIN LATERAL (
    SELECT a.output->>'ruling' AS ruling
    FROM agent_runs a
    WHERE a.scan_id = s.scan_id AND a.agent_role = 'judge'
    ORDER BY a.created_at DESC
    LIMIT 1
) j ON TRUE
LEFT JOIN active_setups t ON t.signal_id = s.id
LEFT JOIN decision_resolutions d ON d.signal_id = s.id"""

_JOURNAL_ORDER = "ORDER BY s.created_at DESC, s.id DESC"

_KEYSET_PREDICATE = (
    "(:after_ts::timestamptz IS NULL OR "
    "(s.created_at, s.id) < (:after_ts::timestamptz, :after_id::uuid))"
)


def _row_to_journal(row: dict[str, Any]) -> JournalRow:
    """Split a journal row into its ``signals`` part and the derived columns."""
    data = dict(row)  # row_to_signal mutates in place; never touch the reader's dict
    extras = {name: data.pop(name) for name in _JOURNAL_EXTRA_COLUMNS}
    signal = DataApiSignalStore.row_to_signal(data)
    return JournalRow.model_validate({"signal": signal, **extras})


def _filter_predicates(filters: SignalFilters) -> tuple[list[str], list[dict[str, Any]]]:
    """Predicates + parameters for the filters that are set; nothing for the rest."""
    predicates: list[str] = []
    params: list[dict[str, Any]] = []
    if filters.since is not None:
        predicates.append("s.created_at >= :since::timestamptz")
        params.append(_str_param("since", filters.since.isoformat()))
    if filters.until is not None:
        predicates.append("s.created_at < :until::timestamptz")
        params.append(_str_param("until", filters.until.isoformat()))
    if filters.symbol is not None:
        predicates.append("s.symbol = :symbol")
        params.append(_str_param("symbol", filters.symbol))
    if filters.decision_kind is not None:
        predicates.append(f"({DECISION_KIND_SQL}) = :decision_kind")
        params.append(_str_param("decision_kind", filters.decision_kind.value))
    if filters.outcome is not None:
        predicates.append("s.outcome = :outcome")
        params.append(_str_param("outcome", filters.outcome.value))
    if filters.tag is not None:
        predicates.append(":tag = ANY(s.tags)")
        params.append(_str_param("tag", filters.tag))
    if filters.session is not None:
        predicates.append("r.session = :session")
        params.append(_str_param("session", filters.session))
    if filters.violated_rule is not None:
        predicates.append("s.payload->>'violated_rule' = :violated_rule")
        params.append(_str_param("violated_rule", filters.violated_rule))
    return predicates, params


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


async def list_signals_page(
    reader: DashboardReader,
    *,
    filters: SignalFilters,
    limit: int,
    after: tuple[datetime, UUID] | None,
) -> list[JournalRow]:
    """One journal page, newest first; returns up to ``limit + 1`` rows.

    The extra row tells the caller a next page exists (it trims to ``limit`` and
    builds the cursor from row ``limit``'s ``(created_at, id)``). ``after`` is
    the decoded cursor of the previous page or ``None`` for the first page.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    predicates, params = _filter_predicates(filters)
    where = " AND ".join([_KEYSET_PREDICATE, *predicates])
    sql = f"""{_JOURNAL_SELECT}
{_JOURNAL_FROM}
WHERE {where}
{_JOURNAL_ORDER}
LIMIT :limit"""
    rows = await reader.read_rows(
        sql, [*_cursor_params(after), *params, _long_param("limit", limit + 1)]
    )
    return [_row_to_journal(row) for row in rows]


async def list_signals_for_scans(
    reader: DashboardReader, *, scan_ids: Sequence[UUID]
) -> list[JournalRow]:
    """Every journal row for the given scans (the overview's per-window chips)."""
    if not scan_ids:
        return []
    # Data API has no array parameters: one comma-joined string, split server-side.
    sql = f"""{_JOURNAL_SELECT}
{_JOURNAL_FROM}
WHERE s.scan_id = ANY(string_to_array(:ids, ',')::uuid[])
{_JOURNAL_ORDER}"""
    rows = await reader.read_rows(
        sql, [_str_param("ids", ",".join(str(scan_id) for scan_id in scan_ids))]
    )
    return [_row_to_journal(row) for row in rows]


# ---------------------------------------------------------------------------
# Setups
# ---------------------------------------------------------------------------


async def list_open_setup_cards(reader: DashboardReader) -> list[SetupCard]:
    """OPEN setups newest first, with the proposal prices lifted out of the payload."""
    sql = f"""SELECT t.id::text AS setup_id, s.id::text AS signal_id, s.symbol, s.direction,
       (s.payload->>'entry_price')::double precision AS entry_price,
       (s.payload->>'stop_loss')::double precision AS stop_loss,
       (s.payload->>'take_profit_1')::double precision AS take_profit_1,
       (s.payload->>'take_profit_2')::double precision AS take_profit_2,
       {_utc_iso("t.opened_at", alias="opened_at")},
       {_utc_iso("t.last_evaluated_at", alias="last_evaluated_at")},
       t.latest_evaluation::text AS latest_evaluation
FROM active_setups t
JOIN signals s ON s.id = t.signal_id
WHERE t.status = :status
ORDER BY t.opened_at DESC, t.id DESC"""
    rows = await reader.read_rows(sql, [_str_param("status", ActiveSetupStatus.OPEN.value)])
    return [SetupCard.model_validate(_parse_latest_evaluation(row)) for row in rows]


async def get_setup_for_signal(
    reader: DashboardReader, signal_id: UUID
) -> StoredActiveSetup | None:
    """The signal's setup (latest if several), or ``None`` when it was never published."""
    sql = f"""SELECT id::text AS id, signal_id::text AS signal_id,
       {_utc_iso("opened_at", alias="opened_at")},
       status,
       {_utc_iso("last_evaluated_at", alias="last_evaluated_at")},
       latest_evaluation::text AS latest_evaluation
FROM active_setups
WHERE signal_id = :signal_id::uuid
ORDER BY opened_at DESC, id DESC
LIMIT 1"""
    rows = await reader.read_rows(sql, [_str_param("signal_id", str(signal_id))])
    if not rows:
        return None
    return StoredActiveSetup.model_validate(_parse_latest_evaluation(rows[0]))


def _parse_latest_evaluation(row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    if data.get("latest_evaluation") is not None:
        data["latest_evaluation"] = _parse_jsonb_field(data["latest_evaluation"])
    return data


# ---------------------------------------------------------------------------
# Scans, reviews, Forecaster coverage
# ---------------------------------------------------------------------------


async def list_scan_runs_since(reader: DashboardReader, *, since: datetime) -> list[StoredScanRun]:
    """Scan runs started at or after ``since``, oldest first (the day's windows)."""
    sql = f"""SELECT id::text AS id,
       {_utc_iso("started_at", alias="started_at")},
       {_utc_iso("completed_at", alias="completed_at")},
       status, error_message, session, strategy, symbols
FROM scan_runs
WHERE started_at >= :since::timestamptz
ORDER BY started_at ASC, id ASC"""
    rows = await reader.read_rows(sql, [_str_param("since", since.isoformat())])
    return [StoredScanRun.model_validate(row) for row in rows]


async def get_review(reader: DashboardReader, signal_id: UUID) -> StoredReview | None:
    """The Critic's review row for the signal, or ``None`` when not reviewed yet."""
    sql = f"""SELECT signal_id::text AS signal_id, decision_kind,
       review::text AS review, model, token_usage::text AS token_usage,
       cost_usd::double precision AS cost_usd, error, attempts,
       {_utc_iso("created_at", alias="created_at")}
FROM decision_reviews
WHERE signal_id = :signal_id::uuid"""
    rows = await reader.read_rows(sql, [_str_param("signal_id", str(signal_id))])
    if not rows:
        return None
    data = dict(rows[0])
    data["review"] = _parse_jsonb_field(data["review"])
    data["token_usage"] = _parse_jsonb_field(data["token_usage"])
    return StoredReview.model_validate(data)


async def count_evaluations_since(reader: DashboardReader, *, since: datetime) -> tuple[int, int]:
    """``(total, errors)`` Forecaster evaluations since ``since`` (health strip)."""
    sql = """SELECT count(*) AS total,
       count(*) FILTER (WHERE status = 'ERROR') AS errors
FROM setup_evaluations
WHERE evaluated_at >= :since::timestamptz"""
    rows = await reader.read_rows(sql, [_str_param("since", since.isoformat())])
    if not rows:
        return 0, 0
    row = rows[0]
    return int(row.get("total") or 0), int(row.get("errors") or 0)
