"""Historian agent: three-stage journal retrieval + empirical track record.

SPEC §3.1 role 2 / FR-1.4 / §4 Step 2.4. The Historian answers a single
question about the current proposal: *"when setups like this happened before,
how did they turn out?"* It does so with a three-stage retrieval over the
``signals`` journal:

    Stage 1 -- hard categorical filters (direction, session, primary_poi_type)
               that must match exactly. Narrows to like-for-like setups.
    Stage 2 -- tag-overlap ranking (PostgreSQL array operators) -- order the
               survivors by how many semantic tags they share with the query.
    Stage 3 -- numeric L2-distance ranking -- re-rank the tag-overlap pool by
               Euclidean distance over a scale-free numeric feature vector and
               return the top-K most-similar precedents.

The SQL that implements all three stages lives in the persistence backends
(``find_similar_signals`` on each ``SignalStore``), because the parameter
binding differs between asyncpg and the RDS Data API. This module owns the
backend-agnostic part: deriving the query parameters from the proposal,
turning the retrieved rows into a ``HistorianReport``, and the LangGraph node.

The win rate it reports is an honest empirical frequency over a (usually small)
sample, never a calibrated probability -- ``sample_size`` travels with it so the
Judge can weight it appropriately (see docs/research/smc-evidence-review.md).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

from src.agents.historian.models import HistorianReport, HistoricalMatch
from src.common.models import SignalOutcome, SignalProposal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable, Mapping
    from typing import Any

    from src.agents.orchestration.graph import AgentState
    from src.common.models import ScanSession
    from src.persistence import SignalStore
    from src.persistence.models import StoredSignal

# ---------------------------------------------------------------------------
# Retrieval tuning
# ---------------------------------------------------------------------------

L2_FEATURE_KEYS: Final[tuple[str, ...]] = ("confluence_score", "ob_confluence_count")
"""Numeric, *scale-free* feature keys that define the stage-3 similarity vector.

