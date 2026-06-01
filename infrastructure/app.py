#!/usr/bin/env python3
"""CDK application entry point for the crypto-signals system.

Instantiates the five Slice 1 stacks and wires cdk-nag's AwsSolutionsChecks
so security findings surface at synth time. NetworkStack is implemented in
Step 1.15; the other four are filled in across Steps 1.16-1.18 (and 2.12).

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

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stacks.compute_stack import ComputeStack
from stacks.data_stack import DataStack
from stacks.monitoring_stack import MonitoringStack
from stacks.network_stack import NetworkStack
from stacks.scheduling_stack import SchedulingStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION"),
)

network = NetworkStack(app, "CryptoSignals-Network", env=env)
DataStack(app, "CryptoSignals-Data", env=env)
ComputeStack(app, "CryptoSignals-Compute", env=env)
SchedulingStack(app, "CryptoSignals-Scheduling", env=env)
MonitoringStack(app, "CryptoSignals-Monitoring", env=env)

# Keep a reference so DataStack (Step 1.16) can consume the VPC + DB security
# group without re-importing them.
_ = (network.vpc, network.db_security_group)

# cdk-nag: fail synth on AWS Solutions security-rule violations. The moment a
# stack adds a resource, any insecure default is flagged immediately.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
