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
- Step 4.0 (design §5.2): every evaluation -- verdict, orphan, or exception --
  leaves one `setup_evaluations` row; exceptions also log the `PROVIDER_ERROR`
  marker; a failure to write the ERROR row itself never aborts the run; the
  verdict row is journaled before the Telegram send so a notifier outage adds
  an ERROR row rather than masking the verdict
- format_forecaster_update rendering
"""

from __future__ import annotations

import logging
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
    SetupEvaluationStatus,
    SignalDirection,
    SignalOutcome,
    SignalProposal,
    SignalStatus,
)
from src.notifications import NotifierUnavailableError, format_forecaster_update
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
        # setup_evaluations rows (Step 4.0); set fail_record to simulate a DB
        # write failure for the statuses in fail_record_statuses. Default scope is
        # the ERROR-row write in run() (verdict rows still land, so the test pins
        # "the ERROR-row write failing must not abort the run"); widen it to a
        # verdict status to simulate the deploy->migrate window where the
        # setup_evaluations table does not exist yet.
        self.evaluations: list[dict[str, Any]] = []
        self.fail_record = False
        self.fail_record_statuses: set[SetupEvaluationStatus] = {SetupEvaluationStatus.ERROR}

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

    async def record_setup_evaluation(self, **kwargs: Any) -> UUID:
        if self.fail_record and kwargs["status"] in self.fail_record_statuses:
            raise RuntimeError("db down")
        self.evaluations.append(kwargs)
        return uuid4()


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
# setup_evaluations journaling (Step 4.0, design §5.2)
# ---------------------------------------------------------------------------


async def test_each_verdict_records_an_evaluation_row() -> None:
    proposal = _proposal()
    signal = _stored_signal(proposal)
    setup = _setup(signal.id)
    store = FakeStore([setup], {signal.id: signal})
    forecaster = Forecaster(
        store=store,  # type: ignore[arg-type]
        provider=_provider(_snapshot()),
        notifier=_notifier(),
        client=_client([_response(_AT_RISK)]),
    )

    updates = await forecaster.run()

    assert len(updates) == 1
    assert len(store.evaluations) == 1
    row = store.evaluations[0]
    assert row["status"] is SetupEvaluationStatus.AT_RISK and row["outcome"] is None
    assert row["setup_id"] == setup.id and row["signal_id"] == setup.signal_id
    assert row["reasoning"] == updates[0].reasoning
    assert row["token_usage"]["tokens_in"] == 100 and row["cost_usd"] is not None
    assert row.get("error") is None  # a verdict row carries no error text


async def test_invalidated_records_outcome_on_evaluation_row() -> None:
    proposal = _proposal()
    signal = _stored_signal(proposal)
    setup = _setup(signal.id)
    store = FakeStore([setup], {signal.id: signal})
    forecaster = Forecaster(
        store=store,  # type: ignore[arg-type]
        provider=_provider(_snapshot()),
        notifier=_notifier(),
        client=_client([_response(_INVALIDATED_LOSS)]),
    )

    await forecaster.run()

    assert len(store.evaluations) == 1
    assert store.evaluations[0]["status"] is SetupEvaluationStatus.INVALIDATED
    assert store.evaluations[0]["outcome"] is SignalOutcome.LOSS


async def test_orphan_setup_records_error_row_without_llm() -> None:
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
    client.messages.create.assert_not_awaited()
    assert len(store.evaluations) == 1
    assert store.evaluations[0]["status"] is SetupEvaluationStatus.ERROR
    assert store.evaluations[0]["setup_id"] == setup.id
    assert store.evaluations[0]["signal_id"] == setup.signal_id
    assert "orphan" in store.evaluations[0]["error"]
    assert str(setup.signal_id) in store.evaluations[0]["error"]


async def test_per_setup_failure_records_error_row_and_logs_marker(
    caplog: pytest.LogCaptureFixture,
) -> None:
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
        client=_client([_response(_STILL_VALID)]),
    )

    with caplog.at_level(logging.WARNING, logger="src.agents.forecaster.forecaster"):
        updates = await forecaster.run()

    assert len(updates) == 1  # the run continued past the failure
    # One row per setup: the ERROR row for setup1 and the verdict row for setup2.
    assert len(store.evaluations) == 2
    errors = [e for e in store.evaluations if e["status"] is SetupEvaluationStatus.ERROR]
    assert len(errors) == 1 and errors[0]["error"].startswith("RuntimeError")
    assert errors[0]["setup_id"] == setup1.id
    assert "binance down" in errors[0]["error"]
    marker = [r for r in caplog.records if "PROVIDER_ERROR provider=forecaster" in r.getMessage()]
    assert len(marker) == 1
    assert f"setup={setup1.id}" in marker[0].getMessage()
    assert "RuntimeError" in marker[0].getMessage()


async def test_error_row_write_failure_does_not_abort_run() -> None:
    p1, p2 = _proposal("BTCUSDT"), _proposal("ETHUSDT")
    s1, s2 = _stored_signal(p1), _stored_signal(p2)
    setup1, setup2 = _setup(s1.id), _setup(s2.id)
    store = FakeStore([setup1, setup2], {s1.id: s1, s2.id: s2})
    store.fail_record = True  # the ERROR-row write for setup 1 raises
    # Provider raises for setup 1; setup 2 is fine.
    provider = _provider(_snapshot(), side_effect=[RuntimeError("binance down"), _snapshot()])
    forecaster = Forecaster(
        store=store,  # type: ignore[arg-type]
        provider=provider,
        notifier=_notifier(),
        client=_client([_response(_STILL_VALID)]),
    )

    updates = await forecaster.run()

    assert len(updates) == 1  # setup 2 still evaluated; the run did not raise
    assert len(store.updated) == 1  # setup 2's verdict was still applied
    # Only setup 2's verdict row landed; setup 1's ERROR row failed to write.
    assert [e["status"] for e in store.evaluations] == [SetupEvaluationStatus.STILL_VALID]
    assert store.evaluations[0]["setup_id"] == setup2.id


async def test_notifier_failure_cannot_mask_the_journaled_verdict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review fix: the verdict row is journaled BEFORE the Telegram send.

    A notifier outage after an INVALIDATED verdict must leave the honest record:
    the setup closed + outcome stamped + the INVALIDATED row, PLUS an ERROR row
    naming the notifier (and the alarm marker) -- never an ERROR row alone.
    """
    proposal = _proposal()
    signal = _stored_signal(proposal)
    setup = _setup(signal.id)
    store = FakeStore([setup], {signal.id: signal})
    notifier = _notifier()
    notifier.send = AsyncMock(
        side_effect=NotifierUnavailableError("telegram down", notifier="telegram")
    )
    forecaster = Forecaster(
        store=store,  # type: ignore[arg-type]
        provider=_provider(_snapshot()),
        notifier=notifier,
        client=_client([_response(_INVALIDATED_LOSS)]),
    )

    with caplog.at_level(logging.WARNING, logger="src.agents.forecaster.forecaster"):
        updates = await forecaster.run()  # must not raise

    # The verdict took effect and was journaled...
    assert store.updated[0][1] is ActiveSetupStatus.LOSS
    assert store.outcomes == [(signal.id, SignalOutcome.LOSS)]
    assert len(store.evaluations) == 2
    assert store.evaluations[0]["status"] is SetupEvaluationStatus.INVALIDATED
    assert store.evaluations[0]["outcome"] is SignalOutcome.LOSS
    # ...and the notifier failure is recorded on its own, alarmed, row.
    assert store.evaluations[1]["status"] is SetupEvaluationStatus.ERROR
    assert "NotifierUnavailableError" in store.evaluations[1]["error"]
    assert "telegram down" in store.evaluations[1]["error"]
    assert any("PROVIDER_ERROR provider=forecaster" in r.getMessage() for r in caplog.records)
    # The failed look is not reported as a produced verdict (run() contract unchanged).
    assert updates == []


