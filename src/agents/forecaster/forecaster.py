"""Forecaster agent: re-evaluate open setups every scan (Slice 2 Step 2.9).

SPEC §3.1.2 FR-2.1 / §4 Step 2.9. The Forecaster is a background loop (not part
of the per-signal pipeline). Each run it:

    1. Reads every OPEN setup from the journal (``list_open_active_setups``).
    2. For each: re-parses the original proposal, refetches current market data,
       and asks Claude whether the setup is STILL_VALID / AT_RISK / INVALIDATED
       (``ForecasterUpdate``).
    3. Acts on the verdict:
         STILL_VALID  -> record the evaluation; the setup stays OPEN.
         AT_RISK      -> record + send a Telegram warning (FR-5.3).
         INVALIDATED  -> close the setup with its terminal outcome, stamp the
                         same outcome on the signal journal, and notify.
    4. Records the evaluation -- or the failure -- in ``setup_evaluations``
       (Slice 4 Step 4.0, dashboard design §5.2 "Forecaster writes one row per
       setup incl. ERROR"). Every setup the sweep looked at leaves exactly one
       row: the verdict with its LLM latency / tokens / cost, an ERROR row for
       an orphaned setup, or an ERROR row carrying the exception text.

Per-setup work is isolated in try/except so one bad setup (e.g. an old payload
that no longer validates) never aborts the rest of the run -- the same
resilience the multi-symbol scan runner uses. A failure also logs the
``PROVIDER_ERROR provider=forecaster`` marker that the MonitoringStack metric
filter counts into ``ProviderErrorAlarm``, so a dead Anthropic key or exhausted
credit is alarmed within the hour instead of silently skipping every setup
(PROJECT_STATE §7 "Forecaster failures are invisible").

This step ships the Forecaster logic only; wiring it to a schedule (a separate
EventBridge rule -> Lambda) is Step 2.10, so running it live -- and the LLM cost
that implies -- is gated until then.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from src.agents.forecaster.models import ForecasterUpdate
from src.common.llm import DEFAULT_MODEL, structured_completion
from src.common.models import (
    ActiveSetupStatus,
    ForecastStatus,
    LlmUsage,
    SetupEvaluationStatus,
    SignalStatus,
)
from src.notifications import format_forecaster_update
from src.providers import Timeframe

if TYPE_CHECKING:  # pragma: no cover - typing only
    from anthropic import AsyncAnthropic

    from src.common.models import SignalProposal
    from src.notifications import Notifier
    from src.persistence import SignalStore
    from src.persistence.models import StoredActiveSetup
    from src.providers import DataProvider, MarketSnapshot

logger = logging.getLogger(__name__)

# Enough 4H history for the Forecaster to see the move since the setup opened.
CANDLE_LIMIT: Final[int] = 200


FORECASTER_SYSTEM_PROMPT: Final[str] = """\
You are the Forecaster in a multi-agent, signal-only crypto trading system. A \
setup was published earlier and the operator may be in the trade manually. Your \
job is to re-evaluate ONE open setup against the CURRENT market and decide \
whether it is still valid, at risk, or finished.

Return exactly one status:
- STILL_VALID: price action remains consistent with the setup; neither the \
target nor the invalidation has been reached and the premise is intact. No \
action is taken, so reserve this for genuinely on-track setups.
- AT_RISK: the setup is still live but threatened -- price is approaching the \
invalidation, or structure is shifting against the thesis. The operator gets a \
warning, so use this when something material has changed.
- INVALIDATED: the setup is finished. Set `outcome` to the terminal result: \
WIN if the target was reached, LOSS if the stop was hit, INVALIDATED if the \
structural premise broke before resolving, EXPIRED if it never resolved, \
BREAKEVEN if it closed around entry.

Rules:
- Reason ONLY from the supplied setup levels and current market data; never \
invent prices you were not given. Compare the current close and the recent \
high/low against the entry, stop, and target.
- Never reference forbidden indicators (RSI, MACD, Bollinger Bands, moving \
averages).
- A human with real money relies on this. Be precise and do not cry wolf: a \
small wobble is STILL_VALID, not AT_RISK.

