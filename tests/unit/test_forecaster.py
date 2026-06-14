"""Unit tests for the Forecaster agent (Slice 2 Step 2.9) -- fully offline.

Covers:
- ForecasterUpdate schema validation (outcome required iff INVALIDATED)
- the three verdict paths through run() with a fake store / provider / notifier
  and a mocked Anthropic client:
    STILL_VALID -> setup stays OPEN, no notify, no outcome
    AT_RISK     -> setup stays OPEN, notify, no outcome
    INVALIDATED -> setup closed with the terminal outcome + outcome logged + notify
- orphan setup (missing signal) is skipped without an LLM call
- a per-setup failure is isolated (the rest of the run continues)
- format_forecaster_update rendering
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from src.agents.forecaster import Forecaster, ForecasterUpdate
from src.common.models import (
    ActiveSetupStatus,
    ForecastStatus,
    SignalDirection,
    SignalOutcome,
    SignalProposal,
    SignalStatus,
)
from src.notifications import format_forecaster_update
from src.persistence.models import StoredActiveSetup, StoredSignal
from src.providers import Kline, MarketSnapshot, Timeframe

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _proposal(symbol: str = "BTCUSDT") -> SignalProposal:
    return SignalProposal(
        scan_id=uuid4(),
        strategy="smc",
        symbol=symbol,
        direction=SignalDirection.LONG,
        entry_price=100.0,
        stop_loss=97.0,
        take_profit_1=109.0,
        risk_reward_ratio=3.0,
        leverage=3.0,
        risk_percent=1.0,
        confluence_narrative="Bullish OB in discount with a liquidity sweep below equal lows.",
    )


def _stored_signal(proposal: SignalProposal) -> StoredSignal:
    return StoredSignal(
        id=uuid4(),
        scan_id=proposal.scan_id,
        symbol=proposal.symbol,
        strategy=proposal.strategy,
        direction=proposal.direction,
        status=SignalStatus.PUBLISHED,
        created_at=datetime(2026, 6, 1, 8, tzinfo=UTC),
        payload=proposal.model_dump(mode="json"),
        tags=list(proposal.tags),
        features=dict(proposal.features),
    )


def _setup(signal_id: UUID) -> StoredActiveSetup:
    return StoredActiveSetup(
        id=uuid4(),
        signal_id=signal_id,
        opened_at=datetime(2026, 6, 1, 8, tzinfo=UTC),
        status=ActiveSetupStatus.OPEN,
    )


def _snapshot(symbol: str = "BTCUSDT") -> MarketSnapshot:
    anchor = datetime(2026, 6, 1, 0, tzinfo=UTC)
    candles = [
        Kline(
            open_time=anchor + timedelta(hours=4 * i),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=50.0,
        )
        for i in range(5)
    ]
    return MarketSnapshot(
        symbol=symbol,
        venue="binance",
        fetched_at=datetime(2026, 6, 2, 0, tzinfo=UTC),
        klines={Timeframe.H4: candles},
    )


class FakeStore:
    """Minimal SignalStore stand-in for the Forecaster's reads + writes."""

    def __init__(
        self,
        setups: list[StoredActiveSetup],
        signals: dict[UUID, StoredSignal],
    ) -> None:
        self._setups = setups
        self._signals = signals
        self.updated: list[tuple[UUID, ActiveSetupStatus, dict[str, Any] | None]] = []
        self.outcomes: list[tuple[UUID, SignalOutcome]] = []

    async def list_open_active_setups(self) -> list[StoredActiveSetup]:
        return list(self._setups)

    async def get_signal(self, signal_id: UUID) -> StoredSignal | None:
        return self._signals.get(signal_id)

    async def update_active_setup(
        self,
        *,
        setup_id: UUID,
        status: ActiveSetupStatus,
        evaluation: dict[str, Any] | None = None,
        evaluated_at: Any = None,
    ) -> None:
        self.updated.append((setup_id, status, evaluation))

    async def set_signal_outcome(
        self,
        *,
        signal_id: UUID,
        outcome: SignalOutcome,
        outcome_metadata: Any = None,
    ) -> None:
        self.outcomes.append((signal_id, outcome))


def _provider(snapshot: MarketSnapshot, *, side_effect: Any = None) -> MagicMock:
    provider = MagicMock()
    if side_effect is not None:
        provider.fetch_market_snapshot = AsyncMock(side_effect=side_effect)
    else:
        provider.fetch_market_snapshot = AsyncMock(return_value=snapshot)
    return provider


def _notifier() -> MagicMock:
    notifier = MagicMock()
    notifier.send = AsyncMock()
    return notifier


def _response(payload: dict[str, Any]) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name="emit_forecast", id="toolu_f", input=payload)
    return SimpleNamespace(
        content=[block], usage=SimpleNamespace(input_tokens=100, output_tokens=40)
    )


def _client(responses: list[Any]) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    return client


_STILL_VALID = {"status": "STILL_VALID", "reasoning": "Price holding above entry; thesis intact."}
_AT_RISK = {"status": "AT_RISK", "reasoning": "Price drifting toward the invalidation level."}
_INVALIDATED_LOSS = {
    "status": "INVALIDATED",
    "reasoning": "Price closed through the stop; the structural premise is broken.",
    "outcome": "LOSS",
}


# ---------------------------------------------------------------------------
# ForecasterUpdate schema
# ---------------------------------------------------------------------------


def test_invalidated_requires_outcome() -> None:
    with pytest.raises(ValidationError):
        ForecasterUpdate.model_validate({"status": "INVALIDATED", "reasoning": "x" * 20})