async def test_verdict_row_failure_still_notifies_and_surfaces_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Final-review fix: journaling must never cost the operator an alert.

    In the deploy->migrate window ``setup_evaluations`` may not exist yet, so the
    verdict-row write raises. The verdict has already taken effect; the operator
    may be in the trade -- the INVALIDATED/AT_RISK Telegram alert must still go
    out, and the journaling failure must still surface to run() (marker + ERROR
    row attempt) so the outage stays visible.
    """
    proposal = _proposal()
    signal = _stored_signal(proposal)
    setup = _setup(signal.id)
    store = FakeStore([setup], {signal.id: signal})
    store.fail_record = True
    store.fail_record_statuses = {SetupEvaluationStatus.INVALIDATED, SetupEvaluationStatus.ERROR}
    notifier = _notifier()
    forecaster = Forecaster(
        store=store,  # type: ignore[arg-type]
        provider=_provider(_snapshot()),
        notifier=notifier,
        client=_client([_response(_INVALIDATED_LOSS)]),
    )

    with caplog.at_level(logging.WARNING, logger="src.agents.forecaster.forecaster"):
        updates = await forecaster.run()  # must not raise

    # The alert still reached the operator...
    notifier.send.assert_awaited_once()
    # ...the verdict took effect...
    assert store.updated[0][1] is ActiveSetupStatus.LOSS
    assert store.outcomes == [(signal.id, SignalOutcome.LOSS)]
    # ...no row could be journaled (table missing), and the failure is visible.
    assert store.evaluations == []
    assert any("PROVIDER_ERROR provider=forecaster" in r.getMessage() for r in caplog.records)
    journal = [
        r for r in caplog.records if "failed to journal forecaster verdict" in r.getMessage()
    ]
    assert len(journal) == 1 and journal[0].levelno == logging.ERROR
    assert str(setup.id) in journal[0].getMessage()
    # run() contract unchanged: a failed look is not reported as a produced verdict.
    assert updates == []


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


# ---------------------------------------------------------------------------
# Step 4.7 Part B: dashboard deep links in Forecaster alerts
# ---------------------------------------------------------------------------

_DASH_BASE = "https://d111.cloudfront.net"


def _linked_forecaster(
    store: FakeStore, notifier: MagicMock, responses: list[Any], base: str | None = _DASH_BASE
) -> Forecaster:
    return Forecaster(
        store=store,  # type: ignore[arg-type]
        provider=_provider(_snapshot()),
        notifier=notifier,
        client=_client([_response(r) for r in responses]),
        dashboard_base_url=base,
    )


async def test_at_risk_alert_links_to_the_setups_own_signal() -> None:
    signal = _stored_signal(_proposal())
    store = FakeStore([_setup(signal.id)], {signal.id: signal})
    notifier = _notifier()

    await _linked_forecaster(store, notifier, [_AT_RISK]).run()

    sent = notifier.send.await_args.args[0]
    assert f"[Open in dashboard]({_DASH_BASE}/signals/{signal.id})" in sent


async def test_each_alert_carries_the_link_of_its_own_signal() -> None:
    first = _stored_signal(_proposal("BTCUSDT"))
    second = _stored_signal(_proposal("ETHUSDT"))
    store = FakeStore([_setup(first.id), _setup(second.id)], {first.id: first, second.id: second})
    notifier = _notifier()

    await _linked_forecaster(store, notifier, [_AT_RISK, _INVALIDATED_LOSS]).run()

    sent = [call.args[0] for call in notifier.send.await_args_list]
    assert f"/signals/{first.id})" in sent[0] and str(second.id) not in sent[0]
    assert f"/signals/{second.id})" in sent[1] and str(first.id) not in sent[1]


async def test_still_valid_stays_silent_even_with_a_dashboard_url() -> None:
    signal = _stored_signal(_proposal())
    store = FakeStore([_setup(signal.id)], {signal.id: signal})
    notifier = _notifier()

    await _linked_forecaster(store, notifier, [_STILL_VALID]).run()

    notifier.send.assert_not_awaited()


@pytest.mark.parametrize("base", [None, "http://insecure.example", ""])
async def test_no_usable_dashboard_url_means_an_unchanged_alert(base: str | None) -> None:
    signal = _stored_signal(_proposal())
    store = FakeStore([_setup(signal.id)], {signal.id: signal})
    notifier = _notifier()

    updates = await _linked_forecaster(store, notifier, [_AT_RISK], base).run()

    sent = notifier.send.await_args.args[0]
    assert "dashboard" not in sent
    assert sent == format_forecaster_update(signal.as_proposal(), updates[0])
