"""Live-Postgres integration test for scripts.migrate.

Marked `integration` and *skipped by default*. Run with:

    docker compose up -d db
    export DATABASE_URL="postgresql://signals:signals@localhost:5432/signals"
    pytest -m integration tests/integration/test_migrate_integration.py

The test additionally skips if DATABASE_URL is unset, so a stray
`pytest -m integration` against an empty environment is a clean skip rather
than a noisy fail.

What we verify (SPEC §5.2 persistence checkpoints):
- Migration runs cleanly on an empty database (creates pgvector + 3 tables).
- Re-running the migration is a no-op (idempotency).
- Resulting columns / constraints match the schema's intent.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

from scripts.migrate import run_migration
from src.persistence import EXPECTED_EXTENSIONS, EXPECTED_TABLES

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


def _table_names(cur: psycopg.Cursor[psycopg.rows.TupleRow]) -> set[str]:
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    return {row[0] for row in cur.fetchall()}


def _extension_names(cur: psycopg.Cursor[psycopg.rows.TupleRow]) -> set[str]:
    cur.execute("SELECT extname FROM pg_extension")
    return {row[0] for row in cur.fetchall()}


def _reset_database(url: str) -> None:
    """Drop everything from the public schema; pristine slate per test.

    Done out-of-band (not in production code) so we don't ship a 'drop all'
    helper that could be misused.
    """
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
        conn.commit()


def test_migration_creates_expected_objects_on_empty_db() -> None:
    url = _require_database_url()
    _reset_database(url)

    run_migration(database_url=url)

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        assert _table_names(cur).issuperset(EXPECTED_TABLES)
        assert _extension_names(cur).issuperset(EXPECTED_EXTENSIONS)


def test_migration_is_idempotent() -> None:
    """SPEC §5.2: 'Migration is idempotent (re-running causes no errors)'."""
    url = _require_database_url()
    _reset_database(url)

    run_migration(database_url=url)
    # Second run on an already-migrated DB must complete without raising.
    run_migration(database_url=url)

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        assert _table_names(cur).issuperset(EXPECTED_TABLES)


def test_signals_status_constraint_enforced() -> None:
    """A bogus status value must be rejected by the CHECK constraint."""
    url = _require_database_url()
    _reset_database(url)
    run_migration(database_url=url)

    # First, insert a scan_run we can FK to.
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scan_runs (id, started_at, status) "
            "VALUES (gen_random_uuid(), NOW(), 'RUNNING') RETURNING id"
        )
        scan_id = cur.fetchone()
        assert scan_id is not None
        conn.commit()

    with (
        psycopg.connect(url) as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        cur.execute(
            "INSERT INTO signals (id, scan_id, symbol, strategy, "
            "status, payload) "
            "VALUES (gen_random_uuid(), %s, 'BTCUSDT', 'smc', "
            "'INVALID_STATUS', '{}'::jsonb)",
            (scan_id[0],),
        )


def test_skip_row_with_null_direction_allowed() -> None:
    """SkipDecision rows have direction NULL; the CHECK must allow it."""
    url = _require_database_url()
    _reset_database(url)
    run_migration(database_url=url)

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scan_runs (id, started_at, status) "
            "VALUES (gen_random_uuid(), NOW(), 'RUNNING') RETURNING id"
        )
        scan_id = cur.fetchone()
        assert scan_id is not None

        cur.execute(
            "INSERT INTO signals (id, scan_id, symbol, strategy, "
            "direction, status, payload) "
            "VALUES (gen_random_uuid(), %s, 'BTCUSDT', 'smc', "
            "NULL, 'SKIPPED', '{}'::jsonb)",
            (scan_id[0],),
        )
        conn.commit()


def test_schema_file_argument_override() -> None:
    """Caller can apply a non-default schema file (mainly for tests)."""
    url = _require_database_url()
    _reset_database(url)

    # A trivial alternate schema must run successfully.
    alt = Path(__file__).parent / "_alt_schema.sql"
    alt.write_text(
        "CREATE TABLE IF NOT EXISTS alt_only_table (id INT PRIMARY KEY);\n",
        encoding="utf-8",
    )
    try:
        run_migration(database_url=url, schema_path=alt)
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            assert "alt_only_table" in _table_names(cur)
    finally:
        alt.unlink(missing_ok=True)
