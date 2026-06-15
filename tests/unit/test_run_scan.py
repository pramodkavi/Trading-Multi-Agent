"""Unit tests for scripts.run_scan (Step 2.7 live pipeline) -- all services mocked.

No network, no DB, no Telegram, no Anthropic. The full pipeline graph is built
with mocked agents (a fake historian store, a fake macro provider, and mocked
Anthropic clients for the Skeptic + Judge); the persistence store, data
provider, and notifier are mocks. The genuine live run is exercised manually by
invoking scripts/run_scan.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import scripts.run_scan as run_scan_module
from scripts.run_scan import (
    _alambda,
    _load_settings,
    compose_message,
    run_one_symbol,
)
from src.agents.forecaster import ForecasterUpdate
from src.agents.historian import HistorianReport, HistorianRepository
from src.agents.judge import Judge, JudgeDecision
from src.agents.orchestration import build_pipeline_graph
from src.agents.skeptic import Skeptic, SkepticObjection
from src.common.models import (
    ForecastStatus,
    JudgeConfidence,
    JudgeRuling,
    ObjectionSeverity,
    SignalDirection,
    SignalProposal,
    SkipDecision,
    SkipReason,
)
from src.config import Settings
from src.providers import (
    DataProvider,
    Kline,
    MacroContext,
    MarketSnapshot,
    NoMacroData,
    Timeframe,
)

# ---------------------------------------------------------------------------
# Synthetic klines (verified publish / skip series under the 2.1e analyzer)
# ---------------------------------------------------------------------------

_ANCHOR = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)


def _c(idx: int, *, open_: float, high: float, low: float, close: float) -> Kline:
    return Kline(
        open_time=_ANCHOR + timedelta(hours=4 * idx),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100.0,
    )


def _flat(idx: int, *, level: float) -> Kline:
    return _c(idx, open_=level, high=level + 0.5, low=level - 0.5, close=level)


def _bullish_series() -> list[Kline]:
    """A full SMC LONG setup verified to publish AND clear the hard gates.

    Candle 16's high is 113 (not 110) so R:R is 3.45, clearing the 1:3 minimum
    the Step 2.11 risk gate enforces; confluence and DISCOUNT zone are unchanged.
    """
    s = [_flat(i, level=100.0) for i in range(10)]
    s.append(_c(10, open_=100.0, high=105.0, low=99.5, close=100.0))
    s += [_flat(11, level=100.0), _flat(12, level=100.0)]
    s.append(_c(13, open_=101.0, high=101.5, low=98.5, close=99.0))
    s.append(_c(14, open_=99.0, high=104.0, low=99.0, close=103.5))
    s.append(_c(15, open_=103.5, high=108.0, low=103.0, close=107.0))
    s.append(_c(16, open_=107.0, high=113.0, low=106.5, close=109.0))
    s += [_flat(17, level=107.5), _flat(18, level=107.5)]
    s.append(_c(19, open_=106.0, high=106.5, low=102.0, close=103.0))
    s += [_flat(i, level=103.5) for i in range(20, 32)]
    return s


def _ranging_series() -> list[Kline]:
    """Flat consolidation -> NO_CLEAR_BIAS skip."""
    return [_flat(i, level=100.0) for i in range(30)]


def _snapshot(candles: list[Kline], symbol: str = "BTCUSDT") -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        venue="binance",
        fetched_at=datetime(2026, 5, 26, 0, 0, 0, tzinfo=UTC),
        klines={Timeframe.H4: candles},
    )


# ---------------------------------------------------------------------------
# Mocked agents / pipeline graph
# ---------------------------------------------------------------------------


class _FakeHistorianStore:
    """Fake graph-side store: serves the Historian and the Step 2.11 risk gate.

    ``open_setups`` lets a test simulate concurrent setups so the risk gate's
    max-concurrent rule (4) trips; default empty so every stateful gate passes.
    """

    def __init__(self, *, open_setups: list[Any] | None = None) -> None:
        self._open = open_setups or []

    async def find_similar_signals(self, **kwargs: Any) -> list[Any]:
        return []

    async def list_open_active_setups(self) -> list[Any]:
        return list(self._open)

    async def get_signal(self, signal_id: Any) -> Any:
        return None

    async def list_recent_signals(self, **kwargs: Any) -> list[Any]:
        return []


class _FakeMacroProvider(DataProvider):
    name = "fake"

    async def fetch_market_snapshot(
        self,
        symbol: str,
        timeframes: list[Timeframe],
        *,
        limit: int = 200,
        include_derivatives: bool = False,
    ) -> MarketSnapshot:
        raise NotImplementedError

    async def fetch_macro_context(self) -> MacroContext | NoMacroData:
        return MacroContext(fetched_at=datetime(2026, 6, 1, 12, tzinfo=UTC), dxy=104.0, vix=18.0)


def _tool_response(payload: dict[str, Any], *, tool_name: str) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name=tool_name, id="toolu_r", input=payload)
    return SimpleNamespace(
        content=[block], usage=SimpleNamespace(input_tokens=100, output_tokens=40)
    )


def _client(responses: list[Any]) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    return client


_OBJECTION_PAYLOAD: dict[str, Any] = {
    "severity": "LOW",
    "recommends_against": False,
    "headline": "Macro is broadly neutral for this setup.",
    "reasoning": "No strong cross-asset headwind is evident in the supplied snapshot.",
    "cited_macro": ["broad USD index 104.0"],
}


def _ruling_payload(ruling: str, *, caveat: str | None = None) -> dict[str, Any]:
    return {
        "ruling": ruling,
        "confidence": "HIGH",
        "reasoning": "Weighed structure, neutral macro, and a thin precedent set.",
        "caveat": caveat,
    }


def _pipeline_graph(
    skeptic_client: MagicMock,
    judge_client: MagicMock,
    *,
    store: _FakeHistorianStore | None = None,
) -> Any:
    fake_store = store or _FakeHistorianStore()
    return build_pipeline_graph(
        store=fake_store,
        historian=HistorianRepository(fake_store),
        skeptic=Skeptic([_FakeMacroProvider()], client=skeptic_client),
        judge=Judge(client=judge_client),
    )


def _publishing_graph() -> Any:
    return _pipeline_graph(
        _client([_tool_response(_OBJECTION_PAYLOAD, tool_name="emit_objection")]),
        _client([_tool_response(_ruling_payload("PUBLISH"), tool_name="emit_ruling")]),
    )


def _vetoing_graph() -> Any:
    """Pipeline whose Judge rules SKIP on a real proposal (a veto)."""
    return _pipeline_graph(
        _client([_tool_response(_OBJECTION_PAYLOAD, tool_name="emit_objection")]),
        _client([_tool_response(_ruling_payload("SKIP"), tool_name="emit_ruling")]),
    )


# ---------------------------------------------------------------------------
# Other mocks
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        anthropic_api_key="sk-ant-test",
        telegram_bot_token="123:ABC",
        database_url="postgresql://u:p@localhost:5433/db",
        telegram_chat_id="111",
        _env_file=None,
    )


def _provider(snapshot: MarketSnapshot) -> MagicMock:
    provider = MagicMock()
    provider.fetch_market_snapshot = AsyncMock(return_value=snapshot)
    provider.aclose = AsyncMock()
    return provider


def _store() -> MagicMock:
    store = MagicMock()
    store.start_scan = AsyncMock()
    store.complete_scan = AsyncMock()
    store.fail_scan = AsyncMock()
    store.create_signal = AsyncMock(return_value=uuid4())
    store.log_agent_run = AsyncMock()
    store.open_active_setup = AsyncMock(return_value=uuid4())
    store.aclose = AsyncMock()
    return store


def _notifier() -> MagicMock:
    notifier = MagicMock()
    notifier.send = AsyncMock()
    notifier.aclose = AsyncMock()
    return notifier


# ---------------------------------------------------------------------------
# Model builders for compose_message
# ---------------------------------------------------------------------------


def _proposal() -> SignalProposal:
    return SignalProposal(
        scan_id=uuid4(),
        strategy="smc",
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit_1=115.0,
        risk_reward_ratio=3.0,
        leverage=5.0,
        risk_percent=1.0,
        confluence_narrative="Bullish OB tap with liquidity sweep below equal lows.",
    )


def _skip() -> SkipDecision:
    return SkipDecision(
        scan_id=uuid4(),
        strategy="smc",
        symbol="BTCUSDT",
        reason=SkipReason.NO_CLEAR_BIAS,
        details="Consolidation; no actionable bias.",
    )


def _report() -> HistorianReport:
    return HistorianReport(
        query_proposal_id=uuid4(),
        strategy="smc",
        direction=SignalDirection.LONG,
        sample_size=8,
        wins=5,
        losses=2,
        breakeven=1,
        inconclusive=0,
        win_rate=5 / 7,
        summary="5W/2L over similar bullish OB setups.",
    )


def _objection(severity: ObjectionSeverity = ObjectionSeverity.MEDIUM) -> SkepticObjection:
    return SkepticObjection(
        severity=severity,
        recommends_against=False,
        headline="Mild dollar-strength headwind.",
        reasoning="The broad USD level is somewhat elevated in the snapshot.",
        cited_macro=["broad USD index 104.0"],
    )


def _ruling(
    ruling: JudgeRuling,
    *,
    caveat: str | None = None,
    confidence: JudgeConfidence = JudgeConfidence.HIGH,
) -> JudgeDecision:
    return JudgeDecision(
        ruling=ruling,
        confidence=confidence,
        reasoning="Weighed structure, history, and macro objection.",
        caveat=caveat,
    )


# ---------------------------------------------------------------------------
# compose_message
# ---------------------------------------------------------------------------


class TestComposeMessage:
    def test_publish_includes_signal_historian_and_skeptic(self) -> None:
        state = {
            "proposal": _proposal(),
            "decision": JudgeRuling.PUBLISH,
            "historian_report": _report(),
            "skeptic_objection": _objection(),
            "judge_decision": _ruling(JudgeRuling.PUBLISH),
        }
        msg = compose_message(state)  # type: ignore[arg-type]
        assert "NEW SIGNAL" in msg
        assert "Historian win rate" in msg
        assert "Skeptic objection" in msg
        assert "manual execution required" in msg  # mandated footer
        assert "Caveat" not in msg

    def test_publish_with_caveat_includes_caveat(self) -> None:
        state = {
            "proposal": _proposal(),
            "decision": JudgeRuling.PUBLISH_WITH_CAVEAT,
            "historian_report": _report(),
            "skeptic_objection": _objection(),
            "judge_decision": _ruling(
                JudgeRuling.PUBLISH_WITH_CAVEAT, caveat="Reduce size: dollar strength."
            ),
        }
        msg = compose_message(state)  # type: ignore[arg-type]
        assert "Caveat" in msg
        assert "Reduce size" in msg

    def test_analyzer_skip_message(self) -> None:
        state = {"proposal": _skip(), "decision": JudgeRuling.SKIP, "judge_decision": None}
        msg = compose_message(state)  # type: ignore[arg-type]
        assert "SKIP" in msg

    def test_judge_veto_message(self) -> None:
        state = {
            "proposal": _proposal(),
            "decision": JudgeRuling.SKIP,
            "judge_decision": _ruling(JudgeRuling.SKIP),
        }
        msg = compose_message(state)  # type: ignore[arg-type]
        assert "JUDGED SKIP" in msg

    def test_nomacrodata_skeptic_renders_unavailable(self) -> None:
        state = {
            "proposal": _proposal(),
            "decision": JudgeRuling.PUBLISH,
            "historian_report": _report(),
            "skeptic_objection": NoMacroData(provider="skeptic", reason="all providers down"),
            "judge_decision": _ruling(JudgeRuling.PUBLISH),
        }
        msg = compose_message(state)  # type: ignore[arg-type]
        assert "unavailable" in msg.lower()

    def test_win_rate_is_markdown_escaped(self) -> None:
        # 5/7 -> 71.4% ; the '.' must be escaped for MarkdownV2.
        state = {
            "proposal": _proposal(),
            "decision": JudgeRuling.PUBLISH,
            "historian_report": _report(),
            "skeptic_objection": _objection(),
            "judge_decision": _ruling(JudgeRuling.PUBLISH),
        }
        msg = compose_message(state)  # type: ignore[arg-type]
        assert "71\\.4%" in msg


# ---------------------------------------------------------------------------
# run_one_symbol — full orchestration through the pipeline
# ---------------------------------------------------------------------------


class TestRunOneSymbol:
    async def test_publish_persists_full_chain_and_notifies(self) -> None:
        provider = _provider(_snapshot(_bullish_series()))
        store = _store()
        notifier = _notifier()

        ctx = await run_one_symbol(
            symbol="BTCUSDT",
            provider=provider,
            store=store,
            graph=_publishing_graph(),
            notifier=notifier,
        )

        provider.fetch_market_snapshot.assert_awaited_once()
        store.start_scan.assert_awaited_once()
        store.create_signal.assert_awaited_once()
        # FR-1.7: analyzer + historian + skeptic + judge reasoning all journaled.
        assert store.log_agent_run.await_count == 4
        # Step 2.8: a publish opens a tracked active setup.
        store.open_active_setup.assert_awaited_once()
        notifier.send.assert_awaited_once()
        store.complete_scan.assert_awaited_once()
        store.fail_scan.assert_not_awaited()
        assert ctx.strategy == "smc"

    async def test_skip_persists_only_analyzer_and_notifies(self) -> None:
        provider = _provider(_snapshot(_ranging_series()))
        store = _store()
        notifier = _notifier()
        # Skeptic/Judge clients are never consumed (conditional edge short-circuits).
        graph = _pipeline_graph(_client([]), _client([]))

        await run_one_symbol(
            symbol="BTCUSDT",
            provider=provider,
            store=store,
            graph=graph,
            notifier=notifier,
        )

        store.create_signal.assert_awaited_once()
        assert store.log_agent_run.await_count == 1  # analyzer only
        store.open_active_setup.assert_not_awaited()  # skips open no setup
        notifier.send.assert_awaited_once()
        store.complete_scan.assert_awaited_once()

    async def test_risk_gate_force_skip_persists_skip_and_preserves_proposal(self) -> None:
        # Three open setups trip the max-concurrent hard rule (4): the analyzer's
        # real proposal is force-skipped before the historian/skeptic/judge run.
        provider = _provider(_snapshot(_bullish_series()))
        store = _store()
        notifier = _notifier()
        gate_store = _FakeHistorianStore(
            open_setups=[SimpleNamespace(signal_id=uuid4()) for _ in range(3)]
        )
        graph = _pipeline_graph(_client([]), _client([]), store=gate_store)

        await run_one_symbol(
            symbol="BTCUSDT",
            provider=provider,
            store=store,
            graph=graph,
            notifier=notifier,
        )

        # A SKIPPED row is written (FR-1.3) and only the analyzer was journaled.
        store.create_signal.assert_awaited_once()
        persisted = store.create_signal.call_args.args[0]
        assert isinstance(persisted, SkipDecision)
        assert persisted.violated_rule == "RULE_4_MAX_CONCURRENT"
        assert store.log_agent_run.await_count == 1  # analyzer only (gate short-circuits)
        # The analyzer agent_run preserves the ORIGINAL proposal (FR-1.7), not the skip.
        logged = store.log_agent_run.call_args.kwargs["output"]
        assert logged["risk_reward_ratio"] >= 3.0
        store.open_active_setup.assert_not_awaited()  # a force-skip opens no setup
        notifier.send.assert_awaited_once()
        store.complete_scan.assert_awaited_once()

    async def test_judge_veto_opens_no_setup(self) -> None:
        provider = _provider(_snapshot(_bullish_series()))
        store = _store()

        await run_one_symbol(
            symbol="BTCUSDT",
            provider=provider,
            store=store,
            graph=_vetoing_graph(),
            notifier=_notifier(),
        )

        # A real proposal the Judge SKIPs is journaled (4 agents) but NOT tracked.
        store.create_signal.assert_awaited_once()
        assert store.log_agent_run.await_count == 4
        store.open_active_setup.assert_not_awaited()

    async def test_no_notifier_skips_telegram(self) -> None:
        provider = _provider(_snapshot(_bullish_series()))
        store = _store()

        await run_one_symbol(
            symbol="BTCUSDT",
            provider=provider,
            store=store,
            graph=_publishing_graph(),
            notifier=None,
        )

        store.create_signal.assert_awaited_once()
        store.complete_scan.assert_awaited_once()

    async def test_provider_failure_marks_scan_failed(self) -> None:
        provider = MagicMock()
        provider.fetch_market_snapshot = AsyncMock(side_effect=RuntimeError("binance down"))
        store = _store()
        notifier = _notifier()

        with pytest.raises(RuntimeError, match="binance down"):
            await run_one_symbol(
                symbol="BTCUSDT",
                provider=provider,
                store=store,
                graph=_publishing_graph(),
                notifier=notifier,
            )

        store.start_scan.assert_awaited_once()
        store.fail_scan.assert_awaited_once()
        store.complete_scan.assert_not_awaited()
        notifier.send.assert_not_awaited()

    async def test_fail_scan_records_error_message(self) -> None:
        provider = MagicMock()
        provider.fetch_market_snapshot = AsyncMock(side_effect=RuntimeError("boom"))
        store = _store()

        with pytest.raises(RuntimeError):
            await run_one_symbol(
                symbol="BTCUSDT",
                provider=provider,
                store=store,
                graph=_publishing_graph(),
                notifier=None,
            )

        store.fail_scan.assert_awaited_once()
        assert "boom" in store.fail_scan.await_args.kwargs["error_message"]


# ---------------------------------------------------------------------------
# lambda_handler — serverless entry point
# ---------------------------------------------------------------------------


class TestLambdaHandler:
    """Exercises the async core (_alambda) under pytest's loop.

    run_one_symbol is replaced (its behaviour is covered by TestRunOneSymbol);
    here we test event parsing, dependency lifecycle, and result shaping. The
    real pipeline build runs (with a mocked Anthropic client) but is never
    invoked because run_one_symbol is mocked.
    """

    def _patch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        run: AsyncMock,
    ) -> dict[str, MagicMock]:
        store = MagicMock()
        store.aclose = AsyncMock()
        provider = MagicMock()
        provider.aclose = AsyncMock()
        notifier = MagicMock()
        notifier.aclose = AsyncMock()
        client = MagicMock()
        client.close = AsyncMock()
        provider_factory = MagicMock(return_value=provider)
        notifier_factory = MagicMock(return_value=notifier)

        monkeypatch.setattr(run_scan_module, "create_store", AsyncMock(return_value=store))
        monkeypatch.setattr(run_scan_module, "BinanceProvider", provider_factory)
        monkeypatch.setattr(run_scan_module, "TelegramNotifier", notifier_factory)
        monkeypatch.setattr(run_scan_module, "AsyncAnthropic", MagicMock(return_value=client))
        monkeypatch.setattr(run_scan_module, "run_one_symbol", run)
        return {
            "store": store,
            "provider": provider,
            "notifier_factory": notifier_factory,
            "client": client,
        }

    @staticmethod
    def _ok_run() -> AsyncMock:
        return AsyncMock(side_effect=lambda **kw: SimpleNamespace(scan_id=uuid4()))

    async def test_event_symbols_list_runs_each(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = self._ok_run()
        deps = self._patch(monkeypatch, run=run)

        result = await _alambda({"symbols": ["BTCUSDT", "ETHUSDT"]}, _settings())

        assert result["ok"] is True
        assert [s["symbol"] for s in result["scans"]] == ["BTCUSDT", "ETHUSDT"]
        assert all(s["status"] == "ok" for s in result["scans"])
        assert run.await_count == 2
        deps["store"].aclose.assert_awaited_once()
        deps["client"].close.assert_awaited_once()

    async def test_event_single_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = self._ok_run()
        self._patch(monkeypatch, run=run)

        result = await _alambda({"symbol": "SOLUSDT"}, _settings())

        assert result["scans"][0]["symbol"] == "SOLUSDT"
        assert run.await_count == 1

    async def test_empty_event_falls_back_to_watchlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = self._ok_run()
        self._patch(monkeypatch, run=run)

        result = await _alambda(None, _settings())

        assert run.await_count == 4  # default 4-symbol watchlist
        assert result["ok"] is True

    async def test_one_symbol_failure_marks_overall_not_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _side_effect(**kwargs: object) -> SimpleNamespace:
            if kwargs["symbol"] == "ETHUSDT":
                raise RuntimeError("boom")
            return SimpleNamespace(scan_id=uuid4())

        run = AsyncMock(side_effect=_side_effect)
        self._patch(monkeypatch, run=run)

        result = await _alambda({"symbols": ["BTCUSDT", "ETHUSDT"]}, _settings())

        assert result["ok"] is False
        statuses = {s["symbol"]: s["status"] for s in result["scans"]}
        assert statuses == {"BTCUSDT": "ok", "ETHUSDT": "error"}
        eth = next(s for s in result["scans"] if s["symbol"] == "ETHUSDT")
        assert "boom" in eth["error"]

    async def test_notify_false_skips_notifier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = self._ok_run()
        deps = self._patch(monkeypatch, run=run)

        await _alambda({"symbol": "BTCUSDT", "notify": False}, _settings())

        deps["notifier_factory"].assert_not_called()

    async def test_forecaster_mode_runs_forecaster(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = MagicMock()
        store.aclose = AsyncMock()
        provider = MagicMock()
        provider.aclose = AsyncMock()
        notifier = MagicMock()
        notifier.aclose = AsyncMock()
        client = MagicMock()
        client.close = AsyncMock()
        forecaster = MagicMock()
        forecaster.run = AsyncMock(
            return_value=[
                ForecasterUpdate(status=ForecastStatus.STILL_VALID, reasoning="x" * 20),
                ForecasterUpdate(status=ForecastStatus.AT_RISK, reasoning="y" * 20),
            ]
        )
        monkeypatch.setattr(run_scan_module, "create_store", AsyncMock(return_value=store))
        monkeypatch.setattr(run_scan_module, "BinanceProvider", MagicMock(return_value=provider))
        monkeypatch.setattr(run_scan_module, "TelegramNotifier", MagicMock(return_value=notifier))
        monkeypatch.setattr(run_scan_module, "AsyncAnthropic", MagicMock(return_value=client))
        monkeypatch.setattr(run_scan_module, "Forecaster", MagicMock(return_value=forecaster))

        result = await _alambda({"mode": "forecaster"}, _settings())

        assert result["mode"] == "forecaster"
        assert result["evaluated"] == 2
        assert result["by_status"] == {"STILL_VALID": 1, "AT_RISK": 1}
        forecaster.run.assert_awaited_once()
        store.aclose.assert_awaited_once()
        client.close.assert_awaited_once()
        provider.aclose.assert_awaited_once()
        notifier.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# _load_settings — secret hydration ordering
# ---------------------------------------------------------------------------


class TestLoadSettings:
    def test_hydrates_secrets_before_building_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        settings = _settings()

        def _hydrate() -> None:
            order.append("hydrate")

        def _get() -> object:
            order.append("get_settings")
            return settings

        monkeypatch.setattr(run_scan_module, "hydrate_secrets_env", _hydrate)
        monkeypatch.setattr(run_scan_module, "get_settings", _get)

        result = _load_settings()

        assert result is settings
        assert order == ["hydrate", "get_settings"]
