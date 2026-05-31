"""NetworkStack: VPC, subnets, and VPC endpoints.

Empty in Step 1.14 (scaffolding). Implemented in Step 1.15:
- VPC with public, private (egress), and isolated subnets across 2 AZs
- VPC endpoints for S3, ECR, Secrets Manager, CloudWatch Logs (avoid NAT cost)
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Stack
from constructs import Construct


class NetworkStack(Stack):
    """Networking foundation for the crypto-signals system."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)
