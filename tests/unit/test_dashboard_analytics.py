"""Slice 4 Step 4.3 -- P6 analytics + Critic reports / reviews (design §4.2 P6, §5.2a).

The pure aggregations (`analytics.py`) are tested on `OutcomeRow` lists built
here; the SQL shape and the routes run against a small fake reader that
dispatches on fixed fragments of the `queries.py` statements and records the
bound parameters.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import overview as assembly
from src.dashboard.api.analytics import (
    EquityCurves,
    SkepticBucket,
    SourceCounts,
    effective_outcome,
    equity_curves,
    skeptic_value,
    source_counts,
    win_rate_by,
)
from src.dashboard.api.app import create_app
from src.dashboard.api.classify import DecisionKind
from src.dashboard.api.cursor import decode_cursor, encode_cursor
from src.dashboard.api.deps import get_store
from src.dashboard.api.queries import (
    _OUTCOME_ROWS_SQL,
    ASC_KEYSET_ORDER,
    ASC_KEYSET_PREDICATE,
    PAGE_LIMIT_SQL,
    OutcomeRow,
    list_outcome_rows,
    list_reviews_page,
)

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)


def _ts(day: int, hour: int = 8, minute: int = 4) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def _o(
    kind: DecisionKind,
    *,
    created_at: datetime | None = None,
    tags: tuple[str, ...] = ("bos",),
    session: str | None = "LONDON",
    strategy: str = "smc_v1",
    judge: str | None = None,
    rr: float | None = 3.0,
    signal_outcome: str | None = None,
    resolution: str | None = None,
    realized_r: float | None = None,
    severity: str | None = None,
    against: bool | None = None,
    no_macro: bool = False,
) -> OutcomeRow:
    if judge is None and kind is DecisionKind.PUBLISHED:
        judge = "PUBLISH"
    if judge is None and kind is DecisionKind.VETOED:
        judge = "SKIP"
    return OutcomeRow(
        id=uuid4(),
        created_at=created_at if created_at is not None else _ts(20),
        symbol="BTCUSDT",
        strategy=strategy,
        session=session,
        tags=list(tags),
        decision_kind=kind,
        judge_ruling=judge,
        risk_reward_ratio=rr if kind is not DecisionKind.GATE_REJECTED else None,
        signal_outcome=signal_outcome,
        resolution_outcome=resolution,
        realized_r=realized_r,
        skeptic_severity=severity,
        skeptic_recommends_against=against,
        skeptic_is_no_macro=no_macro,
    )


P = DecisionKind.PUBLISHED
V = DecisionKind.VETOED
G = DecisionKind.GATE_REJECTED


# ---------------------------------------------------------------------------
# effective outcome + win rate
# ---------------------------------------------------------------------------


def test_effective_outcome_prefers_resolution() -> None:
    both = _o(P, signal_outcome="WIN", resolution="LOSS", realized_r=-1.0)
    assert effective_outcome(both) == ("LOSS", "replayed")
    forecaster = _o(P, signal_outcome="WIN")
    assert effective_outcome(forecaster) == ("WIN", "forecaster")
    assert effective_outcome(_o(P)) == (None, None)
    # A resolution row wins even when it is not decisive (no fallback to signals.outcome).
    unresolvable = _o(P, signal_outcome="WIN", resolution="UNRESOLVABLE", realized_r=0.0)
    assert effective_outcome(unresolvable) == ("UNRESOLVABLE", "replayed")


def test_win_rate_by_tag_unnests() -> None:
    rows = [
        _o(P, tags=("bos", "fvg"), signal_outcome="WIN"),
        _o(P, tags=("bos",), resolution="LOSS", realized_r=-1.0),
        _o(P, tags=(), signal_outcome="WIN"),  # untagged: no tag bucket
        # Not PUBLISHED: never in the published win rate, even with a resolution.
        _o(V, tags=("bos",), resolution="WIN", realized_r=3.0),
        _o(G, tags=("fvg",), resolution="WIN", realized_r=3.0),
    ]
    buckets = {b.key: b for b in win_rate_by(rows, by="tag")}
    assert list(buckets) == ["bos", "fvg"]
    bos, fvg = buckets["bos"], buckets["fvg"]
    assert (bos.wins, bos.losses, bos.decisive, bos.n, bos.win_rate) == (1, 1, 2, 2, 0.5)
    assert (fvg.wins, fvg.losses, fvg.decisive, fvg.n, fvg.win_rate) == (1, 0, 1, 1, 1.0)


def test_win_rate_by_session() -> None:
    rows = [
        _o(P, session="LONDON", signal_outcome="WIN"),
        _o(P, session="LONDON", signal_outcome="WIN"),
        _o(P, session="LONDON", signal_outcome="LOSS"),
        _o(P, session="NY", signal_outcome="LOSS"),
        _o(P, session=None, signal_outcome="WIN"),
    ]
    buckets = {b.key: b for b in win_rate_by(rows, by="session")}
    assert list(buckets) == ["LONDON", "NY", "UNKNOWN"]
    assert buckets["LONDON"].win_rate == pytest.approx(2 / 3)
    assert buckets["NY"].win_rate == 0.0
    assert buckets["UNKNOWN"].wins == 1
    strategies = win_rate_by([*rows, _o(P, strategy="alt", signal_outcome="WIN")], by="strategy")
    assert [(b.key, b.n) for b in strategies] == [("alt", 1), ("smc_v1", 5)]


def test_win_rate_by_time_iso_week() -> None:
    rows = [
        _o(P, created_at=_ts(25, 23), signal_outcome="LOSS"),  # Sunday: still W43
        _o(P, created_at=_ts(20), signal_outcome="WIN"),  # Tuesday W43
        _o(P, created_at=_ts(26, 0, 1), signal_outcome="WIN"),  # Monday: W44
        _o(P, created_at=datetime(2027, 1, 1, 9, tzinfo=UTC), signal_outcome="WIN"),
    ]
    buckets = win_rate_by(rows, by="time")
    assert [b.key for b in buckets] == ["2026-W43", "2026-W44", "2026-W53"]
    assert (buckets[0].wins, buckets[0].losses, buckets[0].win_rate) == (1, 1, 0.5)


def test_win_rate_none_when_no_decisive() -> None:
    """Review Focus 1: zero decisive outcomes -> win_rate null (never 0.0), no division."""
    rows = [
        _o(P, signal_outcome="BREAKEVEN"),
        _o(P, signal_outcome="EXPIRED"),
        _o(P, resolution="EXPIRED", realized_r=0.0),
        _o(P, resolution="UNRESOLVABLE", realized_r=0.0),
        _o(P),  # unresolved
    ]
    for by in ("tag", "session", "strategy", "time"):
        (bucket,) = win_rate_by(rows, by=by)  # type: ignore[arg-type]
        assert bucket.decisive == 0
        assert bucket.win_rate is None
        assert bucket.n == 5
    assert win_rate_by([], by="tag") == []


# ---------------------------------------------------------------------------
# Skeptic value
# ---------------------------------------------------------------------------


def test_skeptic_value_buckets_and_no_macro_excluded() -> None:
    rows = [
        # respected (SKIP): outcomes only from the vetoed counterfactual replay
        _o(V, severity="HIGH", against=True, resolution="LOSS", realized_r=-1.0),
        _o(V, severity="HIGH", against=True, resolution="WIN", realized_r=3.0),
        _o(V, severity="HIGH", against=True),  # no resolution: counted, no outcome
        # overruled with caveat
        _o(
            P,
            judge="PUBLISH_WITH_CAVEAT",
            severity="HIGH",
            against=False,
            signal_outcome="WIN",
        ),
        # overruled
        _o(P, severity="MEDIUM", against=True, resolution="LOSS", realized_r=-1.0),
        _o(P, severity="HIGH", against=True, signal_outcome="LOSS"),
        # NoMacroData runs: excluded, counted beside the chart
        _o(P, no_macro=True, signal_outcome="WIN"),
        _o(V, no_macro=True, resolution="WIN", realized_r=3.0),
        # no Skeptic run at all (Slice-1 era) / gate rejection: ignored
        _o(P, signal_outcome="WIN"),
        _o(G, resolution="WIN", realized_r=3.0),
    ]
    value = skeptic_value(rows)
    assert value.respected == SkepticBucket(n=3, wins=1, losses=1, win_rate=0.5)
    assert value.overruled_with_caveat == SkepticBucket(n=1, wins=1, losses=0, win_rate=1.0)
    assert value.overruled == SkepticBucket(n=2, wins=0, losses=2, win_rate=0.0)
    assert value.no_macro_data_excluded == 2
    # Sources of the outcomes that landed in a bucket: 3 replayed, 2 forecaster.
    assert value.source_counts == SourceCounts(replayed=3, forecaster=2)


def test_skeptic_counts_only_high_or_recommends_against() -> None:
    rows = [
        _o(P, severity="MEDIUM", against=False, signal_outcome="WIN"),  # does not count
        _o(P, severity="LOW", against=False, signal_outcome="WIN"),  # does not count
        _o(P, severity="LOW", against=True, signal_outcome="WIN"),  # counts (against)
        _o(V, severity="HIGH", against=False, resolution="LOSS", realized_r=-1.0),  # counts
        _o(V, severity="MEDIUM", against=None, resolution="WIN", realized_r=3.0),  # no
    ]
    value = skeptic_value(rows)
    assert value.overruled.n == 1
    assert value.respected.n == 1
    assert value.overruled_with_caveat == SkepticBucket(n=0, wins=0, losses=0, win_rate=None)
    assert value.no_macro_data_excluded == 0


# ---------------------------------------------------------------------------
# Equity curves
# ---------------------------------------------------------------------------


def test_equity_curve_r_rules() -> None:
    rows = [
        _o(P, created_at=_ts(20, 9), signal_outcome="LOSS"),  # -1
        _o(P, created_at=_ts(20, 8), signal_outcome="WIN", rr=3.5),  # +RR, first by time
        _o(P, created_at=_ts(20, 10), resolution="WIN", realized_r=2.1, signal_outcome="LOSS"),
        _o(P, created_at=_ts(20, 11), signal_outcome="BREAKEVEN"),  # 0
        _o(P, created_at=_ts(20, 12), signal_outcome="INVALIDATED"),  # 0
        _o(P, created_at=_ts(20, 13), signal_outcome="EXPIRED"),  # 0
        _o(P, created_at=_ts(20, 14), resolution="EXPIRED", realized_r=0.0),  # replayed 0
        _o(P, created_at=_ts(20, 15)),  # unresolved: omitted
        _o(P, created_at=_ts(20, 16), resolution="UNRESOLVABLE", realized_r=0.0),  # omitted
        _o(P, created_at=_ts(20, 17), signal_outcome="WIN", rr=None),  # no R:R: omitted
    ]
    curves = equity_curves(rows)
    assert [(p.r, p.source) for p in curves.published] == [
        (3.5, "forecaster"),
        (-1.0, "forecaster"),
        (2.1, "replayed"),
        (0.0, "forecaster"),
        (0.0, "forecaster"),
        (0.0, "forecaster"),
        (0.0, "replayed"),
    ]
    assert [p.cumulative_r for p in curves.published] == pytest.approx(
        [3.5, 2.5, 4.6, 4.6, 4.6, 4.6, 4.6]
    )
    assert [p.at for p in curves.published] == sorted(p.at for p in curves.published)
    assert curves.vetoed == []
    assert curves.gate_rejected == []


def test_counterfactuals_only_from_resolutions() -> None:
    """Review Focus 3: no resolution row -> not in a counterfactual curve, not in win rates."""
    rows = [
        _o(V, created_at=_ts(20, 8), signal_outcome="WIN"),  # forecaster-only veto: out
        _o(V, created_at=_ts(20, 9), resolution="LOSS", realized_r=-1.0),
        _o(V, created_at=_ts(20, 10), resolution="WIN", realized_r=2.5),
        _o(G, created_at=_ts(20, 8)),  # no resolution: out
        _o(G, created_at=_ts(20, 11), resolution="WIN", realized_r=3.0),
        _o(G, created_at=_ts(20, 12), resolution="UNRESOLVABLE", realized_r=0.0),  # out
        _o(P, created_at=_ts(20, 13), signal_outcome="LOSS"),
    ]
    curves = equity_curves(rows)
    assert [p.r for p in curves.vetoed] == [-1.0, 2.5]
    assert [p.cumulative_r for p in curves.vetoed] == [-1.0, 1.5]
    assert all(p.source == "replayed" for p in [*curves.vetoed, *curves.gate_rejected])
    assert [p.r for p in curves.gate_rejected] == [3.0]
    # Published win rate is untouched by any vetoed / gate-rejected row.
    (bucket,) = win_rate_by(rows, by="strategy")
    assert (bucket.n, bucket.wins, bucket.losses, bucket.win_rate) == (1, 0, 1, 0.0)
    assert curves.source_counts == SourceCounts(replayed=3, forecaster=1)


def test_source_counts() -> None:
    rows = [
        _o(P, signal_outcome="WIN"),
        _o(P, signal_outcome="LOSS", resolution="WIN", realized_r=3.0),
        _o(P, resolution="UNRESOLVABLE", realized_r=0.0),  # replayed, though non-decisive
        _o(P),  # unresolved: no source
        _o(V, resolution="LOSS", realized_r=-1.0),  # not published
    ]
    assert source_counts(rows) == SourceCounts(replayed=2, forecaster=1)
    assert source_counts([]) == SourceCounts(replayed=0, forecaster=0)
    assert equity_curves([]) == EquityCurves(
        published=[], vetoed=[], gate_rejected=[], source_counts=SourceCounts()
    )


# ---------------------------------------------------------------------------
# Fake reader (Data-API-shaped rows) + SQL shape
# ---------------------------------------------------------------------------


def _params(parameters: list[dict[str, Any]]) -> dict[str, Any]:
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


def _wire(row: OutcomeRow) -> dict[str, Any]:
    data = row.model_dump(mode="json")
    data["created_at"] = row.created_at.isoformat()
    return data


def _report(index: int) -> dict[str, Any]:
    return {
        "id": str(UUID(int=index + 1)),
        "period_start": (NOW - timedelta(days=2 * index + 2)).isoformat(),
        "period_end": (NOW - timedelta(days=2 * index)).isoformat(),
        "report": {"summary": f"Report {index} summary, long enough to be valid.", "x": 1},
        "markdown": f"# Report {index}\n\nbody",
        "pr_url": None if index else "https://github.com/x/y/pull/1",
        "created_at": (NOW - timedelta(days=2 * index)).isoformat(),
    }


def _review(index: int) -> dict[str, Any]:
    return {
        "signal_id": str(UUID(int=1000 + index)),
        "decision_kind": "PUBLISHED",
        "failure_category": "NONE" if index % 2 else None,
        "lesson": f"lesson {index}" if index % 2 else None,
        "judge_correct": True if index % 2 else None,
        "gate_correct": None,
        "skeptic_vindicated": "NO" if index % 2 else None,
        "confidence": "HIGH" if index % 2 else None,
        "error": None if index % 2 else "anthropic 529",
        # Two reviews share each created_at: the signal_id half of the keyset matters.
        "created_at": (NOW - timedelta(hours=index // 2)).isoformat(),
    }


@dataclass
class AnalyticsReader:
    """Serves the outcome-rows, Critic-report and review statements."""

    rows: list[OutcomeRow] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def read_rows(self, sql: str, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        params = _params(parameters)
        self.calls.append((sql, params))
        if "AS skeptic_severity" in sql:
            return self._outcomes(params)
        if "FROM critic_reports" in sql:
            return self._reports(sql, params)
        if "FROM decision_reviews" in sql:
            return self._reviews(params)
        raise AssertionError(f"unexpected SQL: {sql}")

    def _outcomes(self, params: dict[str, Any]) -> list[dict[str, Any]]:
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

    def _reports(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        ordered = sorted(self.reports, key=lambda r: (r["created_at"], r["id"]), reverse=True)
        if "id" in params:  # detail
            found = [r for r in ordered if r["id"] == params["id"]]
            return [{**r, "report": json.dumps(r["report"])} for r in found]
        return [
            {k: v for k, v in r.items() if k not in ("report", "markdown")}
            | {"summary": r["report"].get("summary")}
            for r in ordered[: params["limit"]]
        ]

    def _reviews(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        def key(r: dict[str, Any]) -> tuple[datetime, str]:
            return datetime.fromisoformat(r["created_at"]), r["signal_id"]

        ordered = sorted(self.reviews, key=key, reverse=True)
        if params["after_ts"] is not None:
            after = (datetime.fromisoformat(params["after_ts"]), params["after_id"])
            ordered = [r for r in ordered if key(r) < after]
        return ordered[: params["limit"]]


def test_outcome_rows_sql_shape() -> None:
    rows = [
        _o(P, created_at=_ts(20, 8), signal_outcome="WIN", severity="HIGH", against=True),
        _o(V, created_at=_ts(20, 9), resolution="LOSS", realized_r=-1.0, no_macro=True),
    ]
    reader = AnalyticsReader(rows=rows)
    got = asyncio.run(list_outcome_rows(reader, since=_ts(20, 0), until=_ts(21, 0)))
    assert got == rows

    ((sql, params),) = reader.calls
    assert sql == _OUTCOME_ROWS_SQL
    assert params == {
        "since": _ts(20, 0).isoformat(),
        "until": _ts(21, 0).isoformat(),
        "after_ts": None,
        "after_id": None,
        "page_limit": 1000,
    }
    # Paged through read_all_pages (Data API 1 MB cap at 365 days).
    for fragment in (ASC_KEYSET_PREDICATE, ASC_KEYSET_ORDER, PAGE_LIMIT_SQL):
        assert fragment in sql
    assert "s.id::text AS id" in sql
    # Minimal columns: named extracts only, never a whole JSONB document.
    for forbidden in ("payload::text", "output::text", "features", "outcome_metadata", "SELECT *"):
        assert forbidden not in sql
    assert "(s.payload->>'risk_reward_ratio')::double precision AS risk_reward_ratio" in sql
    assert "r.session AS session" in sql
    assert "s.outcome AS signal_outcome" in sql
    # LATERAL Judge + LATERAL Skeptic of the same scan; PK join to the replay.
    assert "a.agent_role = 'judge'" in sql
    assert "a.agent_role = 'skeptic'" in sql
    assert "a.output->>'severity' AS severity" in sql
    assert "(a.output->>'recommends_against')::boolean AS recommends_against" in sql
    assert "(a.output->'provider') IS NOT NULL AS is_no_macro" in sql
    assert "LEFT JOIN decision_resolutions d ON d.signal_id = s.id" in sql
    assert "d.realized_r::double precision AS realized_r" in sql
    # NO_SETUP rows carry nothing analytics uses: filtered server-side.
    assert "s.direction IS NOT NULL OR COALESCE(s.payload->>'violated_rule', '') <> ''" in sql
    assert "s.created_at >= :since::timestamptz AND s.created_at < :until::timestamptz" in sql
    assert "2026-10" not in sql  # every value bound


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(assembly, "utcnow", lambda: NOW)
    return NOW


def _client(reader: AnalyticsReader) -> TestClient:
    app = create_app(version="test")
    app.dependency_overrides[get_store] = lambda: reader
    return TestClient(app)


def _fixture_rows() -> list[OutcomeRow]:
    return [
        _o(P, created_at=_ts(14), tags=("bos",), signal_outcome="WIN", rr=3.0),
        _o(P, created_at=_ts(15), tags=("bos", "fvg"), resolution="LOSS", realized_r=-1.0),
        _o(
            P,
            created_at=_ts(16),
            judge="PUBLISH_WITH_CAVEAT",
            severity="HIGH",
            against=True,
            signal_outcome="WIN",
            rr=4.0,
        ),
        _o(V, created_at=_ts(17), severity="HIGH", against=True, resolution="WIN", realized_r=2.0),
        _o(V, created_at=_ts(18), no_macro=True),
        _o(G, created_at=_ts(19), resolution="LOSS", realized_r=-1.0),
    ]


def test_analytics_routes_defaults_and_422(frozen_clock: datetime) -> None:
    reader = AnalyticsReader(rows=_fixture_rows())
    client = _client(reader)

    response = client.get("/api/analytics/win-rate")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["by"] == "tag"
    assert datetime.fromisoformat(body["until"]) == NOW
    assert datetime.fromisoformat(body["since"]) == NOW - timedelta(days=90)
    assert body["buckets"] == [
        {"key": "bos", "wins": 2, "losses": 1, "decisive": 3, "win_rate": 2 / 3, "n": 3},
        {"key": "fvg", "wins": 0, "losses": 1, "decisive": 1, "win_rate": 0.0, "n": 1},
    ]
    assert body["source_counts"] == {"replayed": 1, "forecaster": 2}
    (_, params) = reader.calls[-1]
    assert datetime.fromisoformat(params["since"]) == NOW - timedelta(days=90)

    weekly = client.get("/api/analytics/win-rate", params={"by": "time"}).json()
    assert [b["key"] for b in weekly["buckets"]] == ["2026-W42"]

    skeptic = client.get("/api/analytics/skeptic-value").json()
    assert skeptic["respected"] == {"n": 1, "wins": 1, "losses": 0, "win_rate": 1.0}
    assert skeptic["overruled_with_caveat"] == {"n": 1, "wins": 1, "losses": 0, "win_rate": 1.0}
    assert skeptic["overruled"] == {"n": 0, "wins": 0, "losses": 0, "win_rate": None}
    assert skeptic["no_macro_data_excluded"] == 1
    assert skeptic["source_counts"] == {"replayed": 1, "forecaster": 1}

    equity = client.get("/api/analytics/equity-curve").json()
    assert [p["cumulative_r"] for p in equity["published"]] == [3.0, 2.0, 6.0]
    assert [p["r"] for p in equity["vetoed"]] == [2.0]
    assert [p["r"] for p in equity["gate_rejected"]] == [-1.0]
    assert equity["source_counts"] == {"replayed": 3, "forecaster": 2}
    assert datetime.fromisoformat(equity["since"]) == NOW - timedelta(days=90)

    # 365 days is allowed; more, empty, inverted or an unknown `by` is 422 with no SQL.
    ok = client.get(
        "/api/analytics/equity-curve",
        params={"since": "2025-10-21T12:00:00Z", "until": "2026-10-21T12:00:00Z"},
    )
    assert ok.status_code == 200, ok.text
    reader.calls.clear()
    for path, params in (
        ("/api/analytics/win-rate", {"since": "2025-10-21T11:59:59Z"}),
        ("/api/analytics/skeptic-value", {"since": "2020-01-01T00:00:00Z"}),
        ("/api/analytics/equity-curve", {"since": "2026-10-21T12:00:00Z"}),
        (
            "/api/analytics/skeptic-value",
            {"since": "2026-10-21T00:00:00Z", "until": "2026-10-20T00:00:00Z"},
        ),
        ("/api/analytics/win-rate", {"by": "symbol"}),
    ):
        response = client.get(path, params=params)
        assert response.status_code == 422, (path, params, response.text)
    assert reader.calls == []


def test_critic_reports_list_omits_markdown(frozen_clock: datetime) -> None:
    reader = AnalyticsReader(reports=[_report(i) for i in range(12)])
    client = _client(reader)

    response = client.get("/api/critic/reports")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 10  # default limit
    assert items[0]["id"] == str(UUID(int=1))  # newest first
    assert items[0]["pr_url"] == "https://github.com/x/y/pull/1"
    assert items[0]["summary"].startswith("Report 0 summary")
    assert set(items[0]) == {"id", "period_start", "period_end", "created_at", "pr_url", "summary"}

    ((sql, params),) = reader.calls
    assert params == {"limit": 10}
    assert "markdown" not in sql
    assert "report::text" not in sql
    assert "report->>'summary' AS summary" in sql
    assert "ORDER BY created_at DESC, id DESC" in sql

    assert len(client.get("/api/critic/reports", params={"limit": 3}).json()["items"]) == 3
    for bad in (0, 51):
        assert client.get("/api/critic/reports", params={"limit": bad}).status_code == 422


def test_critic_report_detail_404(frozen_clock: datetime) -> None:
    reader = AnalyticsReader(reports=[_report(0), _report(1)])
    client = _client(reader)

    response = client.get(f"/api/critic/reports/{UUID(int=2)}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["markdown"] == "# Report 1\n\nbody"
    assert body["report"]["summary"].startswith("Report 1 summary")
    assert body["pr_url"] is None
    (sql, params) = reader.calls[-1]
    assert "markdown" in sql
    assert params == {"id": str(UUID(int=2))}

    missing = client.get(f"/api/critic/reports/{UUID(int=99)}")
    assert missing.status_code == 404
    assert missing.json() == {"reason": "not_found"}
    assert client.get("/api/critic/reports/not-a-uuid").status_code == 422


def test_reviews_page_cursor(frozen_clock: datetime) -> None:
    reviews = [_review(i) for i in range(5)]
    reader = AnalyticsReader(reviews=reviews)
    client = _client(reader)

    first = client.get("/api/critic/reviews", params={"limit": 2})
    assert first.status_code == 200, first.text
    page1 = first.json()
    assert [r["signal_id"] for r in page1["items"]] == [
        str(UUID(int=1001)),
        str(UUID(int=1000)),
    ]
    assert page1["items"][0]["lesson"] == "lesson 1"
    assert page1["items"][0]["judge_correct"] is True
    assert page1["items"][1]["error"] == "anthropic 529"
    assert page1["items"][1]["failure_category"] is None
    assert page1["next_cursor"] is not None
    assert decode_cursor(page1["next_cursor"])[1] == UUID(int=1000)

    (sql, params) = reader.calls[-1]
    assert params == {"after_ts": None, "after_id": None, "limit": 3}  # limit + 1
    assert "review::text" not in sql
    assert "(review->>'judge_correct')::boolean AS judge_correct" in sql
    assert "(created_at, signal_id) < (:after_ts::timestamptz, :after_id::uuid)" in sql
    assert "ORDER BY created_at DESC, signal_id DESC" in sql

    page2 = client.get(
        "/api/critic/reviews", params={"limit": 2, "cursor": page1["next_cursor"]}
    ).json()
    page3 = client.get(
        "/api/critic/reviews", params={"limit": 2, "cursor": page2["next_cursor"]}
    ).json()
    seen = [r["signal_id"] for p in (page1, page2, page3) for r in p["items"]]
    assert sorted(seen) == sorted(r["signal_id"] for r in reviews)
    assert len(set(seen)) == 5
    assert page3["next_cursor"] is None

    # Defaults; limit bounds; a cursor that does not decode is 400 bad_cursor.
    assert len(client.get("/api/critic/reviews").json()["items"]) == 5
    assert reader.calls[-1][1]["limit"] == 51
    assert client.get("/api/critic/reviews", params={"limit": 0}).status_code == 422
    assert client.get("/api/critic/reviews", params={"limit": 201}).status_code == 422
    bad = client.get("/api/critic/reviews", params={"cursor": "%%%"})
    assert bad.status_code == 400
    assert bad.json() == {"reason": "bad_cursor"}


def test_reviews_page_query_direct() -> None:
    reader = AnalyticsReader(reviews=[_review(i) for i in range(5)])
    # Strictly after (NOW, 1001) in DESC order: the tie at NOW continues with 1000.
    rows = asyncio.run(list_reviews_page(reader, limit=10, after=(NOW, UUID(int=1001))))
    assert [r.signal_id.int for r in rows] == [1000, 1003, 1002, 1004]
    assert reader.calls[-1][1]["after_id"] == str(UUID(int=1001))
    assert reader.calls[-1][1]["limit"] == 11
    # The cursor round-trips the review's own keyset.
    token = encode_cursor(rows[0].created_at, rows[0].signal_id)
    assert decode_cursor(token) == (rows[0].created_at, rows[0].signal_id)
    with pytest.raises(ValueError):
        asyncio.run(list_reviews_page(reader, limit=0, after=None))
