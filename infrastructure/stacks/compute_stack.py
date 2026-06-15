"""ComputeStack: the scan Lambda (container image) and its least-privilege role.

Implemented in Step 1.18 for the serverless architecture (SPEC §2.4 / §3.3.3):

- A **Lambda built from a container image** (``Dockerfile.lambda``, AWS base
  image + Runtime Interface Client), pushed to ECR by CDK at deploy time.
- The function runs **outside any VPC**, so its egress to the non-AWS APIs it
  calls (Binance / Anthropic / Telegram) is free over the public internet and
  Aurora is reached over the RDS Data API (HTTPS) -- no VPC attachment, no NAT.
- A **least-privilege execution role** (NFR-3.2): RDS Data API access to the one
  Aurora cluster, read-only ``ssm:GetParameter`` on the two SSM SecureString
  parameters it uses (Anthropic / Telegram), read-write to a single S3 prefix,
  and CloudWatch Logs write -- nothing more.
- Non-secret config is injected via environment variables. Secret *values* are
  NOT baked into the template; the function is given the SSM parameter *names*
  and reads the values from Parameter Store at runtime (src/config/secrets.py).

Step 2.12 moved the third-party API keys from Secrets Manager to SSM Parameter
Store SecureString (free standard tier; see stacks/parameters.py). The cluster's
own credential secret stays in Secrets Manager and is granted via
``grant_data_api_access``.

The cluster / bucket live in the DataStack and are passed in, so those grants are
cross-stack references (CDK emits the exports/imports). The SSM parameters are
provisioned out-of-band (docs/operations.md) and referenced here only by ARN.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from cdk_nag import NagSuppressions
from constructs import Construct

from stacks.parameters import (
    ANTHROPIC_PARAM_ENV,
    ANTHROPIC_PARAM_NAME,
    TELEGRAM_PARAM_ENV,
    TELEGRAM_PARAM_NAME,
)

# This file is infrastructure/stacks/compute_stack.py; parents[2] is the repo
# root, which is the Docker build context for the Lambda image (it holds
# Dockerfile.lambda, pyproject.toml, src/, scripts/). .dockerignore trims it.
REPO_ROOT = Path(__file__).resolve().parents[2]

# The Lambda may write raw kline snapshots / large reasoning blobs here for audit
# (FR-6.3). Scoped so the grant is to this prefix only, not the whole bucket.
S3_AUDIT_PREFIX = "audit/*"


class ComputeStack(Stack):
    """The scan Lambda (container image) + its least-privilege execution role."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cluster: rds.DatabaseCluster,
        bucket: s3.IBucket,
        db_name: str = "signals",
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if cluster.secret is None:  # pragma: no cover - DataStack always generates one
            raise ValueError("cluster must have a generated credentials secret for the Data API")

        # ---- Log group (explicit, so no log-retention custom resource) -------
        # Exposed so the MonitoringStack can attach a metric filter (provider
        # error count) and the provider-error alarm reads from this log group.
        self.log_group = logs.LogGroup(
            self,
            "ScanLambdaLogs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---- The scan Lambda (container image) -------------------------------
        self.function = lambda_.DockerImageFunction(
            self,
            "ScanLambda",
            code=lambda_.DockerImageCode.from_image_asset(
                directory=str(REPO_ROOT),
                file="Dockerfile.lambda",
            ),
            memory_size=1024,
            # One scan finishes in well under 5 min (NFR-4.1); 10 min leaves
            # headroom for a multi-symbol watchlist run, still under the 15 cap.
            timeout=Duration.minutes(10),
            log_group=self.log_group,
            environment={
                "PERSISTENCE_BACKEND": "dataapi",
                "DB_CLUSTER_ARN": cluster.cluster_arn,
                "DB_SECRET_ARN": cluster.secret.secret_arn,
                "DB_NAME": db_name,
                "BLOB_BUCKET": bucket.bucket_name,
                # SSM parameter NAMES (not values): the app reads the values at
                # runtime via ssm:GetParameter (src/config/secrets.py).
                ANTHROPIC_PARAM_ENV: ANTHROPIC_PARAM_NAME,
                TELEGRAM_PARAM_ENV: TELEGRAM_PARAM_NAME,
                "LOG_LEVEL": "INFO",
            },
            description=(
                "Crypto-signals scan: runs one scheduled SMC scan per invocation "
                "(Slice 1, signal-only). Invoked by EventBridge Scheduler."
            ),
        )

        # ---- Least-privilege grants (NFR-3.2) --------------------------------
        # RDS Data API to the one cluster + read of the cluster credentials
        # secret (grant_data_api_access bundles both).
        cluster.grant_data_api_access(self.function)
        # Read-only the two SSM SecureString parameters the scan uses. The
        # default AWS-managed `aws/ssm` KMS key permits decryption to any
        # principal with ssm:GetParameter on the parameter, so no explicit
        # kms:Decrypt statement is required. ARNs are exact (no wildcard).
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    self._param_arn(ANTHROPIC_PARAM_NAME),
                    self._param_arn(TELEGRAM_PARAM_NAME),
                ],
            )
        )
        # Read-write to the audit prefix only (not the whole bucket).
        bucket.grant_read_write(self.function, S3_AUDIT_PREFIX)

        # ---- Outputs ---------------------------------------------------------
        CfnOutput(self, "ScanFunctionArn", value=self.function.function_arn)
        CfnOutput(self, "ScanFunctionName", value=self.function.function_name)

        Tags.of(self).add("project", "crypto-signals")
        Tags.of(self).add("layer", "compute")

        self._apply_nag_suppressions()

    def _param_arn(self, name: str) -> str:
        """Build the ARN of an SSM parameter from its name (e.g. /crypto-signals/x).

        The leading slash is part of the parameter name but not the ARN segment:
        ``arn:...:parameter/crypto-signals/x``.
        """
        return self.format_arn(
            service="ssm",
            resource="parameter",
            resource_name=name.lstrip("/"),
        )

    def _apply_nag_suppressions(self) -> None:
        """Justified cdk-nag suppressions for intentional Slice 1 choices."""
        NagSuppressions.add_resource_suppressions(
            self.function,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "The function uses the AWS-managed AWSLambdaBasicExecutionRole "
                        "for CloudWatch Logs write only -- the standard, minimal Lambda "
                        "logging policy. All other access is scoped via inline grants."
                    ),
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Wildcards are confined to a single S3 prefix (audit/*) from "
                        "grant_read_write and to the per-object action families the S3 / "
                        "Data-API grants generate; every statement is scoped to the one "
                        "cluster, the one bucket prefix, or the two named SSM parameters "
                        "(exact ARNs) -- no account-wide or service-wide access."
                    ),
                },
            ],
            apply_to_children=True,
        )
