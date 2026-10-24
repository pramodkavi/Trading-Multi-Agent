#!/usr/bin/env python3
"""CDK application entry point for the crypto-signals system.

Instantiates the six stacks and wires cdk-nag's AwsSolutionsChecks so security
findings surface at synth time. The five pipeline stacks (Network, Data,
Compute, Scheduling, Monitoring) were built across Steps 1.15-1.18 and 2.12;
the DashboardStack (Slice 4, Step 4.1) is declared LAST and depends only on
Data, so a dashboard deploy failure can never block a pipeline deploy.

Environment:
    Stacks are bound to the account/region the CDK CLI resolves from the
    active AWS profile (CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION). With no
    valid credentials these are unset and synthesis falls back to an
    environment-agnostic template (still valid; uses 2 dummy AZs).

Run from this directory (cdk.json sets `app = python app.py`):
    cdk synth
    cdk ls
    cdk deploy CryptoSignals-Network   # Step 1.15+; needs creds + bootstrap
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stacks.compute_stack import ComputeStack
from stacks.dashboard_stack import DashboardStack
from stacks.data_stack import DataStack
from stacks.monitoring_stack import MonitoringStack
from stacks.network_stack import NetworkStack
from stacks.scheduling_stack import SchedulingStack


def resolve_app_version() -> str:
    """The build identifier the dashboard's ``/api/health`` reports as ``version``.

    The health endpoint is the deploy smoke test, so it should say which commit
    is live. Precedence: (1) ``git rev-parse --short HEAD`` -- the checkout IS
    the deployed tree (``actions/checkout`` leaves a git repo, and on a
    ``workflow_run``-triggered deploy it checks out ``head_sha`` while
    ``GITHUB_SHA`` is the default-branch tip, so the env var can name a commit
    that is not what was synthesized); (2) ``GITHUB_SHA[:7]`` if set and
    non-empty; (3) ``"local"`` (no git, not a checkout). Never raises -- a
    missing version must not fail ``cdk synth``.
    """
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception:  # no git on PATH / not a checkout: fall through to the env var
        pass
    sha = os.environ.get("GITHUB_SHA", "")
    if sha:
        return sha[:7]
    return "local"


# Repository root (this file lives in <root>/infrastructure/).
REPO_ROOT = Path(__file__).resolve().parents[1]

# Region is PINNED to Mumbai (ap-south-1). The scan Lambda calls the Binance
# REST API, which geofences North-American/EU AWS regions with HTTP 451
# ("restricted location"). ap-south-1 is reachable (verified) and is the
# lowest-latency Binance-serving region for the operator (Sri Lanka). The
# account still comes from the ambient CDK env. See Step 1.22 deploy notes.
AWS_REGION = "ap-south-1"


def resolve_spa_dist(repo_root: Path) -> Path:
    """The dashboard SPA build (``dashboard-ui/dist``) the DashboardStack uploads.

    Step 4.5: ``cdk synth`` / ``deploy`` must fail loudly -- not ship an empty
    site -- when the Vite build has not been run (``dist`` is gitignored).
    """
    dist = repo_root / "dashboard-ui" / "dist"
    if not (dist / "index.html").is_file():
        raise SystemExit(
            "dashboard-ui/dist/index.html not found — run `npm ci && npm run build` "
            "in dashboard-ui first"
        )
    return dist


def main() -> None:
    """Build and synthesize the app (guarded so tests can import the helpers)."""
    app = cdk.App()
    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=AWS_REGION,
    )

    network = NetworkStack(app, "CryptoSignals-Network", env=env)
    data = DataStack(
        app,
        "CryptoSignals-Data",
        env=env,
        vpc=network.vpc,
        db_security_group=network.db_security_group,
    )
    compute = ComputeStack(
        app,
        "CryptoSignals-Compute",
        env=env,
        cluster=data.cluster,
        bucket=data.bucket,
    )
    SchedulingStack(
        app,
        "CryptoSignals-Scheduling",
        env=env,
        scan_function=compute.function,
    )
    MonitoringStack(
        app,
        "CryptoSignals-Monitoring",
        env=env,
        scan_function=compute.function,
        scan_log_group=compute.log_group,
        cluster=data.cluster,
    )
    # Slice 4 (design 2026-10-18 §3.5 / §4.4): LAST and coupled only to Data, so a
    # dashboard deploy failure can never block or delay a pipeline deploy.
    DashboardStack(
        app,
        "CryptoSignals-Dashboard",
        env=env,
        cluster=data.cluster,
        app_version=resolve_app_version(),
        spa_dist=resolve_spa_dist(REPO_ROOT),
    )

    # cdk-nag: fail synth on AWS Solutions security-rule violations. The moment a
    # stack adds a resource, any insecure default is flagged immediately.
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

    app.synth()


if __name__ == "__main__":
    main()
