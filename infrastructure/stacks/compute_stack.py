"""ComputeStack: the scan Lambda (container image) and its least-privilege role.

Implemented in Step 1.18 for the serverless architecture (SPEC §2.4 / §3.3.3):

- A **Lambda built from a container image** (``Dockerfile.lambda``, AWS base
  image + Runtime Interface Client), pushed to ECR by CDK at deploy time.
- The function runs **outside any VPC**, so its egress to the non-AWS APIs it
  calls (Binance / Anthropic / Telegram) is free over the public internet and
  Aurora is reached over the RDS Data API (HTTPS) -- no VPC attachment, no NAT.
- A **least-privilege execution role** (NFR-3.2): RDS Data API access to the one
  Aurora cluster, read-only access to the required Secrets Manager secrets,
  read-write to a single S3 prefix, and CloudWatch Logs write -- nothing more.
- Non-secret config is injected via environment variables. Secret *values* are
  NOT baked into the template; the function is given the secret ARNs and reads
  the values from Secrets Manager at runtime (the runtime hydration is wired in
  the follow-on step; this stack grants the access and passes the ARNs).

The cluster / bucket / secrets live in the DataStack and are passed in, so the
grants below are cross-stack references (CDK emits the exports/imports).
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
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from cdk_nag import NagSuppressions
from constructs import Construct

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
        api_secrets: dict[str, secretsmanager.Secret],
        db_name: str = "signals",
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if cluster.secret is None:  # pragma: no cover - DataStack always generates one
            raise ValueError("cluster must have a generated credentials secret for the Data API")

        anthropic_secret = api_secrets["Anthropic"]
        telegram_secret = api_secrets["Telegram"]

        # ---- Log group (explicit, so no log-retention custom resource) -------
        log_group = logs.LogGroup(
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
            log_group=log_group,
            environment={
                "PERSISTENCE_BACKEND": "dataapi",
                "DB_CLUSTER_ARN": cluster.cluster_arn,
                "DB_SECRET_ARN": cluster.secret.secret_arn,
                "DB_NAME": db_name,
                "BLOB_BUCKET": bucket.bucket_name,
                # Secret ARNs (not values): the app reads the values at runtime.
                "ANTHROPIC_SECRET_ARN": anthropic_secret.secret_arn,
                "TELEGRAM_SECRET_ARN": telegram_secret.secret_arn,
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
        # Read-only the third-party secrets the scan actually uses in Slice 1.
        anthropic_secret.grant_read(self.function)
        telegram_secret.grant_read(self.function)
        # Read-write to the audit prefix only (not the whole bucket).
        bucket.grant_read_write(self.function, S3_AUDIT_PREFIX)

        # ---- Outputs ---------------------------------------------------------
        CfnOutput(self, "ScanFunctionArn", value=self.function.function_arn)
        CfnOutput(self, "ScanFunctionName", value=self.function.function_name)

        Tags.of(self).add("project", "crypto-signals")
        Tags.of(self).add("layer", "compute")

        self._apply_nag_suppressions()

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
                        "grant_read_write and to the per-object action families S3/"
                        "Secrets/Data-API grants generate; every statement is scoped to "
                        "the one cluster, the named secrets, or the one bucket prefix -- "
                        "no account-wide or service-wide access."
                    ),
                },
            ],
            apply_to_children=True,
        )