Emit your verdict by calling the provided tool exactly once."""


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _render_setup(setup: StoredActiveSetup, proposal: SignalProposal) -> str:
    tp2 = f" | TP2 {proposal.take_profit_2}" if proposal.take_profit_2 is not None else ""
    return (
        "SETUP UNDER REVIEW\n"
        f"Symbol: {proposal.symbol} | Direction: {proposal.direction.value}\n"
        f"Opened: {setup.opened_at.isoformat()}\n"
        f"Entry {proposal.entry_price} | Stop-loss {proposal.stop_loss} | "
        f"TP1 {proposal.take_profit_1}{tp2}\n"
        f"Reward-to-risk {proposal.risk_reward_ratio}\n"
        f"Original thesis: {proposal.confluence_narrative}"
    )


def _summarise_market(snapshot: MarketSnapshot) -> str:
    candles = snapshot.klines.get(Timeframe.H4) or next(iter(snapshot.klines.values()))
    latest = candles[-1]
    window_high = max(c.high for c in candles)
    window_low = min(c.low for c in candles)
    return (
        "CURRENT MARKET (4H, latest snapshot)\n"
        f"Latest close: {latest.close}\n"
        f"Recent {len(candles)}-candle high / low: {window_high} / {window_low}\n"
        f"Snapshot taken: {snapshot.fetched_at.isoformat()}"
    )


def _build_user_prompt(
    setup: StoredActiveSetup,
    proposal: SignalProposal,
    snapshot: MarketSnapshot,
) -> str:
    return (
        f"{_render_setup(setup, proposal)}\n\n"
        f"{_summarise_market(snapshot)}\n\n"
        "YOUR TASK\n"
        "Decide whether this setup is STILL_VALID, AT_RISK, or INVALIDATED, "
        "citing the current levels above, and (only for INVALIDATED) set the "
        "terminal outcome. Then call the tool."
    )


# ---------------------------------------------------------------------------
# Forecaster
# ---------------------------------------------------------------------------


class Forecaster:
    """Re-evaluates open setups and acts on each verdict.

    Holds its collaborators (store, market-data provider, optional notifier,
    Anthropic client). ``run`` is the standalone entry point Step 2.10 will
    invoke from a scheduled Lambda.
    """

    def __init__(
        self,
        *,
        store: SignalStore,
        provider: DataProvider,
        notifier: Notifier | None = None,
        client: AsyncAnthropic | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._store = store
        self._provider = provider
        self._notifier = notifier
        self._client = client
        self._model = model

    async def run(self) -> list[ForecasterUpdate]:
        """Evaluate and act on every OPEN setup; returns the verdicts produced.

        Per-setup failures are logged and skipped so one bad setup never aborts
        the run. The returned list lets the caller (Step 2.10's Lambda) summarise
        how many setups were still valid / at risk / closed; ERROR rows are
        journaled but never appear in it.

        Step 4.0 (design §5.2): a failure inside :meth:`_evaluate_one` is
        (a) logged with its traceback, (b) logged again as the
        ``PROVIDER_ERROR provider=forecaster`` marker the CloudWatch metric
        filter counts, and (c) journaled as an ERROR ``setup_evaluations`` row.
        The journal write is guarded separately: if the store itself is down the
        failure is logged and the sweep still moves on to the next setup.
        """
        setups = await self._store.list_open_active_setups()
        logger.info("forecaster evaluating %d open setup(s)", len(setups))
        updates: list[ForecasterUpdate] = []
        for setup in setups:
            try:
                update = await self._evaluate_one(setup)
            except Exception as exc:
                logger.exception("forecaster failed for setup %s", setup.id)
                logger.warning(
                    "PROVIDER_ERROR provider=forecaster setup=%s error=%s: %s",
                    setup.id,
                    type(exc).__name__,
                    exc,
                )
                try:
                    await self._store.record_setup_evaluation(
                        setup_id=setup.id,
                        signal_id=setup.signal_id,
                        status=SetupEvaluationStatus.ERROR,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    # The journal write must never take the sweep down with it.
                    logger.exception("failed to record forecaster error row for setup %s", setup.id)
                continue
            if update is not None:
                updates.append(update)
        return updates

    async def _evaluate_one(self, setup: StoredActiveSetup) -> ForecasterUpdate | None:
        signal = await self._store.get_signal(setup.signal_id)
        if signal is None or signal.status is not SignalStatus.PUBLISHED:
            # Orphan: not an exception, so journal the ERROR row here directly
            # (design §5.2) -- no LLM call is made and nothing is returned.
            logger.warning(
                "setup %s references missing/non-published signal %s; skipping",
                setup.id,
                setup.signal_id,
            )
            await self._store.record_setup_evaluation(
                setup_id=setup.id,
                signal_id=setup.signal_id,
                status=SetupEvaluationStatus.ERROR,
                error=f"orphan: signal {setup.signal_id} missing or not PUBLISHED",
            )
            return None
        proposal = signal.as_proposal()
        snapshot = await self._provider.fetch_market_snapshot(
            proposal.symbol, [Timeframe.H4], limit=CANDLE_LIMIT
        )
        update, usage = await self.evaluate_traced(setup, proposal, snapshot)
        await self._apply(setup, proposal, update)
        # Journal after _apply so the row is only written once the verdict has
        # actually taken effect (design §5.2); if _apply raises, run() journals
        # the ERROR row instead.
        await self._store.record_setup_evaluation(
            setup_id=setup.id,
            signal_id=setup.signal_id,
            status=SetupEvaluationStatus(update.status.value),
            outcome=update.outcome,
            reasoning=update.reasoning,
            latency_ms=usage.latency_ms,
            token_usage=usage.token_usage(),
            cost_usd=usage.cost_usd,
        )
        return update

    async def evaluate(
        self,
        setup: StoredActiveSetup,
        proposal: SignalProposal,
        snapshot: MarketSnapshot,
    ) -> ForecasterUpdate:
        """Ask Claude to re-evaluate one setup against current market data.

        Delegates to :meth:`evaluate_traced` and drops the usage -- kept for
        callers that only need the verdict.
        """
        update, _usage = await self.evaluate_traced(setup, proposal, snapshot)
        return update

    async def evaluate_traced(
        self,
        setup: StoredActiveSetup,
        proposal: SignalProposal,
        snapshot: MarketSnapshot,
    ) -> tuple[ForecasterUpdate, LlmUsage]:
        """:meth:`evaluate` plus the LLM call's usage for ``setup_evaluations``.

        Step 4.0 (design §5.2 / NFR-2.3): the verdict row carries latency,
        tokens and cost per evaluation, so the verdict is returned alongside an
        :class:`LlmUsage` -- the same shape as ``Skeptic.evaluate_traced``.
        """
        result = await structured_completion(
            output_schema=ForecasterUpdate,
            system=FORECASTER_SYSTEM_PROMPT,
            user=_build_user_prompt(setup, proposal, snapshot),
            model=self._model,
            client=self._client,
            tool_name="emit_forecast",
            tool_description="Record your verdict on whether the setup is still valid.",
        )
        logger.debug(
            "forecast for %s setup %s: %s (cost_usd=%s)",
            proposal.symbol,
            setup.id,
            result.output.status.value,
            result.cost_usd,
        )
        return result.output, LlmUsage.from_result(result)

    async def _apply(
        self,
        setup: StoredActiveSetup,
        proposal: SignalProposal,
        update: ForecasterUpdate,
    ) -> None:
        evaluation = update.model_dump(mode="json")
        if update.status is ForecastStatus.INVALIDATED:
            # Validated on the model: outcome is present for INVALIDATED.
            outcome = update.outcome
            if outcome is None:  # pragma: no cover - guaranteed by ForecasterUpdate validator
                raise ValueError("INVALIDATED update missing outcome")
            await self._store.update_active_setup(
                setup_id=setup.id,
                status=ActiveSetupStatus(outcome.value),
                evaluation=evaluation,
            )
            await self._store.set_signal_outcome(
                signal_id=setup.signal_id,
                outcome=outcome,
                outcome_metadata=evaluation,
            )
            await self._notify(proposal, update)
        else:
            # STILL_VALID or AT_RISK: the setup stays OPEN; record the evaluation.
            await self._store.update_active_setup(
                setup_id=setup.id,
                status=ActiveSetupStatus.OPEN,
                evaluation=evaluation,
            )
            if update.status is ForecastStatus.AT_RISK:
                await self._notify(proposal, update)

    async def _notify(self, proposal: SignalProposal, update: ForecasterUpdate) -> None:
        if self._notifier is not None:
            await self._notifier.send(format_forecaster_update(proposal, update))
