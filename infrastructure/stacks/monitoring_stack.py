"""MonitoringStack: CloudWatch alarms + Telegram alerting (Step 2.12).

Implements the four alarms of NFR-2.2 (SPEC §3.3.2) and routes them to the
operator's existing Telegram bot:

    CloudWatch Alarm -> SNS topic -> notifier Lambda -> Telegram sendMessage

Alarms:
  1. **Scan failure rate > 10% over 24h** -- Lambda Errors/Invocations metric
     math. At ~5 scans/day this is effectively "any failure today"; that is the
     intended sensitivity for a signal system the operator trades from.
  2. **Agent latency p95 > 2 min** -- Lambda Duration p95. This is the *scan*
     duration (the closest built-in proxy); true per-agent latency lands with
     Langfuse. 2 min = 120_000 ms.
  3. **Aurora CPU > 80%** -- RDS CPUUtilization, sustained (3 x 5-min periods).
  4. **Provider error rate** -- reinterpreted as a COUNT alarm (>= N provider
     errors in 1h) sourced from a CloudWatch Logs **metric filter** on the scan
     log group, because a true 1h *rate* is meaningless at this scan volume.
     The scan logs a ``PROVIDER_ERROR`` marker (scripts/run_scan.py) which the
     filter counts. Richer provider-error tracking lands in Step 2.13.

The notifier Lambda reuses the scan container image (same asset, CMD overridden
to ``scripts.alarm_notifier.lambda_handler``) so there is one image to build and
patch. It reads the Telegram token/chat from the same SSM SecureString parameter
the scan uses.

Budget alarms for AWS infra / Anthropic spend are deferred (NFR-5.1/5.2 land
with the cost-tracking work in Slice 3's Critic).
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
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from cdk_nag import NagSuppressions
from constructs import Construct

from stacks.parameters import TELEGRAM_PARAM_ENV, TELEGRAM_PARAM_NAME

# Repo root is the Docker build context (holds Dockerfile.lambda); parents[2]
# from infrastructure/stacks/monitoring_stack.py.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Custom metric the provider-error log metric filter publishes into.
PROVIDER_ERROR_NAMESPACE = "CryptoSignals"
PROVIDER_ERROR_METRIC = "ProviderErrors"
# The marker scripts/run_scan.py logs when a provider call raises ProviderError.
PROVIDER_ERROR_MARKER = "PROVIDER_ERROR"

# Alarm thresholds (NFR-2.2).
SCAN_FAILURE_RATE_PCT = 10
LATENCY_P95_MS = 120_000  # 2 minutes
AURORA_CPU_PCT = 80
PROVIDER_ERROR_COUNT_1H = 3


class MonitoringStack(Stack):
    """CloudWatch alarms + an SNS->Lambda->Telegram alerting path."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        scan_function: lambda_.IFunction,
        scan_log_group: logs.ILogGroup,
        cluster: rds.DatabaseCluster,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---- Alert channel: SNS topic -> notifier Lambda -> Telegram ---------
        self.topic = sns.Topic(
            self,
            "AlarmTopic",
            display_name="crypto-signals alarms",
            enforce_ssl=True,
        )
        self.notifier = self._build_notifier()
        self.topic.add_subscription(subscriptions.LambdaSubscription(self.notifier))

        # ---- Alarms (NFR-2.2) ------------------------------------------------
        self.alarms: list[cloudwatch.Alarm] = [
            self._scan_failure_rate_alarm(scan_function),
            self._latency_p95_alarm(scan_function),
            self._aurora_cpu_alarm(cluster),
            self._provider_error_alarm(scan_log_group),
        ]
        for alarm in self.alarms:
            alarm.add_alarm_action(cw_actions.SnsAction(self.topic))

        # ---- Outputs ---------------------------------------------------------
        CfnOutput(self, "AlarmTopicArn", value=self.topic.topic_arn)
        CfnOutput(self, "AlarmNotifierName", value=self.notifier.function_name)

        Tags.of(self).add("project", "crypto-signals")
        Tags.of(self).add("layer", "monitoring")

        self._apply_nag_suppressions()

    # ------------------------------------------------------------------
    # Notifier Lambda
    # ------------------------------------------------------------------

    def _build_notifier(self) -> lambda_.DockerImageFunction:
        """A small Lambda that posts CloudWatch alarms to Telegram.

        Reuses the scan container image (same asset hash -> one ECR image) with
        the CMD overridden to the alarm handler. It reads the Telegram token/chat
        from the same SSM SecureString parameter the scan Lambda uses.
        """
        log_group = logs.LogGroup(
            self,
            "AlarmNotifierLogs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        notifier = lambda_.DockerImageFunction(
            self,
            "AlarmNotifier",
            code=lambda_.DockerImageCode.from_image_asset(
                directory=str(REPO_ROOT),
                file="Dockerfile.lambda",
                cmd=["scripts.alarm_notifier.lambda_handler"],
            ),
            memory_size=256,
            timeout=Duration.seconds(30),
            log_group=log_group,
            environment={
                TELEGRAM_PARAM_ENV: TELEGRAM_PARAM_NAME,
                "LOG_LEVEL": "INFO",
            },
            description=(
                "Posts CloudWatch alarm state changes to the operator's Telegram "
                "bot (subscribed to the alarm SNS topic)."
            ),
        )
        notifier.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[self._param_arn(TELEGRAM_PARAM_NAME)],
            )
        )
        return notifier

    # ------------------------------------------------------------------
    # Alarm builders
    # ------------------------------------------------------------------

    def _scan_failure_rate_alarm(self, fn: lambda_.IFunction) -> cloudwatch.Alarm:
        rate = cloudwatch.MathExpression(
            expression="IF(invocations > 0, 100 * errors / invocations, 0)",
            using_metrics={
                "errors": fn.metric_errors(statistic="Sum", period=Duration.days(1)),
                "invocations": fn.metric_invocations(statistic="Sum", period=Duration.days(1)),
            },
            label="ScanFailureRatePct",
            period=Duration.days(1),
        )
        return rate.create_alarm(
            self,
            "ScanFailureRateAlarm",
            alarm_description="Scan Lambda failure rate > 10% over 24h (NFR-2.2).",
            threshold=SCAN_FAILURE_RATE_PCT,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

    def _latency_p95_alarm(self, fn: lambda_.IFunction) -> cloudwatch.Alarm:
        return fn.metric_duration(statistic="p95", period=Duration.hours(1)).create_alarm(
            self,
            "ScanLatencyP95Alarm",
            alarm_description="Scan Lambda p95 duration > 2 min (NFR-2.2 agent latency).",
            threshold=LATENCY_P95_MS,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

    def _aurora_cpu_alarm(self, cluster: rds.DatabaseCluster) -> cloudwatch.Alarm:
        return cluster.metric_cpu_utilization(period=Duration.minutes(5)).create_alarm(
            self,
            "AuroraCpuAlarm",
            alarm_description="Aurora CPU > 80% sustained (NFR-2.2).",
            threshold=AURORA_CPU_PCT,
            evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

    def _provider_error_alarm(self, scan_log_group: logs.ILogGroup) -> cloudwatch.Alarm:
        logs.MetricFilter(
            self,
            "ProviderErrorMetricFilter",
            log_group=scan_log_group,
            filter_pattern=logs.FilterPattern.all_terms(PROVIDER_ERROR_MARKER),
            metric_namespace=PROVIDER_ERROR_NAMESPACE,
            metric_name=PROVIDER_ERROR_METRIC,
            metric_value="1",
            default_value=0,
        )
        metric = cloudwatch.Metric(
            namespace=PROVIDER_ERROR_NAMESPACE,
            metric_name=PROVIDER_ERROR_METRIC,
            statistic="Sum",
            period=Duration.hours(1),
        )
        return metric.create_alarm(
            self,
            "ProviderErrorAlarm",
            alarm_description=(
                "Data-provider errors >= 3 in 1h (NFR-2.2 provider error rate, "
                "expressed as a count at this scan volume)."
            ),
            threshold=PROVIDER_ERROR_COUNT_1H,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _param_arn(self, name: str) -> str:
        """ARN of an SSM parameter from its name (leading slash is not in the ARN)."""
        return self.format_arn(
            service="ssm",
            resource="parameter",
            resource_name=name.lstrip("/"),
        )

    def _apply_nag_suppressions(self) -> None:
        """Justified cdk-nag suppressions for intentional cost/scope choices."""
        NagSuppressions.add_resource_suppressions(
            self.topic,
            [
                {
                    "id": "AwsSolutions-SNS2",
                    "reason": (
                        "The alarm topic carries only CloudWatch alarm metadata (no "
                        "secrets / PII); server-side KMS encryption would add a CMK's "
                        "monthly cost against this project's tight budget. SSL in "
                        "transit is enforced (enforce_ssl=True)."
                    ),
                }
            ],
        )
        NagSuppressions.add_resource_suppressions(
            self.notifier,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "The notifier uses the AWS-managed AWSLambdaBasicExecutionRole "
                        "for CloudWatch Logs write only; its only other grant is "
                        "ssm:GetParameter on one exact parameter ARN."
                    ),
                }
            ],
            apply_to_children=True,
        )
