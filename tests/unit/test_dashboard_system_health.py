"""Slice 4 Step 4.3 Task 4 -- P7 system health: scans, agents + LLM cost, sweeps, config.

Pure helpers (`system.py`) are tested directly; the routes run through a
SQL-dispatching fake reader (no Data API) and a fake `CloudWatchReader`
(no boto3). The overview alarm strip is pinned here too.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.common.models import ScanStatus
from src.common.schedule import SCAN_WINDOWS
from src.dashboard.api import cloudwatch, system
from src.dashboard.api import overview as assembly
from src.dashboard.api.app import create_app
from src.dashboard.api.cloudwatch import (
    CLOUDWATCH_UNAVAILABLE,
    AlarmState,
    get_cloudwatch_reader,
)
from src.dashboard.api.deps import get_store
from src.dashboard.api.market import get_provider_factory
from src.dashboard.api.queries import ConfigFacts, EvaluationStamp, UsageDay
from src.dashboard.api.settings import DashboardSettings, get_dashboard_settings
from src.persistence.models import StoredScanRun
from tests.unit.test_dashboard_cloudwatch import ALARM, FakeReader, _client_error
from tests.unit.test_dashboard_routes import (
    FakeStore,
    Scenario,
    _client_for,
    _param_values,
    _scenario,
    default_factory,
)

DAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 15, 30, tzinfo=UTC)


def _at(hour: int, minute: int = 0, second: int = 0, *, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)


def _settings(langfuse: str | None = None) -> DashboardSettings:
    return DashboardSettings(
        _env_file=None,  # type: ignore[call-arg]
        db_cluster_arn="arn:aws:rds:ap-south-1:123456789012:cluster:signals",
        db_secret_arn="arn:aws:secretsmanager:ap-south-1:123456789012:secret:x",
        langfuse_base_url=langfuse,
    )


@dataclass
class SqlFake:
    """Dispatches the P7 statements on fixed fragments; records every call."""

    scan_rows: list[dict[str, Any]] = field(default_factory=list)
    usage_rows: list[dict[str, Any]] = field(default_factory=list)
    stamp_rows: list[dict[str, Any]] = field(default_factory=list)
    watchlist_rows: list[dict[str, Any]] = field(default_factory=list)
    model_rows: list[dict[str, Any]] = field(default_factory=list)
    skeptic_rows: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def read_rows(self, sql: str, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        params = _param_values(parameters)
        self.calls.append((sql, params))
        if "UNION ALL" in sql:
            return list(self.usage_rows)
        if "unnest(symbols)" in sql:
            return list(self.watchlist_rows)
        if "token_usage->>'model'" in sql:
            return list(self.model_rows)
        if "agent_role = 'skeptic'" in sql:
            return list(self.skeptic_rows)
        if "FROM setup_evaluations" in sql:
            return list(self.stamp_rows)
        if "FROM scan_runs" in sql:
            return list(self.scan_rows[: params["limit"]])
        raise AssertionError(f"unexpected SQL: {sql}")


def _client(
    fake: SqlFake,
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: DashboardSettings | None = None,
) -> TestClient:
    monkeypatch.setattr(assembly, "utcnow", lambda: NOW)
    app = create_app(version="t")
    app.dependency_overrides[get_store] = lambda: fake
    app.dependency_overrides[get_dashboard_settings] = lambda: settings or _settings()
    app.dependency_overrides[get_cloudwatch_reader] = FakeReader
    return TestClient(app)


def _scan(
    started_at: datetime,
    *,
    completed_after: timedelta | None = timedelta(seconds=90),
    status: ScanStatus = ScanStatus.SUCCESS,
    symbols: list[str] | None = None,
    error: str | None = None,
) -> StoredScanRun:
    return StoredScanRun(
        id=uuid4(),
        started_at=started_at,
        completed_at=None if completed_after is None else started_at + completed_after,
        status=status,
        error_message=error,
        session="LONDON",
        strategy="smc",
        symbols=symbols or ["BTCUSDT"],
    )


def _scan_dict(scan: StoredScanRun) -> dict[str, Any]:
    return {
        "id": str(scan.id),
        "started_at": scan.started_at.isoformat(),
        "completed_at": scan.completed_at.isoformat() if scan.completed_at else None,
        "status": scan.status.value,
        "error_message": scan.error_message,
        "session": scan.session,
        "strategy": scan.strategy,
        "symbols": scan.symbols,
    }


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------


def test_scan_history_window_and_duration() -> None:
    london = _scan(_at(8, 3, 20), symbols=["BTCUSDT", "ETHUSDT"])
    adhoc = _scan(_at(10, 0), completed_after=None, status=ScanStatus.RUNNING, symbols=["SOLUSDT"])
    failed = _scan(
        _at(22, 3, 1),
        completed_after=timedelta(seconds=12.5),
        status=ScanStatus.FAILED,
        error="boom",
    )
    rows = system.scan_history_out([failed, adhoc, london])

    assert [r.window for r in rows] == ["DAILY_WRAP", "AD_HOC", "LONDON"]  # order preserved
    assert rows[0].duration_s == 12.5 and rows[0].status is ScanStatus.FAILED
    assert rows[0].error_message == "boom"
    assert rows[1].duration_s is None and rows[1].completed_at is None
    assert rows[2].duration_s == 90.0
    assert rows[2].symbols == ["BTCUSDT", "ETHUSDT"]
    assert rows[2].id == london.id


def test_scan_history_duration_never_negative() -> None:
    skewed = _scan(_at(8, 3), completed_after=timedelta(seconds=-2))
    (row,) = system.scan_history_out([skewed])
    assert row.duration_s == 0.0


def test_system_scans_route_newest_first_limit_50(monkeypatch: pytest.MonkeyPatch) -> None:
    scans = [_scan(_at(22, 3)), _scan(_at(8, 3))]
    fake = SqlFake(scan_rows=[_scan_dict(s) for s in scans])
    resp = _client(fake, monkeypatch).get("/api/system/scans")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["window"] for i in items] == ["DAILY_WRAP", "LONDON"]
    assert items[0]["duration_s"] == 90.0
    ((sql, params),) = fake.calls
    assert "ORDER BY started_at DESC" in sql and params["limit"] == 50


# ---------------------------------------------------------------------------
# Agents + LLM cost
# ---------------------------------------------------------------------------


def test_cost_per_day_sums_three_sources() -> None:
    d1, d2 = date(2026, 10, 20), date(2026, 10, 21)
    usage = [
        UsageDay(date=d1, role="analyzer", runs=4, errors=0, cost_usd=0.10),
        UsageDay(date=d1, role="judge", runs=1, errors=0, cost_usd=0.05),
        UsageDay(date=d1, role="forecaster", runs=3, errors=1, cost_usd=0.02),  # setup_evaluations
        UsageDay(date=d1, role="critic", runs=2, errors=0, cost_usd=0.30),  # agent_runs
        UsageDay(date=d2, role="analyzer", runs=4, errors=0, cost_usd=0.12),
    ]
    days = system.cost_per_day(usage)
    assert [d.date for d in days] == [d1, d2]
    assert days[0].total_usd == pytest.approx(0.47)
    assert days[0].by_role == pytest.approx(
        {"analyzer": 0.10, "judge": 0.05, "forecaster": 0.02, "critic": 0.30}
    )
    assert days[1].total_usd == pytest.approx(0.12)
    assert days[1].by_role == pytest.approx({"analyzer": 0.12})
    assert system.cost_per_day([]) == []


def test_cost_per_day_ignores_null_errors_and_scan_pseudo_role() -> None:
    # `errors: None` (agent_runs has no failure source) and the cost-free `scan`
    # pseudo-role (scan_runs) leave the totals exactly as the LLM sources make them.
    d1 = date(2026, 10, 20)
    usage = [
        UsageDay(date=d1, role="analyzer", runs=4, errors=None, cost_usd=0.10),
        UsageDay(date=d1, role="scan", runs=4, errors=1, cost_usd=0.0),
        UsageDay(date=d1, role="skeptic", runs=3, errors=None, cost_usd=0.05),
    ]
    (day,) = system.cost_per_day(usage)
    assert day.total_usd == pytest.approx(0.15)
    assert day.by_role == pytest.approx({"analyzer": 0.10, "skeptic": 0.05})
    assert "scan" not in day.by_role
    # A day with scan rows only still appears, at zero spend.
    (quiet,) = system.cost_per_day([UsageDay(date=d1, role="scan", runs=4, errors=4, cost_usd=0)])
    assert quiet.total_usd == 0.0 and quiet.by_role == {}


def test_system_agents_route_reads_three_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SqlFake(
        usage_rows=[
            {"day": "2026-10-20", "role": "analyzer", "runs": 4, "errors": 0, "cost_usd": 0.1},
            {"day": "2026-10-20", "role": "forecaster", "runs": 3, "errors": 1, "cost_usd": 0.02},
            {"day": "2026-10-21", "role": "critic", "runs": 2, "errors": 1, "cost_usd": None},
        ]
    )
    resp = _client(fake, monkeypatch).get("/api/system/agents?days=7")
    assert resp.status_code == 200
    body = resp.json()
    assert datetime.fromisoformat(body["since"]) == _at(0, 0, day=date(2026, 10, 14))
    assert datetime.fromisoformat(body["until"]) == NOW
    assert body["usage"][1] == {
        "date": "2026-10-20",
        "role": "forecaster",
        "runs": 3,
        "errors": 1,
        "cost_usd": 0.02,
    }
    assert body["usage"][2]["cost_usd"] == 0.0  # unpriced -> 0 in the sum
    assert [d["date"] for d in body["cost_per_day"]] == ["2026-10-20", "2026-10-21"]
    assert body["cost_per_day"][0]["total_usd"] == pytest.approx(0.12)

    ((sql, params),) = fake.calls
    for table in ("FROM agent_runs", "FROM setup_evaluations", "FROM decision_reviews"):
        assert table in sql
    assert "cost_usd::double precision" in sql or "::double precision" in sql
    assert "status = 'ERROR'" in sql and "error IS NOT NULL" in sql
    assert "FROM scan_runs" in sql  # the `scan` pseudo-role
    assert datetime.fromisoformat(params["since"]) == _at(0, 0, day=date(2026, 10, 14))
    assert ":since" in sql and "2026" not in sql  # bound, never formatted


def _usage_branches(sql: str) -> dict[str, str]:
    """The UNION ALL branches of the usage statement, keyed by their source table."""
    inner = sql.split("FROM (", 1)[1].rsplit(") u", 1)[0]
    branches: dict[str, str] = {}
    for branch in inner.split("UNION ALL"):
        for table in ("agent_runs", "setup_evaluations", "decision_reviews", "scan_runs"):
            if f"FROM {table}" in branch:
                branches[table] = branch
    return branches


def test_agent_usage_sql_does_not_double_count_critic(monkeypatch: pytest.MonkeyPatch) -> None:
    # A successful review is persisted to BOTH decision_reviews and agent_runs (role
    # critic, same cost_usd; critic/reviewer.py), so decision_reviews may add only its
    # FAILED rows -- one run + one error each, zero cost -- or the Critic counts twice.
    fake = SqlFake()
    assert _client(fake, monkeypatch).get("/api/system/agents").status_code == 200
    ((sql, _),) = fake.calls
    branches = _usage_branches(sql)
    assert set(branches) == {"agent_runs", "setup_evaluations", "decision_reviews", "scan_runs"}

    reviews = branches["decision_reviews"]
    assert "WHERE error IS NOT NULL" in reviews
    assert "0::double precision AS cost_usd" in reviews
    assert "sum(cost_usd)" not in reviews and "cost_usd)" not in reviews
    assert "count(*) AS runs, count(*) AS errors" in reviews

    # agent_runs is the source of runs + cost for every role, the Critic included.
    runs = branches["agent_runs"]
    assert "agent_role AS role" in runs and "sum(cost_usd)" in runs
    assert "critic" not in runs  # no role filter excluding (or special-casing) the Critic
    # The Forecaster has no agent_runs rows; setup_evaluations is its whole source.
    evals = branches["setup_evaluations"]
    assert "'forecaster' AS role" in evals and "sum(cost_usd)" in evals


def test_agent_usage_errors_null_without_failure_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # agent_runs rows exist only on success: claiming 0 errors would be a lie, so the
    # branch reports NULL and the outer sum stays NULL for a (day, role) with only
    # agent_runs rows. Forecaster / Critic failures keep their counts; scan_runs adds
    # the pipeline's own failures as the cost-free `scan` pseudo-role.
    fake = SqlFake()
    assert _client(fake, monkeypatch).get("/api/system/agents").status_code == 200
    ((sql, _),) = fake.calls
    branches = _usage_branches(sql)
    assert "NULL::bigint AS errors" in branches["agent_runs"]
    assert "0::bigint AS errors" not in branches["agent_runs"]
    assert "count(*) FILTER (WHERE status = 'ERROR') AS errors" in branches["setup_evaluations"]
    assert "count(*) AS errors" in branches["decision_reviews"]
    scan = branches["scan_runs"]
    assert "'scan' AS role" in scan
    assert "count(*) AS runs" in scan
    assert "count(*) FILTER (WHERE status = 'FAILED') AS errors" in scan
    assert "0::double precision AS cost_usd" in scan
    assert "started_at >= :since::timestamptz" in scan
    assert "sum(errors)::bigint AS errors" in sql  # NULL-preserving outer aggregate


def test_system_agents_route_errors_null(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SqlFake(
        usage_rows=[
            {"day": "2026-10-20", "role": "scan", "runs": 8, "errors": 1, "cost_usd": 0.0},
            {"day": "2026-10-20", "role": "skeptic", "runs": 3, "errors": None, "cost_usd": 0.05},
        ]
    )
    body = _client(fake, monkeypatch).get("/api/system/agents").json()
    scan, skeptic = body["usage"]
    assert skeptic == {
        "date": "2026-10-20",
        "role": "skeptic",
        "runs": 3,
        "errors": None,
        "cost_usd": 0.05,
    }
    assert scan["errors"] == 1 and scan["runs"] == 8 and scan["cost_usd"] == 0.0
    (day,) = body["cost_per_day"]
    assert day["total_usd"] == pytest.approx(0.05) and day["by_role"] == {"skeptic": 0.05}


@pytest.mark.parametrize("path", ["/api/system/agents", "/api/system/sweeps"])
@pytest.mark.parametrize("days", [0, 31])
def test_system_days_bounded(path: str, days: int, monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _client(SqlFake(), monkeypatch).get(f"{path}?days={days}")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Forecaster sweeps
# ---------------------------------------------------------------------------


def test_forecaster_sweeps_bucketing() -> None:
    yesterday = DAY - timedelta(days=1)
    stamps = [
        EvaluationStamp(evaluated_at=_at(3, 0), is_error=False),  # console run -> yday 22:05
        EvaluationStamp(evaluated_at=_at(13, 5, 30), is_error=False),
        EvaluationStamp(evaluated_at=_at(13, 6), is_error=True),
        EvaluationStamp(evaluated_at=_at(15, 4, 59), is_error=False),  # before 15:05 -> 13:05
        EvaluationStamp(evaluated_at=_at(15, 5), is_error=False),  # exactly 15:05 -> 15:05
    ]
    rows = system.forecaster_sweeps(stamps, since=_at(0, 0), now=NOW)
    assert [(r.sweep_start, r.evaluations, r.errors) for r in rows] == [
        (_at(22, 5, day=yesterday), 1, 0),
        (_at(8, 5), 0, 0),  # scheduled, nothing evaluated: shown as an empty sweep
        (_at(13, 5), 3, 1),
        (_at(15, 5), 1, 0),
    ]
    # No sweep after `now` (22:05 today has not started).
    assert all(r.sweep_start <= NOW for r in rows)


def test_system_sweeps_route(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SqlFake(
        stamp_rows=[
            {"evaluated_at": _at(13, 5, 30).isoformat(), "is_error": False},
            {"evaluated_at": _at(13, 6).isoformat(), "is_error": True},
        ]
    )
    resp = _client(fake, monkeypatch).get("/api/system/sweeps?days=1")
    assert resp.status_code == 200
    body = resp.json()
    sweeps = {s["sweep_start"]: s for s in body["sweeps"]}
    assert sweeps["2026-10-21T13:05:00Z"] == {
        "sweep_start": "2026-10-21T13:05:00Z",
        "evaluations": 2,
        "errors": 1,
    }
    ((sql, params),) = fake.calls
    assert "FROM setup_evaluations" in sql
    assert datetime.fromisoformat(params["since"]) == _at(0, 0, day=date(2026, 10, 20))


# ---------------------------------------------------------------------------
# Config (inferred)
# ---------------------------------------------------------------------------


def test_config_inference_flags_inferred(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SqlFake(
        watchlist_rows=[{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}],
        model_rows=[{"model": "claude-sonnet-4-5"}],
        skeptic_rows=[{"real_objection": False}, {"real_objection": True}],
    )
    client = _client(fake, monkeypatch, settings=_settings("https://langfuse.example"))
    resp = client.get("/api/system/config")
    assert resp.status_code == 200
    body = resp.json()

    assert body["watchlist"]["value"] == ["BTCUSDT", "ETHUSDT"]
    assert body["model"]["value"] == "claude-sonnet-4-5"
    assert body["macro_providers_configured"]["value"] is True
    assert body["langfuse_configured"]["value"] is True
    for key in ("watchlist", "model", "macro_providers_configured", "langfuse_configured"):
        assert body[key]["inferred"] is True, key
        assert body[key]["source"], key
    # The dashboard's OWN setting -- labelled as such.
    assert "dashboard" in body["langfuse_configured"]["source"].lower()

    schedule = body["schedule"]
    assert schedule["inferred"] is False
    assert [w["label"] for w in schedule["windows"]] == [w.label for w in SCAN_WINDOWS]
    assert schedule["windows"][0]["time_utc"] == "08:03"

    _, watch_params = next((s, p) for s, p in fake.calls if "unnest(symbols)" in s)
    assert datetime.fromisoformat(watch_params["since"]) == NOW - timedelta(days=7)
    skeptic_sql, _ = next((s, p) for s, p in fake.calls if "agent_role = 'skeptic'" in s)
    assert "LIMIT 10" in skeptic_sql


def test_config_inference_unknowns() -> None:
    config = system.inferred_config(
        ConfigFacts(watchlist=[], model=None, skeptic_outputs=0, real_objections=0),
        langfuse_configured=False,
    )
    assert config.watchlist.value == [] and config.model.value is None
    assert config.macro_providers_configured.value is None  # no Skeptic output to judge by
    assert config.langfuse_configured.value is False
    only_sentinels = system.inferred_config(
        ConfigFacts(watchlist=["BTCUSDT"], model="m", skeptic_outputs=10, real_objections=0),
        langfuse_configured=False,
    )
    assert only_sentinels.macro_providers_configured.value is False


# ---------------------------------------------------------------------------
# Overview alarm strip
# ---------------------------------------------------------------------------


@pytest.fixture
def overview_scenario(monkeypatch: pytest.MonkeyPatch) -> Scenario:
    from tests.unit.test_dashboard_routes import NOW as ROUTES_NOW

    monkeypatch.setattr(assembly, "utcnow", lambda: ROUTES_NOW)
    return _scenario()


def test_overview_alarm_strip_filled(overview_scenario: Scenario) -> None:
    app, client = _client_for(overview_scenario.store)
    reader = FakeReader(alarms=[ALARM])
    app.dependency_overrides[get_cloudwatch_reader] = lambda: reader
    health = client.get("/api/overview").json()["health"]
    assert health["alarms_note"] is None
    assert health["alarms"] == [
        {
            "name": "ScanFailureAlarm",
            "state": "ALARM",
            "updated_at": "2026-10-21T08:30:00Z",
            "reason": "Threshold Crossed",
        }
    ]
    assert reader.calls == [("describe_alarms", "CryptoSignals-Monitoring-")]


def test_overview_alarm_strip_degrades(overview_scenario: Scenario) -> None:
    app, client = _client_for(overview_scenario.store)
    app.dependency_overrides[get_cloudwatch_reader] = lambda: FakeReader(error=_client_error())
    resp = client.get("/api/overview")
    assert resp.status_code == 200
    health = resp.json()["health"]
    assert health["alarms"] is None and health["alarms_note"] == CLOUDWATCH_UNAVAILABLE


class _SlowReader(FakeReader):
    """Alarms that never arrive inside the budget."""

    async def describe_alarms(self, prefix: str) -> list[AlarmState]:
        self.calls.append(("describe_alarms", prefix))
        await asyncio.sleep(10)
        return [ALARM]  # pragma: no cover - cancelled by the budget


def test_overview_alarm_strip_degrades_on_timeout(
    overview_scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cloudwatch, "ALARM_BUDGET_S", 0.05)
    app, client = _client_for(overview_scenario.store)
    reader = _SlowReader()
    app.dependency_overrides[get_cloudwatch_reader] = lambda: reader
    started = time.perf_counter()
    resp = client.get("/api/overview")
    assert time.perf_counter() - started < 5  # the budget, not the 10 s sleep
    assert resp.status_code == 200
    health = resp.json()["health"]
    assert health["alarms"] is None and health["alarms_note"] == CLOUDWATCH_UNAVAILABLE
    assert reader.calls == [("describe_alarms", "CryptoSignals-Monitoring-")]


@dataclass
class _TimedStore(FakeStore):
    """`FakeStore` whose every SQL read takes ``delay_s`` and is logged to ``events``."""

    delay_s: float = 0.0
    events: list[str] = field(default_factory=list)

    async def read_rows(self, sql: str, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.events.append("db")
        await asyncio.sleep(self.delay_s)
        return await super().read_rows(sql, parameters)


class _LoggedSlowReader(FakeReader):
    def __init__(self, events: list[str], delay_s: float) -> None:
        super().__init__(alarms=[ALARM])
        self.events = events
        self.delay_s = delay_s

    async def describe_alarms(self, prefix: str) -> list[AlarmState]:
        self.events.append("alarms")
        await asyncio.sleep(self.delay_s)
        return await super().describe_alarms(prefix)


def test_overview_fetches_alarms_and_prices_concurrently_with_db(
    overview_scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = 0.6
    monkeypatch.setattr(cloudwatch, "ALARM_BUDGET_S", budget)
    events: list[str] = []
    store = _TimedStore(
        **{f.name: getattr(overview_scenario.store, f.name) for f in fields(FakeStore)}
    )
    store.delay_s, store.events = 0.15, events
    factory = default_factory()
    real_call = factory.__call__

    def logged_factory() -> Any:
        events.append("prices")
        return real_call()

    app, client = _client_for(store, factory)
    app.dependency_overrides[get_provider_factory] = lambda: logged_factory
    reader = _LoggedSlowReader(events, delay_s=10.0)  # never answers: the budget decides
    app.dependency_overrides[get_cloudwatch_reader] = lambda: reader

    started = time.perf_counter()
    resp = client.get("/api/overview")
    elapsed = time.perf_counter() - started

    assert resp.status_code == 200
    body = resp.json()
    assert body["health"]["alarms"] is None  # the budget expired, still 200
    assert body["open_setups"][0]["unrealised_r"] == 0.5  # prices still arrived
    last_db = len(events) - 1 - events[::-1].index("db")
    # Both side fetches start before the store's last read -- not after all of them.
    # (A task starts at the handler's first real suspension, i.e. inside the first read.)
    assert events.index("alarms") <= 1
    assert events.index("alarms") < last_db
    assert events.index("prices") < last_db
    db_total = events.count("db") * store.delay_s
    assert events.count("db") >= 4
    # Concurrent: about max(db, budget), well under the serial db + budget.
    assert elapsed < db_total + budget - 0.2, (elapsed, db_total)


@dataclass
class _FailingScanStore(_TimedStore):
    """The scan-runs read fails with a non-resume Data API error (-> 502 db_error)."""

    async def read_rows(self, sql: str, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if "FROM scan_runs" in sql:
            raise _client_error("BadRequestException")
        return await super().read_rows(sql, parameters)


def test_overview_db_failure_cancels_side_fetches(
    overview_scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A DB error mid-handler must not leave the alarm / price tasks running (they
    # would outlive the request; under filterwarnings=error a destroyed pending task
    # fails the run). The 10 s reader is cancelled, so the 502 comes back at once.
    monkeypatch.setattr(cloudwatch, "ALARM_BUDGET_S", 30.0)
    events: list[str] = []
    store = _FailingScanStore(
        **{f.name: getattr(overview_scenario.store, f.name) for f in fields(FakeStore)}
    )
    store.events = events
    app, client = _client_for(store)
    app.dependency_overrides[get_cloudwatch_reader] = lambda: _LoggedSlowReader(events, 10.0)
    started = time.perf_counter()
    resp = client.get("/api/overview")
    assert time.perf_counter() - started < 5
    assert resp.status_code == 502 and resp.json() == {"reason": "db_error"}
    assert "alarms" in events  # it had started, and was cancelled


def test_alarm_budget_default_is_5s() -> None:
    assert cloudwatch.ALARM_BUDGET_S == 5.0


def test_openapi_lists_system_routes() -> None:
    spec = TestClient(create_app(version="t")).get("/api/openapi.json").json()
    for path in (
        "/api/system/scans",
        "/api/system/agents",
        "/api/system/sweeps",
        "/api/system/alarms",
        "/api/system/metrics",
        "/api/system/config",
    ):
        assert path in spec["paths"], path
    params = {p["name"]: p for p in spec["paths"]["/api/system/agents"]["get"]["parameters"]}
    assert params["days"]["schema"]["default"] == 7
    assert params["days"]["schema"]["maximum"] == 30
