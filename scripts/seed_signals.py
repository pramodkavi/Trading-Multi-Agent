"""Seed the signal journal with synthetic signals for Historian development.

SPEC §4 Step 2.4: "Add fixture script to seed 50 synthetic signals for
development." The Historian only returns useful results once the journal holds
PUBLISHED signals *with known outcomes*; this script manufactures a realistic
spread (symbols x directions x sessions x confluence x outcomes) so the
three-stage retrieval can be exercised against a populated DB locally and in
the deployed cluster.

Each synthetic row is structurally identical to real analyzer output (same tags
and ``features`` keys, including ``primary_poi_type``), so it travels through the
exact same retrieval path.

Usage (mirrors scripts/migrate.py):
    # Local dev (asyncpg over a Postgres socket).
    python -m scripts.seed_signals --backend asyncpg \
        --database-url "postgresql://signals:signals@localhost:5433/signals"

    # Serverless (Aurora RDS Data API).
    python -m scripts.seed_signals --backend dataapi \
        --cluster-arn arn:aws:rds:...:cluster:c \
        --secret-arn  arn:aws:secretsmanager:...:secret:s --db-name signals

The synthetic generation (``build_synthetic_signals``) is importable so tests
can seed the same data without shelling out.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from src.common.models import (
    ScanSession,
    SignalDirection,
    SignalOutcome,
    SignalProposal,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from src.persistence import SignalStore

logger = logging.getLogger(__name__)

DATABASE_URL_ENV: Final[str] = "DATABASE_URL"
CLUSTER_ARN_ENV: Final[str] = "DB_CLUSTER_ARN"
SECRET_ARN_ENV: Final[str] = "DB_SECRET_ARN"
DB_NAME_ENV: Final[str] = "DB_NAME"

DEFAULT_COUNT: Final[int] = 50

_SYMBOLS: Final[tuple[str, ...]] = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
_SESSIONS: Final[tuple[ScanSession, ...]] = (
    ScanSession.LONDON,
    ScanSession.NY,
    ScanSession.OVERLAP,
    ScanSession.DAILY_WRAP,
)
# A deterministic outcome spread with enough decisive (win/loss) rows for a
# meaningful win-rate plus some BE / inconclusive to exercise the report math.
_OUTCOME_CYCLE: Final[tuple[SignalOutcome, ...]] = (
    SignalOutcome.WIN,
    SignalOutcome.WIN,
    SignalOutcome.LOSS,
    SignalOutcome.WIN,
    SignalOutcome.BREAKEVEN,
    SignalOutcome.LOSS,
    SignalOutcome.WIN,
    SignalOutcome.INVALIDATED,
    SignalOutcome.WIN,
    SignalOutcome.EXPIRED,
)


@dataclass(frozen=True)
class SyntheticSignal:
    """One synthetic journal entry: the proposal plus its run context + outcome."""

    scan_id: UUID
    started_at: datetime
    session: ScanSession
    proposal: SignalProposal
    outcome: SignalOutcome
    outcome_metadata: dict[str, float | str]


def build_synthetic_signals(
    count: int = DEFAULT_COUNT,
    *,
    anchor: datetime | None = None,
) -> list[SyntheticSignal]:
    """Generate ``count`` synthetic signals with deterministic *structure*.

    The direction / symbol / session / tags / features / geometry / outcome of
    each row are a pure function of its index (no randomness), so the journal is
    reproducible. Identifiers (proposal_id, scan_id) are necessarily unique per
    row. ``anchor`` pins the historical timestamps; defaults to "now". Timestamps
    only affect scan_runs.started_at -- signals.created_at is the DB insert time.
    """
    base = anchor if anchor is not None else datetime.now(UTC)
    return [_build_one(i, base) for i in range(count)]


def _build_one(index: int, base: datetime) -> SyntheticSignal:
    scan_id = uuid4()
    is_long = index % 2 == 0
    direction = SignalDirection.LONG if is_long else SignalDirection.SHORT
    symbol = _SYMBOLS[index % len(_SYMBOLS)]
    session = _SESSIONS[index % len(_SESSIONS)]

    # Confluence factors -- the same booleans the analyzer derives.
    sweep = index % 2 == 0
    displacement = index % 3 == 0
    fvg = index % 3 == 1
    unmitigated = index % 4 != 0
    ote = index % 5 == 0
    score = (2 if sweep else 0) + displacement + fvg + unmitigated + ote
    ob_confluence_count = min(3, int(displacement) + int(fvg) + int(unmitigated))

    # Valid geometry (mirrors SignalProposal's cross-field validators).
    entry = 100.0
    risk = 3.0
    rr = 3.0 + (index % 3) * 0.5
    if is_long:
        stop_loss = entry - risk
        take_profit_1 = entry + rr * risk
    else:
        stop_loss = entry + risk
        take_profit_1 = entry - rr * risk

    tags = [
        "smc",
        f"bias-{'uptrend' if is_long else 'downtrend'}",
        direction.value.lower(),
        "discount" if is_long else "premium",
        "bullish-ob" if is_long else "bearish-ob",
    ]
    if sweep:
        tags.append("liquidity-sweep")
    if displacement:
        tags.append("displacement")
    if fvg:
        tags.append("fvg-confluence")
    if unmitigated:
        tags.append("unmitigated-ob")
    if ote:
        tags.append("ote")

    features: dict[str, float | int | str | bool] = {
        "timeframe": "H4",
        "phase": "UPTREND" if is_long else "DOWNTREND",
        "zone": "DISCOUNT" if is_long else "PREMIUM",
        "primary_poi_type": "order_block",
        "confluence_score": score,
        "current_price": entry,
        "atr": 2.5,
        "ob_index": index,
        "ob_zone_high": entry - (0.5 if is_long else -1.5),
        "ob_zone_low": entry - (1.5 if is_long else -0.5),
        "ob_confluence_count": ob_confluence_count,
        "factor_liquidity_sweep": sweep,
        "factor_ob_displacement": displacement,
        "factor_ob_fvg": fvg,
        "factor_ob_unmitigated": unmitigated,
        "factor_ote": ote,
    }

    proposal = SignalProposal(
        scan_id=scan_id,
        strategy="smc",
        symbol=symbol,
        direction=direction,
        entry_price=entry,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        risk_reward_ratio=rr,
        leverage=3.0,
        risk_percent=1.0,
        tags=tags,
        confluence_narrative=(
            f"Synthetic {direction.value} {symbol} setup off an order block in "
            f"{session.value}; confluence score {score} (seed index {index})."
        ),
        features=features,
    )

    outcome = _OUTCOME_CYCLE[index % len(_OUTCOME_CYCLE)]
    if outcome is SignalOutcome.WIN:
        realized_r = rr
    elif outcome is SignalOutcome.LOSS:
        realized_r = -1.0
    else:
        realized_r = 0.0
    outcome_metadata: dict[str, float | str] = {
        "exit_price": take_profit_1 if outcome is SignalOutcome.WIN else stop_loss,
        "realized_r": realized_r,
        "source": "seed_signals",
    }

    return SyntheticSignal(
        scan_id=scan_id,
        started_at=base - timedelta(hours=index),
        session=session,
        proposal=proposal,
        outcome=outcome,
        outcome_metadata=outcome_metadata,
    )


async def seed(store: SignalStore, signals: Sequence[SyntheticSignal]) -> int:
    """Insert each synthetic signal (scan_run -> signal -> outcome). Returns count."""
    for item in signals:
        await store.start_scan(
            scan_id=item.scan_id,
            started_at=item.started_at,
            session=item.session.value,
            strategy=item.proposal.strategy,
            symbols=[item.proposal.symbol],
        )
        signal_id = await store.create_signal(item.proposal)
        await store.set_signal_outcome(
            signal_id=signal_id,
            outcome=item.outcome,
            outcome_metadata=item.outcome_metadata,
        )
    return len(signals)


async def _build_store(args: argparse.Namespace) -> SignalStore:
    # Built inside the event loop so the asyncpg connection binds to the loop
    # that runs the seeding. Imported here so importing this module for
    # build_synthetic_signals never pulls asyncpg / boto3 (mirrors migrate.py).
    from src.persistence.store import AsyncpgSignalStore, DataApiSignalStore

    if args.backend == "dataapi":
        cluster_arn = args.cluster_arn or os.getenv(CLUSTER_ARN_ENV)
        secret_arn = args.secret_arn or os.getenv(SECRET_ARN_ENV)
        db_name = args.db_name or os.getenv(DB_NAME_ENV) or "signals"
        if not cluster_arn or not secret_arn:
            raise SystemExit(
                "--backend dataapi requires --cluster-arn/--secret-arn "
                f"(or ${CLUSTER_ARN_ENV}/${SECRET_ARN_ENV})."
            )
        return DataApiSignalStore.from_arns(
            cluster_arn=cluster_arn, secret_arn=secret_arn, database=db_name
        )

    dsn = args.database_url or os.getenv(DATABASE_URL_ENV)
    if not dsn:
        raise SystemExit(f"--backend asyncpg requires --database-url or ${DATABASE_URL_ENV}.")
    return await AsyncpgSignalStore.connect(dsn)


async def _run(args: argparse.Namespace) -> int:
    store = await _build_store(args)
    try:
        return await seed(store, build_synthetic_signals(max(1, args.count)))
    finally:
        await store.aclose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_signals",
        description="Seed the signal journal with synthetic signals for Historian dev.",
    )
    parser.add_argument("--backend", choices=["asyncpg", "dataapi"], default="asyncpg")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--database-url", dest="database_url", default=None)
    parser.add_argument("--cluster-arn", dest="cluster_arn", default=None)
    parser.add_argument("--secret-arn", dest="secret_arn", default=None)
    parser.add_argument("--db-name", dest="db_name", default=None)
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)-7s %(name)s: %(message)s")

    inserted = asyncio.run(_run(args))
    logger.info("Seeded %d synthetic signals via %s backend", inserted, args.backend)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
