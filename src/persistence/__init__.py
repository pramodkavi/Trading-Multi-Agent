"""Persistence layer: Postgres schema, migrations, and repositories.

Slice 1 Step 1.8 ships the schema (scan_runs, signals, agent_runs) plus the
migration runner. Step 1.9 adds asyncpg-based repositories on top.

Public API:
    SCHEMA_SQL_PATH    -- absolute path to schema.sql
    EXPECTED_TABLES    -- set of table names defined by schema.sql; used by
                          migration tests to assert post-migration state.
    EXPECTED_EXTENSIONS -- pgvector etc.
"""

from pathlib import Path

SCHEMA_SQL_PATH: Path = Path(__file__).parent / "schema.sql"

EXPECTED_TABLES: frozenset[str] = frozenset({"scan_runs", "signals", "agent_runs"})
EXPECTED_EXTENSIONS: frozenset[str] = frozenset({"vector"})

__all__ = ["EXPECTED_EXTENSIONS", "EXPECTED_TABLES", "SCHEMA_SQL_PATH"]
