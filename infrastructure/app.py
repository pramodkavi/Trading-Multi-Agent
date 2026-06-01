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

Context flags:
    enable_interface_endpoints  (default false) -- turn on the ECR / Secrets
        Manager / CloudWatch Logs interface VPC endpoints in NetworkStack.
        They cost ~$7/mo per AZ each, so they are off by default:
            cdk deploy -c enable_interface_endpoints=true

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

# Context flag (string "true"/"false" or bool); default off for cost.
_endpoints_ctx = app.node.try_get_context("enable_interface_endpoints")
enable_interface_endpoints = str(_endpoints_ctx).lower() == "true"

network = NetworkStack(
    app,
    "CryptoSignals-Network",
    env=env,
    enable_interface_endpoints=enable_interface_endpoints,
)
DataStack(app, "CryptoSignals-Data", env=env)
ComputeStack(app, "CryptoSignals-Compute", env=env)
SchedulingStack(app, "CryptoSignals-Scheduling", env=env)
MonitoringStack(app, "CryptoSignals-Monitoring", env=env)

# Keep a reference so future stacks (DataStack in Step 1.16) can consume the
# VPC + egress SG without re-importing them.
_ = network.vpc

# cdk-nag: fail synth on AWS Solutions security-rule violations. The moment a
# stack adds a resource, any insecure default is flagged immediately.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
