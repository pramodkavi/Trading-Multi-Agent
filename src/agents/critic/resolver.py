"""OutcomeResolver: deterministic ground truth for every scoreable decision (design §5).

Layer 1 of Critic v0. Runs inside the scan Lambda (``{"mode": "resolve"}``)
because it is the only place that can reach Binance. The loop is:

    store.list_unresolved_decisions  ->  classify (inventory)
      ->  provider.fetch_klines (5m)  ->  replay (pure)  ->  store.save_resolution

The queue is the data itself (signals without a ``decision_resolutions`` row),
so the resolver is idempotent and resumable with no cursor state: a decision
whose window is still open, or whose candle fetch failed, is simply *deferred*
and picked up again on the next run.

Propagation rules (design §5.4):

* ``PUBLISHED`` decisions with a terminal outcome (WIN / LOSS / EXPIRED) also
  write ``signals.outcome`` (the resolver is the source of truth, overwriting
  any Forecaster value) and close the OPEN ``active_setups`` row.
* ``VETOED`` / ``GATE_REJECTED`` outcomes are counterfactual and land **only**
  in ``decision_resolutions`` -- they must never leak into the Historian's
  retrieval, which filters on PUBLISHED rows with an outcome.
* ``UNRESOLVABLE`` (proposal missing or corrupt) is written once so it is never
  retried, and never touches ``signals.outcome``.

Error handling (design §11): a ``ProviderError`` for one decision is logged with
the same ``PROVIDER_ERROR`` marker the scan emits (so the existing CloudWatch
alarm counts it), counted as deferred, and the batch continues. Any other
per-decision exception is logged with a traceback, counted as deferred, and
likewise never fails the batch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.agents.critic.inventory import classify, is_scoreable
from src.agents.critic.models import DecisionKind, DecisionRecord, Resolution, ResolutionOutcome
from src.agents.critic.replay import ENGINE_VERSION, MAX_HOLD, replay
from src.common.models import ActiveSetupStatus, SignalOutcome
from src.persistence.models import StoredSignal
from src.persistence.store import SignalStore
from src.providers.base import DataProvider, Kline, ProviderError, Timeframe

logger = logging.getLogger(__name__)

# Outcomes that are propagated onto ``signals.outcome`` for PUBLISHED decisions.
# UNRESOLVABLE is deliberately excluded: it says nothing about the market.
_TERMINAL = frozenset({ResolutionOutcome.WIN, ResolutionOutcome.LOSS, ResolutionOutcome.EXPIRED})

# Candles are fetched from the UTC day floor of the decision so every decision
# on the same symbol/day shares one fetch (design §5.2). The window must then
# extend far enough to cover the *latest* possible decision of that day through
# its full hold: one day for the decision time plus MAX_HOLD.
_ONE_DAY = timedelta(days=1)

# Replay resolution. 5m candles are the finest granularity the design fixes
# (§5.3); the engine is direction-agnostic about the timeframe, but the
# recorded ``candles_used`` and gap-through semantics assume this step.
_REPLAY_TIMEFRAME = Timeframe.M5


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _day_floor(ts: datetime) -> datetime:
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class ResolveSummary:
    """Result of one ``OutcomeResolver.run`` (design §5.5 invocation contract).

    ``deferred`` counts decisions whose window is still open *or* whose candle
    fetch / persistence failed -- both are retried next run. ``remaining`` is
    the number of unresolved decisions whose 7-day window has already closed,
    so a backfill loop over ``run()`` terminates when it reaches zero.
    """

    resolved: int
    deferred: int
    remaining: int


class OutcomeResolver:
    """Chunked, idempotent ground-truth resolution over the unresolved-decision queue."""

    def __init__(
        self,
        *,
        store: SignalStore,
        provider: DataProvider,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._provider = provider
        self._now = now
        # (symbol, window_start, window_end) -> candles, for one invocation only.
        self._cache: dict[tuple[str, datetime, datetime], list[Kline]] = {}

    async def run(self, *, chunk_size: int = 25) -> ResolveSummary:
        """Resolve up to ``chunk_size`` decisions; never raises for a single decision.

        ``now`` is sampled once so every decision in the chunk shares the same
        clock -- this keeps the candle cache key stable across the batch and
        makes the pending/EXPIRED boundary consistent within one run.
        """
        now = self._now()
        signals = await self._store.list_unresolved_decisions(limit=chunk_size, decided_before=now)
        resolved = deferred = 0
        for signal in signals:
            try:
                result = await self.resolve_one(signal, now=now)
            except ProviderError as exc:
                # Same marker as scripts/run_scan.py so the MonitoringStack
                # metric filter (ProviderErrors -> alarm, NFR-2.2) counts it.
                logger.warning(
                    "PROVIDER_ERROR provider=%s symbol=%s error=%s: %s",
                    type(self._provider).__name__,
                    signal.symbol,
                    type(exc).__name__,
                    exc,
                )
                deferred += 1
                continue
            except Exception:
                logger.exception("resolver failed for signal %s; deferred", signal.id)
                deferred += 1
                continue
            if result is None:
                deferred += 1
            else:
                resolved += 1
        remaining = await self._store.count_unresolved_decisions(decided_before=now - MAX_HOLD)
        logger.info(
            "resolver run complete: resolved=%d deferred=%d remaining=%d",
            resolved,
            deferred,
            remaining,
        )
        return ResolveSummary(resolved=resolved, deferred=deferred, remaining=remaining)

    async def resolve_one(
        self, signal: StoredSignal, *, now: datetime | None = None
    ) -> Resolution | None:
        """Resolve a single decision. Returns the saved ``Resolution``, or None when deferred.

        Raises ``ProviderError`` (and any store error) to the caller; ``run``
        turns those into deferrals so one bad decision cannot poison the batch.
        """
        if now is None:
            now = self._now()
        runs = await self._store.list_agent_runs_for_scan(signal.scan_id)
        record = classify(signal, runs)
        if not is_scoreable(record.kind):
            # The queue predicate excludes NO_SETUP rows; this only guards
            # direct callers.
            logger.debug("signal %s kind %s is not scoreable; skipping", signal.id, record.kind)
            return None

        if record.proposal is None:
            # Missing or corrupt proposal payload: write once, never retry.
            res = Resolution(
                signal_id=signal.id,
                decision_kind=record.kind,
                outcome=ResolutionOutcome.UNRESOLVABLE,
                decided_at=signal.created_at,
                entered_at=None,
                resolved_at=now,
                realized_r=0.0,
                candles_used=0,
                engine_version=ENGINE_VERSION,
            )
            await self._store.save_resolution(res)
            logger.info(
                "signal %s (%s) marked UNRESOLVABLE: no valid proposal payload",
                signal.id,
                record.kind,
            )
            return res

        candles = await self._candles(signal.symbol, signal.created_at, now)
        result = replay(
            proposal=record.proposal, decided_at=signal.created_at, candles=candles, now=now
        )
        if result.outcome is None:
            return None  # window still open, nothing hit yet -> retry next run

        res = Resolution(
            signal_id=signal.id,
            decision_kind=record.kind,
            outcome=result.outcome,
            decided_at=signal.created_at,
            entered_at=result.entered_at,
            resolved_at=result.resolved_at or now,
            realized_r=result.realized_r,
            max_adverse_excursion_r=result.max_adverse_excursion_r,
            max_favorable_excursion_r=result.max_favorable_excursion_r,
            tp2_reached=result.tp2_reached,
            candles_used=result.candles_used,
            engine_version=ENGINE_VERSION,
        )
        await self._store.save_resolution(res)
        await self._propagate(record, res)
        return res

    async def _propagate(self, record: DecisionRecord, res: Resolution) -> None:
        """Mirror a PUBLISHED terminal outcome onto ``signals`` and ``active_setups``."""
        if record.kind is not DecisionKind.PUBLISHED or res.outcome not in _TERMINAL:
            return
        meta = {"source": "resolver", **res.model_dump(mode="json")}
        await self._store.set_signal_outcome(
            signal_id=res.signal_id,
            outcome=SignalOutcome(res.outcome.value),
            outcome_metadata=meta,
        )
        await self._store.close_active_setup_for_signal(
            signal_id=res.signal_id,
            status=ActiveSetupStatus(res.outcome.value),
            evaluation={
                "source": "resolver",
                "outcome": res.outcome.value,
                "realized_r": res.realized_r,
            },
        )

    async def _candles(self, symbol: str, decided_at: datetime, now: datetime) -> list[Kline]:
        """5m candles covering ``decided_at .. decided_at + MAX_HOLD``, cached per symbol/day.

        The window is widened to ``[day_floor, day_floor + 1d + MAX_HOLD)`` (capped
        at ``now``) so every decision made on that symbol/day hits the same cache
        key; ``replay`` filters the candles it actually needs. The extra day costs
        at most one more Binance page.
        """
        start = _day_floor(decided_at)
        end = min(now, start + _ONE_DAY + MAX_HOLD)
        if end <= start:
            # Only reachable with a clock behind the decision time; nothing to
            # fetch and replay will report the decision as pending.
            return []
        key = (symbol, start, end)
        cached = self._cache.get(key)
        if cached is None:
            cached = await self._provider.fetch_klines(
                symbol, _REPLAY_TIMEFRAME, start=start, end=end
            )
            self._cache[key] = cached
        return cached
