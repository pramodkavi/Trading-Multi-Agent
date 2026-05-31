"""MonitoringStack: CloudWatch alarms, dashboards, and cost tracking.

Empty in Step 1.14 (scaffolding). Implemented in Step 2.12 (per SPEC §3.3.2):
- Alarms: scan failure rate > 10%/24h, provider error rate > 20%/1h,
  agent latency P95 > 2min, Postgres CPU > 80%
- Budget alarms for AWS infra and Anthropic spend
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Stack
from constructs import Construct


class MonitoringStack(Stack):
    """Observability + alarms for the crypto-signals system."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)
