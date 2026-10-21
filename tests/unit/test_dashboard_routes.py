"""Slice 4 Step 4.2 — data routes: `/api/overview`, `/api/setups/open`, `/api/signals[...]`.

`FakeStore` stands in for `DataApiSignalStore`: it serves the dashboard's `read_rows`
seam by dispatching on fixed SQL fragments of `queries.py` (returning Data-API-shaped
rows) and implements the typed methods the routers and `risk_gates.gather_risk_context`
call. The clock is pinned by monkeypatching `src.dashboard.api.overview.utcnow`, so
every age / countdown / session in the responses is deterministic.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agents.critic.models import DecisionKind, Resolution, ResolutionOutcome
from src.common.models import (
    ActiveSetupStatus,
    AgentRole,
    ScanStatus,
    SetupEvaluationStatus,
    SignalDirection,
    SignalStatus,
)
from src.dashboard.api import cloudwatch, deps, market
from src.dashboard.api import overview as assembly
from src.dashboard.api.app import create_app, handler
from src.dashboard.api.cloudwatch import AlarmState, MetricDay, get_cloudwatch_reader
from src.dashboard.api.cursor import decode_cursor, encode_cursor
from src.dashboard.api.deps import get_store
from src.dashboard.api.market import get_provider_factory
from src.dashboard.api.settings import DashboardSettings, get_dashboard_settings
from src.persistence.models import (
    StoredActiveSetup,
    StoredAgentRun,
    StoredSetupEvaluation,
    StoredSignal,
)
from src.persistence.store import DataApiSignalStore
from src.providers.base import (
    DataProvider,
    Kline,
    MarketSnapshot,
    ProviderUnavailableError,
    Timeframe,
)
from tests.unit.test_dashboard_app import _Ctx, _v2_event

DAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 20, 13, 10, 0, tzinfo=UTC)  # NY session; the 13:05 sweep finished 13:08
EVALUATED_AT = datetime(2026, 10, 20, 13, 5, 30, tzinfo=UTC)  # inside the 13:05 sweep
NARRATIVE = "Synthetic fixture narrative for the dashboard route tests, long enough."


def _at(hour: int, minute: int = 0, second: int = 0, *, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake store
# ---------------------------------------------------------------------------


def _param_values(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    """Data API parameter list -> {name: python value}."""
    out: dict[str, Any] = {}
    for p in parameters:
        value = p["value"]
        if value.get("isNull"):
            out[p["name"]] = None
        elif "stringValue" in value:
            out[p["name"]] = value["stringValue"]
        elif "longValue" in value:
            out[p["name"]] = value["longValue"]
        else:  # pragma: no cover - no other types are used by the dashboard queries
            out[p["name"]] = value
    return out


@dataclass
class FakeStore:
    """`DataApiSignalStore` stand-in: SQL-dispatching `read_rows` + the typed methods."""

    signals: dict[UUID, StoredSignal] = field(default_factory=dict)
    recent: list[StoredSignal] = field(default_factory=list)
    open_setups: list[StoredActiveSetup] = field(default_factory=list)
    runs_by_scan: dict[UUID, list[StoredAgentRun]] = field(default_factory=dict)
    resolutions: dict[UUID, Resolution] = field(default_factory=dict)
    evaluations_by_setup: dict[UUID, list[StoredSetupEvaluation]] = field(default_factory=dict)
    setup_card_rows: list[dict[str, Any]] = field(default_factory=list)
    scan_rows: list[dict[str, Any]] = field(default_factory=list)
    journal_rows: list[dict[str, Any]] = field(default_factory=list)
    page_rows: list[dict[str, Any]] = field(default_factory=list)
    eval_count: dict[str, Any] = field(default_factory=lambda: {"total": 0, "errors": 0})
    setup_rows_by_signal: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_rows_by_signal: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    # -- the read seam -----------------------------------------------------

    async def read_rows(self, sql: str, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        params = _param_values(parameters)
        self.calls.append((sql, params))
        # The journal projection's `has_setup` EXISTS probe also mentions
        # `active_setups t`, so dispatch the journal statements first.
        if "string_to_array(:ids" in sql:
            ids = set(params["ids"].split(","))
            return [r for r in self.journal_rows if r["scan_id"] in ids]
        if "LIMIT :limit" in sql:
            return list(self.page_rows[: params["limit"]])
        if "FROM active_setups t" in sql:
            return list(self.setup_card_rows)
        if "FROM scan_runs" in sql:
            since = datetime.fromisoformat(params["since"])
            return [r for r in self.scan_rows if datetime.fromisoformat(r["started_at"]) >= since]
        if "FROM setup_evaluations" in sql:
            return [dict(self.eval_count)]
        if "FROM active_setups" in sql:
            row = self.setup_rows_by_signal.get(params["signal_id"])
            return [row] if row is not None else []
        if "FROM decision_reviews" in sql:
            row = self.review_rows_by_signal.get(params["signal_id"])
            return [row] if row is not None else []
        raise AssertionError(f"unexpected SQL: {sql}")

    def calls_matching(self, fragment: str) -> list[dict[str, Any]]:
        return [params for sql, params in self.calls if fragment in sql]

    # -- typed methods ------------------------------------------------------

    async def list_open_active_setups(self) -> list[StoredActiveSetup]:
        return list(self.open_setups)

    async def get_signal(self, signal_id: UUID) -> StoredSignal | None:
        return self.signals.get(signal_id)

    async def list_recent_signals(
        self, *, limit: int = 50, symbol: str | None = None
    ) -> list[StoredSignal]:
        rows = [s for s in self.recent if symbol is None or s.symbol == symbol]
        return rows[:limit]

    async def list_agent_runs_for_scan(self, scan_id: UUID) -> list[StoredAgentRun]:
        return sorted(self.runs_by_scan.get(scan_id, []), key=lambda r: r.created_at)

    async def get_resolution(self, signal_id: UUID) -> Resolution | None:
        return self.resolutions.get(signal_id)

    async def list_setup_evaluations(
        self, setup_id: UUID, *, limit: int = 50
    ) -> list[StoredSetupEvaluation]:
        rows = sorted(
            self.evaluations_by_setup.get(setup_id, []),
            key=lambda e: e.evaluated_at,
            reverse=True,  # the store returns newest first
        )
        return rows[: max(1, min(limit, 200))]

    async def get_agent_run(self, run_id: UUID) -> StoredAgentRun | None:
        for runs in self.runs_by_scan.values():
            for run in runs:
                if run.id == run_id:
                    return run
        return None


# ---------------------------------------------------------------------------
# Fake market data provider (BinanceProvider is NEVER constructed in tests)
# ---------------------------------------------------------------------------


def _kline(open_time: datetime, close: float) -> Kline:
    return Kline(
        open_time=open_time, open=close, high=close + 1.0, low=close - 1.0, close=close, volume=1.0
    )


def _series(tf: Timeframe, *, end: datetime, count: int, first_close: float = 100.0) -> list[Kline]:
    """`count` ascending candles of ``tf`` whose last one opens one bar before ``end``."""
    step = market.TIMEFRAME_DURATION[tf]
    return [_kline(end - (count - i) * step, first_close + i) for i in range(count)]


@dataclass
class FakeProvider(DataProvider):
    """`DataProvider` double: serves canned candles, records calls, tracks `aclose`."""

    klines: dict[tuple[str, Timeframe], list[Kline]] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    delay_s: float = 0.0
    calls: list[tuple[str, Timeframe, datetime, datetime]] = field(default_factory=list)
    closed: bool = False
    name: str = "fake"

    async def fetch_market_snapshot(
        self,
        symbol: str,
        timeframes: list[Timeframe],
        *,
        limit: int = 200,
        include_derivatives: bool = False,
    ) -> MarketSnapshot:  # pragma: no cover - the dashboard never asks for snapshots
        raise NotImplementedError

    async def fetch_klines(
        self, symbol: str, timeframe: Timeframe, *, start: datetime, end: datetime
    ) -> list[Kline]:
        self.calls.append((symbol, timeframe, start, end))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if symbol in self.errors:
            raise self.errors[symbol]
        return [k for k in self.klines.get((symbol, timeframe), []) if start <= k.open_time < end]

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class FakeProviderFactory:
    """`ProviderFactory` double: builds a fresh `FakeProvider` per call and keeps them all."""

    klines: dict[tuple[str, Timeframe], list[Kline]] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    delay_s: float = 0.0
    built: list[FakeProvider] = field(default_factory=list)

    def __call__(self) -> FakeProvider:
        provider = FakeProvider(klines=self.klines, errors=dict(self.errors), delay_s=self.delay_s)
        self.built.append(provider)
        return provider


def default_factory() -> FakeProviderFactory:
    """BTCUSDT 5m candles up to NOW, last close 101.0 (LONG 100 / 98 -> +0.5 R)."""
    series = _series(Timeframe.M5, end=NOW, count=6, first_close=96.0)  # closes 96..101
    return FakeProviderFactory(klines={("BTCUSDT", Timeframe.M5): series})


def make_settings() -> DashboardSettings:
    return DashboardSettings(
        _env_file=None,  # type: ignore[call-arg]
        db_cluster_arn="arn:aws:rds:ap-south-1:123456789012:cluster:signals",
        db_secret_arn="arn:aws:secretsmanager:ap-south-1:123456789012:secret:x",
        scan_symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
    )


# ---------------------------------------------------------------------------
# Fixture data builders
# ---------------------------------------------------------------------------


def _proposal_payload(scan_id: UUID, symbol: str, direction: SignalDirection) -> dict[str, Any]:
    long = direction is SignalDirection.LONG
    return {
        "proposal_id": str(uuid4()),
        "scan_id": str(scan_id),
        "strategy": "smc",
        "created_at": _at(8, 3, 55).isoformat(),
        "symbol": symbol,
        "direction": direction.value,
        "entry_price": 100.0,
        "stop_loss": 98.0 if long else 102.0,
        "take_profit_1": 106.0 if long else 94.0,
        "take_profit_2": 110.0 if long else 90.0,
        "risk_reward_ratio": 3.0,
        "leverage": 3.0,
        "risk_percent": 1.0,
        "tags": ["fvg"],
        "confluence_narrative": NARRATIVE,
        "features": {"rr": 3.0},
    }


def _skip_payload(scan_id: UUID, symbol: str, violated_rule: str | None) -> dict[str, Any]:
    return {
        "decision_id": str(uuid4()),
        "scan_id": str(scan_id),
        "strategy": "smc",
        "symbol": symbol,
        "reason": "INSUFFICIENT_RR" if violated_rule else "NO_CLEAR_BIAS",
        "details": "gate rejected the proposal" if violated_rule else "no valid structure",
        "violated_rule": violated_rule,
        "created_at": _at(13, 3, 45).isoformat(),
    }


def _signal(
    scan_id: UUID,
    *,
    symbol: str,
    created_at: datetime,
    direction: SignalDirection | None = None,
    violated_rule: str | None = None,
) -> StoredSignal:
    payload = (
        _proposal_payload(scan_id, symbol, direction)
        if direction is not None
        else _skip_payload(scan_id, symbol, violated_rule)
    )
    return StoredSignal(
        id=uuid4(),
        scan_id=scan_id,
        symbol=symbol,
        strategy="smc",
        direction=direction,
        status=SignalStatus.PUBLISHED if direction is not None else SignalStatus.SKIPPED,
        created_at=created_at,
        payload=payload,
        tags=list(payload.get("tags", [])),
        features=dict(payload.get("features", {})),
    )


def _journal_row(
    signal: StoredSignal,
    *,
    kind: DecisionKind,
    session: str | None,
    ruling: str | None = None,
    has_setup: bool = False,
    violated_rule: str | None = None,
    resolution_outcome: str | None = None,
    realized_r: float | None = None,
) -> dict[str, Any]:
    """One row exactly as `read_rows` hands back the (trimmed) journal projection."""
    payload = signal.payload
    return {
        "id": str(signal.id),
        "scan_id": str(signal.scan_id),
        "symbol": signal.symbol,
        "strategy": signal.strategy,
        "direction": signal.direction.value if signal.direction is not None else None,
        "status": signal.status.value,
        "created_at": signal.created_at.isoformat(),
        "tags": list(signal.tags),
        "outcome": signal.outcome.value if signal.outcome is not None else None,
        "entry_price": payload.get("entry_price"),
        "stop_loss": payload.get("stop_loss"),
        "take_profit_1": payload.get("take_profit_1"),
        "take_profit_2": payload.get("take_profit_2"),
        "risk_reward_ratio": payload.get("risk_reward_ratio"),
        "skip_reason": payload.get("reason"),
        "skip_details": payload.get("details"),
        "decision_kind": kind.value,
        "judge_ruling": ruling,
        "session": session,
        "has_setup": has_setup,
        "violated_rule": violated_rule,
        "resolution_outcome": resolution_outcome,
        "realized_r": realized_r,
    }


def _scan_row(
    scan_id: UUID,
    *,
    started_at: datetime,
    symbol: str,
    session: str,
    status: ScanStatus = ScanStatus.SUCCESS,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(scan_id),
        "started_at": started_at.isoformat(),
        "completed_at": (started_at + timedelta(seconds=40)).isoformat(),
        "status": status.value,
        "error_message": error,
        "session": session,
        "strategy": "smc",
        "symbols": [symbol],
    }


def _run(
    scan_id: UUID,
    role: AgentRole,
    output: dict[str, Any],
    *,
    created_at: datetime,
    latency_ms: int = 0,
    cost_usd: float | None = None,
) -> StoredAgentRun:
    return StoredAgentRun(
        id=uuid4(),
        scan_id=scan_id,
        agent_role=role,
        strategy="smc",
        input_hash="hash-" + role.value,
        output=output,
        latency_ms=latency_ms,
        token_usage={"input_tokens": 10} if cost_usd is not None else {},
        cost_usd=cost_usd,
        created_at=created_at,
    )


def _historian_output(direction: SignalDirection) -> dict[str, Any]:
    return {
        "query_proposal_id": str(uuid4()),
        "strategy": "smc",
        "direction": direction.value,
        "session": "LONDON",
        "primary_poi_type": "order_block",
        "sample_size": 1,
        "wins": 1,
        "losses": 0,
        "breakeven": 0,
        "inconclusive": 0,
        "win_rate": 1.0,
        "matches": [
            {
                "signal_id": str(uuid4()),
                "symbol": "ETHUSDT",
                "direction": direction.value,
                "created_at": "2026-10-01T08:03:55+00:00",
                "outcome": "WIN",
                "tags": ["fvg"],
                "tag_overlap": 1,
                "l2_distance": 0.1,
            }
        ],
        "summary": "one precedent, a win",
    }


@dataclass
class Scenario:
    store: FakeStore
    london_scan: UUID
    published: StoredSignal
    setup: StoredActiveSetup
    published_runs: list[StoredAgentRun]
    vetoed: StoredSignal
    vetoed_runs: list[StoredAgentRun]
    ny_published: StoredSignal
    gate_rejected: StoredSignal
    yesterday_wrap_scan: UUID


def _scenario() -> Scenario:
    store = FakeStore()
    two_days_ago = DAY - timedelta(days=2)
    yesterday = DAY - timedelta(days=1)

    # LONDON window, BTCUSDT: published with caveat, OPEN setup, two evaluations.
    s1 = uuid4()
    published = _signal(
        s1, symbol="BTCUSDT", created_at=_at(8, 3, 55), direction=SignalDirection.LONG
    )
    setup = StoredActiveSetup(
        id=uuid4(),
        signal_id=published.id,
        opened_at=_at(8, 4),
        status=ActiveSetupStatus.OPEN,
        last_evaluated_at=EVALUATED_AT,
        latest_evaluation={
            "status": "AT_RISK",
            "reasoning": "price near the stop",
            "outcome": None,
        },
    )
    published_runs = [
        _run(s1, AgentRole.ANALYZER, dict(published.payload), created_at=_at(8, 3, 53)),
        _run(
            s1,
            AgentRole.HISTORIAN,
            _historian_output(SignalDirection.LONG),
            created_at=_at(8, 3, 54),
        ),
        _run(
            s1,
            AgentRole.SKEPTIC,
            {
                "severity": "LOW",
                "recommends_against": False,
                "headline": "USD mildly firm",
                "reasoning": "the dollar index is firm but not trending; equities calm",
                "cited_macro": ["DXY 104.2"],
            },
            created_at=_at(8, 3, 55),
            latency_ms=1500,
            cost_usd=0.01,
        ),
        _run(
            s1,
            AgentRole.JUDGE,
            {
                "ruling": "PUBLISH_WITH_CAVEAT",
                "confidence": "MEDIUM",
                "reasoning": "clean structure; small sample; mild macro headwind",
                "caveat": "USD mildly firm",
            },
            created_at=_at(8, 3, 56),
            latency_ms=2100,
            cost_usd=0.02,
        ),
    ]
    evaluations = [
        StoredSetupEvaluation(
            id=uuid4(),
            setup_id=setup.id,
            signal_id=published.id,
            evaluated_at=_at(8, 4, 30),
            status=SetupEvaluationStatus.STILL_VALID,
            reasoning="on track",
        ),
        StoredSetupEvaluation(
            id=uuid4(),
            setup_id=setup.id,
            signal_id=published.id,
            evaluated_at=EVALUATED_AT,
            status=SetupEvaluationStatus.AT_RISK,
            reasoning="price near the stop",
            latency_ms=900,
            cost_usd=0.005,
        ),
    ]

    # NY window: ETHUSDT published (setup already closed), SOLUSDT gate-rejected.
    s2, s4 = uuid4(), uuid4()
    ny_published = _signal(
        s2, symbol="ETHUSDT", created_at=_at(13, 3, 45), direction=SignalDirection.SHORT
    )
    gate_rejected = _signal(
        s4, symbol="SOLUSDT", created_at=_at(13, 3, 46), violated_rule="RULE_2_MIN_RR"
    )

    # Two days ago: SOLUSDT vetoed by the Judge (NoMacroData), resolved + reviewed by the Critic.
    s3 = uuid4()
    vetoed = _signal(
        s3,
        symbol="SOLUSDT",
        created_at=_at(13, 3, 45, day=two_days_ago),
        direction=SignalDirection.SHORT,
    )
    vetoed_runs = [
        _run(
            s3,
            AgentRole.ANALYZER,
            dict(vetoed.payload),
            created_at=_at(13, 3, 41, day=two_days_ago),
        ),
        _run(
            s3,
            AgentRole.HISTORIAN,
            _historian_output(SignalDirection.SHORT),
            created_at=_at(13, 3, 42, day=two_days_ago),
        ),
        _run(
            s3,
            AgentRole.SKEPTIC,
            {"provider": "skeptic", "reason": "no macro providers configured"},
            created_at=_at(13, 3, 43, day=two_days_ago),
        ),
        _run(
            s3,
            AgentRole.JUDGE,
            {
                "ruling": "SKIP",
                "confidence": "MEDIUM",
                "reasoning": "no macro context and a thin precedent sample",
                "caveat": None,
            },
            created_at=_at(13, 3, 44, day=two_days_ago),
            latency_ms=1800,
            cost_usd=0.02,
        ),
    ]
    store.resolutions[vetoed.id] = Resolution(
        signal_id=vetoed.id,
        decision_kind=DecisionKind.VETOED,
        outcome=ResolutionOutcome.LOSS,
        decided_at=vetoed.created_at,
        entered_at=vetoed.created_at + timedelta(hours=1),
        resolved_at=vetoed.created_at + timedelta(hours=6),
        realized_r=-1.0,
        candles_used=288,
        engine_version="replay-v1",
    )
    store.review_rows_by_signal[str(vetoed.id)] = {
        "signal_id": str(vetoed.id),
        "decision_kind": "VETOED",
        "review": json.dumps({"judge_correct": True, "lesson": "the veto was right"}),
        "model": "claude-sonnet-4-5",
        "token_usage": json.dumps({"input_tokens": 100}),
        "cost_usd": 0.03,
        "error": None,
        "attempts": 1,
        "created_at": _at(22, 30, day=DAY - timedelta(days=1)).isoformat(),
    }

    for sig in (published, ny_published, gate_rejected, vetoed):
        store.signals[sig.id] = sig
    store.recent = [gate_rejected, ny_published, published, vetoed]  # newest first
    store.open_setups = [setup]
    store.runs_by_scan = {s1: published_runs, s3: vetoed_runs}
    store.evaluations_by_setup = {setup.id: evaluations}
    store.setup_card_rows = [
        {
            "setup_id": str(setup.id),
            "signal_id": str(published.id),
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit_1": 106.0,
            "take_profit_2": 110.0,
            "opened_at": setup.opened_at.isoformat(),
            "last_evaluated_at": EVALUATED_AT.isoformat(),
            "latest_evaluation": json.dumps(setup.latest_evaluation),
        }
    ]
    store.setup_rows_by_signal[str(published.id)] = {
        "id": str(setup.id),
        "signal_id": str(published.id),
        "opened_at": setup.opened_at.isoformat(),
        "status": "OPEN",
        "last_evaluated_at": EVALUATED_AT.isoformat(),
        "latest_evaluation": json.dumps(setup.latest_evaluation),
    }
    # Yesterday's DAILY_WRAP ran (review-only: no journal row); it is the last-scan
    # badge overnight and must never show up in today's windows / chips.
    s5 = uuid4()
    store.scan_rows = [
        _scan_row(s3, started_at=_at(13, 3, 40, day=two_days_ago), symbol="SOLUSDT", session="NY"),
        _scan_row(
            s5, started_at=_at(22, 3, 5, day=yesterday), symbol="BTCUSDT", session="COOLDOWN"
        ),
        _scan_row(s1, started_at=_at(8, 3, 52), symbol="BTCUSDT", session="LONDON"),
        _scan_row(s2, started_at=_at(13, 3, 40), symbol="ETHUSDT", session="NY"),
        _scan_row(s4, started_at=_at(13, 3, 41), symbol="SOLUSDT", session="NY"),
    ]
    store.journal_rows = [
        _journal_row(
            gate_rejected,
            kind=DecisionKind.GATE_REJECTED,
            session="NY",
            violated_rule="RULE_2_MIN_RR",
        ),
        _journal_row(
            ny_published,
            kind=DecisionKind.PUBLISHED,
            session="NY",
            ruling="PUBLISH",
            has_setup=True,
        ),
        _journal_row(
            published,
            kind=DecisionKind.PUBLISHED,
            session="LONDON",
            ruling="PUBLISH_WITH_CAVEAT",
            has_setup=True,
        ),
        _journal_row(
            vetoed,
            kind=DecisionKind.VETOED,
            session="NY",
            ruling="SKIP",
            resolution_outcome="LOSS",
            realized_r=-1.0,
        ),
    ]
    store.eval_count = {"total": 1, "errors": 0}
    return Scenario(
        store=store,
        london_scan=s1,
        published=published,
        setup=setup,
        published_runs=published_runs,
        vetoed=vetoed,
        vetoed_runs=vetoed_runs,
        ny_published=ny_published,
        gate_rejected=gate_rejected,
        yesterday_wrap_scan=s5,
    )


def _page_rows(n: int) -> list[dict[str, Any]]:
    """`n` synthetic journal rows, newest first, distinct `(created_at, id)`."""
    rows = []
    for i in range(n):
        created_at = NOW - timedelta(minutes=i + 1)
        signal = _signal(uuid4(), symbol="BTCUSDT", created_at=created_at)
        rows.append(_journal_row(signal, kind=DecisionKind.NO_SETUP, session="LONDON"))
    return rows


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scenario() -> Scenario:
    return _scenario()


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(assembly, "utcnow", lambda: NOW)
    return NOW


class _NoAlarmsReader:
    """`CloudWatchReader` stand-in for the overview tests: no alarms, no boto3."""

    async def describe_alarms(self, prefix: str) -> list[AlarmState]:
        return []

    async def scan_function_name(self) -> str | None:
        return None

    async def lambda_daily_metrics(
        self, function_name: str, *, days: int = 7, now: datetime
    ) -> list[MetricDay]:
        return []


def _client_for(
    store: FakeStore, factory: FakeProviderFactory | None = None
) -> tuple[FastAPI, TestClient]:
    app = create_app(version="t")
    app.dependency_overrides[get_store] = lambda: store
    provider_factory = factory if factory is not None else default_factory()
    app.dependency_overrides[get_provider_factory] = lambda: provider_factory
    app.dependency_overrides[get_dashboard_settings] = make_settings
    # The overview's alarm strip: never a real boto3 client in tests.
    app.dependency_overrides[get_cloudwatch_reader] = _NoAlarmsReader
    return app, TestClient(app)


@pytest.fixture
def client(scenario: Scenario, frozen_clock: datetime) -> TestClient:
    _, test_client = _client_for(scenario.store)
    return test_client


# ---------------------------------------------------------------------------
# /api/overview
# ---------------------------------------------------------------------------


def test_overview_shape_and_risk_numbers(client: TestClient, scenario: Scenario) -> None:
    resp = client.get("/api/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"generated_at", "health", "risk", "open_setups", "windows_today"}
    assert datetime.fromisoformat(body["generated_at"]) == NOW

    risk = body["risk"]
    assert risk["open_setups"] == 1 and risk["open_cap"] == 3
    assert risk["published_24h"] == 2 and risk["daily_cap"] == 5  # the vetoed row is 2 days old
    assert risk["loss_streak"] == 0 and risk["pause_active"] is False
    assert risk["pause_until"] is None
    assert risk["session"] == "NY" and risk["session_blocked"] is False
    assert datetime.fromisoformat(risk["next_scan_at"]) == _at(15, 3)
    assert risk["seconds_to_next_scan"] == 6780  # 13:10 -> 15:03
    assert risk["seconds_to_next_scan"] > 0

    # Step 4.3: the alarm strip is CloudWatch's answer (the fake reader has none).
    assert body["health"]["alarms"] == []
    assert body["health"]["alarms_note"] is None
    assert body["health"]["db"] == "ok"

    assert len(body["open_setups"]) == 1
    assert body["open_setups"][0]["setup_id"] == str(scenario.setup.id)


def test_overview_health_strip(client: TestClient, scenario: Scenario) -> None:
    body = client.get("/api/overview").json()
    health = body["health"]
    last = health["last_scan"]
    assert last["label"] == "NY"
    assert datetime.fromisoformat(last["started_at"]) == _at(13, 3, 40)
    assert last["status"] == "SUCCESS" and last["error"] is None
    assert last["symbols"] == ["ETHUSDT", "SOLUSDT"]  # the two per-symbol rows of one window

    forecaster = health["forecaster"]
    assert forecaster == {
        "open_setups": 1,
        "stale": 0,  # evaluated 13:05:30, by the 13:05 sweep itself
        "evaluations_last_window": 1,
        "errors_last_window": 0,
    }
    # Scans are fetched from the start of YESTERDAY (so the last-scan badge is never
    # blind overnight); evaluations are counted since the start of the last finished
    # sweep (13:05 + 3 min budget <= 13:10).
    (scan_call,) = scenario.store.calls_matching("FROM scan_runs")
    assert datetime.fromisoformat(scan_call["since"]) == _at(0, 0, day=DAY - timedelta(days=1))
    (count_call,) = scenario.store.calls_matching("FROM setup_evaluations")
    assert datetime.fromisoformat(count_call["since"]) == _at(13, 5)


def test_overview_windows_today(client: TestClient, scenario: Scenario) -> None:
    windows = client.get("/api/overview").json()["windows_today"]
    assert [w["label"] for w in windows] == ["LONDON", "NY", "OVERLAP", "DAILY_WRAP"]
    assert [datetime.fromisoformat(w["fire_time"]) for w in windows] == [
        _at(8, 3),
        _at(13, 3),
        _at(15, 3),
        _at(22, 3),
    ]
    london, ny, overlap, wrap = windows
    assert london["scan"]["status"] == "SUCCESS" and london["scan"]["symbols"] == ["BTCUSDT"]
    assert london["symbols"] == [
        {
            "symbol": "BTCUSDT",
            "kind": "PUBLISHED",
            "violated_rule": None,
            "signal_id": str(scenario.published.id),
        }
    ]
    assert ny["scan"]["symbols"] == ["ETHUSDT", "SOLUSDT"]
    assert [(c["symbol"], c["kind"], c["violated_rule"]) for c in ny["symbols"]] == [
        ("ETHUSDT", "PUBLISHED", None),
        ("SOLUSDT", "GATE_REJECTED", "RULE_2_MIN_RR"),
    ]
    assert overlap["scan"] is None and overlap["symbols"] == []
    assert wrap["scan"] is None and wrap["symbols"] == []
    # Only today's scans reach the chips query: neither the two-day-old scan (outside
    # the fetch window) nor yesterday's wrap (fetched for the last-scan badge only).
    (chips_call,) = scenario.store.calls_matching("string_to_array(:ids")
    assert str(scenario.vetoed.scan_id) not in chips_call["ids"]
    assert str(scenario.yesterday_wrap_scan) not in chips_call["ids"]


def test_overview_stale_across_midnight(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 07:00 UTC, before today's first window: the reference sweep is YESTERDAY's 22:05
    # (a separate :05 invocation), not "no sweep yet". A setup last evaluated at 15:05
    # yesterday missed it (stale); one evaluated at 22:05:40 yesterday did not; a
    # never-evaluated one is stale. Evaluations are counted since yesterday 22:05.
    early = _at(7, 0)
    yesterday = DAY - timedelta(days=1)
    monkeypatch.setattr(assembly, "utcnow", lambda: early)
    missed = StoredActiveSetup(
        id=uuid4(),
        signal_id=scenario.published.id,
        opened_at=_at(8, 4, day=yesterday),
        status=ActiveSetupStatus.OPEN,
        last_evaluated_at=_at(15, 5, 20, day=yesterday),
    )
    never = StoredActiveSetup(
        id=uuid4(),
        signal_id=scenario.published.id,
        opened_at=_at(6, 0),
        status=ActiveSetupStatus.OPEN,
    )
    covered = scenario.setup.model_copy(update={"last_evaluated_at": _at(22, 5, 40, day=yesterday)})
    scenario.store.open_setups = [covered, missed, never]
    # Nothing has run today yet: no scan rows on this UTC day.
    scenario.store.scan_rows = [
        r for r in scenario.store.scan_rows if datetime.fromisoformat(r["started_at"]) <= early
    ]
    base = scenario.store.setup_card_rows[0]
    scenario.store.setup_card_rows = [
        dict(base, last_evaluated_at=_at(22, 5, 40, day=yesterday).isoformat()),
        dict(
            base,
            setup_id=str(missed.id),
            last_evaluated_at=_at(15, 5, 20, day=yesterday).isoformat(),
        ),
        dict(base, setup_id=str(never.id), last_evaluated_at=None, latest_evaluation=None),
    ]
    _, client = _client_for(scenario.store)
    body = client.get("/api/overview").json()
    assert body["health"]["forecaster"]["open_setups"] == 3
    assert body["health"]["forecaster"]["stale"] == 2
    (count_call,) = scenario.store.calls_matching("FROM setup_evaluations")
    assert datetime.fromisoformat(count_call["since"]) == _at(22, 5, day=yesterday)
    # No scan yet today: the badge shows YESTERDAY's wrap, not "none" (overnight blind spot).
    last = body["health"]["last_scan"]
    assert last["label"] == "DAILY_WRAP" and last["symbols"] == ["BTCUSDT"]
    assert datetime.fromisoformat(last["started_at"]) == _at(22, 3, 5, day=yesterday)
    (scan_call,) = scenario.store.calls_matching("FROM scan_runs")
    assert datetime.fromisoformat(scan_call["since"]) == _at(0, 0, day=yesterday)
    # Yesterday's wrap is fetched for the badge only: today's windows stay empty.
    assert all(w["scan"] is None and w["symbols"] == [] for w in body["windows_today"])
    assert body["risk"]["session"] == "ASIAN" and body["risk"]["session_blocked"] is True
    assert datetime.fromisoformat(body["risk"]["next_scan_at"]) == _at(8, 3)


def test_overview_sweep_in_flight_uses_previous_sweep(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 13:06: the 13:05 sweep is still inside its 3-minute budget, so the 08:05 sweep is
    # the reference and a setup it evaluated (08:05:30) is NOT flagged stale.
    in_flight = _at(13, 6)
    monkeypatch.setattr(assembly, "utcnow", lambda: in_flight)
    base = scenario.store.setup_card_rows[0]
    scenario.store.setup_card_rows = [dict(base, last_evaluated_at=_at(8, 5, 30).isoformat())]
    _, client = _client_for(scenario.store)
    body = client.get("/api/overview").json()
    assert body["health"]["forecaster"]["stale"] == 0
    (count_call,) = scenario.store.calls_matching("FROM setup_evaluations")
    assert datetime.fromisoformat(count_call["since"]) == _at(8, 5)


def test_overview_with_nothing_today(frozen_clock: datetime) -> None:
    store = FakeStore()
    _, client = _client_for(store)
    body = client.get("/api/overview").json()
    assert body["health"]["last_scan"] is None
    assert body["health"]["forecaster"] == {
        "open_setups": 0,
        "stale": 0,
        "evaluations_last_window": 0,
        "errors_last_window": 0,
    }
    assert body["open_setups"] == []
    assert all(w["scan"] is None and w["symbols"] == [] for w in body["windows_today"])
    assert body["risk"]["open_setups"] == 0 and body["risk"]["published_24h"] == 0
    # No scans -> the chips query is skipped entirely.
    assert store.calls_matching("string_to_array(:ids") == []


# ---------------------------------------------------------------------------
# /api/setups/open
# ---------------------------------------------------------------------------


def test_setups_open_cards(client: TestClient, scenario: Scenario) -> None:
    resp = client.get("/api/setups/open")
    assert resp.status_code == 200
    (card,) = resp.json()
    assert card == {
        "setup_id": str(scenario.setup.id),
        "signal_id": str(scenario.published.id),
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit_1": 106.0,
        "take_profit_2": 110.0,
        "opened_at": "2026-10-20T08:04:00Z",
        "age_seconds": 5 * 3600 + 6 * 60,  # 08:04 -> 13:10
        "last_evaluated_at": "2026-10-20T13:05:30Z",
        "evaluation_age_seconds": 4 * 60 + 30,
        "latest_status": "AT_RISK",
        "latest_reasoning": "price near the stop",
        "unrealised_r": 0.5,  # LONG entry 100 / stop 98, last 5m close 101
        "unrealised_r_note": None,
    }
    # One query, no typed-method round trips, and the setup list is the only SQL.
    assert [sql for sql, _ in scenario.store.calls if "FROM active_setups t" in sql]
    assert len(scenario.store.calls) == 1


# ---------------------------------------------------------------------------
# Best-effort unrealised R on setup cards (overview + /api/setups/open)
# ---------------------------------------------------------------------------


def test_overview_unrealised_r_present(scenario: Scenario, frozen_clock: datetime) -> None:
    factory = default_factory()
    _, client = _client_for(scenario.store, factory)
    (card,) = client.get("/api/overview").json()["open_setups"]
    assert card["unrealised_r"] == 0.5 and card["unrealised_r_note"] is None
    (setups_card,) = client.get("/api/setups/open").json()
    assert setups_card["unrealised_r"] == 0.5 and setups_card["unrealised_r_note"] is None
    # One provider per request, one latest-price fetch per distinct OPEN-setup symbol:
    # the last 30 minutes of 5m candles, closed afterwards.
    assert len(factory.built) == 2
    for provider in factory.built:
        assert provider.closed
        assert provider.calls == [
            ("BTCUSDT", Timeframe.M5, NOW - timedelta(minutes=30), NOW),
        ]


def test_overview_unrealised_r_present_short(scenario: Scenario, frozen_clock: datetime) -> None:
    base = scenario.store.setup_card_rows[0]
    scenario.store.setup_card_rows = [
        dict(base, symbol="ETHUSDT", direction="SHORT", stop_loss=102.0, take_profit_1=94.0),
        dict(base, setup_id=str(uuid4())),  # a second BTCUSDT card: still one fetch for BTC
    ]
    factory = default_factory()
    factory.klines[("ETHUSDT", Timeframe.M5)] = _series(
        Timeframe.M5, end=NOW, count=3, first_close=95.0
    )  # last close 97 -> SHORT (100 - 97) / (102 - 100) = 1.5
    _, client = _client_for(scenario.store, factory)
    eth, btc = client.get("/api/setups/open").json()
    assert (eth["symbol"], eth["unrealised_r"]) == ("ETHUSDT", 1.5)
    assert (btc["symbol"], btc["unrealised_r"]) == ("BTCUSDT", 0.5)
    (provider,) = factory.built
    assert sorted(c[0] for c in provider.calls) == ["BTCUSDT", "ETHUSDT"]


def test_overview_unrealised_r_degrades_on_provider_error(
    scenario: Scenario, frozen_clock: datetime
) -> None:
    # Review Focus 2: Binance down must not 500 the landing page.
    factory = default_factory()
    factory.errors["BTCUSDT"] = ProviderUnavailableError("binance 503", provider="binance")
    _, client = _client_for(scenario.store, factory)
    resp = client.get("/api/overview")
    assert resp.status_code == 200
    (card,) = resp.json()["open_setups"]
    assert card["unrealised_r"] is None and card["unrealised_r_note"] == "price unavailable"
    resp = client.get("/api/setups/open")
    assert resp.status_code == 200
    (card,) = resp.json()
    assert card["unrealised_r"] is None and card["unrealised_r_note"] == "price unavailable"
    assert all(p.closed for p in factory.built)


def test_overview_unrealised_r_degrades_on_timeout(
    scenario: Scenario, frozen_clock: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review Focus 2: Binance slow -> the 5 s budget (patched down here) expires, 200 + null.
    monkeypatch.setattr(market, "PRICE_BUDGET_S", 0.05)
    factory = default_factory()
    factory.delay_s = 10.0
    _, client = _client_for(scenario.store, factory)
    for path in ("/api/overview", "/api/setups/open"):
        resp = client.get(path)
        assert resp.status_code == 200
        body = resp.json()
        (card,) = body["open_setups"] if path == "/api/overview" else body
        assert card["unrealised_r"] is None and card["unrealised_r_note"] == "price unavailable"
    assert len(factory.built) == 2 and all(p.closed for p in factory.built)


def test_no_open_setups_builds_no_provider(frozen_clock: datetime) -> None:
    factory = default_factory()
    _, client = _client_for(FakeStore(), factory)
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/setups/open").json() == []
    assert factory.built == []


# ---------------------------------------------------------------------------
# /api/signals (journal page)
# ---------------------------------------------------------------------------


def test_signals_page_default_limit_and_next_cursor(client: TestClient, scenario: Scenario) -> None:
    scenario.store.page_rows = _page_rows(51)
    resp = client.get("/api/signals")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"items", "next_cursor"}
    assert len(body["items"]) == 50
    assert [i["id"] for i in body["items"]] == [r["id"] for r in scenario.store.page_rows[:50]]
    last = scenario.store.page_rows[49]
    assert body["next_cursor"] is not None
    assert decode_cursor(body["next_cursor"]) == (
        datetime.fromisoformat(last["created_at"]),
        UUID(last["id"]),
    )
    (call,) = scenario.store.calls_matching("LIMIT :limit")
    assert call["limit"] == 51 and call["after_ts"] is None and call["after_id"] is None
    first = body["items"][0]
    assert first["decision_kind"] == "NO_SETUP" and first["status"] == "SKIPPED"
    assert first["direction"] is None and first["has_setup"] is False
    assert first["skip_reason"] == "NO_CLEAR_BIAS" and first["entry_price"] is None
    assert set(first) == JOURNAL_ROW_KEYS


JOURNAL_ROW_KEYS = {
    "id",
    "scan_id",
    "symbol",
    "strategy",
    "direction",
    "status",
    "created_at",
    "tags",
    "outcome",
    "entry_price",
    "stop_loss",
    "take_profit_1",
    "take_profit_2",
    "risk_reward_ratio",
    "skip_reason",
    "skip_details",
    "decision_kind",
    "judge_ruling",
    "session",
    "has_setup",
    "violated_rule",
    "resolution_outcome",
    "realized_r",
}


def test_signals_page_rows_are_trimmed(client: TestClient, scenario: Scenario) -> None:
    # Design §3.4 (Data API 1 MB cap): a journal row carries the analyzer summary, never
    # the full payload / features / outcome_metadata -- those are on the lifecycle only.
    scenario.store.page_rows = [
        _journal_row(scenario.published, kind=DecisionKind.PUBLISHED, session="LONDON"),
        _journal_row(scenario.gate_rejected, kind=DecisionKind.GATE_REJECTED, session="NY"),
    ]
    published, rejected = client.get("/api/signals").json()["items"]
    assert set(published) == JOURNAL_ROW_KEYS and set(rejected) == JOURNAL_ROW_KEYS
    assert "payload" not in published and "features" not in published
    assert "outcome_metadata" not in published
    assert (published["entry_price"], published["stop_loss"]) == (100.0, 98.0)
    assert (published["take_profit_1"], published["take_profit_2"]) == (106.0, 110.0)
    assert published["risk_reward_ratio"] == 3.0 and published["tags"] == ["fvg"]
    assert published["skip_reason"] is None and published["skip_details"] is None
    assert rejected["skip_reason"] == "INSUFFICIENT_RR"
    assert rejected["skip_details"] == "gate rejected the proposal"
    assert rejected["entry_price"] is None and rejected["risk_reward_ratio"] is None
    assert NARRATIVE not in json.dumps(published)


def test_signals_last_page_has_no_cursor(client: TestClient, scenario: Scenario) -> None:
    scenario.store.page_rows = _page_rows(3)
    body = client.get("/api/signals?limit=3").json()
    assert len(body["items"]) == 3 and body["next_cursor"] is None
    (call,) = scenario.store.calls_matching("LIMIT :limit")
    assert call["limit"] == 4


def test_signals_exactly_limit_rows_has_no_cursor(client: TestClient, scenario: Scenario) -> None:
    scenario.store.page_rows = _page_rows(50)
    body = client.get("/api/signals").json()
    assert len(body["items"]) == 50 and body["next_cursor"] is None


def test_signals_limit_over_200_is_422(client: TestClient, scenario: Scenario) -> None:
    # Design §3.4 says "max 200"; the plan's ruling is to *reject* out-of-range limits with
    # 422 (`Query(ge=1, le=200)`) rather than silently clamp.
    assert client.get("/api/signals?limit=500").status_code == 422
    assert client.get("/api/signals?limit=201").status_code == 422
    assert client.get("/api/signals?limit=0").status_code == 422
    assert scenario.store.calls == []
    assert client.get("/api/signals?limit=200").status_code == 200
    (call,) = scenario.store.calls_matching("LIMIT :limit")
    assert call["limit"] == 201


def test_signals_bad_cursor_is_400(client: TestClient, scenario: Scenario) -> None:
    resp = client.get("/api/signals?cursor=zzz")
    assert resp.status_code == 400
    assert resp.json() == {"reason": "bad_cursor"}
    assert scenario.store.calls == []


def test_signals_cursor_forwarded_to_keyset(client: TestClient, scenario: Scenario) -> None:
    ts, signal_id = _at(9, 0), uuid4()
    resp = client.get("/api/signals", params={"cursor": encode_cursor(ts, signal_id)})
    assert resp.status_code == 200
    (call,) = scenario.store.calls_matching("LIMIT :limit")
    assert call["after_ts"] == ts.isoformat() and call["after_id"] == str(signal_id)


def test_signals_filters_forwarded(client: TestClient, scenario: Scenario) -> None:
    resp = client.get(
        "/api/signals",
        params={
            "since": "2026-10-19T00:00:00+00:00",
            "until": "2026-10-21T00:00:00Z",
            "symbol": "ETHUSDT",
            "kind": "VETOED",
            "outcome": "LOSS",
            "tag": "order_block",
            "session": "OVERLAP",
            "violated_rule": "RULE_3_PREMIUM_DISCOUNT",
            "limit": 20,
        },
    )
    assert resp.status_code == 200
    (call,) = scenario.store.calls_matching("LIMIT :limit")
    assert call["since"] == "2026-10-19T00:00:00+00:00"
    assert call["until"] == "2026-10-21T00:00:00+00:00"
    assert call["symbol"] == "ETHUSDT"
    assert call["decision_kind"] == "VETOED"
    assert call["outcome"] == "LOSS"
    assert call["tag"] == "order_block"
    assert call["session"] == "OVERLAP"
    assert call["violated_rule"] == "RULE_3_PREMIUM_DISCOUNT"
    assert call["limit"] == 21


def test_signals_naive_datetimes_are_read_as_utc(client: TestClient, scenario: Scenario) -> None:
    assert client.get("/api/signals", params={"since": "2026-10-19T00:00:00"}).status_code == 200
    (call,) = scenario.store.calls_matching("LIMIT :limit")
    assert call["since"] == "2026-10-19T00:00:00+00:00"


def test_signals_invalid_enum_filters_are_422(client: TestClient) -> None:
    assert client.get("/api/signals?kind=BOGUS").status_code == 422
    assert client.get("/api/signals?outcome=MAYBE").status_code == 422


# ---------------------------------------------------------------------------
# /api/signals/{id} (lifecycle)
# ---------------------------------------------------------------------------


def test_lifecycle_published_signal(client: TestClient, scenario: Scenario) -> None:
    resp = client.get(f"/api/signals/{scenario.published.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"signal", "steps", "evaluations", "resolution", "review", "setup"}

    signal = body["signal"]
    assert signal["id"] == str(scenario.published.id)
    assert signal["decision_kind"] == "PUBLISHED" and signal["has_setup"] is True
    assert signal["judge_ruling"] == "PUBLISH_WITH_CAVEAT" and signal["session"] == "LONDON"
    assert signal["direction"] == "LONG" and signal["entry_price"] == 100.0
    # The lifecycle is the one place the FULL signal travels (journal rows are trimmed).
    assert set(signal) == JOURNAL_ROW_KEYS | {"payload", "features", "outcome_metadata"}
    assert signal["payload"] == scenario.published.payload
    assert signal["payload"]["confluence_narrative"] == NARRATIVE
    assert signal["features"] == {"rr": 3.0} and signal["outcome_metadata"] is None

    steps = body["steps"]
    assert [s["role"] for s in steps] == ["analyzer", "historian", "skeptic", "judge"]
    assert [s["run_id"] for s in steps] == [str(r.id) for r in scenario.published_runs]
    assert steps[0]["summary"] == {
        "direction": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit_1": 106.0,
        "take_profit_2": 110.0,
        "risk_reward_ratio": 3.0,
        "tags": ["fvg"],
    }
    historian = steps[1]["summary"]
    assert set(historian) == {"win_rate", "sample_size", "wins", "losses", "matches"}
    assert historian["win_rate"] == 1.0 and historian["sample_size"] == 1
    assert [set(m) for m in historian["matches"]] == [{"signal_id", "symbol", "outcome"}]
    assert steps[2]["summary"] == {
        "severity": "LOW",
        "recommends_against": False,
        "headline": "USD mildly firm",
    }
    assert steps[3]["summary"] == {
        "ruling": "PUBLISH_WITH_CAVEAT",
        "confidence": "MEDIUM",
        "caveat": "USD mildly firm",
    }
    # Trimmed means trimmed: the long texts are only on the runs endpoint.
    assert all("reasoning" not in s["summary"] for s in steps)
    assert "confluence_narrative" not in steps[0]["summary"]
    assert (steps[3]["latency_ms"], steps[3]["cost_usd"]) == (2100, 0.02)
    assert (steps[0]["latency_ms"], steps[0]["cost_usd"]) == (0, None)
    assert datetime.fromisoformat(steps[0]["created_at"]) == _at(8, 3, 53)

    evaluations = body["evaluations"]
    assert [e["status"] for e in evaluations] == ["STILL_VALID", "AT_RISK"]  # oldest first
    assert evaluations[1]["reasoning"] == "price near the stop"
    assert evaluations[1]["setup_id"] == str(scenario.setup.id)
    assert (evaluations[1]["latency_ms"], evaluations[1]["cost_usd"]) == (900, 0.005)

    assert body["resolution"] is None and body["review"] is None
    setup = body["setup"]
    assert setup["setup_id"] == str(scenario.setup.id)
    assert setup["age_seconds"] == 5 * 3600 + 6 * 60
    assert setup["evaluation_age_seconds"] == 4 * 60 + 30
    assert setup["latest_status"] == "AT_RISK" and setup["entry_price"] == 100.0


def test_lifecycle_vetoed_signal(client: TestClient, scenario: Scenario) -> None:
    resp = client.get(f"/api/signals/{scenario.vetoed.id}")
    assert resp.status_code == 200
    body = resp.json()
    signal = body["signal"]
    assert signal["decision_kind"] == "VETOED" and signal["has_setup"] is False
    assert signal["judge_ruling"] == "SKIP" and signal["status"] == "PUBLISHED"
    assert signal["resolution_outcome"] == "LOSS" and signal["realized_r"] == -1.0

    steps = body["steps"]
    assert [s["role"] for s in steps] == ["analyzer", "historian", "skeptic", "judge"]
    assert steps[2]["summary"] == {"no_macro_data": True}
    assert steps[3]["summary"]["ruling"] == "SKIP"

    assert body["evaluations"] == [] and body["setup"] is None
    resolution = body["resolution"]
    assert resolution["signal_id"] == str(scenario.vetoed.id)
    assert resolution["decision_kind"] == "VETOED" and resolution["outcome"] == "LOSS"
    assert resolution["realized_r"] == -1.0 and resolution["engine_version"] == "replay-v1"
    review = body["review"]
    assert review["signal_id"] == str(scenario.vetoed.id)
    assert review["decision_kind"] == "VETOED" and review["model"] == "claude-sonnet-4-5"
    assert review["review"] == {"judge_correct": True, "lesson": "the veto was right"}
    assert review["token_usage"] == {"input_tokens": 100} and review["attempts"] == 1
    # The setup lookup and the review lookup both went through the read seam.
    assert scenario.store.calls_matching("WHERE signal_id = :signal_id::uuid\nORDER BY opened_at")
    assert scenario.store.calls_matching("FROM decision_reviews")


def test_lifecycle_404(client: TestClient, scenario: Scenario) -> None:
    resp = client.get(f"/api/signals/{uuid4()}")
    assert resp.status_code == 404
    assert resp.json() == {"reason": "not_found"}
    assert scenario.store.calls == []  # the typed get_signal miss short-circuits
    assert client.get("/api/signals/not-a-uuid").status_code == 422


# ---------------------------------------------------------------------------
# /api/signals/{id}/runs/{run_id}
# ---------------------------------------------------------------------------


def test_run_detail_full_output(client: TestClient, scenario: Scenario) -> None:
    run = scenario.published_runs[0]
    resp = client.get(f"/api/signals/{scenario.published.id}/runs/{run.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(run.id) and body["scan_id"] == str(scenario.london_scan)
    assert body["agent_role"] == "analyzer" and body["strategy"] == "smc"
    assert body["input_hash"] == "hash-analyzer"
    assert body["output"] == run.output  # full JSON, narrative included
    assert body["output"]["confluence_narrative"] == NARRATIVE
    assert body["latency_ms"] == 0 and body["token_usage"] == {} and body["cost_usd"] is None
    assert datetime.fromisoformat(body["created_at"]) == run.created_at
    assert set(body) == {
        "id",
        "scan_id",
        "agent_role",
        "strategy",
        "input_hash",
        "output",
        "latency_ms",
        "token_usage",
        "cost_usd",
        "created_at",
    }


def test_run_detail_wrong_scan_404(client: TestClient, scenario: Scenario) -> None:
    # The run exists, but it belongs to the published signal's scan, not the vetoed one's.
    run = scenario.published_runs[3]
    resp = client.get(f"/api/signals/{scenario.vetoed.id}/runs/{run.id}")
    assert resp.status_code == 404
    assert resp.json() == {"reason": "not_found"}


def test_run_detail_missing_run_or_signal_404(client: TestClient, scenario: Scenario) -> None:
    run = scenario.published_runs[0]
    assert client.get(f"/api/signals/{scenario.published.id}/runs/{uuid4()}").status_code == 404
    assert client.get(f"/api/signals/{uuid4()}/runs/{run.id}").status_code == 404
    assert client.get(f"/api/signals/{scenario.published.id}/runs/nope").status_code == 422


# ---------------------------------------------------------------------------
# OpenAPI + Mangum
# ---------------------------------------------------------------------------


def test_openapi_lists_new_routes() -> None:
    spec = TestClient(create_app(version="t")).get("/api/openapi.json").json()
    for path in (
        "/api/overview",
        "/api/setups/open",
        "/api/signals",
        "/api/signals/{signal_id}",
        "/api/signals/{signal_id}/runs/{run_id}",
        "/api/signals/{signal_id}/chart",
        "/api/market/klines",
    ):
        assert path in spec["paths"], path
    klines = spec["paths"]["/api/market/klines"]["get"]
    assert {"400", "502"} <= set(klines["responses"])
    kparams = {p["name"]: p for p in klines["parameters"]}
    assert kparams["limit"]["schema"]["default"] == 200
    assert kparams["limit"]["schema"]["minimum"] == 1
    assert kparams["limit"]["schema"]["maximum"] == 500
    journal = spec["paths"]["/api/signals"]["get"]
    assert {"400", "502", "503"} <= set(journal["responses"])
    assert "404" in spec["paths"]["/api/signals/{signal_id}"]["get"]["responses"]
    params = {p["name"]: p for p in journal["parameters"]}
    assert params["limit"]["schema"]["default"] == 50
    assert params["limit"]["schema"]["maximum"] == 200 and params["limit"]["schema"]["minimum"] == 1
    assert set(params) == {
        "since",
        "until",
        "symbol",
        "kind",
        "outcome",
        "tag",
        "session",
        "violated_rule",
        "limit",
        "cursor",
    }


class _NoAlarmsCwClient:
    """boto3 `cloudwatch` client stand-in for the import-time handler: one empty page."""

    def describe_alarms(self, **_kwargs: Any) -> dict[str, Any]:
        return {"MetricAlarms": [], "CompositeAlarms": []}


@pytest.fixture
def container_store(scenario: Scenario, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeStore]:
    """Route the import-time `handler` app's `get_store()` at the fake (no override hook)."""
    monkeypatch.setenv("DB_CLUSTER_ARN", "arn:aws:rds:ap-south-1:123456789012:cluster:signals")
    monkeypatch.setenv("DB_SECRET_ARN", "arn:aws:secretsmanager:ap-south-1:123456789012:secret:x")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    get_dashboard_settings.cache_clear()
    deps.reset_store()
    monkeypatch.setattr(DataApiSignalStore, "from_arns", lambda **_kwargs: scenario.store)
    # The import-time app has no override hook: swap the default factory itself.
    monkeypatch.setattr(market, "default_provider_factory", default_factory())
    # ... and its CloudWatch client (the per-container cache) at a no-alarms fake.
    monkeypatch.setattr(cloudwatch, "_CLIENT", _NoAlarmsCwClient())
    yield scenario.store
    deps.reset_store()
    get_dashboard_settings.cache_clear()


@pytest.mark.usefixtures("lambda_event_loop", "frozen_clock")
def test_mangum_serves_overview_event(container_store: FakeStore) -> None:
    result = handler(_v2_event("/api/overview"), _Ctx())
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["risk"]["open_setups"] == 1 and body["risk"]["published_24h"] == 2
    assert body["health"]["last_scan"]["label"] == "NY"
    assert body["open_setups"][0]["unrealised_r"] == 0.5
    assert body["health"]["alarms"] == [] and body["health"]["alarms_note"] is None
    assert datetime.fromisoformat(body["generated_at"]) == NOW
    assert container_store.calls_matching("FROM scan_runs")


@pytest.mark.usefixtures("lambda_event_loop", "frozen_clock")
def test_mangum_serves_lifecycle_404(container_store: FakeStore) -> None:
    result = handler(_v2_event(f"/api/signals/{uuid4()}"), _Ctx())
    assert result["statusCode"] == 404
    assert json.loads(result["body"]) == {"reason": "not_found"}
