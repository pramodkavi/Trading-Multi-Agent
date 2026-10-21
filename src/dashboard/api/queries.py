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
* **Journal rows are trimmed** (design §3.4): the Data API caps a response at
  1 MB and ``confluence_narrative`` alone may be 4 000 chars, so a 201-row page
  of full rows could not be served. The projection never selects ``payload``,
  ``features`` or ``outcome_metadata`` whole; it lifts the analyzer summary out
  of the payload in SQL (:data:`_JOURNAL_COLUMNS`). The full ``StoredSignal``
  travels only on the lifecycle, via ``store.get_signal``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date as date_type
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.common.models import ActiveSetupStatus, SignalDirection, SignalOutcome, SignalStatus
from src.dashboard.api.classify import DECISION_KIND_SQL, DecisionKind
from src.persistence.models import (
    StoredActiveSetup,
    StoredCriticReport,
    StoredReview,
    StoredScanRun,
)
from src.persistence.repositories import _parse_jsonb_field
from src.persistence.store import _cursor_params, _long_param, _str_param, _utc_iso

__all__ = [
    "ASC_KEYSET_ORDER",
    "ASC_KEYSET_PREDICATE",
    "PAGE_LIMIT_SQL",
    "ConfigFacts",
    "CriticReportSummary",
    "DashboardReader",
    "DecisionRow",
    "EvaluationStamp",
    "JournalRow",
    "OutcomeRow",
    "ReviewSummary",
    "SetupCard",
    "SignalFilters",
    "UsageDay",
    "agent_usage_by_day",
    "config_inference",
    "count_evaluations_since",
    "get_critic_report",
    "get_review",
    "get_setup_for_signal",
    "list_critic_reports",
    "list_decisions_between",
    "list_evaluation_stamps",
    "list_open_setup_cards",
    "list_outcome_rows",
    "list_recent_scan_runs",
    "list_reviews_page",
    "list_scan_runs_since",
    "list_signals_for_scans",
    "list_signals_page",
    "read_all_pages",
]

logger = logging.getLogger(__name__)


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
    """One journal line: the trimmed ``signals`` row plus the columns derived around it.

    Deliberately **not** a ``StoredSignal`` (design §3.4): no ``payload`` /
    ``features`` / ``outcome_metadata``. The analyzer summary (prices, R:R for a
    proposal; reason / details for a skip) is lifted out of the payload in SQL.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # -- signals columns (trimmed) -------------------------------------------
    id: UUID
    scan_id: UUID
    symbol: str
    strategy: str
    direction: SignalDirection | None
    status: SignalStatus
    created_at: datetime
    tags: list[str]
    outcome: SignalOutcome | None
    # -- analyzer summary, lifted from the payload in SQL -------------------
    entry_price: float | None = Field(description="payload->>'entry_price' (proposals).")
    stop_loss: float | None
    take_profit_1: float | None
    take_profit_2: float | None
    risk_reward_ratio: float | None
    skip_reason: str | None = Field(description="payload->>'reason' (skips).")
    skip_details: str | None = Field(description="left(payload->>'details', 200) (skips).")
    # -- derived (design §5.1) ---------------------------------------------
    decision_kind: DecisionKind = Field(description="Derived in SQL by DECISION_KIND_SQL.")
    judge_ruling: str | None = Field(description="Latest Judge run's ruling for the scan.")
    session: str | None = Field(description="scan_runs.session (never from the payload).")
    has_setup: bool = Field(description="An active_setups row exists for the signal.")
    violated_rule: str | None = Field(description="payload->>'violated_rule' for gate skips.")
    resolution_outcome: str | None = Field(description="decision_resolutions.outcome.")
    realized_r: float | None = Field(description="decision_resolutions.realized_r.")


class DecisionRow(BaseModel):
    """One decision reduced to what the funnel counts (design §4.2 P4).

    Minimal on purpose (Data API 1 MB cap): no payload, no analyzer summary --
    a year of rows must fit one response.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    created_at: datetime
    symbol: str
    scan_started_at: datetime = Field(description="scan_runs.started_at of the signal's scan.")
    decision_kind: DecisionKind
    judge_ruling: str | None
    violated_rule: str | None
    has_setup: bool


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

