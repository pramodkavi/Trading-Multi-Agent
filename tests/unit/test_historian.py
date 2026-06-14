"""Unit tests for the Historian agent (Step 2.4b).

No database here: a fake ``SignalStore`` returns canned ``StoredSignal`` rows so
we exercise the backend-agnostic logic in isolation --

- the similarity helpers (_tag_overlap / _extract_l2_vector / _l2_distance),
- HistorianRepository.retrieve: query-param derivation, win-rate / outcome math,
  match ranking, and summary text,
- make_historian_node: proposal -> report, skip -> None.

The actual stage-1/2/3 SQL is covered by tests/unit/test_find_similar_dataapi.py
(Data API param/SQL shape, mocked) and tests/integration/test_historian_integration.py
(real Postgres ranking, opt-in).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from src.agents.historian import HistorianRepository, make_historian_node
from src.agents.historian.historian import (
    _extract_l2_vector,
    _l2_distance,
    _tag_overlap,
)
from src.common.models import (
    ScanContext,
    ScanSession,
    SignalDirection,
    SignalOutcome,
    SignalProposal,
    SignalStatus,
    SkipDecision,
    SkipReason,
)
from src.persistence.models import StoredSignal

# ---------------------------------------------------------------------------
# Fakes / builders
# ---------------------------------------------------------------------------


class FakeStore:
    """Records find_similar_signals kwargs; returns a fixed row list."""

    def __init__(self, rows: list[StoredSignal] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.calls: list[dict[str, Any]] = []

    async def find_similar_signals(self, **kwargs: Any) -> list[StoredSignal]:
        self.calls.append(kwargs)
        return self.rows


def make_proposal(**overrides: Any) -> SignalProposal:
    base: dict[str, Any] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "direction": SignalDirection.LONG,
        "entry_price": 100.0,
        "stop_loss": 97.0,
        "take_profit_1": 109.0,
        "risk_reward_ratio": 3.0,
        "leverage": 3.0,
        "risk_percent": 1.0,
        "tags": ["smc", "bias-uptrend", "long", "discount", "bullish-ob", "liquidity-sweep"],
        "confluence_narrative": "Bullish OB in discount with a liquidity sweep below equal lows.",
        "features": {
            "primary_poi_type": "order_block",
            "confluence_score": 4,
            "ob_confluence_count": 2,
        },
    }
    base.update(overrides)
    return SignalProposal(**base)


def make_stored(
    *,
    outcome: SignalOutcome | None,
    tags: list[str] | None = None,
    features: dict[str, Any] | None = None,
    direction: SignalDirection = SignalDirection.LONG,
    created_at: datetime | None = None,
) -> StoredSignal:
    return StoredSignal(
        id=uuid4(),
        scan_id=uuid4(),
        symbol="BTCUSDT",
        strategy="smc",
        direction=direction,
        status=SignalStatus.PUBLISHED,
        created_at=created_at or datetime(2026, 6, 1, 8, tzinfo=UTC),
        payload={},
        tags=tags if tags is not None else ["smc", "long", "bullish-ob"],
        features=features
        if features is not None
        else {"confluence_score": 4, "ob_confluence_count": 2},
        outcome=outcome,
        outcome_metadata=None,
    )


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------


def test_tag_overlap_counts_intersection() -> None:
    assert _tag_overlap(["a", "b", "c"], ["b", "c", "d"]) == 2
    assert _tag_overlap(["a"], ["x", "y"]) == 0
    assert _tag_overlap([], ["a"]) == 0


def test_extract_l2_vector_only_scale_free_numerics() -> None:
    features = {
        "confluence_score": 5,
        "ob_confluence_count": 3,
        "current_price": 64000.0,  # excluded: price-scale
        "zone": "DISCOUNT",  # excluded: categorical
        "factor_ote": True,  # excluded: bool
    }
    vector = _extract_l2_vector(features)
    assert vector == [("confluence_score", 5.0), ("ob_confluence_count", 3.0)]


def test_l2_distance_matches_euclidean() -> None:
    query = [("confluence_score", 4.0), ("ob_confluence_count", 2.0)]
    # match differs by (1, 2) -> sqrt(1 + 4) = sqrt(5)
    match_features = {"confluence_score": 5, "ob_confluence_count": 0}
    assert _l2_distance(query, match_features) == 5**0.5


def test_l2_distance_missing_key_contributes_zero() -> None:
    query = [("confluence_score", 4.0), ("ob_confluence_count", 2.0)]
    # ob_confluence_count absent -> contributes 0; only confluence_score differs by 1
    match_features = {"confluence_score": 5}
    assert _l2_distance(query, match_features) == 1.0


# ---------------------------------------------------------------------------
# HistorianRepository.retrieve -- query derivation
# ---------------------------------------------------------------------------


async def test_retrieve_derives_query_params_from_proposal() -> None:
    store = FakeStore()
    repo = HistorianRepository(store)
    proposal = make_proposal()

    await repo.retrieve(proposal, session=ScanSession.LONDON)

    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["direction"] == "LONG"
    assert call["session"] == "LONDON"
    assert call["primary_poi_type"] == "order_block"
    assert call["query_tags"] == proposal.tags
    assert call["l2_features"] == [("confluence_score", 4.0), ("ob_confluence_count", 2.0)]
    assert call["exclude_signal_id"] is None


async def test_retrieve_without_session_passes_none() -> None:
    store = FakeStore()
    repo = HistorianRepository(store)
    await repo.retrieve(make_proposal(), session=None)
    assert store.calls[0]["session"] is None


# ---------------------------------------------------------------------------
# HistorianReport -- win-rate / outcome math
# ---------------------------------------------------------------------------


async def test_report_win_rate_and_outcome_counts() -> None:
    rows = [
        make_stored(outcome=SignalOutcome.WIN),
        make_stored(outcome=SignalOutcome.WIN),
        make_stored(outcome=SignalOutcome.WIN),
        make_stored(outcome=SignalOutcome.LOSS),
        make_stored(outcome=SignalOutcome.BREAKEVEN),
        make_stored(outcome=SignalOutcome.INVALIDATED),
    ]
    repo = HistorianRepository(FakeStore(rows))
    report = await repo.retrieve(make_proposal(), session=ScanSession.NY)

    assert report.sample_size == 6
    assert report.wins == 3
    assert report.losses == 1
    assert report.breakeven == 1
    assert report.inconclusive == 1
    assert report.decisive == 4
    assert report.win_rate == 0.75
    assert "75% win rate" in report.summary
    assert report.direction is SignalDirection.LONG
    assert report.session == "NY"
    assert report.primary_poi_type == "order_block"


async def test_report_empty_sample() -> None:
    repo = HistorianRepository(FakeStore([]))
    report = await repo.retrieve(make_proposal(), session=ScanSession.LONDON)
    assert report.sample_size == 0
    assert report.win_rate is None
    assert report.matches == []
    assert "No comparable" in report.summary


async def test_report_no_decisive_outcomes() -> None:
    rows = [
        make_stored(outcome=SignalOutcome.BREAKEVEN),
        make_stored(outcome=SignalOutcome.INVALIDATED),
        make_stored(outcome=SignalOutcome.EXPIRED),
    ]
    repo = HistorianRepository(FakeStore(rows))
    report = await repo.retrieve(make_proposal(), session=None)
    assert report.win_rate is None
    assert report.breakeven == 1
    assert report.inconclusive == 2
    assert "no decisive" in report.summary


# ---------------------------------------------------------------------------
# Match ranking
# ---------------------------------------------------------------------------


async def test_matches_ranked_by_similarity() -> None:
    query = make_proposal(
        tags=["smc", "long", "bullish-ob", "liquidity-sweep"],
        features={
            "primary_poi_type": "order_block",
            "confluence_score": 4,
            "ob_confluence_count": 2,
        },
    )
    near = make_stored(
        outcome=SignalOutcome.WIN,
        tags=["smc", "long", "bullish-ob", "liquidity-sweep"],  # overlap 4
        features={"confluence_score": 4, "ob_confluence_count": 2},  # l2 = 0
    )
    far = make_stored(
        outcome=SignalOutcome.LOSS,
        tags=["smc", "short"],  # overlap 1
        features={"confluence_score": 1, "ob_confluence_count": 0},  # l2 large
    )
    # store returns them in "wrong" order; the repository re-sorts by similarity
    repo = HistorianRepository(FakeStore([far, near]))
    report = await repo.retrieve(query, session=None)

    assert report.matches[0].signal_id == near.id
    assert report.matches[0].l2_distance == 0.0
    assert report.matches[0].tag_overlap == 4
    assert report.matches[1].signal_id == far.id
    assert report.matches[1].l2_distance > report.matches[0].l2_distance


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------


async def test_node_skip_returns_no_report() -> None:
    store = FakeStore([make_stored(outcome=SignalOutcome.WIN)])
    node = make_historian_node(HistorianRepository(store))
    skip = SkipDecision(
        scan_id=uuid4(),
        strategy="smc",
        symbol="ETHUSDT",
        reason=SkipReason.NO_CLEAR_BIAS,
        details="No directional bias this scan.",
    )
    result = await node({"proposal": skip})
    assert result == {"historian_report": None}
    assert store.calls == []  # retrieval never ran for a skip


async def test_node_proposal_produces_report_with_session() -> None:
    store = FakeStore([make_stored(outcome=SignalOutcome.WIN)])
    node = make_historian_node(HistorianRepository(store))
    proposal = make_proposal()
    ctx = ScanContext(session=ScanSession.OVERLAP, symbols=["BTCUSDT"], strategy="smc")

    result = await node({"proposal": proposal, "scan_context": ctx})

    report = result["historian_report"]
    assert report is not None
    assert report.query_proposal_id == proposal.proposal_id
    assert report.sample_size == 1
    assert store.calls[0]["session"] == "OVERLAP"


async def test_node_missing_proposal_returns_no_report() -> None:
    store = FakeStore()
    node = make_historian_node(HistorianRepository(store))
    result = await node({})
    assert result == {"historian_report": None}
