"""Schema migration over the RDS Data API (serverless deploy, Step 1.17).

The psycopg-based runner in ``scripts/migrate.py`` opens a Postgres socket --
which the VPC-less Lambda cannot do. This module applies the same
``schema.sql`` through the Data API instead, so the cloud has an
equivalent, idempotent migration path (``schema.sql`` is all
``CREATE ... IF NOT EXISTS``).

The one wrinkle: ``execute_statement`` runs **a single SQL statement** per
call -- it will not accept the whole multi-statement file. So we split the
script into individual statements and submit them one by one.

``split_sql_statements`` is deliberately simple: strip ``--`` line comments,
split on ``;``, drop the blanks. That is correct for our schema, which has
**no dollar-quoted bodies, functions, or triggers, and no ``;`` or ``--``
inside string literals**. ``test_dataapi_migrate`` asserts the exact statement
count, so if a future schema revision violates that assumption the test fails
loudly and we upgrade the splitter (or batch via a stored-procedure apply).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# schema.sql lives beside this module in the persistence package. Computed
# locally (not imported from ``__init__``) to avoid an import cycle:
# ``__init__`` imports the store/factory which would otherwise re-enter here.
SCHEMA_SQL_PATH: Path = Path(__file__).parent / "schema.sql"

_LINE_COMMENT = re.compile(r"--[^\n]*")


def split_sql_statements(sql: str) -> list[str]:
    """Split a multi-statement SQL script into individual statements.

    Strips ``--`` line comments, splits on ``;``, and drops whitespace-only
    fragments. See the module docstring for the assumptions this relies on
    (no dollar-quoting, no ``;``/``--`` inside literals).
    """
    without_comments = _LINE_COMMENT.sub("", sql)
    return [stmt.strip() for stmt in without_comments.split(";") if stmt.strip()]


def apply_schema_via_data_api(
    *,
    client: Any,
    cluster_arn: str,
    secret_arn: str,
    database: str,
    schema_sql: str,
) -> int:
    """Execute each statement in ``schema_sql`` through the Data API client.

    Returns the number of statements applied. The client is injected so unit
    tests can pass a mock ``rds-data`` client; production callers use
    :func:`run_data_api_migration`.
    """
    statements = split_sql_statements(schema_sql)
    logger.info("Applying %d statements via the RDS Data API", len(statements))
    for index, statement in enumerate(statements, start=1):
        logger.debug("statement %d/%d: %s", index, len(statements), statement.split("\n", 1)[0])
        client.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database,
            sql=statement,
        )
    logger.info("Data API migration applied successfully (%d statements)", len(statements))
    return len(statements)


def run_data_api_migration(
    *,
    cluster_arn: str,
    secret_arn: str,
    database: str,
    schema_path: Path = SCHEMA_SQL_PATH,
    region_name: str | None = None,
) -> int:
    """Build a boto3 ``rds-data`` client and apply ``schema_path``.

    ``boto3`` is imported lazily so importing this module (and its unit tests,
    which inject a mock) never requires AWS credentials or region resolution.
    """
    import boto3

    client = boto3.client("rds-data", region_name=region_name)
    schema_sql = schema_path.read_text(encoding="utf-8")
    return apply_schema_via_data_api(
        client=client,
        cluster_arn=cluster_arn,
        secret_arn=secret_arn,
        database=database,
        schema_sql=schema_sql,
    )
