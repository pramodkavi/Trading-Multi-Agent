"""In-process reservation ledger keeping the §1.6 stateful caps exact under
parallel multi-symbol scans (SPEC §4 Step 2.13).

Why this exists
---------------
The risk gate (:mod:`src.agents.orchestration.risk_gates`) reads portfolio state
— open-setup count, signals in the last 24h, correlated exposure — at the
*start* of a symbol's pipeline (``gather_risk_context``), but the reservation
that actually consumes a slot (``open_active_setup``) only lands at the *end*,
after the Skeptic and Judge. Step 2.13 runs the watchlist concurrently, so two
symbols can both read the same stale counts, both clear ``check_max_concurrent``
/ ``check_daily_cap`` / ``check_correlated_exposure``, and both publish —
exceeding a non-overridable §1.6 hard cap by one. Sequential scanning never
could: each symbol saw the previous symbol's committed setup.

How it closes the window
------------------------
A symbol that clears the stateful gates *reserves* a pending slot under an
:class:`asyncio.Lock`. Later gates fold those reservations into the DB counts via
:meth:`augment`, so the caps hold across simultaneous publishes. The whole
read → evaluate → reserve sequence runs inside the lock, so two gates can never
both pass on the same stale snapshot.

A reservation is released (:meth:`release`) only when the symbol turns out **not**
to publish — a Judge veto, or a pipeline error. A reservation that *does* publish
is left in place for the rest of the batch: once its ``active_setups`` row is
committed the DB and the ledger both count it, so the effective count can only
ever err *strict* (block one extra borderline signal), never over the cap. That
is the deliberate trade-off behind the "exact" choice — a hard rule must never be
violated, and over-strictness in a rare race is the safe direction to round.

Lifetime is one scan batch == one Lambda invocation: ``_run_symbols`` builds one
ledger, threads it through ``build_pipeline_graph`` into the risk-gate node, and
lets it fall out of scope when the batch ends. The single-symbol CLI path and the
unit tests pass **no** ledger, and the gate behaves exactly as before (Step 2.11).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src.common.models import SignalDirection

if TYPE_CHECKING:  # pragma: no cover - typing only
    from uuid import UUID

    from src.agents.orchestration.risk_gates import RiskContext


class ScanReservationLedger:
    """Tracks pending (not-yet-persisted) publishes for one parallel scan batch.

    Concurrency contract:

    * :meth:`reserve` and :meth:`augment` mutate/read shared state and **must**
      be called while holding :attr:`lock` (the risk-gate node does this, wrapping
      its whole read → evaluate → reserve critical section in the lock).
    * :meth:`release` takes the lock itself, so it must **not** be called while the
      caller already holds it (the scan runner calls it after the graph returns,
      with the lock long released).

    :class:`asyncio.Lock` is not reentrant; keeping the two call sites distinct is
    what makes that safe.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # scan_id -> (symbol, direction) of a pending publish that has cleared the
        # stateful gates but whose active_setups row is not yet committed.
        self._pending: dict[UUID, tuple[str, SignalDirection]] = {}

    @property
    def lock(self) -> asyncio.Lock:
        """The mutex serialising the risk gate's read → evaluate → reserve."""
        return self._lock

    def reserve(self, *, scan_id: UUID, symbol: str, direction: SignalDirection) -> None:
        """Record a pending publish for ``scan_id``.

        MUST be called while holding :attr:`lock`. Re-reserving the same scan_id
        simply overwrites (a scan reserves at most once).
        """
        self._pending[scan_id] = (symbol, direction)

    async def release(self, scan_id: UUID) -> None:
        """Drop the reservation for ``scan_id`` if present (idempotent).

        Called when a reserved symbol will not publish (Judge veto or pipeline
        error) so its slot does not wrongly block later symbols. Acquires
        :attr:`lock` itself; never call while already holding it.
        """
        async with self._lock:
            self._pending.pop(scan_id, None)

    def augment(self, context: RiskContext) -> RiskContext:
        """Return ``context`` with the pending reservations folded into the
        stateful-rule inputs.

        MUST be called while holding :attr:`lock`. Each pending publish counts as
        one more open setup (rule 4), one more signal in the 24h window (rule 5),
        and contributes its (symbol, direction) to the open exposure the
        correlation rule (rule 9) inspects. The loss-streak rule (6) is unaffected
        — it depends on *resolved* outcomes, which a pending publish cannot change.

        The gate reserves *after* evaluating, so the symbol being evaluated is
        never present in ``_pending`` yet and never counts against itself.
        """
        pending = len(self._pending)
        if pending == 0:
            return context
        return context.model_copy(
            update={
                "open_setup_count": context.open_setup_count + pending,
                "published_last_24h": context.published_last_24h + pending,
                "open_exposure": context.open_exposure + tuple(self._pending.values()),
            }
        )

    def pending_count(self) -> int:
        """Number of reservations currently held (for tests / logging)."""
        return len(self._pending)


__all__ = ["ScanReservationLedger"]
