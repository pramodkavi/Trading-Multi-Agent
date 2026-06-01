"""Idempotent schema migration runner.

Reads src/persistence/schema.sql and executes it against the target Postgres
database. Because every DDL statement uses IF NOT EXISTS, re-running the
script after a successful prior run is a no-op -- a property SPEC §5.2
requires for persistence steps.

Usage:
    # Local dev (psycopg over a Postgres socket): pull connection from env.
    python -m scripts.migrate

    # Override URL explicitly (handy for one-off targets).
    python -m scripts.migrate --database-url "postgresql://user:pw@host/db"

    # Serverless (Aurora RDS Data API; the VPC-less Lambda path).
    python -m scripts.migrate --backend dataapi \
        --cluster-arn arn:aws:rds:...:cluster:c \
        --secret-arn  arn:aws:secretsmanager:...:secret:s

    # Just print the SQL that would be executed; no connection touched.
    python -m scripts.migrate --dry-run

Connection precedence:
    psycopg  -- --database-url flag > DATABASE_URL env var.
    dataapi  -- --cluster-arn/--secret-arn flags > DB_CLUSTER_ARN/DB_SECRET_ARN
                env vars; --db-name > DB_NAME > 'signals'.

Why psycopg (sync) here instead of asyncpg:
    Migrations are one-shot operations. Async buys us nothing on a single
    connection executing one batch. psycopg has first-class type stubs
    (good for our mypy --strict policy) and integrates trivially with
    pgvector's adapter (psycopg.connect(...) + register_vector()).
    Step 1.9's repository layer is async because runtime hot paths benefit
    from it -- different tool for a different job.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg

from src.persistence import SCHEMA_SQL_PATH, run_data_api_migration

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

DATABASE_URL_ENV: str = "DATABASE_URL"
CLUSTER_ARN_ENV: str = "DB_CLUSTER_ARN"
SECRET_ARN_ENV: str = "DB_SECRET_ARN"
DB_NAME_ENV: str = "DB_NAME"


def run_migration(
    *,
    database_url: str,
    schema_path: Path = SCHEMA_SQL_PATH,
) -> None:
    """Execute schema_path against the database at database_url.

    Opens a single transaction; commits on success, rolls back on any failure.
    psycopg's context manager handles both. Logs the rendered SQL at DEBUG
    so operators can audit what ran.

    Raises:
        FileNotFoundError: schema_path does not exist.
        psycopg.Error: any DB connection or execution failure (callers can
            wrap or let this propagate; for the CLI we let it surface as a
            non-zero exit).
    """
    sql = _read_schema(schema_path)
    logger.info("Applying %s (%d bytes)", schema_path.name, len(sql))

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(sql)
        conn.commit()

    logger.info("Migration applied successfully")


def _read_schema(schema_path: Path) -> str:
    if not schema_path.exists():
        raise FileNotFoundError(f"schema file not found: {schema_path}")
    return schema_path.read_text(encoding="utf-8")


def _resolve_database_url(cli_arg: str | None) -> str:
    """CLI flag wins; otherwise env var; otherwise error out.

    We intentionally do NOT supply a default. Connecting to the wrong DB by
    accident is one of those mistakes that's hard to undo on prod; require
    the operator to be explicit.
    """
    if cli_arg:
        return cli_arg
    env_value = os.getenv(DATABASE_URL_ENV)
    if env_value:
        return env_value
    raise SystemExit(f"Provide --database-url or set {DATABASE_URL_ENV} environment variable.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate",
        description="Apply the crypto-signals Postgres schema (idempotent).",
    )
    parser.add_argument(
        "--backend",
        choices=["psycopg", "dataapi"],
        default="psycopg",
        help=(
            "How to reach the database: 'psycopg' (a Postgres socket, local "
            "dev) or 'dataapi' (Aurora RDS Data API, serverless). Default psycopg."
        ),
    )
    parser.add_argument(
        "--database-url",
        dest="database_url",
        default=None,
        help=(
            "Postgres connection URL "
            "(postgresql://user:pw@host:5432/db). "
            f"Falls back to ${DATABASE_URL_ENV}. Used by --backend psycopg."
        ),
    )
    parser.add_argument(
        "--cluster-arn",
        dest="cluster_arn",
        default=None,
        help=f"Aurora cluster ARN for --backend dataapi. Falls back to ${CLUSTER_ARN_ENV}.",
    )
    parser.add_argument(
        "--secret-arn",
        dest="secret_arn",
        default=None,
        help=f"Credentials secret ARN for --backend dataapi. Falls back to ${SECRET_ARN_ENV}.",
    )
    parser.add_argument(
        "--db-name",
        dest="db_name",
        default=None,
        help=(
            "Database name for --backend dataapi (default 'signals'). "
            f"Falls back to ${DB_NAME_ENV}."
        ),
    )
    parser.add_argument(
        "--schema-file",
        dest="schema_file",
        type=Path,
        default=SCHEMA_SQL_PATH,
        help="Override schema.sql path (mainly for tests).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the SQL that would be executed; do not connect.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.dry_run:
        sql = _read_schema(args.schema_file)
        sys.stdout.write(sql)
        return 0

    if args.backend == "dataapi":
        cluster_arn, secret_arn, db_name = _resolve_data_api_config(args)
        count = run_data_api_migration(
            cluster_arn=cluster_arn,
            secret_arn=secret_arn,
            database=db_name,
            schema_path=args.schema_file,
        )
        logger.info("Applied %d statements via the Data API", count)
        return 0

    database_url = _resolve_database_url(args.database_url)
    run_migration(database_url=database_url, schema_path=args.schema_file)
    return 0


def _resolve_data_api_config(args: argparse.Namespace) -> tuple[str, str, str]:
    """Resolve cluster ARN / secret ARN / db name for the Data API backend.

    Flag wins over env var. The two ARNs are required (no sensible default for
    a destination that mutates schema); db name defaults to 'signals'.
    """
    cluster_arn = args.cluster_arn or os.getenv(CLUSTER_ARN_ENV)
    secret_arn = args.secret_arn or os.getenv(SECRET_ARN_ENV)
    db_name = args.db_name or os.getenv(DB_NAME_ENV) or "signals"
    if not cluster_arn or not secret_arn:
        raise SystemExit(
            "--backend dataapi requires --cluster-arn/--secret-arn "
            f"(or ${CLUSTER_ARN_ENV}/${SECRET_ARN_ENV})."
        )
    return cluster_arn, secret_arn, db_name


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
