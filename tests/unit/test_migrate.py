"""Unit tests for scripts.migrate.

These tests do not touch a real database. They verify:
- CLI argument plumbing (database-url precedence, dry-run, schema-file override)
- _resolve_database_url precedence rules and error path
- run_migration opens a connection and executes the schema text
- the bundled schema.sql is well-formed enough to read

The live-database side is covered by tests/integration/test_migrate_integration.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts import migrate
from src.persistence import EXPECTED_EXTENSIONS, EXPECTED_TABLES, SCHEMA_SQL_PATH

# ---------------------------------------------------------------------------
# _resolve_database_url
# ---------------------------------------------------------------------------


class TestResolveDatabaseUrl:
    def test_cli_arg_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://env/db")
        assert migrate._resolve_database_url("postgresql://cli/db") == "postgresql://cli/db"

    def test_env_used_when_no_cli_arg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://env/db")
        assert migrate._resolve_database_url(None) == "postgresql://env/db"

    def test_neither_set_raises_systemexit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(SystemExit, match="DATABASE_URL"):
            migrate._resolve_database_url(None)


# ---------------------------------------------------------------------------
# _read_schema
# ---------------------------------------------------------------------------


class TestReadSchema:
    def test_returns_file_contents(self, tmp_path: Path) -> None:
        f = tmp_path / "test.sql"
        f.write_text("SELECT 1;\n", encoding="utf-8")
        assert migrate._read_schema(f) == "SELECT 1;\n"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            migrate._read_schema(tmp_path / "missing.sql")

    def test_bundled_schema_file_exists(self) -> None:
        # If this test fails, schema.sql got renamed or moved.
        assert SCHEMA_SQL_PATH.exists()
        contents = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        assert "CREATE TABLE" in contents
        for table in EXPECTED_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table}" in contents
        for ext in EXPECTED_EXTENSIONS:
            assert f"CREATE EXTENSION IF NOT EXISTS {ext}" in contents


# ---------------------------------------------------------------------------
# run_migration
# ---------------------------------------------------------------------------


def _build_psycopg_connect_mock() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Wire psycopg.connect as a context-manager-returning mock.

    Returns (connect_mock, conn_mock, cursor_mock) so tests can assert on
    each layer.
    """
    cursor_mock = MagicMock()
    cursor_mock.__enter__ = MagicMock(return_value=cursor_mock)
    cursor_mock.__exit__ = MagicMock(return_value=False)

    conn_mock = MagicMock()
    conn_mock.cursor.return_value = cursor_mock
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)

    connect_mock = MagicMock(return_value=conn_mock)
    return connect_mock, conn_mock, cursor_mock


class TestRunMigration:
    def test_connects_and_executes_schema(self, tmp_path: Path) -> None:
        sql = "CREATE TABLE IF NOT EXISTS t (id INT);"
        schema = tmp_path / "schema.sql"
        schema.write_text(sql, encoding="utf-8")

        connect_mock, conn_mock, cursor_mock = _build_psycopg_connect_mock()
        with patch("scripts.migrate.psycopg.connect", connect_mock):
            migrate.run_migration(
                database_url="postgresql://test/db",
                schema_path=schema,
            )

        connect_mock.assert_called_once_with("postgresql://test/db")
        cursor_mock.execute.assert_called_once_with(sql)
        conn_mock.commit.assert_called_once()

    def test_missing_schema_file_raises_before_connecting(self, tmp_path: Path) -> None:
        connect_mock, _, _ = _build_psycopg_connect_mock()
        with (
            patch("scripts.migrate.psycopg.connect", connect_mock),
            pytest.raises(FileNotFoundError),
        ):
            migrate.run_migration(
                database_url="postgresql://test/db",
                schema_path=tmp_path / "nope.sql",
            )
        connect_mock.assert_not_called()


# ---------------------------------------------------------------------------
# main / CLI plumbing
# ---------------------------------------------------------------------------


