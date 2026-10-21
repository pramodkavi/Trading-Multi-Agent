"""Dashboard read-only SQL layer (Slice 4 Step 4.2, design §5.1 / §3.4 / §6.2).

No HTTP here: a `FakeReader` records the `(sql, parameters)` each query sends through
the `DashboardReader` seam and returns canned Data-API-shaped rows, so the tests pin
the SQL *shape* (joins, predicates, ordering, keyset paging, every filter bound by
name, no user value in the SQL text) and the row -> model parsing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.common.models import (
    ActiveSetupStatus,
    ScanStatus,
    SignalDirection,
    SignalOutcome,
    SignalStatus,
)
from src.dashboard.api.classify import DECISION_KIND_SQL, DecisionKind
from src.dashboard.api.queries import (
    JournalRow,
    SetupCard,
    SignalFilters,
    count_evaluations_since,
    get_review,
    get_setup_for_signal,
    list_open_setup_cards,
    list_scan_runs_since,
    list_signals_for_scans,
    list_signals_page,
)
from src.persistence.models import StoredActiveSetup, StoredReview, StoredScanRun

NOW = datetime(2026, 10, 20, 8, 3, 52, 123456, tzinfo=UTC)
ISO_NOW = "2026-10-20T08:03:52.123456+00:00"  # the `_utc_iso` rendering
NARRATIVE = "Synthetic fixture for the dashboard query parsing tests."


class FakeReader:
    """Records every `read_rows` call and returns the canned `rows`."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = rows if rows is not None else []
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def read_rows(self, sql: str, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.calls.append((sql, parameters))
        return self.rows


def _params(call: tuple[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Index a Data API parameter list by name -> typed value dict."""
    names = [p["name"] for p in call[1]]
    assert len(names) == len(set(names)), f"duplicate parameter names: {names}"
    return {p["name"]: p["value"] for p in call[1]}


def _proposal_payload(direction: str) -> dict[str, Any]:
    if direction == "LONG":
        entry, stop, tp1, tp2 = 100.0, 98.0, 106.0, 110.0
    else:
        entry, stop, tp1, tp2 = 100.0, 102.0, 94.0, 90.0
    return {
        "proposal_id": str(uuid4()),
        "scan_id": str(uuid4()),
        "strategy": "smc",
        "created_at": NOW.isoformat(),
        "symbol": "BTCUSDT",
        "direction": direction,
        "entry_price": entry,
        "stop_loss": stop,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "risk_reward_ratio": 3.0,
        "leverage": 3.0,
        "risk_percent": 1.0,
        "tags": ["fvg"],
        "confluence_narrative": NARRATIVE,
        "features": {"rr": 3.0},
    }


def _skip_payload(violated_rule: str | None) -> dict[str, Any]:
    return {
        "decision_id": str(uuid4()),
        "scan_id": str(uuid4()),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "reason": "INSUFFICIENT_RR" if violated_rule else "NO_CLEAR_BIAS",
        "details": "gate rejected the proposal" if violated_rule else "no valid structure",
        "violated_rule": violated_rule,
        "created_at": NOW.isoformat(),
    }


def _journal_row(
    *,
    kind: str,
    direction: str | None = None,
    ruling: str | None = None,
    violated_rule: str | None = None,
    has_setup: bool = False,
    resolution_outcome: str | None = None,
    realized_r: float | None = None,
    signal_id: UUID | None = None,
    created_at: str = ISO_NOW,
) -> dict[str, Any]:
    """One row exactly as `read_rows` would hand it back for the journal projection.

    The projection is *trimmed* (design §3.4, Data API 1 MB cap): no `payload`,
    `features` or `outcome_metadata` column -- the analyzer summary is lifted out
    of the payload in SQL (`entry_price` ... `skip_details`).
    """
    payload = _proposal_payload(direction) if direction else _skip_payload(violated_rule)
    return {
        "id": str(signal_id or uuid4()),
        "scan_id": str(uuid4()),
        "symbol": "BTCUSDT",
        "strategy": "smc",
        "direction": direction,
        "status": "PUBLISHED" if direction else "SKIPPED",
        "created_at": created_at,
        "tags": ["fvg"] if direction else [],
        "outcome": "WIN" if resolution_outcome == "WIN" else None,
        "entry_price": payload.get("entry_price"),
        "stop_loss": payload.get("stop_loss"),
        "take_profit_1": payload.get("take_profit_1"),
        "take_profit_2": payload.get("take_profit_2"),
        "risk_reward_ratio": payload.get("risk_reward_ratio"),
        "skip_reason": payload.get("reason"),
        "skip_details": payload.get("details"),
        "decision_kind": kind,
        "judge_ruling": ruling,
        "session": "LONDON",
        "has_setup": has_setup,
        "violated_rule": violated_rule,
        "resolution_outcome": resolution_outcome,
        "realized_r": realized_r,
    }


# ---------------------------------------------------------------------------
# list_signals_page -- SQL shape
# ---------------------------------------------------------------------------


async def test_signals_page_default_sql_shape() -> None:
    reader = FakeReader()
    rows = await list_signals_page(reader, filters=SignalFilters(), limit=50, after=None)
    assert rows == []
    assert len(reader.calls) == 1
    sql, _ = reader.calls[0]
    assert "FROM signals s" in sql
    assert "JOIN scan_runs r ON r.id = s.scan_id" in sql
    assert "LEFT JOIN LATERAL" in sql
    assert "agent_role = 'judge'" in sql
    # `has_setup` is an EXISTS probe, not a join: a signal with several setup rows
    # must never fan out into duplicate journal lines.
    assert "LEFT JOIN active_setups" not in sql
    assert "EXISTS (SELECT 1 FROM active_setups t WHERE t.signal_id = s.id) AS has_setup" in sql
    assert "LEFT JOIN decision_resolutions d ON d.signal_id = s.id" in sql
    assert DECISION_KIND_SQL in sql
    assert "AS decision_kind" in sql
    assert "ORDER BY s.created_at DESC, s.id DESC" in sql
    assert "LIMIT :limit" in sql
    # Trimmed projection (design §3.4): the three big JSONB columns are never selected
    # as a whole -- 201 full rows could exceed the Data API's 1 MB response cap.
    assert "s.payload::text" not in sql
    assert "AS payload" not in sql
    assert "s.features" not in sql
    assert "s.outcome_metadata" not in sql
    # ... only the analyzer summary is lifted out of the payload, in SQL.
    for key in ("entry_price", "stop_loss", "take_profit_1", "take_profit_2", "risk_reward_ratio"):
        assert f"(s.payload->>'{key}')::double precision AS {key}" in sql, key
    assert "s.payload->>'reason' AS skip_reason" in sql
    assert "left(s.payload->>'details', 200) AS skip_details" in sql
    assert "s.payload->>'violated_rule' AS violated_rule" in sql
    assert "s.tags" in sql and "s.outcome" in sql
    params = _params(reader.calls[0])
    assert params["after_ts"] == {"isNull": True}
    assert params["after_id"] == {"isNull": True}
    assert params["limit"] == {"longValue": 51}
    # No filter -> no filter predicate and no filter parameter.
    assert set(params) == {"after_ts", "after_id", "limit"}
    for fragment in (
        "s.symbol = :symbol",
        "s.outcome = :outcome",
        ":tag = ANY(s.tags)",
        "r.session = :session",
        "= :violated_rule",
        "= :decision_kind",
        ":since",
        ":until",
    ):
        assert fragment not in sql, fragment


async def test_signals_page_session_filter_joins_scan_runs() -> None:
    reader = FakeReader()
    await list_signals_page(reader, filters=SignalFilters(session="LONDON"), limit=10, after=None)
    sql, _ = reader.calls[0]
    assert "r.session = :session" in sql
    assert "payload->>'session'" not in sql
    assert _params(reader.calls[0])["session"] == {"stringValue": "LONDON"}
    assert "LONDON" not in sql


async def test_signals_page_every_filter_binds_a_param() -> None:
    since = datetime(2026, 10, 19, 0, 0, tzinfo=UTC)
    until = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
    filters = SignalFilters(
        since=since,
        until=until,
        symbol="ETHUSDT",
        decision_kind=DecisionKind.VETOED,
        outcome=SignalOutcome.LOSS,
        tag="order_block",
        session="OVERLAP",
        violated_rule="RULE_3_PREMIUM_DISCOUNT",
    )
    reader = FakeReader()
    await list_signals_page(reader, filters=filters, limit=20, after=None)
    sql, _ = reader.calls[0]
    params = _params(reader.calls[0])

    expected_predicates = {
        "since": "s.created_at >= :since::timestamptz",
        "until": "s.created_at < :until::timestamptz",
        "symbol": "s.symbol = :symbol",
        "decision_kind": f"({DECISION_KIND_SQL}) = :decision_kind",
        "outcome": "s.outcome = :outcome",
        "tag": ":tag = ANY(s.tags)",
        "session": "r.session = :session",
        "violated_rule": "s.payload->>'violated_rule' = :violated_rule",
    }
    for name, predicate in expected_predicates.items():
        assert predicate in sql, name
    expected_values = {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "symbol": "ETHUSDT",
        "decision_kind": "VETOED",
        "outcome": "LOSS",
        "tag": "order_block",
        "session": "OVERLAP",
        "violated_rule": "RULE_3_PREMIUM_DISCOUNT",
    }
    for name, value in expected_values.items():
        assert params[name] == {"stringValue": value}, name
    assert params["limit"] == {"longValue": 21}
    assert set(params) == {*expected_values, "after_ts", "after_id", "limit"}

    # No filter value is ever formatted into the SQL text. The decision-kind CASE
    # legitimately spells out the four kind literals, so strip it before checking.
    sql_without_case = sql.replace(DECISION_KIND_SQL, "")
    for name, value in expected_values.items():
        assert value not in sql_without_case, name


async def test_signals_page_cursor_predicate() -> None:
    ts = NOW - timedelta(hours=5)
    signal_id = uuid4()
    reader = FakeReader()
    await list_signals_page(reader, filters=SignalFilters(), limit=50, after=(ts, signal_id))
    sql, _ = reader.calls[0]
    assert (
        "(:after_ts::timestamptz IS NULL OR "
        "(s.created_at, s.id) < (:after_ts::timestamptz, :after_id::uuid))"
    ) in sql
    params = _params(reader.calls[0])
    assert params["after_ts"] == {"stringValue": ts.isoformat()}
    assert params["after_id"] == {"stringValue": str(signal_id)}
    assert ts.isoformat() not in sql and str(signal_id) not in sql


@pytest.mark.parametrize("limit", [0, -1])
async def test_signals_page_rejects_non_positive_limit(limit: int) -> None:
    reader = FakeReader()
    with pytest.raises(ValueError, match="limit"):
        await list_signals_page(reader, filters=SignalFilters(), limit=limit, after=None)
    assert reader.calls == []


# ---------------------------------------------------------------------------
# list_signals_page -- parsing
# ---------------------------------------------------------------------------


async def test_signals_page_parses_rows() -> None:
    published_id, vetoed_id, gate_id, no_setup_id = uuid4(), uuid4(), uuid4(), uuid4()
    reader = FakeReader(
        [
            _journal_row(
                kind="PUBLISHED",
                direction="LONG",
                ruling="PUBLISH",
                has_setup=True,
                resolution_outcome="WIN",
                realized_r=2.5,
                signal_id=published_id,
            ),
            _journal_row(kind="VETOED", direction="SHORT", ruling="SKIP", signal_id=vetoed_id),
            _journal_row(
                kind="GATE_REJECTED",
                violated_rule="RULE_3_PREMIUM_DISCOUNT",
                resolution_outcome="LOSS",
                realized_r=-1.0,
                signal_id=gate_id,
            ),
            _journal_row(kind="NO_SETUP", signal_id=no_setup_id),
        ]
    )
    rows = await list_signals_page(reader, filters=SignalFilters(), limit=50, after=None)
    assert [type(r) for r in rows] == [JournalRow] * 4
    assert [r.decision_kind for r in rows] == [
        DecisionKind.PUBLISHED,
        DecisionKind.VETOED,
        DecisionKind.GATE_REJECTED,
        DecisionKind.NO_SETUP,
    ]
    assert [r.has_setup for r in rows] == [True, False, False, False]
    assert [r.realized_r for r in rows] == [2.5, None, -1.0, None]
    assert [r.resolution_outcome for r in rows] == ["WIN", None, "LOSS", None]
    assert [r.judge_ruling for r in rows] == ["PUBLISH", "SKIP", None, None]
    assert [r.violated_rule for r in rows] == [None, None, "RULE_3_PREMIUM_DISCOUNT", None]
    assert all(r.session == "LONDON" for r in rows)

    published = rows[0]
    assert published.id == published_id
    assert published.direction is SignalDirection.LONG
    assert published.status is SignalStatus.PUBLISHED
    assert published.created_at == NOW
    assert published.tags == ["fvg"]
    assert published.outcome is SignalOutcome.WIN
    # The analyzer summary comes from the SQL-lifted columns, not a parsed payload.
    assert (published.entry_price, published.stop_loss) == (100.0, 98.0)
    assert (published.take_profit_1, published.take_profit_2) == (106.0, 110.0)
    assert published.risk_reward_ratio == 3.0
    assert published.skip_reason is None and published.skip_details is None

    gate = rows[2]
    assert gate.direction is None and gate.status is SignalStatus.SKIPPED
    assert gate.violated_rule == "RULE_3_PREMIUM_DISCOUNT"
    assert gate.skip_reason == "INSUFFICIENT_RR"
    assert gate.skip_details == "gate rejected the proposal"
    assert gate.entry_price is None and gate.risk_reward_ratio is None
    assert gate.tags == [] and gate.outcome is None

    # The row model carries no full JSONB column at all (design §3.4).
    for big in ("payload", "features", "outcome_metadata", "signal"):
        assert big not in JournalRow.model_fields, big

    # Frozen value objects: the router may not mutate them.
    with pytest.raises(Exception, match="frozen"):
        rows[0].has_setup = False


async def test_signals_page_vetoed_row() -> None:
    reader = FakeReader([_journal_row(kind="VETOED", direction="SHORT", ruling="SKIP")])
    (row,) = await list_signals_page(reader, filters=SignalFilters(), limit=50, after=None)
    assert row.decision_kind is DecisionKind.VETOED
    assert row.has_setup is False
    assert row.judge_ruling == "SKIP"
    # `signals.status` is PUBLISHED even for a Judge veto (design §5.1): the kind is
    # derived from the Judge run, never from the status column.
    assert row.status is SignalStatus.PUBLISHED
    assert row.direction is SignalDirection.SHORT
    assert (row.entry_price, row.stop_loss, row.take_profit_1) == (100.0, 102.0, 94.0)


async def test_signals_page_does_not_mutate_reader_rows() -> None:
    canned = _journal_row(kind="PUBLISHED", direction="LONG", ruling="PUBLISH", has_setup=True)
    snapshot = dict(canned)
    reader = FakeReader([canned])
    await list_signals_page(reader, filters=SignalFilters(), limit=50, after=None)
    assert canned == snapshot  # the reader's dicts are never mutated in place


# ---------------------------------------------------------------------------
# list_open_setup_cards
# ---------------------------------------------------------------------------


async def test_open_setup_cards_sql_and_parse() -> None:
    setup_id, signal_id = uuid4(), uuid4()
    reader = FakeReader(
        [
            {
                "setup_id": str(setup_id),
                "signal_id": str(signal_id),
                "symbol": "ETHUSDT",
                "direction": "SHORT",
                "entry_price": 100.0,
                "stop_loss": 102.0,
                "take_profit_1": 94.0,
                "take_profit_2": None,
                "opened_at": ISO_NOW,
                "last_evaluated_at": "2026-10-20T13:03:10.000000+00:00",
                "latest_evaluation": json.dumps({"status": "AT_RISK", "reasoning": "near SL"}),
            },
            {
                "setup_id": str(uuid4()),
                "signal_id": str(uuid4()),
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "entry_price": 50000.0,
                "stop_loss": 49000.0,
                "take_profit_1": 53000.0,
                "take_profit_2": 56000.0,
                "opened_at": "2026-10-19T22:03:05.000000+00:00",
                "last_evaluated_at": None,
                "latest_evaluation": None,
            },
        ]
    )
    cards = await list_open_setup_cards(reader)
    sql, _ = reader.calls[0]
    assert "FROM active_setups t" in sql
    assert "JOIN signals s ON s.id = t.signal_id" in sql
    assert "WHERE t.status = :status" in sql
    assert "ORDER BY t.opened_at DESC" in sql
    assert "'OPEN'" not in sql
    for key in ("entry_price", "stop_loss", "take_profit_1", "take_profit_2"):
        assert f"(s.payload->>'{key}')::double precision AS {key}" in sql
    assert _params(reader.calls[0]) == {"status": {"stringValue": ActiveSetupStatus.OPEN.value}}

    assert [type(c) for c in cards] == [SetupCard, SetupCard]
    first, second = cards
    assert first.setup_id == setup_id and first.signal_id == signal_id
    assert first.symbol == "ETHUSDT" and first.direction is SignalDirection.SHORT
    assert (first.entry_price, first.stop_loss, first.take_profit_1, first.take_profit_2) == (
        100.0,
        102.0,
        94.0,
        None,
    )
    assert first.opened_at == NOW
    assert first.last_evaluated_at == datetime(2026, 10, 20, 13, 3, 10, tzinfo=UTC)
    assert first.latest_evaluation == {"status": "AT_RISK", "reasoning": "near SL"}
    assert second.take_profit_2 == 56000.0
    assert second.last_evaluated_at is None and second.latest_evaluation is None


async def test_open_setup_cards_skip_and_log_malformed_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A setup whose signal payload lacks a price (console-edited / older row) must not
    # take the whole overview down: the row is skipped with a WARNING naming it.
    bad_setup = uuid4()
    good_setup = uuid4()
    reader = FakeReader(
        [
            {
                "setup_id": str(bad_setup),
                "signal_id": str(uuid4()),
                "symbol": "ETHUSDT",
                "direction": "SHORT",
                "entry_price": None,  # payload->>'entry_price' missing
                "stop_loss": 102.0,
                "take_profit_1": 94.0,
                "take_profit_2": None,
                "opened_at": ISO_NOW,
                "last_evaluated_at": None,
                "latest_evaluation": None,
            },
            {
                "setup_id": str(good_setup),
                "signal_id": str(uuid4()),
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "entry_price": 50000.0,
                "stop_loss": 49000.0,
                "take_profit_1": 53000.0,
                "take_profit_2": None,
                "opened_at": ISO_NOW,
                "last_evaluated_at": None,
                "latest_evaluation": None,
            },
        ]
    )
    with caplog.at_level("WARNING", logger="src.dashboard.api.queries"):
        cards = await list_open_setup_cards(reader)
    assert [c.setup_id for c in cards] == [good_setup]
    (record,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert str(bad_setup) in record.getMessage()
    assert "malformed payload" in record.getMessage()


# ---------------------------------------------------------------------------
# list_scan_runs_since / list_signals_for_scans
# ---------------------------------------------------------------------------


async def test_scan_runs_since_sql_and_parse() -> None:
    since = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    scan_id = uuid4()
    reader = FakeReader(
        [
            {
                "id": str(scan_id),
                "started_at": ISO_NOW,
                "completed_at": "2026-10-20T08:04:40.000000+00:00",
                "status": "SUCCESS",
                "error_message": None,
                "session": "LONDON",
                "strategy": "smc",
                "symbols": ["BTCUSDT"],
            }
        ]
    )
    runs = await list_scan_runs_since(reader, since=since)
    sql, _ = reader.calls[0]
    assert "FROM scan_runs" in sql
    assert "started_at >= :since::timestamptz" in sql
    assert "ORDER BY started_at ASC, id ASC" in sql
    assert _params(reader.calls[0]) == {"since": {"stringValue": since.isoformat()}}
    assert since.isoformat() not in sql
    assert runs == [
        StoredScanRun(
            id=scan_id,
            started_at=NOW,
            completed_at=datetime(2026, 10, 20, 8, 4, 40, tzinfo=UTC),
            status=ScanStatus.SUCCESS,
            error_message=None,
            session="LONDON",
            strategy="smc",
            symbols=["BTCUSDT"],
        )
    ]


async def test_signals_for_scans_uses_string_to_array() -> None:
    ids = [uuid4(), uuid4(), uuid4()]
    reader = FakeReader([_journal_row(kind="NO_SETUP")])
    rows = await list_signals_for_scans(reader, scan_ids=ids)
    sql, _ = reader.calls[0]
    assert "string_to_array(:ids, ',')::uuid[]" in sql
    assert "s.scan_id = ANY(string_to_array(:ids, ',')::uuid[])" in sql
    assert DECISION_KIND_SQL in sql  # same journal projection as the page
    assert "ORDER BY s.created_at DESC, s.id DESC" in sql
    assert "LIMIT :limit" not in sql  # unpaged: a window is at most a handful of rows
    assert _params(reader.calls[0]) == {"ids": {"stringValue": ",".join(str(i) for i in ids)}}
    for scan_id in ids:
        assert str(scan_id) not in sql
    assert len(rows) == 1 and rows[0].decision_kind is DecisionKind.NO_SETUP


async def test_signals_for_scans_empty_ids_skips_the_query() -> None:
    reader = FakeReader([_journal_row(kind="NO_SETUP")])
    assert await list_signals_for_scans(reader, scan_ids=[]) == []
    assert reader.calls == []


# ---------------------------------------------------------------------------
# get_setup_for_signal / get_review / count_evaluations_since
# ---------------------------------------------------------------------------


async def test_get_setup_for_signal() -> None:
    setup_id, signal_id = uuid4(), uuid4()
    reader = FakeReader(
        [
            {
                "id": str(setup_id),
                "signal_id": str(signal_id),
                "opened_at": ISO_NOW,
                "status": "OPEN",
                "last_evaluated_at": None,
                "latest_evaluation": json.dumps({"status": "STILL_VALID"}),
            }
        ]
    )
    setup = await get_setup_for_signal(reader, signal_id)
    sql, _ = reader.calls[0]
    assert "FROM active_setups" in sql
    assert "WHERE signal_id = :signal_id::uuid" in sql
    assert "LIMIT 1" in sql
    assert _params(reader.calls[0]) == {"signal_id": {"stringValue": str(signal_id)}}
    assert setup == StoredActiveSetup(
        id=setup_id,
        signal_id=signal_id,
        opened_at=NOW,
        status=ActiveSetupStatus.OPEN,
        last_evaluated_at=None,
        latest_evaluation={"status": "STILL_VALID"},
    )

    assert await get_setup_for_signal(FakeReader([]), signal_id) is None


async def test_get_review() -> None:
    signal_id = uuid4()
    reader = FakeReader(
        [
            {
                "signal_id": str(signal_id),
                "decision_kind": "PUBLISHED",
                "review": json.dumps({"grade": "B", "failure_category": None}),
                "model": "claude-sonnet-4-5",
                "token_usage": json.dumps({"input_tokens": 10}),
                "cost_usd": 0.0123,
                "error": None,
                "attempts": 1,
                "created_at": ISO_NOW,
            }
        ]
    )
    review = await get_review(reader, signal_id)
    sql, _ = reader.calls[0]
    assert "FROM decision_reviews" in sql
    assert "WHERE signal_id = :signal_id::uuid" in sql
    assert "cost_usd::double precision" in sql
    assert _params(reader.calls[0]) == {"signal_id": {"stringValue": str(signal_id)}}
    assert review == StoredReview(
        signal_id=signal_id,
        decision_kind="PUBLISHED",
        review={"grade": "B", "failure_category": None},
        model="claude-sonnet-4-5",
        token_usage={"input_tokens": 10},
        cost_usd=0.0123,
        error=None,
        attempts=1,
        created_at=NOW,
    )

    assert await get_review(FakeReader([]), signal_id) is None


async def test_count_evaluations_since() -> None:
    since = NOW - timedelta(hours=6)
    reader = FakeReader([{"total": 7, "errors": 2}])
    assert await count_evaluations_since(reader, since=since) == (7, 2)
    sql, _ = reader.calls[0]
    assert "count(*) AS total" in sql
    assert "count(*) FILTER (WHERE status = 'ERROR') AS errors" in sql
    assert "FROM setup_evaluations" in sql
    assert "evaluated_at >= :since::timestamptz" in sql
    assert _params(reader.calls[0]) == {"since": {"stringValue": since.isoformat()}}


async def test_count_evaluations_since_tolerates_empty_result() -> None:
    assert await count_evaluations_since(FakeReader([]), since=NOW) == (0, 0)
