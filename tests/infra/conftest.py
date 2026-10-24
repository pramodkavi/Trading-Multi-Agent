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
def spa_dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A tiny stand-in for ``dashboard-ui/dist`` (Step 4.5).

    ``Source.asset`` of a plain directory is zipped at synth regardless of
    ``aws:cdk:bundling-stacks``, so the tests use a two-file tree instead of the
    real (gitignored, possibly absent) Vite build.
    """
    dist = tmp_path_factory.mktemp("dist")
    (dist / "index.html").write_text(
        '<!doctype html><html><head><script type="module" src="/assets/app.js"></script>'
        '</head><body><div id="root"></div></body></html>\n',
        encoding="utf-8",
    )
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log('stub');\n", encoding="utf-8")
    return dist


@pytest.fixture(scope="session")
def templates(spa_dist: Path) -> dict[str, Any]:
    """Synthesize the data/compute/scheduling/monitoring/dashboard stacks once.

    Built env-agnostically (no account/region) -- enough for resource-shape
    assertions; cross-stack references render as Fn::ImportValue.
    """
    import aws_cdk as core
    from aws_cdk import assertions
    from stacks.compute_stack import ComputeStack
    from stacks.dashboard_stack import DashboardStack
    from stacks.data_stack import DataStack
    from stacks.monitoring_stack import MonitoringStack
    from stacks.network_stack import NetworkStack
    from stacks.scheduling_stack import SchedulingStack

    # Skip asset bundling in unit tests: the code/layer bundlers would otherwise
    # copy src/ and pip-install ~130 MB of wheels on every test run. With an
    # empty bundling-stacks list CDK stages a placeholder and still renders the
    # Function/LayerVersion resources we assert on. The real bundling path is
    # exercised by `python app.py` (cdk synth) in CI's quality job.
    app = core.App(context={"aws:cdk:bundling-stacks": []})
    network = NetworkStack(app, "Network")
    data = DataStack(
        app,
        "Data",
        vpc=network.vpc,
        db_security_group=network.db_security_group,
    )
    compute = ComputeStack(app, "Compute", cluster=data.cluster, bucket=data.bucket)
    scheduling = SchedulingStack(app, "Scheduling", scan_function=compute.function)
    monitoring = MonitoringStack(
        app,
        "Monitoring",
        scan_function=compute.function,
        scan_log_group=compute.log_group,
        cluster=data.cluster,
    )
    # Slice 4 Step 4.1: coupled only to Data (design §7 / §4.4). A fixed
    # app_version so the APP_VERSION env assertion is deterministic.
    dashboard = DashboardStack(
        app, "Dashboard", cluster=data.cluster, app_version="t3st000", spa_dist=spa_dist
    )
    return {
        "data": assertions.Template.from_stack(data),
        "compute": assertions.Template.from_stack(compute),
        "scheduling": assertions.Template.from_stack(scheduling),
        "monitoring": assertions.Template.from_stack(monitoring),
        "dashboard": assertions.Template.from_stack(dashboard),
        # Synthesized cloud assembly dir (staged assets, e.g. the SPA config.json).
        "dashboard_outdir": app.outdir,
    }
