"""NetworkStack: VPC, subnets, security group, and VPC endpoints.

Topology (Step 1.15) -- NAT-free, cost-optimised for a signal-only egress
workload (SPEC NFR-5.1 target < $100/mo):

    VPC (2 AZs, 10.20.0.0/16)
      PUBLIC subnets            -> the Fargate scan task runs here with a
                                   public IP (egress only; the task security
                                   group denies all inbound). Outbound to the
                                   non-AWS APIs it must reach -- Binance,
                                   Anthropic, Telegram -- goes via the free
                                   Internet Gateway. No NAT Gateway.
      PRIVATE_ISOLATED subnets  -> RDS Postgres (Step 1.16). No route to the
                                   internet, satisfying NFR-3.3 ("Postgres in a
                                   private subnet with no public IP").

Why no `PRIVATE_WITH_EGRESS` subnet: that tier requires a NAT Gateway route,
and we deliberately run zero NAT Gateways (each is ~$32/mo + data for a
workload that scans a few times a day). VPC *interface* endpoints cannot
substitute for NAT here because the task's heaviest egress is to non-AWS
hosts, which endpoints do not serve.

VPC endpoints:
  - S3 gateway endpoint: always on. It is free and keeps ECR image-layer and
    S3 blob traffic on the AWS backbone.
  - ECR (api + dkr), Secrets Manager, CloudWatch Logs interface endpoints:
    coded but OFF by default (`enable_interface_endpoints=False`). Each
    interface endpoint bills ~$0.01/AZ/hr (~$7/mo per AZ), so four of them in
    two AZs is ~$58/mo -- most of the budget, 24/7. With a public-subnet task
    these calls already work for free over the IGW; the endpoints are a
    keep-AWS-traffic-private upgrade to flip on later via context:
        cdk deploy -c enable_interface_endpoints=true
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
from cdk_nag import NagSuppressions
from constructs import Construct

VPC_CIDR = "10.20.0.0/16"


class NetworkStack(Stack):
    """Networking foundation: VPC, subnets, egress-only SG, and endpoints."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        enable_interface_endpoints: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---- VPC ---------------------------------------------------------
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(VPC_CIDR),
            max_azs=2,
            nat_gateways=0,
            restrict_default_security_group=True,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
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

        # ---- Egress-only security group for the scan task ----------------
        # No inbound rules; all outbound allowed so the task can reach
        # Binance / Anthropic / Telegram and (over the IGW) AWS service APIs.
        self.task_security_group = ec2.SecurityGroup(
            self,
            "ScanTaskSg",
            vpc=self.vpc,
            description="Egress-only SG for the Fargate scan task (no inbound).",
            allow_all_outbound=True,
        )

        # ---- S3 gateway endpoint (free) ----------------------------------
        self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        # ---- Interface endpoints (optional; cost-gated) ------------------
        if enable_interface_endpoints:
            self._add_interface_endpoints()

        # ---- Outputs (handy for verification + later cross-stack wiring) -
        CfnOutput(self, "VpcId", value=self.vpc.vpc_id, description="VPC id.")
        CfnOutput(
            self,
            "PublicSubnetIds",
            value=",".join(s.subnet_id for s in self.vpc.public_subnets),
            description="Public subnet ids (Fargate scan task).",
        )
        CfnOutput(
            self,
            "IsolatedSubnetIds",
            value=",".join(s.subnet_id for s in self.vpc.isolated_subnets),
            description="Isolated subnet ids (RDS).",
        )
        CfnOutput(
            self,
            "TaskSecurityGroupId",
            value=self.task_security_group.security_group_id,
            description="Egress-only SG for the scan task.",
        )

        Tags.of(self).add("project", "crypto-signals")
        Tags.of(self).add("layer", "network")

        self._apply_nag_suppressions()

    def _add_interface_endpoints(self) -> None:
        """Add ECR / Secrets Manager / CloudWatch Logs interface endpoints.

        Placed in the isolated subnets (the RDS tier) so AWS-service traffic
        from anywhere in the VPC resolves to a private ENI. Off by default;
        see the module docstring for the cost rationale.
        """
        targets: dict[str, ec2.InterfaceVpcEndpointAwsService] = {
            "EcrApiEndpoint": ec2.InterfaceVpcEndpointAwsService.ECR,
            "EcrDkrEndpoint": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            "SecretsManagerEndpoint": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            "CloudWatchLogsEndpoint": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
        }
        for construct_id, service in targets.items():
            self.vpc.add_interface_endpoint(
                construct_id,
                service=service,
                subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                private_dns_enabled=True,
            )

    def _apply_nag_suppressions(self) -> None:
        """Justified cdk-nag suppressions for intentional Slice 1 choices."""
        NagSuppressions.add_resource_suppressions(
            self.task_security_group,
            [
                {
                    "id": "AwsSolutions-EC23",
                    "reason": (
                        "Egress-only security group: it has no inbound rules. "
                        "The all-outbound rule is required so the stateless scan "
                        "task can reach Binance, Anthropic, and Telegram."
                    ),
                }
            ],
        )