Deliberately excludes absolute price-scale features (current_price, atr,
ob_zone_high/low): an L2 distance over those would be dominated by the
instrument's price magnitude (BTC at 60k vs ETH at 3k) rather than by setup
shape, which is meaningless for similarity. These two are small, comparable
integer scores. The set is intended to GROW as the analyzer emits more
scale-free features (e.g. ATR-normalized pullback depth, R:R) -- both the SQL
and the Python recompute key off this one constant, so they stay in sync."""

DEFAULT_TOP_K: Final[int] = 10
"""Default number of precedents returned (SPEC FR-1.4 K=10)."""

DEFAULT_TAG_POOL: Final[int] = 50
"""Stage-2 candidate pool size handed to stage-3 re-ranking."""

PRIMARY_POI_TYPE_FEATURE: Final[str] = "primary_poi_type"
"""Feature key carrying the POI kind used by the stage-1 hard filter."""


# ---------------------------------------------------------------------------
# Similarity helpers (the canonical definitions; the SQL mirrors these)
# ---------------------------------------------------------------------------


def _tag_overlap(query_tags: list[str], match_tags: list[str]) -> int:
    """Count of tags shared between the query and a match (stage-2 metric)."""
    return len(set(query_tags) & set(match_tags))


def _extract_l2_vector(features: Mapping[str, Any]) -> list[tuple[str, float]]:
    """Project a features map onto the L2 vector: (key, float value) for each
    L2 key present as a real number (bools excluded -- they are tag-encoded)."""
    vector: list[tuple[str, float]] = []
    for key in L2_FEATURE_KEYS:
        value = features.get(key)
        if isinstance(value, bool):  # bool is an int subclass; never an L2 dim
            continue
        if isinstance(value, int | float):
            vector.append((key, float(value)))
    return vector


def _l2_distance(query_vector: list[tuple[str, float]], match_features: Mapping[str, Any]) -> float:
    """Euclidean distance from the query vector to a match's features.

    A key absent (or non-numeric) in the match contributes nothing -- mirroring
    the SQL ``COALESCE(match_value, query_value) - query_value`` so the Python
    report and the DB ranking agree by construction.
    """
    total = 0.0
    for key, query_value in query_vector:
        raw = match_features.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            match_value = query_value
        else:
            match_value = float(raw)
        total += (match_value - query_value) ** 2
    return math.sqrt(total)


# ---------------------------------------------------------------------------
# HistorianRepository
# ---------------------------------------------------------------------------


class HistorianRepository:
    """Backend-agnostic Historian retrieval over a ``SignalStore``.

    Owns the *orchestration* of the three-stage retrieval and the construction
    of the ``HistorianReport``. The stage-1/2/3 SQL itself is delegated to the
    injected store's ``find_similar_signals`` so the same logic runs against
    either asyncpg (local) or the RDS Data API (cloud).
    """

    def __init__(self, store: SignalStore) -> None:
        self._store = store

    async def retrieve(
        self,
        proposal: SignalProposal,
        *,
        session: ScanSession | None = None,
        limit: int = DEFAULT_TOP_K,
        tag_pool: int = DEFAULT_TAG_POOL,
    ) -> HistorianReport:
        """Find precedents resembling ``proposal`` and summarise their outcomes."""
        primary_poi = proposal.features.get(PRIMARY_POI_TYPE_FEATURE)
        primary_poi_type = primary_poi if isinstance(primary_poi, str) else None
        query_tags = list(proposal.tags)
        query_vector = _extract_l2_vector(proposal.features)
        session_value = session.value if session is not None else None

        rows = await self._store.find_similar_signals(
            direction=proposal.direction.value,
            session=session_value,
            primary_poi_type=primary_poi_type,
            query_tags=query_tags,
            l2_features=query_vector,
            limit=limit,
            tag_pool=tag_pool,
            exclude_signal_id=None,
        )

        matches = [self._to_match(row, query_tags, query_vector) for row in rows]
        # The SQL already ranked + limited; re-sort on the same key so the
        # displayed order is deterministic regardless of DB tie-breaking.
        matches.sort(key=lambda m: (m.l2_distance, -m.tag_overlap, str(m.signal_id)))

        return self._build_report(proposal, session_value, primary_poi_type, matches)

    @staticmethod
    def _to_match(
        row: StoredSignal,
        query_tags: list[str],
        query_vector: list[tuple[str, float]],
    ) -> HistoricalMatch:
        return HistoricalMatch(
            signal_id=row.id,
            symbol=row.symbol,
            direction=row.direction,
            created_at=row.created_at,
            outcome=row.outcome,
            tags=list(row.tags),
            tag_overlap=_tag_overlap(query_tags, list(row.tags)),
            l2_distance=_l2_distance(query_vector, row.features),
        )

    @staticmethod
    def _build_report(
        proposal: SignalProposal,
        session_value: str | None,
        primary_poi_type: str | None,
        matches: list[HistoricalMatch],
    ) -> HistorianReport:
        wins = sum(1 for m in matches if m.outcome is SignalOutcome.WIN)
        losses = sum(1 for m in matches if m.outcome is SignalOutcome.LOSS)
        breakeven = sum(1 for m in matches if m.outcome is SignalOutcome.BREAKEVEN)
        inconclusive = sum(
            1 for m in matches if m.outcome in (SignalOutcome.INVALIDATED, SignalOutcome.EXPIRED)
        )
        decisive = wins + losses
        win_rate = wins / decisive if decisive > 0 else None

        summary = _summarise(
            direction=proposal.direction.value,
            session_value=session_value,
            primary_poi_type=primary_poi_type,
            sample_size=len(matches),
            wins=wins,
            losses=losses,
            breakeven=breakeven,
            inconclusive=inconclusive,
            win_rate=win_rate,
        )

        return HistorianReport(
            query_proposal_id=proposal.proposal_id,
            strategy=proposal.strategy,
            direction=proposal.direction,
            session=session_value,
            primary_poi_type=primary_poi_type,
            sample_size=len(matches),
            wins=wins,
            losses=losses,
            breakeven=breakeven,
            inconclusive=inconclusive,
            win_rate=win_rate,
            matches=matches,
            summary=summary,
        )


def _summarise(
    *,
    direction: str,
    session_value: str | None,
    primary_poi_type: str | None,
    sample_size: int,
    wins: int,
    losses: int,
    breakeven: int,
    inconclusive: int,
    win_rate: float | None,
) -> str:
    """One-paragraph plain-language report for the Judge / Telegram (FR-5.2)."""
    poi = primary_poi_type or "any-POI"
    where = f" in {session_value}" if session_value else ""
    descriptor = f"{direction} {poi} setup(s){where}"
    if sample_size == 0:
        return f"No comparable {descriptor} found in the journal."
    if win_rate is None:
        verdict = "no decisive (win/loss) outcomes yet"
    else:
        verdict = f"{win_rate * 100:.0f}% win rate (n={wins + losses} decisive)"
    return (
        f"{sample_size} similar {descriptor}: "
        f"{wins}W / {losses}L / {breakeven}BE / {inconclusive} inconclusive "
        f"-> {verdict}."
    )


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------


def make_historian_node(
    repository: HistorianRepository,
) -> Callable[[AgentState], Awaitable[AgentState]]:
    """Build the ``historian`` LangGraph node bound to a repository.

    A factory (rather than a bare module-level function) so the store/repository
    is injected via closure and never has to live in the checkpointed AgentState
    -- the same pattern the Skeptic/Judge nodes will use when Step 2.7 wires the
    full pipeline (analyzer -> historian -> skeptic -> judge). The node is a
    no-op for SkipDecisions: there is no setup to find precedents for.
    """

    async def historian_node(state: AgentState) -> AgentState:
        proposal = state.get("proposal")
        if not isinstance(proposal, SignalProposal):
            return {"historian_report": None}
        scan_context = state.get("scan_context")
        session = scan_context.session if scan_context is not None else None
        report = await repository.retrieve(proposal, session=session)
        return {"historian_report": report}

    return historian_node
