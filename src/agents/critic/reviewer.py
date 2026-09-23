"""DecisionReviewer: Claude grades each resolved decision (design §6.1 / §6.2).

Layer 2 of Critic v0. Runs in GitHub Actions (``scripts/run_critic.py review``)
because it needs only Aurora (via the Data API) and Anthropic. The loop is:

    store.list_pending_reviews  ->  classify (inventory)  ->  store.get_resolution
      ->  structured_completion(DecisionReview)  ->  store.save_review
                                                 +   store.log_agent_run(critic)

The queue is the data itself: decisions with a ``decision_resolutions`` row and
no successful ``decision_reviews`` row, plus ``NO_SETUP`` decisions older than
seven days (reviewed without a resolution). A failed call writes an error row
(``review = {}``) that the queue predicate re-admits after one day, so a
transient Anthropic outage costs one retry, not a lost decision.

Each successful review is written twice on purpose (design §6.2): the
``decision_reviews`` row is what the report aggregates, and the ``agent_runs``
row with ``agent_role='critic'`` is what keeps NFR-2.3 cost tracking uniform
across every agent in the system.

Concurrency: calls run under an ``asyncio.Semaphore`` (default 4) so a chunk of
25 finishes in a few round trips without tripping Anthropic rate limits.
Per-decision failures are isolated -- one bad decision never fails the batch.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from src.agents.critic.inventory import classify
from src.agents.critic.models import DecisionKind, DecisionReview, Resolution
from src.agents.critic.prompts import REVIEW_SYSTEM_PROMPT, build_review_user_prompt
from src.common.llm import DEFAULT_MODEL, StructuredCompletionResult, structured_completion
from src.common.models import AgentRole

if TYPE_CHECKING:  # pragma: no cover - typing only
    from anthropic import AsyncAnthropic

    from src.persistence.models import StoredSignal
    from src.persistence.store import SignalStore

logger = logging.getLogger(__name__)

# NO_SETUP decisions are reviewed only once they are this old (design §6.1):
# young ones add nothing the aggregate cannot get later, and waiting keeps the
# review queue aligned with the resolver's 7-day replay window.
NO_SETUP_MIN_AGE: Final[timedelta] = timedelta(days=7)

# A failed review row is retried once it is this old (design §11).
FAILED_REVIEW_RETRY_AFTER: Final[timedelta] = timedelta(days=1)

# Error text column is unbounded TEXT, but keep the row readable.
_MAX_ERROR_CHARS: Final[int] = 2000

_TOOL_NAME: Final[str] = "emit_decision_review"
_TOOL_DESCRIPTION: Final[str] = "Record your graded review of this trading decision."


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _input_hash(signal_id: object) -> str:
    """Stable SHA-256 for the critic's ``agent_runs.input_hash`` (one review per signal)."""
    return hashlib.sha256(f"critic:{signal_id}".encode()).hexdigest()


@dataclass(frozen=True)
class ReviewSummary:
    """Result of one ``DecisionReviewer.run`` (the CLI prints ``remaining=`` from it).

    ``cost_usd`` sums the successful calls of this run only (unknown-model
    costs count as 0.0); ``remaining`` is the size of the pending queue after the
    batch, so a backfill loop over ``run()`` terminates when it reaches zero.
    """

    reviewed: int
    failed: int
    remaining: int
    cost_usd: float


