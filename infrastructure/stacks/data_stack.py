"""DataStack: RDS Serverless v2 PostgreSQL, S3, and Secrets Manager.

Empty in Step 1.14 (scaffolding). Implemented in Step 1.16:
- RDS Serverless v2 PostgreSQL 16 cluster in isolated subnets
- S3 bucket with versioning + lifecycle (Glacier after 90 days)
- Secrets Manager entries (placeholder values, populated post-deploy)
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Stack
from constructs import Construct


class DataStack(Stack):
    """Persistence + storage + secrets for the crypto-signals system."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)
