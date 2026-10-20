"""CDK synth-time assertions for the Step 2.12 changes (and later stack additions).

Covers the SSM-Parameter-Store secret migration (DataStack/ComputeStack), the
NFR-2.2 alarms + Telegram alerting path (MonitoringStack), and the Slice 4
DashboardStack scaffold. jsii (-> Node) emits deprecation/runtime warnings the
project's ``filterwarnings=error`` would otherwise turn into failures; they are
not about our code, so ignore them here.
"""

from __future__ import annotations

import json
import re
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
# MonitoringStack: 5 alarms + SNS -> notifier Lambda + provider-error filters
# ---------------------------------------------------------------------------


def test_monitoring_has_five_alarms(templates: dict[str, Any]) -> None:
    templates["monitoring"].resource_count_is("AWS::CloudWatch::Alarm", 5)


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
    # handler overridden. The layer is built in THIS stack too (same asset
    # content -> one upload) rather than imported from Compute: an imported
    # LayerVersion ARN is a CloudFormation export, and every dependency bump
    # would replace the layer and break `cdk deploy --all` with
    # "Cannot update export ... in use by CryptoSignals-Monitoring".
    templates["monitoring"].resource_count_is("AWS::Lambda::LayerVersion", 1)
    templates["monitoring"].has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.11",
            "Handler": "scripts.alarm_notifier.lambda_handler",
            "Architectures": ["x86_64"],
            "PackageType": assertions.Match.absent(),
            "Layers": [{"Ref": assertions.Match.string_like_regexp("^DepsLayer")}],
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
    templates["monitoring"].resource_count_is("AWS::Logs::MetricFilter", 2)
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


def test_monitoring_forecaster_error_metric_filter_and_alarm(templates: dict[str, Any]) -> None:
    # Final-review fix (Step 4.0): the shared ProviderErrorAlarm needs >= 3
    # markers in an hour, but the Forecaster emits at most one per open setup
    # per sweep (cap 3), so a dead Anthropic key with 1-2 open setups would never
    # alarm. A dedicated filter on BOTH terms + a threshold-1 alarm closes that.
    templates["monitoring"].has_resource_properties(
        "AWS::Logs::MetricFilter",
        assertions.Match.object_like(
            {
                "FilterPattern": assertions.Match.string_like_regexp(
                    "PROVIDER_ERROR.*provider=forecaster"
                ),
                "MetricTransformations": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "MetricNamespace": "CryptoSignals",
                                "MetricName": "ForecasterErrors",
                            }
                        )
                    ]
                ),
            }
        ),
    )
    templates["monitoring"].has_resource_properties(
        "AWS::CloudWatch::Alarm",
        assertions.Match.object_like(
            {
                "Namespace": "CryptoSignals",
                "MetricName": "ForecasterErrors",
                "Threshold": 1,
                "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            }
        ),
    )


# ---------------------------------------------------------------------------
# MonitoringStack: $15/month AWS Budget -> alarm topic (dashboard design §9)
# ---------------------------------------------------------------------------


def test_monitoring_has_monthly_cost_budget(templates: dict[str, Any]) -> None:
    templates["monitoring"].resource_count_is("AWS::Budgets::Budget", 1)
    templates["monitoring"].has_resource_properties(
        "AWS::Budgets::Budget",
        {
            "Budget": assertions.Match.object_like(
                {
                    "BudgetType": "COST",
                    "TimeUnit": "MONTHLY",
                    "BudgetLimit": {"Amount": 15, "Unit": "USD"},
                }
            ),
            "NotificationsWithSubscribers": assertions.Match.array_with(
                [
                    assertions.Match.object_like(
                        {
                            "Notification": assertions.Match.object_like(
                                {"NotificationType": "ACTUAL", "Threshold": 80}
                            )
                        }
                    ),
                    assertions.Match.object_like(
                        {
                            "Notification": assertions.Match.object_like(
                                {"NotificationType": "FORECASTED", "Threshold": 100}
                            )
                        }
                    ),
                ]
            ),
        },
    )


def test_alarm_topic_allows_budgets_to_publish(templates: dict[str, Any]) -> None:
    # AWS Budgets publishes to SNS as a service principal; without this topic
    # policy the notification is silently dropped (no Telegram message).
    templates["monitoring"].has_resource_properties(
        "AWS::SNS::TopicPolicy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Principal": {"Service": "budgets.amazonaws.com"},
                                "Action": "sns:Publish",
                            }
                        )
                    ]
                )
            }
        },
    )


def test_budget_depends_on_alarm_topic_policy(templates: dict[str, Any]) -> None:
    # AWS Budgets validates topic access at create time ("Invalid SNS topic");
    # without an explicit DependsOn, CFN may create the Budget before the
    # TopicPolicy update that grants budgets.amazonaws.com publish lands.
    template = templates["monitoring"]
    policy_ids = list(template.find_resources("AWS::SNS::TopicPolicy"))
    assert len(policy_ids) == 1
    template.has_resource(
        "AWS::Budgets::Budget",
        {"DependsOn": assertions.Match.array_with([policy_ids[0]])},
    )


# ---------------------------------------------------------------------------
# SchedulingStack: Critic v0 outcome resolver runs on rate(2 days) (Step 2.14)
# ---------------------------------------------------------------------------


def test_scheduling_forecaster_sweep_passes_mode_input(templates: dict[str, Any]) -> None:
    # Pre-existing Step 2.10 schedule; pinned here so the resolver test below
    # cannot pass by accident against the wrong schedule's Input.
    templates["scheduling"].has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "cron(5 8,13,15,22 * * ? *)",
            "Target": assertions.Match.object_like({"Input": '{"mode":"forecaster"}'}),
        },
    )


