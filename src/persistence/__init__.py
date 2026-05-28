"""Persistence layer: Postgres schema, migrations, models, and repositories.

Slice 1 Step 1.8 ships the schema (scan_runs, signals, agent_runs) plus the
migration runner. Step 1.9 adds asyncpg repositories + read-side wrappers.

Public API:
    SCHEMA_SQL_PATH     -- absolute path to schema.sql
    EXPECTED_TABLES     -- set of table names defined by schema.sql; used by
                           migration tests to assert post-migration state.
    EXPECTED_EXTENSIONS -- pgvector etc.
    StoredSignal        -- read-side wrapper for a signals row.
    StoredScanRun       -- read-side wrapper for a scan_runs row.
    StoredAgentRun      -- read-side wrapper for an agent_runs row.
    SignalRepository    -- async CRUD over signals.
    AgentRunRepository  -- async writes for agent_runs.
    ScanRunRepository   -- async lifecycle ops for scan_runs.
"""

from pathlib import Path

from src.persistence.models import StoredAgentRun, StoredScanRun, StoredSignal
from src.persistence.repositories import (
    AgentRunRepository,
    ScanRunRepository,
    SignalRepository,
)

SCHEMA_SQL_PATH: Path = Path(__file__).parent / "schema.sql"

EXPECTED_TABLES: frozenset[str] = frozenset({"scan_runs", "signals", "agent_runs"})
EXPECTED_EXTENSIONS: frozenset[str] = frozenset({"vector"})

__all__ = [
    "EXPECTED_EXTENSIONS",
    "EXPECTED_TABLES",
    "SCHEMA_SQL_PATH",
    "AgentRunRepository",
    "ScanRunRepository",
    "SignalRepository",
    "StoredAgentRun",
    "StoredScanRun",
    "StoredSignal",
]