# The trimmed ``signals`` projection (design §3.4): never the whole ``payload`` /
# ``features`` / ``outcome_metadata`` JSONB -- only the analyzer summary, cast in
# SQL. The full row is ``store.signal_projection`` (lifecycle via ``get_signal``).
_JOURNAL_COLUMNS = f"""s.id::text AS id, s.scan_id::text AS scan_id, s.symbol, s.strategy,
       s.direction, s.status,
       {_utc_iso("s.created_at", alias="created_at")},
       s.tags, s.outcome,
       (s.payload->>'entry_price')::double precision AS entry_price,
       (s.payload->>'stop_loss')::double precision AS stop_loss,
       (s.payload->>'take_profit_1')::double precision AS take_profit_1,
       (s.payload->>'take_profit_2')::double precision AS take_profit_2,
       (s.payload->>'risk_reward_ratio')::double precision AS risk_reward_ratio,
       s.payload->>'reason' AS skip_reason,
       left(s.payload->>'details', 200) AS skip_details"""

# ``has_setup`` is an EXISTS probe rather than a LEFT JOIN: a signal with more than
# one ``active_setups`` row (re-opened after a console edit, a replayed setup) must
# never fan out into duplicate journal lines or a wrong page length.
_HAS_SETUP_SQL = "EXISTS (SELECT 1 FROM active_setups t WHERE t.signal_id = s.id) AS has_setup"

_JOURNAL_SELECT = f"""SELECT {_JOURNAL_COLUMNS},
       ({DECISION_KIND_SQL}) AS decision_kind,
       j.ruling AS judge_ruling,
       r.session AS session,
       {_HAS_SETUP_SQL},
       s.payload->>'violated_rule' AS violated_rule,
       d.outcome AS resolution_outcome,
       d.realized_r::double precision AS realized_r"""

# ``j`` is the latest Judge run sharing the signal's scan (one scan_runs row per
# symbol, design §5.1). Shared by the journal and the funnel.
_SIGNAL_SCAN_JUDGE_FROM = """FROM signals s
JOIN scan_runs r ON r.id = s.scan_id
LEFT JOIN LATERAL (
    SELECT a.output->>'ruling' AS ruling
    FROM agent_runs a
    WHERE a.scan_id = s.scan_id AND a.agent_role = 'judge'
    ORDER BY a.created_at DESC
    LIMIT 1
) j ON TRUE"""

# ``d`` is the Critic's replay (one row per signal, PK).
_JOURNAL_FROM = f"""{_SIGNAL_SCAN_JUDGE_FROM}
LEFT JOIN decision_resolutions d ON d.signal_id = s.id"""

_JOURNAL_ORDER = "ORDER BY s.created_at DESC, s.id DESC"

_KEYSET_PREDICATE = (
    "(:after_ts::timestamptz IS NULL OR "
    "(s.created_at, s.id) < (:after_ts::timestamptz, :after_id::uuid))"
)


def _row_to_journal(row: dict[str, Any]) -> JournalRow:
    """A journal projection row (every column already a plain value) -> ``JournalRow``."""
    return JournalRow.model_validate(row)


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
# Funnel (P4)
# ---------------------------------------------------------------------------

# Ascending keyset over ``(s.created_at, s.id)`` for :func:`read_all_pages`; both
# cursor params NULL on the first page (``store._cursor_params(None)``).
ASC_KEYSET_PREDICATE = (
    "(:after_ts::timestamptz IS NULL OR "
    "(s.created_at, s.id) > (:after_ts::timestamptz, :after_id::uuid))"
)
PAGE_LIMIT_SQL = "LIMIT :page_limit"
ASC_KEYSET_ORDER = "ORDER BY s.created_at ASC, s.id ASC"


