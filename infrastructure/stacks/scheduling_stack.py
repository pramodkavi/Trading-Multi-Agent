"""SchedulingStack: EventBridge Scheduler rules that trigger scan tasks.

Empty in Step 1.14 (scaffolding). Implemented in Step 1.18:
- EventBridge Scheduler with one rule to start (3 8 * * * UTC)
- Fargate task target with appropriate IAM permissions

Slice 2 Step 2.10 adds the Forecaster schedule; Slice 3 Step 3.7 the Critic.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Stack
from constructs import Construct


class SchedulingStack(Stack):
    """Cron scheduling for the crypto-signals scans."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)