def test_outcome_forbidden_when_not_invalidated() -> None:
    with pytest.raises(ValidationError):
        ForecasterUpdate.model_validate(
            {"status": "AT_RISK", "reasoning": "x" * 20, "outcome": "LOSS"}
        )


def test_valid_invalidated_with_outcome() -> None:
    update = ForecasterUpdate.model_validate(
        {"status": "INVALIDATED", "reasoning": "x" * 20, "outcome": "WIN"}
    )
    assert update.outcome is SignalOutcome.WIN


# ---------------------------------------------------------------------------
# run() verdict paths
# ---------------------------------------------------------------------------


async def test_still_valid_keeps_open_without_notify_or_outcome() -> None:
    proposal = _proposal()
    signal = _stored_signal(proposal)
    setup = _setup(signal.id)
    store = FakeStore([setup], {signal.id: signal})
    notifier = _notifier()
    forecaster = Forecaster(
        store=store,  # type: ignore[arg-type]
        provider=_provider(_snapshot()),
        notifier=notifier,
        client=_client([_response(_STILL_VALID)]),
    )

    updates = await forecaster.run()

    assert len(updates) == 1
    assert updates[0].status is ForecastStatus.STILL_VALID
    assert store.updated == [(setup.id, ActiveSetupStatus.OPEN, updates[0].model_dump(mode="json"))]
    assert store.outcomes == []
    notifier.send.assert_not_awaited()


async def test_at_risk_notifies_and_stays_open() -> None:
    proposal = _proposal()
    signal = _stored_signal(proposal)
    setup = _setup(signal.id)
    store = FakeStore([setup], {signal.id: signal})
    notifier = _notifier()
    forecaster = Forecaster(
        store=store,  # type: ignore[arg-type]
        provider=_provider(_snapshot()),
        notifier=notifier,
        client=_client([_response(_AT_RISK)]),
    )

    updates = await forecaster.run()

    assert updates[0].status is ForecastStatus.AT_RISK
    assert store.updated[0][1] is ActiveSetupStatus.OPEN  # still open
    assert store.outcomes == []  # not closed
    notifier.send.assert_awaited_once()


async def test_invalidated_closes_logs_outcome_and_notifies() -> None:
    proposal = _proposal()
    signal = _stored_signal(proposal)
    setup = _setup(signal.id)
    store = FakeStore([setup], {signal.id: signal})
    notifier = _notifier()
    forecaster = Forecaster(
        store=store,  # type: ignore[arg-type]
        provider=_provider(_snapshot()),
        notifier=notifier,
        client=_client([_response(_INVALIDATED_LOSS)]),
    )

    updates = await forecaster.run()

    assert updates[0].status is ForecastStatus.INVALIDATED
    # active_setups status mirrors the terminal outcome
    assert store.updated[0][1] is ActiveSetupStatus.LOSS
    # the signal journal gets the outcome stamped
    assert store.outcomes == [(signal.id, SignalOutcome.LOSS)]
    notifier.send.assert_awaited_once()


async def test_orphan_setup_skipped_without_llm() -> None:
    setup = _setup(uuid4())  # references a signal not in the store
    store = FakeStore([setup], {})
    client = _client([])
    forecaster = Forecaster(
        store=store,  # type: ignore[arg-type]
        provider=_provider(_snapshot()),
        notifier=_notifier(),
        client=client,
    )

    updates = await forecaster.run()

    assert updates == []
    assert store.updated == []
    client.messages.create.assert_not_awaited()


async def test_per_setup_failure_is_isolated() -> None:
    p1, p2 = _proposal("BTCUSDT"), _proposal("ETHUSDT")
    s1, s2 = _stored_signal(p1), _stored_signal(p2)
    setup1, setup2 = _setup(s1.id), _setup(s2.id)
    store = FakeStore([setup1, setup2], {s1.id: s1, s2.id: s2})
    # First setup's market fetch blows up; the second succeeds.
    provider = _provider(_snapshot(), side_effect=[RuntimeError("binance down"), _snapshot()])
    forecaster = Forecaster(
        store=store,  # type: ignore[arg-type]
        provider=provider,
        notifier=_notifier(),
        client=_client([_response(_STILL_VALID)]),  # only the survivor calls the LLM
    )

    updates = await forecaster.run()

    assert len(updates) == 1  # the failing setup was skipped, not fatal
    assert len(store.updated) == 1


# ---------------------------------------------------------------------------
# format_forecaster_update
# ---------------------------------------------------------------------------


def test_format_at_risk_has_warning_header() -> None:
    msg = format_forecaster_update(
        _proposal(), ForecasterUpdate(status=ForecastStatus.AT_RISK, reasoning="x" * 20)
    )
    assert "AT RISK" in msg
    assert "Why:" in msg
    assert "Outcome" not in msg


def test_format_invalidated_includes_outcome() -> None:
    msg = format_forecaster_update(
        _proposal(),
        ForecasterUpdate(
            status=ForecastStatus.INVALIDATED,
            reasoning="x" * 20,
            outcome=SignalOutcome.LOSS,
        ),
    )
    assert "SETUP CLOSED" in msg
    assert "LOSS" in msg


def test_format_escapes_markdown() -> None:
    msg = format_forecaster_update(
        _proposal(),
        ForecasterUpdate(status=ForecastStatus.AT_RISK, reasoning="Price at 1.5 support now."),
    )
    assert "1\\.5" in msg  # the '.' must be escaped for MarkdownV2