async def read_all_pages(
    reader: DashboardReader,
    sql_with_keyset: str,
    params: list[dict[str, Any]],
    *,
    page_size: int = 1000,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """Every row of a range statement, fetched in keyset pages (Data API 1 MB cap).

    A 365-day aggregate can exceed the Data API's 1 MB response, so the range
    readers (the funnel's :func:`list_decisions_between`, Task 3's
    ``list_outcome_rows``) page through it instead of reading it in one call.
    ``sql_with_keyset`` must contain :data:`ASC_KEYSET_PREDICATE` in its
    ``WHERE``, end with :data:`ASC_KEYSET_ORDER` + :data:`PAGE_LIMIT_SQL` (a
    missing fragment is ``ValueError`` before any call), and project ``id`` and ``created_at`` (the
    ``_utc_iso`` string). The cursor advances from the last row of each page
    until a page returns fewer than ``page_size`` rows. More than
    ``max_pages * page_size`` rows raises ``RuntimeError`` rather than looping on.
    """
    missing = [
        fragment
        for fragment in (ASC_KEYSET_PREDICATE, ASC_KEYSET_ORDER, PAGE_LIMIT_SQL)
        if fragment not in sql_with_keyset
    ]
    if missing:
        raise ValueError(f"paged statement lacks the ascending keyset fragment(s): {missing}")
    if page_size < 1 or max_pages < 1:
        raise ValueError("page_size and max_pages must be >= 1")
    rows: list[dict[str, Any]] = []
    after: tuple[datetime, UUID] | None = None
    for _ in range(max_pages):
        page = await reader.read_rows(
            sql_with_keyset,
            [*params, *_cursor_params(after), _long_param("page_limit", page_size)],
        )
        rows.extend(page)
        if len(page) < page_size:
            return rows
        last = page[-1]
        after = (datetime.fromisoformat(str(last["created_at"])), UUID(str(last["id"])))
    # Unreachable at today's volume (~7k rows/yr vs the 20k cap); a 500 by design until mapped.
    raise RuntimeError(f"result too large: more than {max_pages * page_size} rows")


# The funnel projection: eight short columns per decision, nothing from the payload
# but the one ``violated_rule`` extract. ``scan_started_at`` is the funnel's marker.
_DECISIONS_BETWEEN_SQL = f"""SELECT s.id::text AS id,
       {_utc_iso("s.created_at", alias="created_at")},
       s.symbol,
       {_utc_iso("r.started_at", alias="scan_started_at")},
       ({DECISION_KIND_SQL}) AS decision_kind,
       j.ruling AS judge_ruling,
       s.payload->>'violated_rule' AS violated_rule,
       {_HAS_SETUP_SQL}
{_SIGNAL_SCAN_JUDGE_FROM}
WHERE s.created_at >= :since::timestamptz AND s.created_at < :until::timestamptz
  AND {ASC_KEYSET_PREDICATE}
{ASC_KEYSET_ORDER}
{PAGE_LIMIT_SQL}"""


async def list_decisions_between(
    reader: DashboardReader, *, since: datetime, until: datetime
) -> list[DecisionRow]:
    """Every decision with ``since <= created_at < until``, oldest first (the funnel).

    Read in 1 000-row keyset pages (:func:`read_all_pages`) so a capped year never
    hits the Data API's 1 MB response limit.
    """
    rows = await read_all_pages(
        reader,
        _DECISIONS_BETWEEN_SQL,
        [_str_param("since", since.isoformat()), _str_param("until", until.isoformat())],
    )
    return [DecisionRow.model_validate(row) for row in rows]


# ---------------------------------------------------------------------------
# Analytics (P6)
# ---------------------------------------------------------------------------


class OutcomeRow(BaseModel):
    """One proposal / gate rejection reduced to what P6 aggregates (design §5.2a).

    Minimal (Data API 1 MB cap): the one ``risk_reward_ratio`` extract from the
    payload, the two outcome sources and three named extracts of the scan's
    Skeptic run -- never a whole JSONB document.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    created_at: datetime
    symbol: str
    strategy: str
    session: str | None = Field(description="scan_runs.session.")
    tags: list[str]
    decision_kind: DecisionKind
    judge_ruling: str | None
    risk_reward_ratio: float | None
    signal_outcome: str | None = Field(description="signals.outcome (the Forecaster's verdict).")
    resolution_outcome: str | None = Field(description="decision_resolutions.outcome.")
    realized_r: float | None = Field(description="decision_resolutions.realized_r.")
    skeptic_severity: str | None = Field(description="Latest Skeptic run's output->>'severity'.")
    skeptic_recommends_against: bool | None
    skeptic_is_no_macro: bool = Field(
        description="The Skeptic output is the NoMacroData sentinel ({provider, reason})."
    )


# ``k`` is the latest Skeptic run sharing the signal's scan (as ``j`` is the Judge's).
# NoMacroData serialises as ``{provider, reason}``: ``output->'provider' IS NOT NULL``
# is the key-exists test. The ``?`` operator is avoided on purpose -- the Data API
# parses SQL JDBC-style and ``?`` there is a positional-placeholder hazard.
# NO_SETUP rows (no proposal, no gate rule) feed no P6 chart: filtered server-side.
_OUTCOME_ROWS_SQL = f"""SELECT s.id::text AS id,
       {_utc_iso("s.created_at", alias="created_at")},
       s.symbol, s.strategy, r.session AS session, s.tags,
       ({DECISION_KIND_SQL}) AS decision_kind,
       j.ruling AS judge_ruling,
       (s.payload->>'risk_reward_ratio')::double precision AS risk_reward_ratio,
       s.outcome AS signal_outcome,
       d.outcome AS resolution_outcome,
       d.realized_r::double precision AS realized_r,
       k.severity AS skeptic_severity,
       k.recommends_against AS skeptic_recommends_against,
       COALESCE(k.is_no_macro, FALSE) AS skeptic_is_no_macro
{_SIGNAL_SCAN_JUDGE_FROM}
LEFT JOIN LATERAL (
    SELECT a.output->>'severity' AS severity,
           (a.output->>'recommends_against')::boolean AS recommends_against,
           (a.output->'provider') IS NOT NULL AS is_no_macro
    FROM agent_runs a
    WHERE a.scan_id = s.scan_id AND a.agent_role = 'skeptic'
    ORDER BY a.created_at DESC
    LIMIT 1
) k ON TRUE
LEFT JOIN decision_resolutions d ON d.signal_id = s.id
WHERE s.created_at >= :since::timestamptz AND s.created_at < :until::timestamptz
  AND (s.direction IS NOT NULL OR COALESCE(s.payload->>'violated_rule', '') <> '')
  AND {ASC_KEYSET_PREDICATE}
{ASC_KEYSET_ORDER}
{PAGE_LIMIT_SQL}"""


async def list_outcome_rows(
    reader: DashboardReader, *, since: datetime, until: datetime
) -> list[OutcomeRow]:
    """Every proposal / gate rejection with ``since <= created_at < until``, oldest first.

    Read in 1 000-row keyset pages (:func:`read_all_pages`), like the funnel.
    """
    rows = await read_all_pages(
        reader,
        _OUTCOME_ROWS_SQL,
        [_str_param("since", since.isoformat()), _str_param("until", until.isoformat())],
    )
    return [OutcomeRow.model_validate(row) for row in rows]


# ---------------------------------------------------------------------------
# Critic reports / reviews (P6)
# ---------------------------------------------------------------------------


class CriticReportSummary(BaseModel):
    """A ``critic_reports`` list line: no ``markdown``, no JSONB beyond the summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    period_start: datetime
    period_end: datetime
    created_at: datetime
    pr_url: str | None
    summary: str | None = Field(description="report->>'summary'.")


class ReviewSummary(BaseModel):
    """A ``decision_reviews`` list line: named extracts of the ``review`` JSONB.

    The review fields are null when the review call errored (``review`` is ``{}``
    and ``error`` is set).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: UUID
    decision_kind: str
    failure_category: str | None
    lesson: str | None
    judge_correct: bool | None
    gate_correct: bool | None
    skeptic_vindicated: str | None
    confidence: str | None
    error: str | None = Field(description="First 500 chars of the failure text.")
    created_at: datetime


async def list_critic_reports(reader: DashboardReader, *, limit: int) -> list[CriticReportSummary]:
    """The latest ``limit`` Critic reports, newest first, without their markdown."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    sql = f"""SELECT id::text AS id,
       {_utc_iso("period_start", alias="period_start")},
       {_utc_iso("period_end", alias="period_end")},
       {_utc_iso("created_at", alias="created_at")},
       pr_url,
       report->>'summary' AS summary
FROM critic_reports
ORDER BY created_at DESC, id DESC
LIMIT :limit"""
    rows = await reader.read_rows(sql, [_long_param("limit", limit)])
    return [CriticReportSummary.model_validate(row) for row in rows]


async def get_critic_report(reader: DashboardReader, report_id: UUID) -> StoredCriticReport | None:
    """One Critic report with its full JSONB and markdown, or ``None``."""
    sql = f"""SELECT id::text AS id,
       {_utc_iso("period_start", alias="period_start")},
       {_utc_iso("period_end", alias="period_end")},
       report::text AS report, markdown, pr_url,
       {_utc_iso("created_at", alias="created_at")}
FROM critic_reports
WHERE id = :id::uuid"""
    rows = await reader.read_rows(sql, [_str_param("id", str(report_id))])
    if not rows:
        return None
    data = dict(rows[0])
    data["report"] = _parse_jsonb_field(data["report"])
    return StoredCriticReport.model_validate(data)


_REVIEW_KEYSET_PREDICATE = (
    "(:after_ts::timestamptz IS NULL OR "
    "(created_at, signal_id) < (:after_ts::timestamptz, :after_id::uuid))"
)


async def list_reviews_page(
    reader: DashboardReader, *, limit: int, after: tuple[datetime, UUID] | None
) -> list[ReviewSummary]:
    """One page of Critic reviews, newest first; up to ``limit + 1`` rows (see the journal)."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    sql = f"""SELECT signal_id::text AS signal_id, decision_kind,
       review->>'failure_category' AS failure_category,
       review->>'lesson' AS lesson,
       (review->>'judge_correct')::boolean AS judge_correct,
       (review->>'gate_correct')::boolean AS gate_correct,
       review->>'skeptic_vindicated' AS skeptic_vindicated,
       review->>'confidence' AS confidence,
       left(error, 500) AS error,
       {_utc_iso("created_at", alias="created_at")}
FROM decision_reviews
WHERE {_REVIEW_KEYSET_PREDICATE}
ORDER BY created_at DESC, signal_id DESC
LIMIT :limit"""
    rows = await reader.read_rows(sql, [*_cursor_params(after), _long_param("limit", limit + 1)])
    return [ReviewSummary.model_validate(row) for row in rows]


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
    cards: list[SetupCard] = []
    for row in rows:
        try:
            cards.append(SetupCard.model_validate(_parse_latest_evaluation(row)))
        except ValidationError as exc:
            # One malformed payload (a missing price after a console edit, an older
            # row) must not take the whole overview down: skip it, say which.
            logger.warning("setup %s skipped: malformed payload: %s", row.get("setup_id"), exc)
    return cards


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


# ---------------------------------------------------------------------------
# System health (P7, Step 4.3)
# ---------------------------------------------------------------------------


class UsageDay(BaseModel):
    """Runs, failures and LLM spend of one agent role on one UTC day.

    Three sources (design §4.2 P7): ``agent_runs`` (the scan pipeline -- a row is
    only written on success, so ``errors`` is 0), ``setup_evaluations`` (role
    ``forecaster``; ``ERROR`` rows are failures) and ``decision_reviews`` (role
    ``critic``; a set ``error`` is a failure). Unpriced calls (NULL cost) add 0.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date_type
    role: str
    runs: int = Field(ge=0)
    errors: int = Field(ge=0)
    cost_usd: float = Field(ge=0, description="Sum of cost_usd; NULL (unpriced) counts as 0.")


class EvaluationStamp(BaseModel):
    """One Forecaster evaluation reduced to what sweep bucketing needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated_at: datetime
    is_error: bool


class ConfigFacts(BaseModel):
    """What the database says about the deployed configuration (design §8: inferred)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    watchlist: list[str] = Field(description="Union of scan_runs.symbols in the window.")
    model: str | None = Field(description="Latest agent_runs.token_usage->>'model'.")
    skeptic_outputs: int = Field(ge=0, description="How many recent Skeptic outputs were read.")
    real_objections: int = Field(ge=0, description="... of which were not NoMacroData.")


async def list_recent_scan_runs(reader: DashboardReader, *, limit: int = 50) -> list[StoredScanRun]:
    """The latest ``limit`` scan runs, newest first (P7 scan history)."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    sql = f"""SELECT id::text AS id,
       {_utc_iso("started_at", alias="started_at")},
       {_utc_iso("completed_at", alias="completed_at")},
       status, error_message, session, strategy, symbols
FROM scan_runs
ORDER BY started_at DESC, id DESC
LIMIT :limit"""
    rows = await reader.read_rows(sql, [_long_param("limit", limit)])
    return [StoredScanRun.model_validate(row) for row in rows]


def _utc_day(column: str) -> str:
    return f"to_char({column} AT TIME ZONE 'UTC', 'YYYY-MM-DD')"


# One statement, three sources, aggregated per (UTC day, role). A bounded result:
# at most 30 days x 6 roles rows.
_USAGE_BY_DAY_SQL = f"""SELECT day, role,
       sum(runs)::bigint AS runs,
       sum(errors)::bigint AS errors,
       COALESCE(sum(cost_usd), 0)::double precision AS cost_usd
FROM (
    SELECT {_utc_day("created_at")} AS day, agent_role AS role,
           count(*) AS runs, 0::bigint AS errors,
           sum(cost_usd)::double precision AS cost_usd
    FROM agent_runs
    WHERE created_at >= :since::timestamptz
    GROUP BY 1, 2
    UNION ALL
    SELECT {_utc_day("evaluated_at")} AS day, 'forecaster' AS role,
           count(*) AS runs, count(*) FILTER (WHERE status = 'ERROR') AS errors,
           sum(cost_usd)::double precision AS cost_usd
    FROM setup_evaluations
    WHERE evaluated_at >= :since::timestamptz
    GROUP BY 1
    UNION ALL
    SELECT {_utc_day("created_at")} AS day, 'critic' AS role,
           count(*) AS runs, count(*) FILTER (WHERE error IS NOT NULL) AS errors,
           sum(cost_usd)::double precision AS cost_usd
    FROM decision_reviews
    WHERE created_at >= :since::timestamptz
    GROUP BY 1
) u
GROUP BY day, role
ORDER BY day ASC, role ASC"""


async def agent_usage_by_day(reader: DashboardReader, *, since: datetime) -> list[UsageDay]:
    """Per-day, per-role runs / failures / cost since ``since`` (P7 agents + LLM cost)."""
    rows = await reader.read_rows(_USAGE_BY_DAY_SQL, [_str_param("since", since.isoformat())])
    return [
        UsageDay(
            date=date_type.fromisoformat(str(row["day"])),
            role=str(row["role"]),
            runs=int(row.get("runs") or 0),
            errors=int(row.get("errors") or 0),
            cost_usd=float(row.get("cost_usd") or 0.0),
        )
        for row in rows
    ]


async def list_evaluation_stamps(
    reader: DashboardReader, *, since: datetime
) -> list[EvaluationStamp]:
    """Every Forecaster evaluation since ``since`` as ``(evaluated_at, is_error)``, oldest first.

    Two short columns per row: <= 3 open setups x 4 sweeps a day x 30 days.
    """
    sql = f"""SELECT {_utc_iso("evaluated_at", alias="evaluated_at")},
       (status = 'ERROR') AS is_error
FROM setup_evaluations
WHERE evaluated_at >= :since::timestamptz
ORDER BY evaluated_at ASC"""
    rows = await reader.read_rows(sql, [_str_param("since", since.isoformat())])
    return [EvaluationStamp.model_validate(row) for row in rows]


# The last N Skeptic outputs decide "macro providers configured" (design §8).
_SKEPTIC_SAMPLE = 10


async def config_inference(reader: DashboardReader, *, since: datetime) -> ConfigFacts:
    """The config panel's database facts (design §8 -- every value is inferred).

    Watchlist = union of ``scan_runs.symbols`` since ``since``; model = the latest
    ``token_usage->>'model'``; and how many of the last 10 Skeptic outputs are a
    real objection rather than the NoMacroData sentinel (``{provider, reason}``).
    """
    watch_sql = """SELECT DISTINCT unnest(symbols) AS symbol
FROM scan_runs
WHERE started_at >= :since::timestamptz
ORDER BY 1"""
    watch = await reader.read_rows(watch_sql, [_str_param("since", since.isoformat())])
    model_sql = """SELECT token_usage->>'model' AS model
FROM agent_runs
WHERE token_usage->>'model' IS NOT NULL
ORDER BY created_at DESC
LIMIT 1"""
    model_rows = await reader.read_rows(model_sql, [])
    # `output->'provider' IS NULL` = the key is absent (no `?` operator: the Data
    # API parses SQL JDBC-style, see _OUTCOME_ROWS_SQL).
    skeptic_sql = f"""SELECT (output->'provider') IS NULL AS real_objection
FROM agent_runs
WHERE agent_role = 'skeptic'
ORDER BY created_at DESC
LIMIT {_SKEPTIC_SAMPLE}"""
    skeptic = await reader.read_rows(skeptic_sql, [])
    return ConfigFacts(
        watchlist=[str(row["symbol"]) for row in watch if row.get("symbol")],
        model=str(model_rows[0]["model"]) if model_rows and model_rows[0].get("model") else None,
        skeptic_outputs=len(skeptic),
        real_objections=sum(1 for row in skeptic if row.get("real_objection")),
    )


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