def test_scheduling_stack_has_resolver_rate_schedule(templates: dict[str, Any]) -> None:
    # Design 2026-09-22 §7.2: every 2 days the SAME scan Lambda is invoked in
    # resolve mode with the default chunk size, keeping outcomes flowing even
    # when the GitHub Actions critic workflow is unavailable.
    templates["scheduling"].has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "rate(2 days)",
            "Target": assertions.Match.object_like({"Input": '{"mode":"resolve","chunk_size":25}'}),
        },
    )


def test_scheduling_stack_exports_resolver_schedule_name(templates: dict[str, Any]) -> None:
    templates["scheduling"].has_output("ResolverScheduleName", assertions.Match.any_value())


def test_scheduling_stack_schedule_count(templates: dict[str, Any]) -> None:
    # 4 scan windows + forecaster sweep + outcome resolver.
    templates["scheduling"].resource_count_is("AWS::Scheduler::Schedule", 6)


# ---------------------------------------------------------------------------
# DashboardStack (Slice 4 Step 4.1, design §3.2 / §8 / §10)
# ---------------------------------------------------------------------------


def test_dashboard_lambda_is_zip_fastapi_handler(templates: dict[str, Any]) -> None:
    templates["dashboard"].resource_count_is("AWS::Lambda::Function", 1)
    templates["dashboard"].has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.11",
            "Handler": "src.dashboard.api.app.handler",
            "Architectures": ["x86_64"],
            "PackageType": assertions.Match.absent(),
            "VpcConfig": assertions.Match.absent(),
            "Layers": [{"Ref": assertions.Match.string_like_regexp("^DepsLayer")}],
            "MemorySize": 512,
            "Timeout": 25,
            "Environment": {
                "Variables": assertions.Match.object_like(
                    {
                        "DB_NAME": "signals",
                        "APP_VERSION": "t3st000",
                        "LOG_LEVEL": "INFO",
                        "DB_CLUSTER_ARN": assertions.Match.any_value(),
                        "DB_SECRET_ARN": assertions.Match.any_value(),
                    }
                )
            },
        },
    )


def test_dashboard_stack_owns_a_layer_with_the_dashboard_extra(templates: dict[str, Any]) -> None:
    templates["dashboard"].resource_count_is("AWS::Lambda::LayerVersion", 1)
    templates["dashboard"].has_resource_properties(
        "AWS::Lambda::LayerVersion",
        {
            "CompatibleRuntimes": ["python3.11"],
            "Description": assertions.Match.string_like_regexp("extras: dashboard"),
        },
    )


def test_dashboard_http_api_routes_health_only(templates: dict[str, Any]) -> None:
    t = templates["dashboard"]
    t.resource_count_is("AWS::ApiGatewayV2::Api", 1)
    t.has_resource_properties("AWS::ApiGatewayV2::Api", {"ProtocolType": "HTTP"})
    t.resource_count_is("AWS::ApiGatewayV2::Route", 1)
    t.has_resource_properties(
        "AWS::ApiGatewayV2::Route",
        {
            "RouteKey": "GET /api/health",
            # No authorizer here; Step 4.4 adds the JWT authorizer to the other routes.
            "AuthorizerId": assertions.Match.absent(),
        },
    )
    t.resource_count_is("AWS::ApiGatewayV2::Integration", 1)
    t.has_resource_properties(
        "AWS::ApiGatewayV2::Integration",
        {"IntegrationType": "AWS_PROXY", "PayloadFormatVersion": "2.0"},
    )


def test_dashboard_default_stage_logged_and_throttled(templates: dict[str, Any]) -> None:
    templates["dashboard"].resource_count_is("AWS::ApiGatewayV2::Stage", 1)
    templates["dashboard"].has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "StageName": "$default",
            "AutoDeploy": True,
            "AccessLogSettings": assertions.Match.object_like(
                {"DestinationArn": assertions.Match.any_value()}
            ),
            "DefaultRouteSettings": assertions.Match.object_like(
                {"ThrottlingRateLimit": 10, "ThrottlingBurstLimit": 20}
            ),
        },
    )


def test_dashboard_log_groups_two_week_retention(templates: dict[str, Any]) -> None:
    templates["dashboard"].resource_count_is("AWS::Logs::LogGroup", 2)
    for _, props in templates["dashboard"].find_resources("AWS::Logs::LogGroup").items():
        assert props["Properties"]["RetentionInDays"] == 14


def test_dashboard_role_has_data_api_but_no_ssm(templates: dict[str, Any]) -> None:
    policies = templates["dashboard"].find_resources("AWS::IAM::Policy")
    statements = [
        s for p in policies.values() for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    actions = {
        a
        for s in statements
        for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    }
    assert "rds-data:ExecuteStatement" in actions
    assert "secretsmanager:GetSecretValue" in actions
    assert not any(a.startswith(("ssm:", "s3:", "lambda:")) for a in actions)


def test_dashboard_imports_only_from_data_stack(templates: dict[str, Any]) -> None:
    # Design §7 / anti-goal §4.4: no coupling to Compute / Scheduling / Monitoring.
    rendered = json.dumps(templates["dashboard"].to_json())
    imports = re.findall(r'"Fn::ImportValue":\s*"([^"]+)"', rendered)
    assert imports, "expected the cluster ARN / secret ARN imports from Data"
    assert all(name.startswith("Data:") for name in imports), imports


def test_dashboard_outputs(templates: dict[str, Any]) -> None:
    templates["dashboard"].has_output("DashboardApiUrl", assertions.Match.any_value())
    templates["dashboard"].has_output("DashboardFunctionName", assertions.Match.any_value())
