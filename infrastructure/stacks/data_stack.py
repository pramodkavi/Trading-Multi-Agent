"""DataStack: Aurora Serverless v2 (Data API) and the S3 audit bucket.

Implemented in Step 1.16 for the serverless architecture (SPEC §2.2 / §2.4):

- **Aurora Serverless v2 PostgreSQL 16** in the NetworkStack isolated subnets,
  with the **RDS Data API enabled** (the Lambda queries it over HTTPS, no VPC
  attachment) and **scale-to-zero** (min 0 ACU auto-pause) for cost. The
  cluster's generated secret (the one Secrets Manager secret this system keeps)
  IS the Data API credential.
- **S3 bucket** for audit blobs: versioned, Glacier after 90 days, public
  access blocked, SSL enforced, encrypted.

Third-party API keys (Anthropic / Telegram / FRED / Twelve Data) are **not**
created here. As of Step 2.12 they live in **SSM Parameter Store SecureString**
parameters (free standard tier vs. Secrets Manager's per-secret monthly charge --
see ``stacks/parameters.py``). CloudFormation cannot create SecureString
parameters, so they are provisioned out-of-band (``docs/operations.md``); the
ComputeStack/MonitoringStack only reference them for the IAM grants.

Dev removal policy: the cluster and bucket are `DESTROY` (no final snapshot,
auto-empty the bucket) for easy teardown. Production must override these to
RETAIN / SNAPSHOT.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from cdk_nag import NagSuppressions
from constructs import Construct

DATABASE_NAME = "signals"


class DataStack(Stack):
    """Aurora Serverless v2 (Data API) + the S3 audit bucket."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        db_security_group: ec2.ISecurityGroup,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---- S3 audit/blob bucket ----------------------------------------
        self.bucket = s3.Bucket(
            self,
            "BlobBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="archive-old-blobs",
                    enabled=True,
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(90),
                        )
                    ],
                )
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ---- Aurora Serverless v2 (Data API, scale-to-zero) --------------
        self.cluster = rds.DatabaseCluster(
            self,
            "Aurora",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.of("16.6", "16"),
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[db_security_group],
            serverless_v2_min_capacity=0,
            serverless_v2_max_capacity=2,
            writer=rds.ClusterInstance.serverless_v2("Writer"),
            enable_data_api=True,
            credentials=rds.Credentials.from_generated_secret(
                "signals_admin",
                secret_name="crypto-signals/db",
            ),
            default_database_name=DATABASE_NAME,
            iam_authentication=True,
            storage_encrypted=True,
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Third-party API keys are NOT created here -- they are SSM Parameter
        # Store SecureString parameters provisioned out-of-band (Step 2.12,
        # docs/operations.md), because CloudFormation cannot create SecureString
        # parameters. See stacks/parameters.py for the names.

        # ---- Outputs -----------------------------------------------------
        CfnOutput(self, "ClusterArn", value=self.cluster.cluster_arn)
        CfnOutput(self, "ClusterIdentifier", value=self.cluster.cluster_identifier)
        if self.cluster.secret is not None:
            CfnOutput(self, "DbSecretArn", value=self.cluster.secret.secret_arn)
        CfnOutput(self, "DatabaseName", value=DATABASE_NAME)
        CfnOutput(self, "BucketName", value=self.bucket.bucket_name)

        Tags.of(self).add("project", "crypto-signals")
        Tags.of(self).add("layer", "data")

        self._apply_nag_suppressions()

    def _apply_nag_suppressions(self) -> None:
        """Justified cdk-nag suppressions for intentional Slice 1 / dev choices."""
        NagSuppressions.add_resource_suppressions(
            self.cluster,
            [
                {
                    "id": "AwsSolutions-RDS10",
                    "reason": (
                        "Deletion protection is intentionally off on the dev cluster "
                        "for easy teardown (removal_policy=DESTROY). Production overrides "
                        "to enable deletion protection + SNAPSHOT."
                    ),
                },
                {
                    "id": "AwsSolutions-RDS11",
                    "reason": (
                        "Default Postgres port (5432) is acceptable; access is restricted "
                        "to the VPC security group and the IAM-authenticated Data API."
                    ),
                },
                {
                    "id": "AwsSolutions-SMG4",
                    "reason": (
                        "Automatic rotation of the cluster's credentials secret is "
                        "deferred: the rotation Lambda would need a Secrets Manager VPC "
                        "endpoint, which this cost-optimised NAT/endpoint-free design "
                        "omits. The DB is reached via the IAM-authenticated Data API."
                    ),
                },
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            self.bucket,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": (
                        "Server access logging deferred to the Slice 2 hardening step; "
                        "the dev bucket blocks all public access and enforces SSL."
                    ),
                }
            ],
        )
