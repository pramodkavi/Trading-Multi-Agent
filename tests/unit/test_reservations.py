"""Unit tests for ScanReservationLedger (Step 2.13).

The ledger keeps the §1.6 stateful caps exact under parallel multi-symbol scans:
a passing proposal reserves a pending slot, later gates count it via ``augment``,
and a non-publishing symbol releases its slot. These tests pin that arithmetic
and the release idempotency; the end-to-end gate behaviour is covered in
test_run_scan.py / test_risk_gates.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from src.agents.orchestration import RiskContext, ScanReservationLedger
from src.common.models import SignalDirection

_NOW = datetime(2026, 6, 15, 13, 0, 0, tzinfo=UTC)


def _context(
    *,
    open_setup_count: int = 0,
    published_last_24h: int = 0,
    open_exposure: tuple[tuple[str, SignalDirection], ...] = (),
) -> RiskContext:
    return RiskContext(
        now=_NOW,
        open_setup_count=open_setup_count,
        published_last_24h=published_last_24h,
        consecutive_losses=0,
        open_exposure=open_exposure,
    )


def test_lock_is_an_asyncio_lock() -> None:
    ledger = ScanReservationLedger()
    assert isinstance(ledger.lock, asyncio.Lock)


def test_augment_no_reservations_is_identity() -> None:
    ledger = ScanReservationLedger()
    ctx = _context(open_setup_count=2, published_last_24h=3)
    out = ledger.augment(ctx)
    assert out.open_setup_count == 2
    assert out.published_last_24h == 3
    assert out.open_exposure == ()
    assert ledger.pending_count() == 0


def test_reserve_then_augment_adds_one_to_every_stateful_input() -> None:
    ledger = ScanReservationLedger()
    ledger.reserve(scan_id=uuid4(), symbol="ETHUSDT", direction=SignalDirection.LONG)

    ctx = _context(
        open_setup_count=1,
        published_last_24h=1,
        open_exposure=(("BTCUSDT", SignalDirection.SHORT),),
    )
    out = ledger.augment(ctx)

    assert out.open_setup_count == 2  # rule 4
    assert out.published_last_24h == 2  # rule 5
    # rule 9: the pending (symbol, direction) is appended to the DB exposure.
    assert out.open_exposure == (
        ("BTCUSDT", SignalDirection.SHORT),
        ("ETHUSDT", SignalDirection.LONG),
    )


def test_multiple_reservations_accumulate() -> None:
    ledger = ScanReservationLedger()
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        ledger.reserve(scan_id=uuid4(), symbol=symbol, direction=SignalDirection.LONG)
    assert ledger.pending_count() == 3
    out = ledger.augment(_context(open_setup_count=0, published_last_24h=0))
    assert out.open_setup_count == 3
    assert out.published_last_24h == 3
    assert len(out.open_exposure) == 3


def test_augment_does_not_touch_unrelated_fields() -> None:
    ledger = ScanReservationLedger()
    ledger.reserve(scan_id=uuid4(), symbol="BTCUSDT", direction=SignalDirection.LONG)
    ctx = _context(open_setup_count=0, published_last_24h=0)
    out = ledger.augment(ctx)
    # The loss-streak inputs depend on resolved outcomes, not pending publishes.
    assert out.consecutive_losses == ctx.consecutive_losses
    assert out.latest_loss_at == ctx.latest_loss_at
    assert out.now == ctx.now
    assert out.funding_rate == ctx.funding_rate


async def test_release_removes_reservation() -> None:
    ledger = ScanReservationLedger()
    scan_id = uuid4()
    ledger.reserve(scan_id=scan_id, symbol="BTCUSDT", direction=SignalDirection.LONG)
    assert ledger.pending_count() == 1

    await ledger.release(scan_id)
    assert ledger.pending_count() == 0
    # augment is back to identity after the release.
    assert ledger.augment(_context(open_setup_count=5)).open_setup_count == 5


async def test_release_unknown_scan_id_is_noop() -> None:
    ledger = ScanReservationLedger()
    ledger.reserve(scan_id=uuid4(), symbol="BTCUSDT", direction=SignalDirection.LONG)
    await ledger.release(uuid4())  # never reserved
    assert ledger.pending_count() == 1


async def test_release_is_idempotent() -> None:
    ledger = ScanReservationLedger()
    scan_id = uuid4()
    ledger.reserve(scan_id=scan_id, symbol="BTCUSDT", direction=SignalDirection.LONG)
    await ledger.release(scan_id)
    await ledger.release(scan_id)  # second release must not raise
    assert ledger.pending_count() == 0
