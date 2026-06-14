"""Unit tests for the Data API migration runner.

Covers statement splitting (the part with assumptions worth pinning down) and
the apply loop against a mock ``rds-data`` client -- no AWS, no database. The
``run_data_api_migration`` wrapper is exercised with ``boto3.client``
monkeypatched so the boto3 wiring is verified without credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.persistence import SCHEMA_SQL_PATH
from src.persistence import dataapi_migrate as migrate

# scan_runs/signals/agent_runs + their indexes + the vector extension.
# Pinned so a future schema.sql change that breaks the simple splitter (e.g.
# a function body with embedded semicolons) fails loudly here.
EXPECTED_STATEMENT_COUNT = 18

CLUSTER_ARN = "arn:aws:rds:us-east-1:123456789012:cluster:crypto"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:crypto-signals/db"


class FakeRdsDataClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute_statement(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"numberOfRecordsUpdated": 0}


# ---------------------------------------------------------------------------
# split_sql_statements
# ---------------------------------------------------------------------------


def test_split_strips_line_comments_and_blanks() -> None:
    sql = """
    -- a leading comment
    CREATE TABLE a (id INT);  -- trailing comment

    CREATE TABLE b (id INT);
    """
    statements = migrate.split_sql_statements(sql)
    assert statements == ["CREATE TABLE a (id INT)", "CREATE TABLE b (id INT)"]


def test_split_drops_trailing_fragment_after_last_semicolon() -> None:
    # No statement after the final ';' -> no empty trailing entry.
    assert migrate.split_sql_statements("SELECT 1;\n\n") == ["SELECT 1"]


def test_split_empty_input_yields_no_statements() -> None:
    assert migrate.split_sql_statements("-- just a comment\n") == []


def test_split_real_schema_has_expected_count() -> None:
    sql = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    statements = migrate.split_sql_statements(sql)
    assert len(statements) == EXPECTED_STATEMENT_COUNT
    # Idempotency guard preserved on every DDL statement.
    assert all("IF NOT EXISTS" in s for s in statements)


# ---------------------------------------------------------------------------
# apply_schema_via_data_api
# ---------------------------------------------------------------------------


def test_apply_executes_each_statement_with_target() -> None:
    client = FakeRdsDataClient()
    sql = "CREATE TABLE a (id INT); CREATE TABLE b (id INT);"

    count = migrate.apply_schema_via_data_api(
        client=client,
        cluster_arn=CLUSTER_ARN,
        secret_arn=SECRET_ARN,
        database="signals",
        schema_sql=sql,
    )

    assert count == 2
    assert len(client.calls) == 2
    first = client.calls[0]
    assert first["resourceArn"] == CLUSTER_ARN
    assert first["secretArn"] == SECRET_ARN
    assert first["database"] == "signals"
    assert first["sql"] == "CREATE TABLE a (id INT)"
    assert client.calls[1]["sql"] == "CREATE TABLE b (id INT)"


def test_apply_real_schema_runs_all_statements() -> None:
    client = FakeRdsDataClient()
    sql = SCHEMA_SQL_PATH.read_text(encoding="utf-8")

    count = migrate.apply_schema_via_data_api(
        client=client,
        cluster_arn=CLUSTER_ARN,
        secret_arn=SECRET_ARN,
        database="signals",
        schema_sql=sql,
    )

    assert count == EXPECTED_STATEMENT_COUNT
    assert len(client.calls) == EXPECTED_STATEMENT_COUNT


def test_apply_propagates_client_errors() -> None:
    class BoomClient:
        def execute_statement(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("data api down")

    with pytest.raises(RuntimeError, match="data api down"):
        migrate.apply_schema_via_data_api(
            client=BoomClient(),
            cluster_arn=CLUSTER_ARN,
            secret_arn=SECRET_ARN,
            database="signals",
            schema_sql="CREATE TABLE a (id INT);",
        )


# ---------------------------------------------------------------------------
# run_data_api_migration (boto3 wiring)
# ---------------------------------------------------------------------------


def test_run_builds_client_and_reads_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeRdsDataClient()
    captured: dict[str, Any] = {}

    def fake_client(service: str, region_name: str | None = None) -> FakeRdsDataClient:
        captured["service"] = service
        captured["region_name"] = region_name
        return client

    import boto3

    monkeypatch.setattr(boto3, "client", fake_client)

    schema_file = tmp_path / "schema.sql"
    schema_file.write_text("CREATE TABLE a (id INT);\nCREATE TABLE b (id INT);\n", encoding="utf-8")

    count = migrate.run_data_api_migration(
        cluster_arn=CLUSTER_ARN,
        secret_arn=SECRET_ARN,
        database="signals",
        schema_path=schema_file,
        region_name="us-east-1",
    )

    assert count == 2
    assert captured == {"service": "rds-data", "region_name": "us-east-1"}
    assert len(client.calls) == 2
