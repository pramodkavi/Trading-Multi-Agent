"""Shared fixtures for CDK synth-time assertion tests (Step 2.12).

The CDK app uses absolute imports rooted at the ``infrastructure`` directory
(``from stacks.x import Y``), matching how ``cdk synth`` runs (cwd = infrastructure).
Put that directory on ``sys.path`` so the stacks import the same way under pytest.

These tests synthesize stacks with ``aws_cdk`` (jsii -> Node) and assert on the
resulting CloudFormation. They do NOT apply the cdk-nag aspect -- nag compliance
is validated separately by synthesizing the full app (``python app.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_INFRA_DIR = Path(__file__).resolve().parents[2] / "infrastructure"
if str(_INFRA_DIR) not in sys.path:
    sys.path.insert(0, str(_INFRA_DIR))


@pytest.fixture(scope="session")
def templates() -> dict[str, Any]:
    """Synthesize the data/compute/monitoring stacks once; return their Templates.

    Built env-agnostically (no account/region) -- enough for resource-shape
    assertions; cross-stack references render as Fn::ImportValue.
    """
    import aws_cdk as core
    from aws_cdk import assertions
    from stacks.compute_stack import ComputeStack
    from stacks.data_stack import DataStack
    from stacks.monitoring_stack import MonitoringStack
    from stacks.network_stack import NetworkStack

    app = core.App()
    network = NetworkStack(app, "Network")
    data = DataStack(
        app,
        "Data",
        vpc=network.vpc,
        db_security_group=network.db_security_group,
    )
    compute = ComputeStack(app, "Compute", cluster=data.cluster, bucket=data.bucket)
    monitoring = MonitoringStack(
        app,
        "Monitoring",
        scan_function=compute.function,
        scan_log_group=compute.log_group,
        cluster=data.cluster,
    )
    return {
        "data": assertions.Template.from_stack(data),
        "compute": assertions.Template.from_stack(compute),
        "monitoring": assertions.Template.from_stack(monitoring),
    }
