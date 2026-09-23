"""Unit tests for the Critic v0 OutcomeResolver (design §5) -- fully offline.

Fake store + fake provider (the ``test_forecaster.py`` pattern); no network,
no database. Covers the queue -> classify -> fetch -> replay -> persist loop,
the PUBLISHED-only propagation into ``signals.outcome`` / ``active_setups``,
deferral of still-open windows and provider errors, per-decision fault
isolation, and the same-symbol/same-day candle cache.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.agents.critic.models import DecisionKind, Resolution, ResolutionOutcome
from src.agents.critic.replay import ENGINE_VERSION, MAX_HOLD
from src.agents.critic.resolver import OutcomeResolver, ResolveSummary
from src.common.models import (
    ActiveSetupStatus,
    AgentRole,
    SignalDirection,
    SignalOutcome,
    SignalStatus,
)
from src.persistence.models import StoredAgentRun, StoredSignal
from src.providers.base import Kline, ProviderError, ProviderUnavailableError, Timeframe

T0 = datetime(2026, 7, 1, 8, 3, tzinfo=UTC)
NOW = T0 + MAX_HOLD + timedelta(days=1)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _proposal_payload(scan_id: UUID, **over: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "proposal_id": str(uuid4()),
        "scan_id": str(scan_id),
        "strategy": "smc",
        "created_at": T0.isoformat(),
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit_1": 106.0,
        "take_profit_2": None,
        "risk_reward_ratio": 3.0,
        "leverage": 3.0,
        "risk_percent": 1.0,
        "tags": [],
        "confluence_narrative": "Bullish FVG in discount after a liquidity sweep.",
        "features": {},
    }
    p.update(over)
    return p


def _skip_payload(scan_id: UUID, violated_rule: str | None) -> dict[str, Any]:
    return {
        "decision_id": str(uuid4()),
        "scan_id": str(scan_id),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "reason": "INSUFFICIENT_RR" if violated_rule else "NO_CLEAR_BIAS",
        "details": "gate rejected" if violated_rule else "no valid structure",
        "violated_rule": violated_rule,
        "created_at": T0.isoformat(),
    }


def _signal(payload: dict[str, Any], scan_id: UUID, *, created_at: datetime = T0) -> StoredSignal:
    has_dir = "direction" in payload
    return StoredSignal(
        id=uuid4(),
        scan_id=scan_id,
        symbol="BTCUSDT",
        strategy="smc",
        direction=SignalDirection.LONG if has_dir else None,
        status=SignalStatus.PUBLISHED if has_dir else SignalStatus.SKIPPED,
        created_at=created_at,
        payload=payload,
        tags=[],
        features={},
    )


def _run(scan_id: UUID, role: AgentRole, output: dict[str, Any]) -> StoredAgentRun:
    return StoredAgentRun(
        id=uuid4(),
        scan_id=scan_id,
        agent_role=role,
        strategy="smc",
        input_hash="h",
        output=output,
        latency_ms=0,
        token_usage={},
        cost_usd=None,
        created_at=T0,
    )


def _judge(scan_id: UUID, ruling: str) -> StoredAgentRun:
    return _run(scan_id, AgentRole.JUDGE, {"ruling": ruling})


def _candles(*rows: tuple[int, float, float, float, float]) -> list[Kline]:
    return [
        Kline(open_time=T0 + timedelta(minutes=5 * i), open=o, high=h, low=lo, close=c, volume=1.0)
        for i, o, h, lo, c in rows
    ]


WIN_CANDLES = ((0, 100.0, 100.5, 99.9, 100.2), (1, 100.2, 106.5, 100.1, 106.0))
LOSS_CANDLES = ((0, 100.0, 100.5, 99.9, 100.2), (1, 100.2, 100.4, 97.5, 98.0))


class FakeStore:
    """Records every write; serves a fixed queue and a fixed ``remaining`` count."""

    def __init__(
        self,
        signals: list[StoredSignal],
        runs: dict[UUID, list[StoredAgentRun]],
        remaining: int = 0,
    ) -> None:
        self._signals = signals
        self._runs = runs
        self._remaining = remaining
        self.list_calls: list[tuple[int, datetime]] = []
        self.count_calls: list[datetime] = []
        self.saved: list[Resolution] = []
        self.outcomes: list[tuple[UUID, SignalOutcome, dict[str, Any] | None]] = []
        self.closed: list[tuple[UUID, ActiveSetupStatus, dict[str, Any]]] = []

    async def list_unresolved_decisions(
        self, *, limit: int, decided_before: datetime
    ) -> list[StoredSignal]:
        self.list_calls.append((limit, decided_before))
        return self._signals[:limit]

    async def count_unresolved_decisions(self, *, decided_before: datetime) -> int:
        self.count_calls.append(decided_before)
        return self._remaining

    async def list_agent_runs_for_scan(self, scan_id: UUID) -> list[StoredAgentRun]:
        return self._runs.get(scan_id, [])

    async def save_resolution(self, resolution: Resolution) -> None:
        self.saved.append(resolution)

    async def set_signal_outcome(
        self, *, signal_id: UUID, outcome: SignalOutcome, outcome_metadata: Any = None
    ) -> None:
        self.outcomes.append((signal_id, outcome, outcome_metadata))

    async def close_active_setup_for_signal(
        self, *, signal_id: UUID, status: ActiveSetupStatus, evaluation: Any
    ) -> None:
        self.closed.append((signal_id, status, dict(evaluation)))


def _provider(candles: list[Kline] | Exception) -> MagicMock:
    p = MagicMock()
    p.fetch_klines = (
        AsyncMock(side_effect=candles)
        if isinstance(candles, Exception)
        else AsyncMock(return_value=candles)
    )
    return p


def _resolver(store: FakeStore, provider: MagicMock, now: datetime = NOW) -> OutcomeResolver:
    return OutcomeResolver(store=store, provider=provider, now=lambda: now)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Happy paths per decision kind
# ---------------------------------------------------------------------------


async def test_published_win_writes_resolution_outcome_and_closes_setup() -> None:
    scan = uuid4()
    sig = _signal(_proposal_payload(scan), scan)
    store = FakeStore([sig], {scan: [_judge(scan, "PUBLISH")]})
    provider = _provider(_candles(*WIN_CANDLES))

    summary = await _resolver(store, provider).run(chunk_size=25)

    assert summary == ResolveSummary(resolved=1, deferred=0, remaining=0)
    assert len(store.saved) == 1
    res = store.saved[0]
    assert res.signal_id == sig.id
    assert res.decision_kind is DecisionKind.PUBLISHED
    assert res.outcome is ResolutionOutcome.WIN
    assert res.decided_at == T0
    assert res.entered_at == T0
    assert res.resolved_at == T0 + timedelta(minutes=5)
    assert res.realized_r == pytest.approx(3.0)
    assert res.candles_used == 2
    assert res.engine_version == ENGINE_VERSION

    # PUBLISHED -> signals.outcome + active_setups closed with the same verdict.
    assert [(sid, out) for sid, out, _ in store.outcomes] == [(sig.id, SignalOutcome.WIN)]
    meta = store.outcomes[0][2]
    assert meta is not None and meta["source"] == "resolver"
    assert meta["outcome"] == "WIN" and meta["engine_version"] == ENGINE_VERSION
    assert [(sid, st) for sid, st, _ in store.closed] == [(sig.id, ActiveSetupStatus.WIN)]
    assert store.closed[0][2] == {
        "source": "resolver",
        "outcome": "WIN",
        "realized_r": pytest.approx(3.0),
    }

    provider.fetch_klines.assert_awaited_once()
    args, _kwargs = provider.fetch_klines.await_args
    assert args[0] == "BTCUSDT"
    assert args[1] is Timeframe.M5


async def test_published_expired_propagates_expired() -> None:
    scan = uuid4()
    sig = _signal(_proposal_payload(scan), scan)
    store = FakeStore([sig], {scan: [_judge(scan, "PUBLISH_WITH_CAVEAT")]})
    # Entry never touched -> EXPIRED once the 72h validity has passed.
    provider = _provider(_candles((0, 101.0, 101.5, 100.5, 101.0)))

    await _resolver(store, provider).run()

    assert store.saved[0].outcome is ResolutionOutcome.EXPIRED
    assert store.saved[0].entered_at is None
    assert [(sid, out) for sid, out, _ in store.outcomes] == [(sig.id, SignalOutcome.EXPIRED)]
    assert [(sid, st) for sid, st, _ in store.closed] == [(sig.id, ActiveSetupStatus.EXPIRED)]


async def test_published_with_no_judge_run_is_published_slice1_era() -> None:
    scan = uuid4()
    sig = _signal(_proposal_payload(scan), scan)
    store = FakeStore([sig], {})

    await _resolver(store, _provider(_candles(*LOSS_CANDLES))).run()

    assert store.saved[0].decision_kind is DecisionKind.PUBLISHED
    assert store.saved[0].outcome is ResolutionOutcome.LOSS
    assert [(sid, out) for sid, out, _ in store.outcomes] == [(sig.id, SignalOutcome.LOSS)]


async def test_vetoed_loss_does_not_touch_signal_outcome() -> None:
    scan = uuid4()
    sig = _signal(_proposal_payload(scan), scan)
    store = FakeStore([sig], {scan: [_judge(scan, "SKIP")]})

    await _resolver(store, _provider(_candles(*LOSS_CANDLES))).run()

    assert store.saved[0].decision_kind is DecisionKind.VETOED
    assert store.saved[0].outcome is ResolutionOutcome.LOSS
    assert store.saved[0].realized_r == pytest.approx(-1.0)
    assert store.outcomes == [] and store.closed == []


async def test_gate_rejected_with_analyzer_proposal_is_counterfactual_only() -> None:
    scan = uuid4()
    analyzer = _run(scan, AgentRole.ANALYZER, _proposal_payload(scan))
    store = FakeStore([_signal(_skip_payload(scan, "RULE_2_MIN_RR"), scan)], {scan: [analyzer]})

    summary = await _resolver(store, _provider(_candles(*WIN_CANDLES))).run()

    assert summary.resolved == 1
    assert store.saved[0].decision_kind is DecisionKind.GATE_REJECTED
    assert store.saved[0].outcome is ResolutionOutcome.WIN
    assert store.outcomes == [] and store.closed == []


async def test_gate_rejected_without_proposal_is_unresolvable() -> None:
    scan = uuid4()
    store = FakeStore([_signal(_skip_payload(scan, "RULE_7_SESSION_BLOCK"), scan)], {})
    provider = _provider([])

    summary = await _resolver(store, provider).run()

    assert summary.resolved == 1
    res = store.saved[0]
    assert res.decision_kind is DecisionKind.GATE_REJECTED
    assert res.outcome is ResolutionOutcome.UNRESOLVABLE
    assert res.decided_at == T0 and res.resolved_at == NOW
    assert res.entered_at is None and res.realized_r == 0.0 and res.candles_used == 0
    assert res.engine_version == ENGINE_VERSION
    provider.fetch_klines.assert_not_awaited()


async def test_published_corrupt_payload_is_unresolvable_and_never_touches_outcome() -> None:
    scan = uuid4()
    sig = _signal(_proposal_payload(scan, stop_loss="not-a-number"), scan)
    store = FakeStore([sig], {scan: [_judge(scan, "PUBLISH")]})
    provider = _provider(_candles(*WIN_CANDLES))

    await _resolver(store, provider).run()

    assert store.saved[0].decision_kind is DecisionKind.PUBLISHED
    assert store.saved[0].outcome is ResolutionOutcome.UNRESOLVABLE
    assert store.outcomes == [] and store.closed == []
    provider.fetch_klines.assert_not_awaited()


# ---------------------------------------------------------------------------
# Deferral and fault isolation
# ---------------------------------------------------------------------------


async def test_pending_window_is_deferred_not_saved() -> None:
    scan = uuid4()
    sig = _signal(_proposal_payload(scan), scan)
    store = FakeStore([sig], {scan: [_judge(scan, "PUBLISH")]}, remaining=0)
    provider = _provider(_candles((0, 100.0, 100.5, 99.9, 100.2)))

    summary = await _resolver(store, provider, now=T0 + timedelta(hours=1)).run()

    assert summary == ResolveSummary(resolved=0, deferred=1, remaining=0)
    assert store.saved == [] and store.outcomes == [] and store.closed == []


async def test_provider_error_defers_logs_marker_and_continues_batch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scan1, scan2 = uuid4(), uuid4()
    s1 = _signal(_proposal_payload(scan1), scan1)
    # Different symbol so the cache does not short-circuit the second fetch.
    s2 = _signal(_proposal_payload(scan2), scan2).model_copy(update={"symbol": "ETHUSDT"})
    store = FakeStore(
        [s1, s2], {scan1: [_judge(scan1, "PUBLISH")], scan2: [_judge(scan2, "PUBLISH")]}
    )
    provider = MagicMock()
    provider.fetch_klines = AsyncMock(
        side_effect=[ProviderUnavailableError("451", provider="binance"), _candles(*WIN_CANDLES)]
    )

    with caplog.at_level(logging.WARNING, logger="src.agents.critic.resolver"):
        summary = await _resolver(store, provider).run()

    assert summary.resolved == 1 and summary.deferred == 1
    assert [r.signal_id for r in store.saved] == [s2.id]
    marker = [rec for rec in caplog.records if rec.getMessage().startswith("PROVIDER_ERROR")]
    assert len(marker) == 1
    assert "symbol=BTCUSDT" in marker[0].getMessage()
    assert "ProviderUnavailableError" in marker[0].getMessage()


async def test_unexpected_exception_is_isolated_and_counted_deferred(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scan1, scan2 = uuid4(), uuid4()
    s1 = _signal(_proposal_payload(scan1), scan1)
    s2 = _signal(_proposal_payload(scan2), scan2)
    store = FakeStore(
        [s1, s2], {scan1: [_judge(scan1, "PUBLISH")], scan2: [_judge(scan2, "PUBLISH")]}
    )
    real_save = store.save_resolution
    calls = {"n": 0}

    async def flaky_save(resolution: Resolution) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        await real_save(resolution)

    store.save_resolution = flaky_save  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="src.agents.critic.resolver"):
        summary = await _resolver(store, _provider(_candles(*WIN_CANDLES))).run()

    assert summary.resolved == 1 and summary.deferred == 1
    assert [r.signal_id for r in store.saved] == [s2.id]
    # The failed one never reached propagation.
    assert [sid for sid, _, _ in store.outcomes] == [s2.id]
    assert any(str(s1.id) in rec.getMessage() for rec in caplog.records if rec.exc_info)


async def test_resolve_one_returns_none_for_non_scoreable_decision() -> None:
    scan = uuid4()
    sig = _signal(_skip_payload(scan, None), scan)  # NO_SETUP
    store = FakeStore([], {})
    provider = _provider([])

    assert await _resolver(store, provider).resolve_one(sig) is None
    assert store.saved == []
    provider.fetch_klines.assert_not_awaited()


# ---------------------------------------------------------------------------
# Queue contract and candle fetching
# ---------------------------------------------------------------------------


async def test_run_queries_with_now_and_counts_remaining_with_closed_window() -> None:
    store = FakeStore([], {}, remaining=42)

    summary = await _resolver(store, _provider([])).run(chunk_size=7)

    assert summary == ResolveSummary(resolved=0, deferred=0, remaining=42)
    assert store.list_calls == [(7, NOW)]
    assert store.count_calls == [NOW - MAX_HOLD]


async def test_fetch_window_starts_at_day_floor_and_is_bounded() -> None:
    scan = uuid4()
    created = datetime(2026, 7, 1, 13, 3, tzinfo=UTC)
    sig = _signal(_proposal_payload(scan), scan, created_at=created)
    store = FakeStore([sig], {scan: [_judge(scan, "PUBLISH")]})
    provider = _provider([])

    await _resolver(store, provider, now=created + MAX_HOLD + timedelta(days=3)).run()

    kwargs = provider.fetch_klines.await_args.kwargs
    assert kwargs["start"] == datetime(2026, 7, 1, tzinfo=UTC)
    # Wide enough to cover every decision made on that day through its full hold.
    assert kwargs["end"] >= created + MAX_HOLD
    assert kwargs["end"] <= datetime(2026, 7, 2, tzinfo=UTC) + MAX_HOLD


async def test_fetch_window_end_is_capped_at_now() -> None:
    scan = uuid4()
    sig = _signal(_proposal_payload(scan), scan)
    store = FakeStore([sig], {scan: [_judge(scan, "PUBLISH")]})
    provider = _provider([])
    now = T0 + timedelta(hours=1)

    await _resolver(store, provider, now=now).run()

    assert provider.fetch_klines.await_args.kwargs["end"] == now


async def test_same_symbol_same_day_shares_one_fetch() -> None:
    scan1, scan2 = uuid4(), uuid4()
    s1 = _signal(_proposal_payload(scan1), scan1)  # 08:03
    s2 = _signal(_proposal_payload(scan2), scan2, created_at=T0 + timedelta(hours=5))  # 13:03
    store = FakeStore(
        [s1, s2], {scan1: [_judge(scan1, "PUBLISH")], scan2: [_judge(scan2, "PUBLISH")]}
    )
    provider = _provider(_candles(*WIN_CANDLES))

    summary = await _resolver(store, provider).run()

    assert provider.fetch_klines.await_count == 1
    # Both still resolved independently (the second never entered -> EXPIRED).
    assert summary.resolved == 2
    assert {r.outcome for r in store.saved} == {ResolutionOutcome.WIN, ResolutionOutcome.EXPIRED}


async def test_different_symbols_do_not_share_a_fetch() -> None:
    scan1, scan2 = uuid4(), uuid4()
    s1 = _signal(_proposal_payload(scan1), scan1)
    s2 = _signal(_proposal_payload(scan2), scan2).model_copy(update={"symbol": "ETHUSDT"})
    store = FakeStore(
        [s1, s2], {scan1: [_judge(scan1, "PUBLISH")], scan2: [_judge(scan2, "PUBLISH")]}
    )
    provider = _provider(_candles(*WIN_CANDLES))

    await _resolver(store, provider).run()

    assert provider.fetch_klines.await_count == 2
    symbols = {call.args[0] for call in provider.fetch_klines.await_args_list}
    assert symbols == {"BTCUSDT", "ETHUSDT"}


async def test_chunk_size_is_passed_to_the_queue() -> None:
    scan_ids = [uuid4() for _ in range(3)]
    signals = [_signal(_proposal_payload(s), s) for s in scan_ids]
    store = FakeStore(signals, {s: [_judge(s, "PUBLISH")] for s in scan_ids})

    summary = await _resolver(store, _provider(_candles(*WIN_CANDLES))).run(chunk_size=2)

    assert summary.resolved == 2
    assert len(store.saved) == 2


def test_resolve_summary_is_frozen() -> None:
    summary = ResolveSummary(resolved=1, deferred=2, remaining=3)
    with pytest.raises(AttributeError):
        summary.resolved = 9  # type: ignore[misc]


def test_provider_error_base_is_what_the_resolver_defers() -> None:
    # Guard: the resolver must catch the *base* class so every provider subclass defers.
    assert issubclass(ProviderUnavailableError, ProviderError)
