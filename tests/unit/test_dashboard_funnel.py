"""Slice 4 Step 4.3 -- the decision funnel (design §4.2 P4): `GET /api/funnel`.

Pure counting is tested on `DecisionRow` lists built here; the SQL shape and the
route run against a small fake reader that serves the one funnel statement
(dispatching on its `scan_started_at` alias) and records the bound parameters.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import overview as assembly
from src.dashboard.api.app import create_app
from src.dashboard.api.classify import DecisionKind
from src.dashboard.api.deps import get_store
from src.dashboard.api.funnel import FunnelGroup, build_funnel, top_rejections
from src.dashboard.api.queries import (
    _DECISIONS_BETWEEN_SQL,
    ASC_KEYSET_ORDER,
    ASC_KEYSET_PREDICATE,
    PAGE_LIMIT_SQL,
    DecisionRow,
    list_decisions_between,
    read_all_pages,
)
from src.persistence.store import _str_param

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)


def _ts(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def _row(
    kind: DecisionKind,
    *,
    scan_at: datetime,
    symbol: str = "BTCUSDT",
    created_at: datetime | None = None,
    judge_ruling: str | None = None,
    violated_rule: str | None = None,
    has_setup: bool = False,
) -> DecisionRow:
    return DecisionRow(
        id=uuid4(),
        created_at=created_at if created_at is not None else scan_at + timedelta(minutes=1),
        symbol=symbol,
        scan_started_at=scan_at,
        decision_kind=kind,
        judge_ruling=judge_ruling,
        violated_rule=violated_rule,
        has_setup=has_setup,
    )


def _published(scan_at: datetime, *, symbol: str = "BTCUSDT", caveat: bool = False) -> DecisionRow:
    return _row(
        DecisionKind.PUBLISHED,
        scan_at=scan_at,
        symbol=symbol,
        judge_ruling="PUBLISH_WITH_CAVEAT" if caveat else "PUBLISH",
        has_setup=True,
    )


def _vetoed(scan_at: datetime, *, symbol: str = "BTCUSDT") -> DecisionRow:
    return _row(DecisionKind.VETOED, scan_at=scan_at, symbol=symbol, judge_ruling="SKIP")


def _gate(scan_at: datetime, rule: str, *, symbol: str = "BTCUSDT") -> DecisionRow:
    return _row(DecisionKind.GATE_REJECTED, scan_at=scan_at, symbol=symbol, violated_rule=rule)


def _no_setup(scan_at: datetime, *, symbol: str = "BTCUSDT") -> DecisionRow:
    return _row(DecisionKind.NO_SETUP, scan_at=scan_at, symbol=symbol)


LONDON_20 = _ts(20, 8, 3, 5)
NY_20 = _ts(20, 13, 3, 4)
LONDON_21 = _ts(21, 8, 3, 2)
AD_HOC_20 = _ts(20, 10, 41)  # a console test invoke


def _fixture_rows() -> list[DecisionRow]:
    return [
        # 2026-10-20 LONDON: four symbols, one scan invocation.
        _published(LONDON_20, symbol="BTCUSDT"),
        _vetoed(LONDON_20, symbol="ETHUSDT"),
        _gate(LONDON_20, "premium_discount", symbol="SOLUSDT"),
        _no_setup(LONDON_20, symbol="BNBUSDT"),
        # 2026-10-20 NY.
        _published(NY_20, symbol="ETHUSDT", caveat=True),
        _gate(NY_20, "min_rr", symbol="BTCUSDT"),
        # 2026-10-20 AD_HOC console invoke.
        _gate(AD_HOC_20, "premium_discount", symbol="BTCUSDT"),
        # 2026-10-21 LONDON.
        _no_setup(LONDON_21, symbol="BTCUSDT"),
        _no_setup(LONDON_21, symbol="ETHUSDT"),
    ]


# ---------------------------------------------------------------------------
# build_funnel
# ---------------------------------------------------------------------------


def test_build_funnel_by_day() -> None:
    groups = build_funnel(_fixture_rows(), group_by="day")
    assert [g.key for g in groups] == ["2026-10-20", "2026-10-21"]
    day20, day21 = groups
    assert day20 == FunnelGroup(
        key="2026-10-20",
        scans=3,  # LONDON, NY, AD_HOC
        symbols_scanned=7,
        proposals=6,
        gate_rejected=3,
        gate_passed=3,
        judge_publish=1,
        judge_caveat=1,
        judge_skip=1,
        judge_none=0,
        published=2,
        rejections_by_rule={"premium_discount": 2, "min_rr": 1},
    )
    assert day21.scans == 1
    assert day21.symbols_scanned == 2
    assert day21.proposals == 0
    assert day21.rejections_by_rule == {}


def test_judge_buckets_sum_to_gate_passed() -> None:
    # A Slice-1-era proposal: PUBLISHED (direction set) but no Judge run at all.
    no_judge = _row(DecisionKind.PUBLISHED, scan_at=NY_20, judge_ruling=None, has_setup=True)
    rows = [*_fixture_rows(), no_judge]
    for group_by in ("day", "window", "symbol"):
        groups = build_funnel(rows, group_by=group_by)
        for g in groups:
            total = g.judge_publish + g.judge_caveat + g.judge_skip + g.judge_none
            assert total == g.gate_passed, (group_by, g)
    (day20, _) = build_funnel(rows, group_by="day")
    assert day20.judge_none == 1
    assert day20.gate_passed == 4
    assert day20.published == 3


def test_build_funnel_by_window() -> None:
    groups = {g.key: g for g in build_funnel(_fixture_rows(), group_by="window")}
    assert list(groups) == ["LONDON", "NY", "AD_HOC"]  # schedule order, AD_HOC last
    london = groups["LONDON"]
    assert london.scans == 2  # 2026-10-20 and 2026-10-21
    assert london.symbols_scanned == 6
    assert london.proposals == 3
    assert london.gate_passed == 2
    assert london.judge_publish == 1
    assert london.judge_skip == 1
    assert london.published == 1
    assert groups["NY"].judge_caveat == 1


def test_funnel_ad_hoc_window_group() -> None:
    """Review Focus 4: AD_HOC rows are their own group -- not dropped, not folded."""
    rows = _fixture_rows()
    groups = {g.key: g for g in build_funnel(rows, group_by="window")}
    ad_hoc = groups["AD_HOC"]
    assert ad_hoc.scans == 1
    assert ad_hoc.symbols_scanned == 1
    assert ad_hoc.gate_rejected == 1
    assert ad_hoc.rejections_by_rule == {"premium_discount": 1}
    # Nothing is lost between the groups.
    assert sum(g.symbols_scanned for g in groups.values()) == len(rows)
    # A run 30 minutes after a fire time is AD_HOC too (tolerance boundary).
    late = build_funnel([_no_setup(_ts(20, 13, 33))], group_by="window")
    assert [g.key for g in late] == ["AD_HOC"]


def test_build_funnel_by_symbol() -> None:
    groups = {g.key: g for g in build_funnel(_fixture_rows(), group_by="symbol")}
    assert list(groups) == ["BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"]
    btc = groups["BTCUSDT"]
    assert btc.symbols_scanned == 4
    assert btc.scans == 4  # LONDON 20, NY 20, AD_HOC 20, LONDON 21
    assert btc.proposals == 3
    assert btc.gate_rejected == 2
    assert btc.published == 1
    assert btc.rejections_by_rule == {"min_rr": 1, "premium_discount": 1}


def test_funnel_scans_count_invocations_not_rows() -> None:
    # One invocation over four symbols: started_at differs by milliseconds per
    # symbol row (one scan_runs row per symbol) yet it is ONE scan.
    rows = [
        _no_setup(LONDON_20 + timedelta(milliseconds=i), symbol=s)
        for i, s in enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"])
    ]
    (group,) = build_funnel(rows, group_by="day")
    assert group.symbols_scanned == 4
    assert group.scans == 1


def test_build_funnel_empty() -> None:
    assert build_funnel([], group_by="day") == []


# ---------------------------------------------------------------------------
# top_rejections
# ---------------------------------------------------------------------------


def test_top_rejections_order() -> None:
    rows = [
        _gate(_ts(20, 8, 3), "min_rr"),
        _gate(_ts(20, 13, 3), "session"),
        _gate(_ts(20, 15, 3), "session"),
        _gate(_ts(20, 22, 3), "premium_discount"),
        _gate(_ts(21, 8, 3), "premium_discount"),
        _gate(_ts(10, 8, 3), "max_concurrent"),  # before `since`
        _published(_ts(21, 8, 3)),  # not a rejection
    ]
    assert top_rejections(rows, since=_ts(15, 0)) == [
        ("premium_discount", 2),
        ("session", 2),
        ("min_rr", 1),
    ]
    assert top_rejections(rows, since=_ts(1, 0))[-1] == ("min_rr", 1)
    assert ("max_concurrent", 1) in top_rejections(rows, since=_ts(1, 0))


# ---------------------------------------------------------------------------
# SQL + route (fake reader)
# ---------------------------------------------------------------------------


def _wire(row: DecisionRow) -> dict[str, Any]:
    """A `DecisionRow` as the Data API returns it (strings / bools)."""
    return {
        "id": str(row.id),
        "created_at": row.created_at.isoformat(),
        "symbol": row.symbol,
        "scan_started_at": row.scan_started_at.isoformat(),
        "decision_kind": row.decision_kind.value,
        "judge_ruling": row.judge_ruling,
        "violated_rule": row.violated_rule,
        "has_setup": row.has_setup,
    }


def _params(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    """Data API parameter list -> {name: python value}."""
    out: dict[str, Any] = {}
    for p in parameters:
        value = p["value"]
        if value.get("isNull"):
            out[p["name"]] = None
        elif "stringValue" in value:
            out[p["name"]] = value["stringValue"]
        else:
            out[p["name"]] = value["longValue"]
    return out


@dataclass
class FunnelReader:
    """Serves the funnel statement only: `since` / `until`, ascending keyset, page limit."""

    rows: list[DecisionRow] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def read_rows(self, sql: str, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        params = _params(parameters)
        self.calls.append((sql, params))
        if "AS scan_started_at" not in sql:
            raise AssertionError(f"unexpected SQL: {sql}")
        since = datetime.fromisoformat(params["since"])
        until = datetime.fromisoformat(params["until"])
        selected = sorted(
            (r for r in self.rows if since <= r.created_at < until),
            key=lambda r: (r.created_at, str(r.id)),
        )
        if params["after_ts"] is not None:
            after = (datetime.fromisoformat(params["after_ts"]), params["after_id"])
            selected = [r for r in selected if (r.created_at, str(r.id)) > after]
        return [_wire(r) for r in selected[: params["page_limit"]]]


@dataclass
class PagedReader:
    """Returns canned pages in order; records each call's parameters."""

    pages: list[list[dict[str, Any]]]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def read_rows(self, sql: str, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.calls.append(_params(parameters))
        return self.pages[len(self.calls) - 1]


_PAGED_SQL = (
    f"SELECT 1 FROM signals s WHERE {ASC_KEYSET_PREDICATE} {ASC_KEYSET_ORDER} {PAGE_LIMIT_SQL}"
)


def _page(n: int, *, start: datetime) -> list[dict[str, Any]]:
    return [
        {"id": str(uuid4()), "created_at": (start + timedelta(seconds=i)).isoformat()}
        for i in range(n)
    ]


def test_read_all_pages_advances_keyset_and_stops() -> None:
    first = _page(1000, start=_ts(1, 0))
    second = _page(1000, start=_ts(2, 0))
    third = _page(3, start=_ts(3, 0))
    reader = PagedReader(pages=[first, second, third])
    since = _str_param("since", "x")
    rows = asyncio.run(read_all_pages(reader, _PAGED_SQL, [since]))
    assert len(rows) == 2003
    assert rows == [*first, *second, *third]
    assert len(reader.calls) == 3
    assert all(c["page_limit"] == 1000 and c["since"] == "x" for c in reader.calls)
    assert reader.calls[0]["after_ts"] is None
    assert reader.calls[0]["after_id"] is None
    for call, previous in zip(reader.calls[1:], (first, second), strict=True):
        assert datetime.fromisoformat(call["after_ts"]) == datetime.fromisoformat(
            previous[-1]["created_at"]
        )
        assert call["after_id"] == previous[-1]["id"]


def test_read_all_pages_single_short_page() -> None:
    reader = PagedReader(pages=[_page(3, start=_ts(1, 0))])
    rows = asyncio.run(read_all_pages(reader, _PAGED_SQL, []))
    assert len(rows) == 3
    assert len(reader.calls) == 1
    # An exactly-empty first page is a single call too.
    empty = PagedReader(pages=[[]])
    assert asyncio.run(read_all_pages(empty, _PAGED_SQL, [])) == []
    assert len(empty.calls) == 1


def test_read_all_pages_guard_raises() -> None:
    reader = PagedReader(pages=[_page(2, start=_ts(d, 0)) for d in range(1, 5)])
    with pytest.raises(RuntimeError, match="result too large: more than 6 rows"):
        asyncio.run(read_all_pages(reader, _PAGED_SQL, [], page_size=2, max_pages=3))
    assert len(reader.calls) == 3
    # A statement without the keyset / page limit is refused before any call.
    with pytest.raises(ValueError, match="keyset"):
        asyncio.run(read_all_pages(reader, "SELECT 1", []))
    # ... and so is one whose ORDER BY is not the ascending keyset order.
    descending = _PAGED_SQL.replace(ASC_KEYSET_ORDER, "ORDER BY s.created_at DESC, s.id DESC")
    with pytest.raises(ValueError, match=re.escape(ASC_KEYSET_ORDER)):
        asyncio.run(read_all_pages(reader, descending, []))
    assert len(reader.calls) == 3


def test_read_all_pages_ties_on_created_at() -> None:
    # Three rows share one created_at: the id half of the keyset must carry the
    # second page past the first without skipping or repeating a row.
    at = _ts(20, 8, 4)
    rows = [
        _row(DecisionKind.NO_SETUP, scan_at=LONDON_20, created_at=at, symbol=s)
        for s in ("A", "B", "C")
    ]
    reader = FunnelReader(rows=rows)
    params = [
        _str_param("since", _ts(20, 0).isoformat()),
        _str_param("until", _ts(21, 0).isoformat()),
    ]
    got = asyncio.run(read_all_pages(reader, _DECISIONS_BETWEEN_SQL, params, page_size=2))
    assert [r["id"] for r in got] == sorted(str(r.id) for r in rows)
    assert len(reader.calls) == 2
    assert reader.calls[1][1]["after_id"] == got[1]["id"]


def test_decisions_between_pages_through_fake_reader() -> None:
    # 2 500 rows over three pages come back whole and in order.
    rows = [_no_setup(_ts(1, 0) + timedelta(seconds=i)) for i in range(2500)]
    reader = FunnelReader(rows=rows)
    got = asyncio.run(list_decisions_between(reader, since=_ts(1, 0), until=_ts(30, 0)))
    assert [r.id for r in got] == [r.id for r in rows]
    assert len(reader.calls) == 3


def test_decisions_between_sql_shape_minimal_columns() -> None:
    reader = FunnelReader(rows=_fixture_rows())
    rows = asyncio.run(list_decisions_between(reader, since=_ts(20, 0), until=_ts(21, 0)))
    assert len(rows) == 7
    # The four LONDON rows share created_at (ties order by id): compare as a set.
    assert {r.decision_kind for r in rows[:4]} == set(DecisionKind)
    assert {r.scan_started_at for r in rows[:4]} == {LONDON_20}

    ((sql, params),) = reader.calls
    assert params == {
        "since": _ts(20, 0).isoformat(),
        "until": _ts(21, 0).isoformat(),
        "after_ts": None,
        "after_id": None,
        "page_limit": 1000,
    }
    # Minimal columns (Data API 1 MB cap): no whole JSONB, no journal extras.
    for forbidden in ("payload::text", "output::text", "features", "outcome_metadata", "SELECT *"):
        assert forbidden not in sql
    assert "decision_resolutions" not in sql
    assert "s.payload->>'violated_rule' AS violated_rule" in sql
    # One statement: scan join, LATERAL judge, EXISTS setup probe, bound range.
    assert "JOIN scan_runs r ON r.id = s.scan_id" in sql
    assert "LEFT JOIN LATERAL" in sql
    assert "EXISTS (SELECT 1 FROM active_setups t WHERE t.signal_id = s.id) AS has_setup" in sql
    assert "s.created_at >= :since::timestamptz AND s.created_at < :until::timestamptz" in sql
    assert "ORDER BY s.created_at ASC, s.id ASC" in sql
    # Paged in keyset chunks (Data API 1 MB cap at 365 days).
    assert (
        "(:after_ts::timestamptz IS NULL OR "
        "(s.created_at, s.id) > (:after_ts::timestamptz, :after_id::uuid))"
    ) in sql
    assert sql.rstrip().endswith("LIMIT :page_limit")
    # Every value is bound: the timestamps never appear in the SQL text.
    assert "2026-10" not in sql


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(assembly, "utcnow", lambda: NOW)
    return NOW


def _client(reader: FunnelReader) -> TestClient:
    app = create_app(version="test")
    app.dependency_overrides[get_store] = lambda: reader
    return TestClient(app)


def test_funnel_route_defaults_and_422(frozen_clock: datetime) -> None:
    reader = FunnelReader(rows=_fixture_rows())
    client = _client(reader)

    # Defaults: the last 30 days, grouped by day; one fetch serves both tables.
    response = client.get("/api/funnel")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["group_by"] == "day"
    assert datetime.fromisoformat(body["until"]) == NOW
    assert datetime.fromisoformat(body["since"]) == NOW - timedelta(days=30)
    assert [g["key"] for g in body["groups"]] == ["2026-10-20", "2026-10-21"]
    assert body["totals"]["key"] == "total"
    assert body["totals"]["symbols_scanned"] == 9
    assert body["totals"]["scans"] == 4
    assert body["totals"]["rejections_by_rule"] == {"premium_discount": 2, "min_rr": 1}
    assert body["top_rejections_7d"] == [
        {"rule": "premium_discount", "count": 2},
        {"rule": "min_rr", "count": 1},
    ]
    assert body["top_rejections_30d"] == body["top_rejections_7d"]
    assert len(reader.calls) == 1

    # A short window: the groups cover only the requested range; the rejection
    # tables come from one extra fetch of the last 30 days.
    reader.calls.clear()
    response = client.get(
        "/api/funnel",
        params={"since": "2026-10-21T00:00:00Z", "until": "2026-10-21T12:00:00Z"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [g["key"] for g in body["groups"]] == ["2026-10-21"]
    assert body["totals"]["symbols_scanned"] == 2
    assert body["top_rejections_30d"][0] == {"rule": "premium_discount", "count": 2}
    (_, requested), (_, recent) = reader.calls
    assert datetime.fromisoformat(requested["since"]) == _ts(21, 0)
    assert datetime.fromisoformat(recent["since"]) == NOW - timedelta(days=30)
    assert datetime.fromisoformat(recent["until"]) == NOW

    # group_by=window keeps AD_HOC; naive timestamps are read as UTC.
    response = client.get(
        "/api/funnel",
        params={"since": "2026-10-01T00:00:00", "group_by": "window"},
    )
    assert response.status_code == 200, response.text
    assert [g["key"] for g in response.json()["groups"]] == ["LONDON", "NY", "AD_HOC"]

    # Exactly 365 days is allowed; one second more is 422, as is an empty / inverted range
    # and an unknown grouping. None of them touches the database.
    ok = client.get(
        "/api/funnel",
        params={"since": "2025-10-21T12:00:00Z", "until": "2026-10-21T12:00:00Z"},
    )
    assert ok.status_code == 200, ok.text
    reader.calls.clear()
    for params in (
        {"since": "2025-10-21T11:59:59Z", "until": "2026-10-21T12:00:00Z"},
        {"since": "2020-01-01T00:00:00Z"},
        {"since": "2026-10-21T12:00:00Z", "until": "2026-10-21T12:00:00Z"},
        {"since": "2026-10-21T12:00:00Z", "until": "2026-10-20T12:00:00Z"},
        {"group_by": "week"},
    ):
        response = client.get("/api/funnel", params=params)
        assert response.status_code == 422, (params, response.text)
    assert reader.calls == []


def test_funnel_top_rejections_anchored_to_now(frozen_clock: datetime) -> None:
    """A historical range keeps the rejection tables on the last 7 / 30 days of NOW."""
    old_scan = NOW - timedelta(days=70)
    old = [_gate(old_scan, "max_concurrent"), _gate(old_scan, "max_concurrent")]
    recent = [
        _gate(NOW - timedelta(days=20), "min_rr"),  # in the 30d table only
        _gate(NOW - timedelta(days=2), "session"),  # in both tables
    ]
    reader = FunnelReader(rows=[*old, *recent])
    client = _client(reader)

    until = NOW - timedelta(days=60)
    response = client.get(
        "/api/funnel",
        params={"since": (until - timedelta(days=30)).isoformat(), "until": until.isoformat()},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"]["rejections_by_rule"] == {"max_concurrent": 2}
    assert body["top_rejections_7d"] == [{"rule": "session", "count": 1}]
    assert body["top_rejections_30d"] == [
        {"rule": "min_rr", "count": 1},
        {"rule": "session", "count": 1},
    ]
    # Two fetches: the requested range, then exactly [now - 30d, now).
    (_, requested), (_, last_30d) = reader.calls
    assert datetime.fromisoformat(requested["until"]) == until
    assert datetime.fromisoformat(last_30d["since"]) == NOW - timedelta(days=30)
    assert datetime.fromisoformat(last_30d["until"]) == NOW
