"""Live-Postgres integration test for the Historian's three-stage retrieval.

Marked `integration` and skipped by default. Run with:

    docker compose up -d db
    export DATABASE_URL="postgresql://signals:signals@localhost:5433/signals"
    pytest -m integration tests/integration/test_historian_integration.py

This is where the actual stage-1/2/3 SQL (the JOIN, the array-operator tag
overlap, the sqrt/power L2 distance, the LIMITs) is exercised against a real
PostgreSQL -- the unit tests only assert SQL shape against a mock. We seed the
synthetic journal, then retrieve through AsyncpgSignalStore + HistorianRepository
and assert the hard filters, ranking, and win-rate math hold end to end.
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import psycopg
import pytest

from scripts.migrate import run_migration
from scripts.seed_signals import build_synthetic_signals, seed
from src.agents.historian import HistorianRepository
from src.common.models import ScanSession, SignalDirection, SignalProposal
from src.persistence.store import AsyncpgSignalStore

pytestmark = pytest.mark.integration

DATABASE_URL_ENV = "DATABASE_URL"


def _require_database_url() -> str:
    url = os.getenv(DATABASE_URL_ENV)
    if not url:
        pytest.skip(
            f"{DATABASE_URL_ENV} not set; start `docker compose up -d db` "
            "and export DATABASE_URL to run integration tests"
        )
    return url


def _reset_database(url: str) -> None:
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
        conn.commit()
    run_migration(database_url=url)


@pytest.fixture
async def store() -> AsyncpgSignalStore:  # type: ignore[misc]
    url = _require_database_url()
    _reset_database(url)
    pool = await asyncpg.create_pool(url, min_size=1, max_size=4)
    backend = AsyncpgSignalStore(pool)
    # Seed a populated journal (50 synthetic signals with outcomes).
    await seed(backend, build_synthetic_signals(50))
    try:
        yield backend
    finally:
        await backend.aclose()


def _long_query(**overrides: object) -> SignalProposal:
    base: dict[str, object] = {
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
        "confluence_narrative": "Bullish OB in discount with a sweep below equal lows.",
        "features": {
            "primary_poi_type": "order_block",
            "confluence_score": 4,
            "ob_confluence_count": 2,
        },
    }
    base.update(overrides)
    return SignalProposal(**base)  # type: ignore[arg-type]


async def test_retrieve_ranks_and_filters(store: AsyncpgSignalStore) -> None:
    repo = HistorianRepository(store)
    # In the synthetic set, LONDON scans (index % 4 == 0) are all LONG.
    report = await repo.retrieve(_long_query(), session=ScanSession.LONDON)

    assert report.sample_size > 0
    assert report.sample_size <= 10  # default top-K
    # Stage-1 hard filter held: every match is a LONG order-block setup.
    assert all(m.direction is SignalDirection.LONG for m in report.matches)
    # Stage-3 ordering: non-decreasing L2 distance (most similar first).
    distances = [m.l2_distance for m in report.matches]
    assert distances == sorted(distances)
    # Every match carries a known outcome, so the win-rate is well-defined.
    assert all(m.outcome is not None for m in report.matches)
    assert (
        report.wins + report.losses + report.breakeven + report.inconclusive == report.sample_size
    )


async def test_retrieve_session_filter_excludes_nonmatching(store: AsyncpgSignalStore) -> None:
    repo = HistorianRepository(store)
    # NY scans (index % 4 == 1) are all SHORT, so a LONG/NY query matches nothing.
    report = await repo.retrieve(_long_query(), session=ScanSession.NY)
    assert report.sample_size == 0
    assert report.win_rate is None
    assert "No comparable" in report.summary