class TestMainCLI:
    def test_dry_run_prints_schema_and_skips_connect(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        connect_mock, _, _ = _build_psycopg_connect_mock()
        with patch("scripts.migrate.psycopg.connect", connect_mock):
            rc = migrate.main(["--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "CREATE EXTENSION IF NOT EXISTS vector" in out
        connect_mock.assert_not_called()

    def test_apply_uses_cli_url_over_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://env/db")
        connect_mock, _, _ = _build_psycopg_connect_mock()
        with patch("scripts.migrate.psycopg.connect", connect_mock):
            rc = migrate.main(["--database-url", "postgresql://cli/db"])
        assert rc == 0
        connect_mock.assert_called_once_with("postgresql://cli/db")

    def test_apply_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://env/db")
        connect_mock, _, _ = _build_psycopg_connect_mock()
        with patch("scripts.migrate.psycopg.connect", connect_mock):
            rc = migrate.main([])
        assert rc == 0
        connect_mock.assert_called_once_with("postgresql://env/db")

    def test_apply_with_no_url_anywhere_exits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(SystemExit):
            migrate.main([])

    def test_schema_file_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        custom = tmp_path / "custom.sql"
        custom.write_text("SELECT 1;\n", encoding="utf-8")
        monkeypatch.setenv("DATABASE_URL", "postgresql://env/db")

        connect_mock, _, cursor_mock = _build_psycopg_connect_mock()
        with patch("scripts.migrate.psycopg.connect", connect_mock):
            rc = migrate.main(["--schema-file", str(custom)])
        assert rc == 0
        cursor_mock.execute.assert_called_once_with("SELECT 1;\n")


# ---------------------------------------------------------------------------
# Schema content invariants
# ---------------------------------------------------------------------------


class TestSchemaContent:
    """Asserts about schema.sql that are cheap to verify without a DB.

    These catch regressions like 'someone removed an index' or 'someone changed
    a CHECK constraint name'. They are not a substitute for the integration
    test, but they fail fast and are CI-friendly.
    """

    def _schema(self) -> str:
        return SCHEMA_SQL_PATH.read_text(encoding="utf-8")

    def test_uses_if_not_exists_for_idempotency(self) -> None:
        sql = self._schema()
        # Every CREATE TABLE / CREATE EXTENSION must be guarded.
        for stmt in ("CREATE TABLE", "CREATE EXTENSION", "CREATE INDEX"):
            count_total = sql.count(stmt)
            count_guarded = sql.count(f"{stmt} IF NOT EXISTS")
            assert (
                count_total == count_guarded
            ), f"Non-idempotent '{stmt}' statement detected; all DDL must use IF NOT EXISTS"

    def test_signals_status_constrained_to_known_values(self) -> None:
        sql = self._schema()
        assert "status IN ('PUBLISHED', 'SKIPPED')" in sql

    def test_signals_direction_nullable_check(self) -> None:
        # Skip rows have direction NULL; CHECK must allow NULL.
        sql = self._schema()
        assert "direction IS NULL OR direction IN ('LONG', 'SHORT')" in sql

    def test_agent_runs_includes_all_six_roles(self) -> None:
        sql = self._schema()
        for role in ("analyzer", "historian", "skeptic", "judge", "forecaster", "critic"):
            assert f"'{role}'" in sql

    def test_foreign_keys_cascade_on_scan_delete(self) -> None:
        sql = self._schema()
        # Both child tables must cascade so test-cleanup doesn't leave orphans.
        assert sql.count("REFERENCES scan_runs(id) ON DELETE CASCADE") == 2

    def test_active_setups_status_constrained(self) -> None:
        sql = self._schema()
        # active_setups.status carries the OPEN + terminal lifecycle (Step 2.8),
        # and cascades from its parent signal.
        assert "'OPEN'" in sql
        assert "REFERENCES signals(id) ON DELETE CASCADE" in sql

    def test_pgvector_extension_enabled(self) -> None:
        # Slice 3 Step 3.4 adds a vector(1536) column; we want the extension
        # in place now so that migration ordering is clean.
        assert "CREATE EXTENSION IF NOT EXISTS vector" in self._schema()


# ---------------------------------------------------------------------------
# Module-constant sanity
# ---------------------------------------------------------------------------


class TestPersistenceConstants:
    def test_expected_tables_match_schema_scope(self) -> None:
        # Slice 1 shipped scan_runs/signals/agent_runs; Step 2.8 added active_setups.
        assert frozenset({"scan_runs", "signals", "agent_runs", "active_setups"}) == EXPECTED_TABLES

    def test_schema_sql_path_resolves(self) -> None:
        assert SCHEMA_SQL_PATH.exists()
        assert SCHEMA_SQL_PATH.name == "schema.sql"


# ---------------------------------------------------------------------------
# Ergonomic helper used by integration test as well
# ---------------------------------------------------------------------------


def _force_drop_all_helper_present() -> Any:
    """Documentation-only sentinel: there is no drop-helper in Slice 1.

    Integration tests rely on docker compose down -v to reset state because
    we don't want a footgun 'drop all' helper in production code. Slice 2
    Step 2.x may introduce a tests-only fixture if needed.
    """
