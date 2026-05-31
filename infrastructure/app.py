#!/usr/bin/env python3
"""CDK application entry point for the crypto-signals system.

Instantiates the five Slice 1 stacks and wires cdk-nag's AwsSolutionsChecks
so security findings surface at synth time. The stacks are empty in Step 1.14;
resources are added in Steps 1.15-1.18 (and 2.12 for monitoring).

Run from this directory (cdk.json sets `app = python app.py`):
    cdk synth          # write CloudFormation templates to cdk.out/
    cdk ls             # list the stacks
    cdk deploy <name>  # deploy (Step 1.15+; needs AWS creds + bootstrap)

Stack naming: `CryptoSignals-<Layer>` so all stacks group together in the
CloudFormation console and are easy to target individually.
"""

from __future__ import annotations

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stacks.compute_stack import ComputeStack
from stacks.data_stack import DataStack
from stacks.monitoring_stack import MonitoringStack
from stacks.network_stack import NetworkStack
from stacks.scheduling_stack import SchedulingStack

app = cdk.App()

NetworkStack(app, "CryptoSignals-Network")
DataStack(app, "CryptoSignals-Data")
ComputeStack(app, "CryptoSignals-Compute")
SchedulingStack(app, "CryptoSignals-Scheduling")
MonitoringStack(app, "CryptoSignals-Monitoring")

# cdk-nag: fail synth on AWS Solutions security-rule violations. Empty stacks
# produce no findings today; the moment a stack adds a resource (Step 1.15+)
# any insecure default is flagged immediately.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
