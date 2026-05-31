"""ComputeStack: ECR, ECS Fargate cluster, and the scan task definition.

Empty in Step 1.14 (scaffolding). Implemented in Step 1.17:
- ECR repository
- ECS Fargate cluster
- Fargate task definition: container image, least-privilege IAM role,
  log group, secrets injected from Secrets Manager
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Stack
from constructs import Construct


class ComputeStack(Stack):
    """Container compute for the crypto-signals scan task."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)
