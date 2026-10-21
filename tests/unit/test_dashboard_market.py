"""Slice 4 Step 4.3 -- market data: `market.py`, `/api/market/klines`, `/api/signals/{id}/chart`.

`BinanceProvider` is never constructed here: every route test overrides the
`get_provider_factory` dependency with `FakeProviderFactory` (see
`test_dashboard_routes.py`), which records each provider it builds, so "no provider
was constructed" is asserted as `factory.built == []`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from src.common.models import AgentRole, SignalDirection
from src.dashboard.api import market
from src.dashboard.api import overview as assembly
from src.providers.base import ProviderError, ProviderUnavailableError, Timeframe
from tests.unit.test_dashboard_routes import (
    NOW,
    FakeProviderFactory,
    Scenario,
    _client_for,
    _proposal_payload,
    _run,
    _scenario,
    _series,
    _signal,
)

H4 = timedelta(hours=4)


@pytest.fixture
def scenario() -> Scenario:
    return _scenario()


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(assembly, "utcnow", lambda: NOW)
    return NOW


# ---------------------------------------------------------------------------
# market.py helpers
# ---------------------------------------------------------------------------


def test_unrealised_r_long_short_and_zero_risk() -> None:
    assert market.unrealised_r(SignalDirection.LONG, 100.0, 98.0, 101.0) == 0.5
    assert market.unrealised_r(SignalDirection.LONG, 100.0, 98.0, 97.0) == -1.5
    assert market.unrealised_r(SignalDirection.SHORT, 100.0, 102.0, 97.0) == 1.5
    assert market.unrealised_r(SignalDirection.SHORT, 100.0, 102.0, 103.0) == -1.5
    assert market.unrealised_r(SignalDirection.LONG, 100.0, 97.0, 101.0) == 0.33  # 2 dp
    with pytest.raises(ValueError, match="entry"):
        market.unrealised_r(SignalDirection.LONG, 100.0, 100.0, 101.0)


def test_timeframe_duration_covers_every_timeframe() -> None:
    assert set(market.TIMEFRAME_DURATION) == set(Timeframe)
    assert market.TIMEFRAME_DURATION[Timeframe.H4] == H4
    assert market.TIMEFRAME_DURATION[Timeframe.M5] == timedelta(minutes=5)


async def test_provider_closed_on_error() -> None:
    factory = FakeProviderFactory(
        errors={"BTCUSDT": ProviderUnavailableError("down", provider="binance")}
    )
    with pytest.raises(ProviderError):
        await market.fetch_recent_klines(factory, "BTCUSDT", Timeframe.H4, limit=10, end=NOW)
    (provider,) = factory.built
    assert provider.closed


async def test_fetch_recent_klines_window_and_trim() -> None:
    factory = FakeProviderFactory(
        klines={("BTCUSDT", Timeframe.H4): _series(Timeframe.H4, end=NOW, count=20)}
    )
    out = await market.fetch_recent_klines(factory, "BTCUSDT", Timeframe.H4, limit=5, end=NOW)
    assert len(out) == 5
    assert [k.open_time for k in out] == sorted(k.open_time for k in out)
    (provider,) = factory.built
    assert provider.closed
    assert provider.calls == [("BTCUSDT", Timeframe.H4, NOW - 5 * H4, NOW)]


async def test_latest_prices_budget_returns_partial_or_empty() -> None:
    m5 = Timeframe.M5
    klines = {
        ("BTCUSDT", m5): _series(m5, end=NOW, count=6, first_close=96.0),  # last close 101
        ("ETHUSDT", m5): _series(m5, end=NOW, count=6, first_close=10.0),
    }
    # Partial: ETH errors, SOL has no candles -> both omitted; BTC priced. One provider.
    factory = FakeProviderFactory(
        klines=klines, errors={"ETHUSDT": ProviderUnavailableError("eth down", provider="binance")}
    )
    prices = await market.latest_prices(
        factory, ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BTCUSDT"], now=NOW
    )
    assert prices == {"BTCUSDT": 101.0}
    (provider,) = factory.built
    assert provider.closed
    assert sorted(c[0] for c in provider.calls) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert {(c[1], c[2], c[3]) for c in provider.calls} == {(m5, NOW - timedelta(minutes=30), NOW)}

    # Empty: the overall budget expires -> {} (never raises), provider still closed.
    slow = FakeProviderFactory(klines=klines, delay_s=10.0)
    assert await market.latest_prices(slow, ["BTCUSDT"], now=NOW, budget_s=0.05) == {}
    (slow_provider,) = slow.built
    assert slow_provider.closed

    # No symbols -> no provider at all.
    idle = FakeProviderFactory()
    assert await market.latest_prices(idle, [], now=NOW) == {}
    assert idle.built == []


# ---------------------------------------------------------------------------
# GET /api/market/klines
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("frozen_clock")
def test_klines_returns_ascending_candles(scenario: Scenario) -> None:
    factory = FakeProviderFactory(
        klines={("BTCUSDT", Timeframe.H4): _series(Timeframe.H4, end=NOW, count=10)}
    )
    _, client = _client_for(scenario.store, factory)
    resp = client.get("/api/market/klines", params={"symbol": "BTCUSDT", "tf": "4h", "limit": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "BTCUSDT" and body["timeframe"] == "4h"
    times = [datetime.fromisoformat(k["open_time"]) for k in body["klines"]]
    assert len(times) == 3 and times == sorted(times)
    assert times[-1] == NOW - H4
    assert set(body["klines"][0]) == {"open_time", "open", "high", "low", "close", "volume"}
    (provider,) = factory.built
    assert provider.closed
    assert provider.calls == [("BTCUSDT", Timeframe.H4, NOW - 3 * H4, NOW)]
    assert scenario.store.calls == []  # market data never touches the database


@pytest.mark.usefixtures("frozen_clock")
def test_klines_defaults_to_200_h4(scenario: Scenario) -> None:
    factory = FakeProviderFactory()
    _, client = _client_for(scenario.store, factory)
    resp = client.get("/api/market/klines", params={"symbol": "ETHUSDT"})
    assert resp.status_code == 200 and resp.json()["klines"] == []
    (provider,) = factory.built
    assert provider.calls == [("ETHUSDT", Timeframe.H4, NOW - 200 * H4, NOW)]


@pytest.mark.usefixtures("frozen_clock")
def test_klines_rejects_unknown_symbol_without_provider(scenario: Scenario) -> None:
    # Review Focus 6: a non-watchlist symbol is 400 with NO provider construction.
    factory = FakeProviderFactory()
    _, client = _client_for(scenario.store, factory)
    resp = client.get("/api/market/klines", params={"symbol": "DOGEUSDT"})
    assert resp.status_code == 400
    assert resp.json() == {"reason": "unknown_symbol"}
    assert factory.built == []


@pytest.mark.usefixtures("frozen_clock")
def test_klines_limit_bounds_422(scenario: Scenario) -> None:
    factory = FakeProviderFactory()
    _, client = _client_for(scenario.store, factory)
    for params in (
        {"symbol": "BTCUSDT", "limit": 0},
        {"symbol": "BTCUSDT", "limit": 501},
        {"symbol": "BTCUSDT", "tf": "2h"},
        {},
    ):
        assert client.get("/api/market/klines", params=params).status_code == 422, params
    assert factory.built == []
    assert client.get("/api/market/klines?symbol=BTCUSDT&limit=500").status_code == 200
    assert client.get("/api/market/klines?symbol=BTCUSDT&limit=1&tf=5m").status_code == 200


@pytest.mark.usefixtures("frozen_clock")
def test_provider_error_maps_to_502(scenario: Scenario, caplog: pytest.LogCaptureFixture) -> None:
    factory = FakeProviderFactory(
        errors={"BTCUSDT": ProviderUnavailableError("upstream-secret-detail", provider="binance")}
    )
    _, client = _client_for(scenario.store, factory)
    with caplog.at_level(logging.WARNING, logger="src.dashboard.api.errors"):
        resp = client.get("/api/market/klines", params={"symbol": "BTCUSDT"})
    assert resp.status_code == 502
    assert resp.json() == {"reason": "market_unavailable"}
    assert "upstream-secret-detail" not in resp.text
    assert "upstream-secret-detail" in caplog.text  # logged for the operator, never echoed
    (provider,) = factory.built
    assert provider.closed


# ---------------------------------------------------------------------------
# GET /api/signals/{id}/chart
# ---------------------------------------------------------------------------


def _h4_factory(symbol: str) -> FakeProviderFactory:
    return FakeProviderFactory(
        klines={(symbol, Timeframe.H4): _series(Timeframe.H4, end=NOW, count=100)}
    )


@pytest.mark.usefixtures("frozen_clock")
def test_chart_overlay_from_payload(scenario: Scenario) -> None:
    signal = scenario.published  # BTCUSDT LONG, created 08:03:55
    signal.payload["features"] = {"rr": 3.0, "ob_zone_low": 99.0, "ob_zone_high": 100.5}
    factory = _h4_factory("BTCUSDT")
    _, client = _client_for(scenario.store, factory)
    resp = client.get(f"/api/signals/{signal.id}/chart", params={"before": 2, "after": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["signal_id"] == str(signal.id) and body["symbol"] == "BTCUSDT"
    assert body["timeframe"] == "4h"
    assert datetime.fromisoformat(body["created_at"]) == signal.created_at
    assert body["overlay"] == {
        "direction": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit_1": 106.0,
        "take_profit_2": 110.0,
        "poi_low": 99.0,
        "poi_high": 100.5,
    }
    (provider,) = factory.built
    assert provider.closed
    # Window: 2 bars before created_at .. 1 bar after (12:03:55 < now, so not clamped).
    assert provider.calls == [
        ("BTCUSDT", Timeframe.H4, signal.created_at - 2 * H4, signal.created_at + H4)
    ]
    times = [datetime.fromisoformat(k["open_time"]) for k in body["klines"]]
    assert times == sorted(times) and times
    assert all(signal.created_at - 2 * H4 <= t < signal.created_at + H4 for t in times)


@pytest.mark.usefixtures("frozen_clock")
def test_chart_overlay_from_analyzer_for_gate_rejected(scenario: Scenario) -> None:
    rejected = scenario.gate_rejected  # SOLUSDT skip row, RULE_2_MIN_RR
    proposal = _proposal_payload(rejected.scan_id, "SOLUSDT", SignalDirection.SHORT)
    proposal["features"] = {"ob_zone_low": 100.2, "ob_zone_high": 101.0}
    scenario.store.runs_by_scan[rejected.scan_id] = [
        _run(rejected.scan_id, AgentRole.ANALYZER, proposal, created_at=rejected.created_at),
    ]
    factory = _h4_factory("SOLUSDT")
    _, client = _client_for(scenario.store, factory)
    body = client.get(f"/api/signals/{rejected.id}/chart").json()
    assert body["overlay"] == {
        "direction": "SHORT",
        "entry_price": 100.0,
        "stop_loss": 102.0,
        "take_profit_1": 94.0,
        "take_profit_2": 90.0,
        "poi_low": 100.2,
        "poi_high": 101.0,
    }

    # A gate-rejected signal whose analyzer run is missing -> no overlay, still 200.
    scenario.store.runs_by_scan[rejected.scan_id] = []
    resp = client.get(f"/api/signals/{rejected.id}/chart")
    assert resp.status_code == 200 and resp.json()["overlay"] is None


@pytest.mark.usefixtures("frozen_clock")
def test_chart_no_setup_has_null_overlay(scenario: Scenario) -> None:
    no_setup = _signal(uuid4(), symbol="BNBUSDT", created_at=NOW - timedelta(hours=1))
    scenario.store.signals[no_setup.id] = no_setup
    factory = _h4_factory("BNBUSDT")
    _, client = _client_for(scenario.store, factory)
    resp = client.get(f"/api/signals/{no_setup.id}/chart")
    assert resp.status_code == 200
    body = resp.json()
    assert body["overlay"] is None and body["symbol"] == "BNBUSDT"
    assert body["klines"]


@pytest.mark.usefixtures("frozen_clock")
def test_chart_window_clamped_to_now(scenario: Scenario) -> None:
    signal = scenario.published
    factory = _h4_factory("BTCUSDT")
    _, client = _client_for(scenario.store, factory)
    resp = client.get(f"/api/signals/{signal.id}/chart")  # before=60, after=30 defaults
    assert resp.status_code == 200
    (provider,) = factory.built
    # created_at + 30 bars is days in the future: the end is clamped to now.
    assert provider.calls == [("BTCUSDT", Timeframe.H4, signal.created_at - 60 * H4, NOW)]
    assert resp.json()["overlay"]["poi_low"] is None  # no ob_zone in these features


@pytest.mark.usefixtures("frozen_clock")
def test_chart_404(scenario: Scenario) -> None:
    factory = FakeProviderFactory()
    _, client = _client_for(scenario.store, factory)
    resp = client.get(f"/api/signals/{uuid4()}/chart")
    assert resp.status_code == 404
    assert resp.json() == {"reason": "not_found"}
    assert factory.built == []
    signal_id = scenario.published.id
    assert client.get(f"/api/signals/{signal_id}/chart?before=0").status_code == 422
    assert client.get(f"/api/signals/{signal_id}/chart?after=-1").status_code == 422
    assert client.get(f"/api/signals/{signal_id}/chart?before=501").status_code == 422
    assert factory.built == []


@pytest.mark.usefixtures("frozen_clock")
def test_chart_provider_error_is_502(scenario: Scenario) -> None:
    factory = FakeProviderFactory(
        errors={"BTCUSDT": ProviderUnavailableError("down", provider="binance")}
    )
    _, client = _client_for(scenario.store, factory)
    resp = client.get(f"/api/signals/{scenario.published.id}/chart")
    assert resp.status_code == 502 and resp.json() == {"reason": "market_unavailable"}
    assert factory.built[0].closed
