"""CDK stacks for the crypto-signals system.

Five stacks compose the Slice 1 infrastructure. They are empty in Step 1.14
(scaffolding) and filled in across Steps 1.15-1.18 and 2.12.
"""

from stacks.compute_stack import ComputeStack
from stacks.data_stack import DataStack
from stacks.monitoring_stack import MonitoringStack
from stacks.network_stack import NetworkStack
from stacks.scheduling_stack import SchedulingStack

__all__ = [
    "ComputeStack",
    "DataStack",
    "MonitoringStack",
    "NetworkStack",
    "SchedulingStack",
]
