"""Unit tests for the synthetic-signal seed fixture (scripts/seed_signals.py).

Verifies the generator produces valid, analyzer-shaped proposals with a useful
outcome spread, and that seed() drives the store lifecycle (scan -> signal ->
outcome) for each row. No database -- a fake store records the calls.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from scripts.seed_signals import build_synthetic_signals, seed
from src.common.models import (
    SignalDirection,
    SignalOutcome,
    SignalProposal,
)


class RecordingStore:
    def __init__(self) -> None:
        self.scans: list[dict[str, Any]] = []
        self.signals: list[SignalProposal] = []
        self.outcomes: list[dict[str, Any]] = []
        self.closed = False

    async def start_scan(self, **kwargs: Any) -> None:
        self.scans.append(kwargs)

    async def create_signal(self, payload: SignalProposal) -> UUID:
        self.signals.append(payload)
        return uuid4()

    async def set_signal_outcome(self, **kwargs: Any) -> None:
        self.outcomes.append(kwargs)

    async def aclose(self) -> None:
        self.closed = True


def test_build_synthetic_signals_count_and_shape() -> None:
    signals = build_synthetic_signals(50)
    assert len(signals) == 50
    for item in signals:
        assert isinstance(item.proposal, SignalProposal)
        # scan_id is shared between the scan_run and the proposal (FK integrity).
        assert item.proposal.scan_id == item.scan_id
        assert isinstance(item.outcome, SignalOutcome)
        features = item.proposal.features
        assert features["primary_poi_type"] == "order_block"
        assert "confluence_score" in features
        assert "ob_confluence_count" in features
        assert "smc" in item.proposal.tags


def test_build_synthetic_signals_has_useful_distribution() -> None:
    signals = build_synthetic_signals(50)
    directions = {s.proposal.direction for s in signals}
    sessions = {s.session for s in signals}
    outcomes = [s.outcome for s in signals]

    assert directions == {SignalDirection.LONG, SignalDirection.SHORT}
    assert len(sessions) >= 3
    # Enough decisive outcomes for a meaningful Historian win rate.
    assert outcomes.count(SignalOutcome.WIN) >= 5
    assert outcomes.count(SignalOutcome.LOSS) >= 3


def test_build_synthetic_signals_deterministic_structure() -> None:
    # IDs/timestamps are unique per row (PKs/FKs); the *structure* is what is
    # deterministic given the index.
    def shape(item: Any) -> tuple[Any, ...]:
        p = item.proposal
        return (
            p.direction,
            p.symbol,
            item.session,
            tuple(p.tags),
            tuple(sorted(p.features.items())),
            p.entry_price,
            p.stop_loss,
            p.take_profit_1,
            p.risk_reward_ratio,
            item.outcome,
        )

    a = build_synthetic_signals(7)
    b = build_synthetic_signals(7)
    assert [shape(s) for s in a] == [shape(s) for s in b]


async def test_seed_drives_store_lifecycle() -> None:
    store = RecordingStore()
    inserted = await seed(store, build_synthetic_signals(5))

    assert inserted == 5
    assert len(store.scans) == 5
    assert len(store.signals) == 5
    assert len(store.outcomes) == 5
    # Each scan carries the session + the proposal's symbol.
    assert store.scans[0]["session"] in {"LONDON", "NY", "OVERLAP", "DAILY_WRAP"}
    assert store.scans[0]["symbols"] == [store.signals[0].symbol]
    # Outcome write targets a signal and carries metadata.
    assert isinstance(store.outcomes[0]["outcome"], SignalOutcome)
    assert "source" in store.outcomes[0]["outcome_metadata"]
