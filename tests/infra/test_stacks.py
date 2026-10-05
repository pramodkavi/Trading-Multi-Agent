"""CDK synth-time assertions for the Step 2.12 changes.

Covers the SSM-Parameter-Store secret migration (DataStack/ComputeStack) and the
NFR-2.2 alarms + Telegram alerting path (MonitoringStack). jsii (-> Node) emits
deprecation/runtime warnings the project's ``filterwarnings=error`` would
otherwise turn into failures; they are not about our code, so ignore them here.
"""

from __future__ import annotations

from typing import Any

import pytest
from aws_cdk import assertions

pytestmark = pytest.mark.filterwarnings("ignore")

ANTHROPIC_PARAM = "/crypto-signals/anthropic-api-key"
TELEGRAM_PARAM = "/crypto-signals/telegram-bot-token"


# ---------------------------------------------------------------------------
# DataStack: API-key Secrets Manager placeholders are gone (moved to SSM)
# ---------------------------------------------------------------------------


def test_data_stack_keeps_only_the_db_secret(templates: dict[str, Any]) -> None:
    # The four API-key placeholder secrets were removed; only Aurora's own
    # generated credentials secret (the Data API credential) remains.
    templates["data"].resource_count_is("AWS::SecretsManager::Secret", 1)


# ---------------------------------------------------------------------------
# ComputeStack: scan Lambda reads SSM parameter names, granted GetParameter
# ---------------------------------------------------------------------------


def test_compute_lambda_has_ssm_param_name_env(templates: dict[str, Any]) -> None:
    templates["compute"].has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": assertions.Match.object_like(
                    {
                        "ANTHROPIC_PARAM_NAME": ANTHROPIC_PARAM,
                        "TELEGRAM_PARAM_NAME": TELEGRAM_PARAM,
                    }
                )
            }
        },
    )


def test_compute_lambda_no_secret_arn_env(templates: dict[str, Any]) -> None:
    # The old Secrets Manager ARN env vars must be gone.
    templates["compute"].has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": assertions.Match.not_(
                    assertions.Match.object_like(
                        {"ANTHROPIC_SECRET_ARN": assertions.Match.any_value()}
                    )
                )
            }
        },
    )


def test_compute_role_can_get_ssm_parameters(templates: dict[str, Any]) -> None:
    templates["compute"].has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [assertions.Match.object_like({"Action": "ssm:GetParameter"})]
                )
            }
        },
    )


# ---------------------------------------------------------------------------
# ComputeStack: zip packaging + dependency layer (design 2026-10-03 §3.1-3.2)
# ---------------------------------------------------------------------------


def test_compute_lambda_is_a_zip_function_on_python311(templates: dict[str, Any]) -> None:
    templates["compute"].resource_count_is("AWS::Lambda::Function", 1)
    templates["compute"].has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.11",
            "Handler": "scripts.run_scan.lambda_handler",
            "Architectures": ["x86_64"],
            "PackageType": assertions.Match.absent(),
            # The layer lives in this stack, so it renders as a plain Ref.
            "Layers": [{"Ref": assertions.Match.string_like_regexp("^DepsLayer")}],
            "MemorySize": 1024,
            "Timeout": 600,
        },
    )


def test_compute_stack_owns_the_dependency_layer(templates: dict[str, Any]) -> None:
    templates["compute"].resource_count_is("AWS::Lambda::LayerVersion", 1)
    templates["compute"].has_resource_properties(
        "AWS::Lambda::LayerVersion",
        {
            "CompatibleRuntimes": ["python3.11"],
            "CompatibleArchitectures": ["x86_64"],
        },
    )


# ---------------------------------------------------------------------------
# MonitoringStack: 4 alarms + SNS -> notifier Lambda + provider-error filter
# ---------------------------------------------------------------------------


def test_monitoring_has_four_alarms(templates: dict[str, Any]) -> None:
    templates["monitoring"].resource_count_is("AWS::CloudWatch::Alarm", 4)


def test_monitoring_has_sns_topic_and_lambda_subscription(templates: dict[str, Any]) -> None:
    templates["monitoring"].resource_count_is("AWS::SNS::Topic", 1)
    templates["monitoring"].resource_count_is("AWS::Lambda::Function", 1)
    templates["monitoring"].has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "lambda"}
    )


def test_monitoring_notifier_is_a_zip_function_sharing_the_layer(
    templates: dict[str, Any],
) -> None:
    # Design 2026-10-03 §3.3: same zip code asset shape as the scan Lambda,
    # handler overridden, layer IMPORTED from Compute (no LayerVersion here).
    templates["monitoring"].resource_count_is("AWS::Lambda::LayerVersion", 0)
    templates["monitoring"].has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.11",
            "Handler": "scripts.alarm_notifier.lambda_handler",
            "Architectures": ["x86_64"],
            "PackageType": assertions.Match.absent(),
            # Cross-stack reference to Compute's DepsLayer.
            "Layers": [{"Fn::ImportValue": assertions.Match.any_value()}],
            "MemorySize": 256,
            "Timeout": 30,
        },
    )


def test_monitoring_notifier_reads_telegram_param(templates: dict[str, Any]) -> None:
    templates["monitoring"].has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": assertions.Match.object_like({"TELEGRAM_PARAM_NAME": TELEGRAM_PARAM})
            }
        },
    )


def test_monitoring_provider_error_metric_filter(templates: dict[str, Any]) -> None:
    templates["monitoring"].resource_count_is("AWS::Logs::MetricFilter", 1)
    templates["monitoring"].has_resource_properties(
        "AWS::Logs::MetricFilter",
        assertions.Match.object_like(
            {
                "FilterPattern": assertions.Match.string_like_regexp("PROVIDER_ERROR"),
                "MetricTransformations": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "MetricNamespace": "CryptoSignals",
                                "MetricName": "ProviderErrors",
                            }
                        )
                    ]
                ),
            }
        ),
    )