class DecisionReviewer:
    """Chunked, idempotent per-decision Claude review over the pending-review queue."""

    def __init__(
        self,
        *,
        store: SignalStore,
        client: AsyncAnthropic,
        model: str = DEFAULT_MODEL,
        concurrency: int = 4,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self._store = store
        self._client = client
        self._model = model
        self._concurrency = concurrency
        self._now = now

    async def run(self, *, chunk_size: int = 25) -> ReviewSummary:
        """Review up to ``chunk_size`` pending decisions; never raises for a single one.

        ``now`` is sampled once so the queue predicate (``no_setup_before`` /
        ``retry_before``) is identical for the list and the trailing count.
        """
        now = self._now()
        no_setup_before = now - NO_SETUP_MIN_AGE
        retry_before = now - FAILED_REVIEW_RETRY_AFTER
        signals = await self._store.list_pending_reviews(
            limit=chunk_size, no_setup_before=no_setup_before, retry_before=retry_before
        )
        semaphore = asyncio.Semaphore(self._concurrency)
        results = await asyncio.gather(*(self._guarded(sig, semaphore) for sig in signals))

        reviewed = failed = 0
        cost_usd = 0.0
        for result in results:
            if result is None:
                failed += 1
            else:
                reviewed += 1
                cost_usd += result.cost_usd or 0.0
        remaining = await self._store.count_pending_reviews(
            no_setup_before=no_setup_before, retry_before=retry_before
        )
        logger.info(
            "reviewer run complete: reviewed=%d failed=%d remaining=%d cost_usd=%.4f",
            reviewed,
            failed,
            remaining,
            cost_usd,
        )
        return ReviewSummary(
            reviewed=reviewed, failed=failed, remaining=remaining, cost_usd=cost_usd
        )

    async def _guarded(
        self, signal: StoredSignal, semaphore: asyncio.Semaphore
    ) -> StructuredCompletionResult[DecisionReview] | None:
        """Run ``review_one`` under the semaphore; a persistence error counts as failed."""
        async with semaphore:
            try:
                return await self.review_one(signal)
            except Exception:
                logger.exception("reviewer could not persist review for signal %s", signal.id)
                return None

    async def review_one(
        self, signal: StoredSignal
    ) -> StructuredCompletionResult[DecisionReview] | None:
        """Review a single decision. Returns the completion result, or None on failure.

        A failure of the Claude call (after ``structured_completion``'s own
        retries) is recorded as an error row so the queue retries it after
        ``FAILED_REVIEW_RETRY_AFTER``. A failure *loading* the decision (store
        error) is logged and returns None without writing anything -- the row is
        still pending and will be picked up next run. Store errors while
        persisting a result propagate to the caller (``run`` counts them failed).
        """
        try:
            runs = await self._store.list_agent_runs_for_scan(signal.scan_id)
            record = classify(signal, runs)
            resolution: Resolution | None = None
            if record.kind is not DecisionKind.NO_SETUP:
                resolution = await self._store.get_resolution(signal.id)
        except Exception:
            logger.exception("reviewer could not load decision %s; left pending", signal.id)
            return None

        try:
            result = await structured_completion(
                output_schema=DecisionReview,
                system=REVIEW_SYSTEM_PROMPT,
                user=build_review_user_prompt(record, resolution),
                model=self._model,
                client=self._client,
                tool_name=_TOOL_NAME,
                tool_description=_TOOL_DESCRIPTION,
            )
        except Exception as exc:
            logger.exception("review call failed for signal %s (%s)", signal.id, record.kind)
            await self._store.save_review(
                signal_id=signal.id,
                decision_kind=record.kind.value,
                review={},
                model=self._model,
                token_usage={},
                cost_usd=None,
                error=f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS],
            )
            return None

        review = result.output.model_dump(mode="json")
        token_usage = {"tokens_in": result.tokens_in, "tokens_out": result.tokens_out}
        await self._store.save_review(
            signal_id=signal.id,
            decision_kind=record.kind.value,
            review=review,
            model=result.model,
            token_usage=token_usage,
            cost_usd=result.cost_usd,
            error=None,
        )
        await self._store.log_agent_run(
            scan_id=signal.scan_id,
            agent_role=AgentRole.CRITIC,
            strategy=signal.strategy,
            input_hash=_input_hash(signal.id),
            output=review,
            latency_ms=result.latency_ms,
            token_usage=token_usage,
            cost_usd=result.cost_usd,
        )
        logger.debug(
            "review for %s signal %s (%s): %s / %s (cost_usd=%s)",
            signal.symbol,
            signal.id,
            record.kind.value,
            result.output.failure_category.value,
            result.output.confidence.value,
            result.cost_usd,
        )
        return result
