"""NetworkStack: a minimal private VPC whose only job is to host Aurora.

Serverless topology (revised Step 1.15 -- see SPEC §2.4):

    VPC (2 AZs, 10.20.0.0/16)
      PRIVATE_ISOLATED subnets x2  -> Aurora Serverless v2 (Step 1.16). No
                                      route to the internet, satisfying NFR-3.3
                                      ("Aurora in isolated subnets, no public IP").

There is deliberately **no public subnet, no Internet Gateway, no NAT, and no
VPC endpoints**. The agent pipeline runs on Lambda *outside* this VPC and
reaches the database over the IAM-authenticated RDS Data API (HTTPS) -- not a
VPC connection -- so nothing here needs internet access. Aurora requires a DB
subnet group spanning >= 2 AZs, which is the only reason a VPC exists at all.

The DB security group is created here (the single place the network boundary
lives) and exposed for DataStack to attach to the cluster. Its 5432-from-VPC
ingress is forward-looking: in Slice 1 nothing inside the VPC connects to
Aurora (the Data API is AWS-managed); the Slice 4 dashboard will run in-VPC
with a persistent connection and use this rule.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_logs as logs
from constructs import Construct

VPC_CIDR = "10.20.0.0/16"
POSTGRES_PORT = 5432


class NetworkStack(Stack):
    """Minimal private VPC + DB security group for Aurora."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---- VPC: isolated subnets only, no internet path ----------------
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(VPC_CIDR),
            max_azs=2,
            nat_gateways=0,
            restrict_default_security_group=True,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="isolated",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
        )

        # ---- VPC flow logs (CloudWatch, short retention for cost) --------
        flow_log_group = logs.LogGroup(
            self,
            "VpcFlowLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.vpc.add_flow_log(
            "FlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_log_group),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )

        # ---- DB security group -------------------------------------------
        # Aurora attaches to this in DataStack (Step 1.16). Ingress is
        # Postgres-only, from within the VPC -- for the Slice 4 dashboard,
        # which is the one component that connects directly (the Data API
        # path does not traverse the VPC).
        self.db_security_group = ec2.SecurityGroup(
            self,
            "DbSg",
            vpc=self.vpc,
            description="Aurora cluster SG: Postgres 5432 from within the VPC only.",
            allow_all_outbound=False,
        )
        self.db_security_group.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(POSTGRES_PORT),
            description="Postgres from within the VPC (e.g. the Slice 4 dashboard).",
        )

        # ---- Outputs -----------------------------------------------------
        CfnOutput(self, "VpcId", value=self.vpc.vpc_id, description="VPC id.")
        CfnOutput(
            self,
            "IsolatedSubnetIds",
            value=",".join(s.subnet_id for s in self.vpc.isolated_subnets),
            description="Isolated subnet ids (Aurora DB subnet group).",
        )
        CfnOutput(
            self,
            "DbSecurityGroupId",
            value=self.db_security_group.security_group_id,
            description="Security group for the Aurora cluster.",
        )

        Tags.of(self).add("project", "crypto-signals")
        Tags.of(self).add("layer", "network")
