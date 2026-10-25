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
import shutil
import subprocess
from pathlib import Path
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


# Step 4.5: CDK-managed singletons (BucketDeployment + auto_delete_objects) add
# their own Lambda functions / the AWS CLI layer; they are not "ours".
_CDK_SINGLETON_PREFIXES = (
    "CustomCDKBucketDeployment",
    "CustomS3AutoDeleteObjectsCustomResourceProvider",
    "SpaHtmlDeploymentAwsCliLayer",
    "SpaAssetsDeploymentAwsCliLayer",
)


def _own_resources(templates: dict[str, Any], cfn_type: str) -> dict[str, Any]:
    found: dict[str, Any] = templates["dashboard"].find_resources(cfn_type)
    return {k: v for k, v in found.items() if not k.startswith(_CDK_SINGLETON_PREFIXES)}


def test_dashboard_lambda_is_zip_fastapi_handler(templates: dict[str, Any]) -> None:
    assert len(_own_resources(templates, "AWS::Lambda::Function")) == 1
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
    assert len(_own_resources(templates, "AWS::Lambda::LayerVersion")) == 1
    templates["dashboard"].has_resource_properties(
        "AWS::Lambda::LayerVersion",
        {
            "CompatibleRuntimes": ["python3.11"],
            "Description": assertions.Match.string_like_regexp("extras: dashboard"),
        },
    )


def _routes_by_key(templates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    routes = templates["dashboard"].find_resources("AWS::ApiGatewayV2::Route")
    return {r["Properties"]["RouteKey"]: r["Properties"] for r in routes.values()}


def test_dashboard_http_api_routes(templates: dict[str, Any]) -> None:
    # Step 4.4: the open health route + the authorized proxy route, nothing else.
    t = templates["dashboard"]
    t.resource_count_is("AWS::ApiGatewayV2::Api", 1)
    t.has_resource_properties("AWS::ApiGatewayV2::Api", {"ProtocolType": "HTTP"})
    t.resource_count_is("AWS::ApiGatewayV2::Route", 2)
    assert set(_routes_by_key(templates)) == {"GET /api/health", "ANY /api/{proxy+}"}
    # Both routes share ONE Lambda integration (one integration, one permission set).
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
                {
                    "DestinationArn": assertions.Match.any_value(),
                    # HTTP-API variables: CDK's default (Common Log) format uses the
                    # REST-era $context.resourcePath, which an HTTP API logs as "-".
                    "Format": assertions.Match.string_like_regexp(
                        r"\$context\.routeKey.*\$context\.path"
                    ),
                }
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


def _dashboard_statements(templates: dict[str, Any]) -> list[dict[str, Any]]:
    # The API function's role only -- the BucketDeployment singleton's own S3
    # policy (Step 4.5, CDK-managed) is not the dashboard API's grant.
    policies = _own_resources(templates, "AWS::IAM::Policy")
    assert len(policies) == 1
    return [s for p in policies.values() for s in p["Properties"]["PolicyDocument"]["Statement"]]


def _actions(statement: dict[str, Any]) -> list[str]:
    action = statement["Action"]
    return [action] if isinstance(action, str) else list(action)


def test_dashboard_role_has_data_api_but_no_ssm(templates: dict[str, Any]) -> None:
    actions = {a for s in _dashboard_statements(templates) for a in _actions(s)}
    assert "rds-data:ExecuteStatement" in actions
    assert "secretsmanager:GetSecretValue" in actions
    assert not any(a.startswith(("ssm:", "s3:", "lambda:")) for a in actions)


def test_dashboard_role_cloudwatch_read_only(templates: dict[str, Any]) -> None:
    # Step 4.3 (design §8): the alarm strip + Lambda metrics, read-only.
    statements = _dashboard_statements(templates)
    cw = [s for s in statements if any(a.startswith("cloudwatch:") for a in _actions(s))]
    actions = {a for s in cw for a in _actions(s)}
    assert actions == {
        "cloudwatch:DescribeAlarms",
        "cloudwatch:ListMetrics",
        "cloudwatch:GetMetricData",
    }
    assert all(s["Effect"] == "Allow" for s in cw)

    (alarms,) = [s for s in cw if "cloudwatch:DescribeAlarms" in _actions(s)]
    assert _actions(alarms) == ["cloudwatch:DescribeAlarms"]
    rendered = json.dumps(alarms["Resource"])
    assert ":cloudwatch:" in rendered
    assert ":alarm:CryptoSignals-Monitoring-*" in rendered
    assert alarms["Resource"] != "*"

    (metrics,) = [s for s in cw if "cloudwatch:ListMetrics" in _actions(s)]
    assert sorted(_actions(metrics)) == ["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"]
    assert metrics["Resource"] == "*"  # these two have no resource-level permissions

    every = {a for s in statements for a in _actions(s)}
    assert not any(a.startswith(("ssm:", "s3:", "lambda:")) for a in every)
    assert not any(a.startswith("cloudwatch:") and a not in actions for a in every)


def test_dashboard_cloudwatch_iam5_suppression_present(templates: dict[str, Any]) -> None:
    # The `*` / alarm-prefix wildcards are justified in code (AwsSolutions-IAM5).
    policies = _own_resources(templates, "AWS::IAM::Policy")
    rules = [
        rule
        for p in policies.values()
        for rule in p.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])
        if rule["id"] == "AwsSolutions-IAM5"
    ]
    assert rules, "expected a justified AwsSolutions-IAM5 suppression on the dashboard policy"
    assert all("resource-level" in rule["reason"] for rule in rules)
    applies = json.dumps([rule.get("applies_to") for rule in rules])
    assert "Resource::*" in applies and "CryptoSignals-Monitoring-" in applies


def _nagged_dashboard(spa_dist: Path) -> Any:
    """A fresh app with the AwsSolutions pack applied to the dashboard stack only."""
    import aws_cdk as core
    from cdk_nag import AwsSolutionsChecks
    from stacks.dashboard_stack import DashboardStack
    from stacks.data_stack import DataStack
    from stacks.network_stack import NetworkStack

    app = core.App(context={"aws:cdk:bundling-stacks": []})
    network = NetworkStack(app, "Network")
    data = DataStack(app, "Data", vpc=network.vpc, db_security_group=network.db_security_group)
    dashboard = DashboardStack(
        app, "Dashboard", cluster=data.cluster, app_version="t", spa_dist=spa_dist
    )
    core.Aspects.of(dashboard).add(AwsSolutionsChecks())
    return dashboard


def test_dashboard_cloudwatch_grants_pass_cdk_nag(spa_dist: Path) -> None:
    # The fixture does not apply the nag aspect; run it on a fresh app so a malformed
    # `appliesTo` (an unsuppressed IAM5 finding) fails here, not only in the full synth.
    from aws_cdk import assertions as cdk_assertions

    dashboard = _nagged_dashboard(spa_dist)
    errors = cdk_assertions.Annotations.from_stack(dashboard).find_error(
        "*", cdk_assertions.Match.string_like_regexp("AwsSolutions-IAM5")
    )
    assert errors == []


def test_dashboard_imports_only_from_data_stack(templates: dict[str, Any]) -> None:
    # Design §7 / anti-goal §4.4: no coupling to Compute / Scheduling / Monitoring.
    rendered = json.dumps(templates["dashboard"].to_json())
    imports = re.findall(r'"Fn::ImportValue":\s*"([^"]+)"', rendered)
    assert imports, "expected the cluster ARN / secret ARN imports from Data"
    assert all(name.startswith("Data:") for name in imports), imports


def test_dashboard_outputs(templates: dict[str, Any]) -> None:
    templates["dashboard"].has_output("DashboardApiUrl", assertions.Match.any_value())
    templates["dashboard"].has_output("DashboardFunctionName", assertions.Match.any_value())


# ---------------------------------------------------------------------------
# DashboardStack Step 4.4: Cognito user pool + PKCE client + JWT authorizer
# ---------------------------------------------------------------------------


def _user_pool(templates: dict[str, Any]) -> dict[str, Any]:
    pools = templates["dashboard"].find_resources("AWS::Cognito::UserPool")
    assert len(pools) == 1
    return next(iter(pools.values()))


def _client_props(templates: dict[str, Any]) -> dict[str, Any]:
    clients = templates["dashboard"].find_resources("AWS::Cognito::UserPoolClient")
    assert len(clients) == 1
    props: dict[str, Any] = next(iter(clients.values()))["Properties"]
    return props


def test_user_pool_admin_create_only(templates: dict[str, Any]) -> None:
    props = _user_pool(templates)["Properties"]
    assert props["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    # Sign-in by email, case-insensitive; recovery by email only.
    assert props["UsernameAttributes"] == ["email"]
    assert props["UsernameConfiguration"] == {"CaseSensitive": False}
    assert props["AccountRecoverySetting"] == {
        "RecoveryMechanisms": [{"Name": "verified_email", "Priority": 1}]
    }


def test_user_pool_password_policy(templates: dict[str, Any]) -> None:
    policy = _user_pool(templates)["Properties"]["Policies"]["PasswordPolicy"]
    assert policy["MinimumLength"] == 12
    assert policy["RequireLowercase"] is True
    assert policy["RequireUppercase"] is True
    assert policy["RequireNumbers"] is True
    assert policy["RequireSymbols"] is True
    assert policy["TemporaryPasswordValidityDays"] == 3


def test_user_pool_mfa_required_totp_only(templates: dict[str, Any]) -> None:
    # Operator decision 2026-10-21: TOTP MFA required (optional MFA had no
    # enrolment path on the classic Hosted UI).
    props = _user_pool(templates)["Properties"]
    assert props["MfaConfiguration"] == "ON"
    assert props["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]  # no SMS (no SNS cost/role)
    assert "SmsConfiguration" not in props


def test_user_pool_lite_plan_and_retained(templates: dict[str, Any]) -> None:
    pool = _user_pool(templates)
    assert pool["UpdateReplacePolicy"] == "Retain"
    assert pool["DeletionPolicy"] == "Retain"
    props = pool["Properties"]
    assert props["DeletionProtection"] == "ACTIVE"
    assert props["UserPoolTier"] == "LITE"
    # No paid threat protection (advanced security) on the LITE tier.
    assert "UserPoolAddOns" not in props


def test_client_public_code_grant_only(templates: dict[str, Any]) -> None:
    props = _client_props(templates)
    assert props.get("GenerateSecret", False) is False
    assert props["AllowedOAuthFlows"] == ["code"]
    assert props["AllowedOAuthFlowsUserPoolClient"] is True
    assert sorted(props["AllowedOAuthScopes"]) == ["email", "openid", "profile"]
    # Callback / logout URLs (token helper + CloudFront since Step 4.5) are pinned
    # in test_client_callbacks_include_cloudfront_and_helper.
    assert props["SupportedIdentityProviders"] == ["COGNITO"]
    assert props["PreventUserExistenceErrors"] == "ENABLED"
    assert props["EnableTokenRevocation"] is True
    # Access + ID 1 h, refresh 30 days.
    assert props["AccessTokenValidity"] == 60
    assert props["IdTokenValidity"] == 60
    assert props["RefreshTokenValidity"] == 43200
    assert props["TokenValidityUnits"] == {
        "AccessToken": "minutes",
        "IdToken": "minutes",
        "RefreshToken": "minutes",
    }


def test_client_flows_minimal(templates: dict[str, Any]) -> None:
    # No USER_PASSWORD_AUTH, no custom auth, no admin flows.
    assert sorted(_client_props(templates)["ExplicitAuthFlows"]) == [
        "ALLOW_REFRESH_TOKEN_AUTH",
        "ALLOW_USER_SRP_AUTH",
    ]


def test_cognito_domain_prefix_and_classic_ui(templates: dict[str, Any]) -> None:
    domains = templates["dashboard"].find_resources("AWS::Cognito::UserPoolDomain")
    assert len(domains) == 1
    props = next(iter(domains.values()))["Properties"]
    rendered = json.dumps(props["Domain"])
    assert "crypto-signals-dash-" in rendered and "AWS::AccountId" in rendered
    assert props["ManagedLoginVersion"] == 1  # classic Hosted UI


def _authorizer_logical_id(templates: dict[str, Any]) -> str:
    authorizers = templates["dashboard"].find_resources("AWS::ApiGatewayV2::Authorizer")
    assert len(authorizers) == 1
    return next(iter(authorizers))


def test_proxy_route_uses_user_pool_authorizer(templates: dict[str, Any]) -> None:
    proxy = _routes_by_key(templates)["ANY /api/{proxy+}"]
    assert proxy["AuthorizationType"] == "JWT"
    assert proxy["AuthorizerId"] == {"Ref": _authorizer_logical_id(templates)}
    # Same integration as the health route.
    health = _routes_by_key(templates)["GET /api/health"]
    assert proxy["Target"] == health["Target"]


def test_health_route_has_no_authorizer(templates: dict[str, Any]) -> None:
    health = _routes_by_key(templates)["GET /api/health"]
    assert "AuthorizerId" not in health
    assert health.get("AuthorizationType", "NONE") == "NONE"
    assert "AuthorizationScopes" not in health


def test_proxy_route_requires_openid_scope(templates: dict[str, Any]) -> None:
    # Access tokens only: Cognito ID tokens carry no `scope` claim, so API Gateway
    # rejects them; access tokens from the code grant carry `openid`.
    proxy = _routes_by_key(templates)["ANY /api/{proxy+}"]
    assert proxy["AuthorizationScopes"] == ["openid"]


def test_jwt_authorizer_issuer_and_audience(templates: dict[str, Any]) -> None:
    authorizers = templates["dashboard"].find_resources("AWS::ApiGatewayV2::Authorizer")
    props = next(iter(authorizers.values()))["Properties"]
    assert props["AuthorizerType"] == "JWT"
    assert props["IdentitySource"] == ["$request.header.Authorization"]
    jwt = props["JwtConfiguration"]
    assert len(jwt["Audience"]) == 1
    assert jwt["Audience"][0]["Ref"].startswith("DashboardUserPoolDashboardSpaClient")
    issuer = json.dumps(jwt["Issuer"])
    assert "https://cognito-idp." in issuer and "DashboardUserPool" in issuer


def test_cognito_outputs(templates: dict[str, Any]) -> None:
    t = templates["dashboard"]
    outputs = t.find_outputs("*")
    for name in (
        "DashboardUserPoolId",
        "DashboardUserPoolClientId",
        "DashboardCognitoDomain",
        "DashboardJwtIssuer",
    ):
        assert name in outputs, name
    domain = json.dumps(outputs["DashboardCognitoDomain"]["Value"])
    assert "https://" in domain and ".auth." in domain and "amazoncognito.com" in domain
    issuer = json.dumps(outputs["DashboardJwtIssuer"]["Value"])
    assert "https://cognito-idp." in issuer


def test_dashboard_nag_clean_with_cognito(spa_dist: Path) -> None:
    # Full AwsSolutions pack on the dashboard stack: no error or warning at all,
    # with COG3/COG8 the only Cognito suppressions (pool only) and APIG4 on the
    # health route only (the proxy route is satisfied by the authorizer). COG2
    # (MFA) is satisfied by required TOTP and must not be suppressed anywhere.
    # Step 4.5 pins the SPA suppressions too: CFR1-4 on the distribution only,
    # S1 on the SPA bucket only, and IAM4/IAM5/L1 only on the dashboard API
    # function or the CDK-managed BucketDeployment / auto-delete singletons.
    from aws_cdk import assertions as cdk_assertions

    dashboard = _nagged_dashboard(spa_dist)
    annotations = cdk_assertions.Annotations.from_stack(dashboard)
    nag = cdk_assertions.Match.string_like_regexp("AwsSolutions-")
    assert annotations.find_error("*", nag) == []
    assert annotations.find_warning("*", nag) == []

    template = cdk_assertions.Template.from_stack(dashboard).to_json()
    resources = template["Resources"]
    suppressed: dict[str, set[str]] = {}
    for logical_id, res in resources.items():
        rules = res.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])
        for rule in rules:
            suppressed.setdefault(rule["id"], set()).add(logical_id)

    def ids_of(cfn_type: str) -> set[str]:
        return {lid for lid, r in resources.items() if r["Type"] == cfn_type}

    pool_ids = ids_of("AWS::Cognito::UserPool")
    assert "AwsSolutions-COG2" not in suppressed, suppressed.get("AwsSolutions-COG2")
    allowed_cog = ("AwsSolutions-COG3", "AwsSolutions-COG8")
    for rule in allowed_cog:  # pool-scoped only: on any other construct this fails
        assert suppressed[rule] == pool_ids, rule
    (apig4,) = suppressed["AwsSolutions-APIG4"]
    assert resources[apig4]["Properties"]["RouteKey"] == "GET /api/health"

    # Step 4.5: the exact allowed set of suppressed rule ids, stack-wide.
    assert set(suppressed) == {
        "AwsSolutions-APIG4",
        "AwsSolutions-COG3",
        "AwsSolutions-COG8",
        "AwsSolutions-IAM4",
        "AwsSolutions-IAM5",
        "AwsSolutions-L1",
        "AwsSolutions-CFR1",
        "AwsSolutions-CFR2",
        "AwsSolutions-CFR3",
        "AwsSolutions-CFR4",
        "AwsSolutions-S1",
    }, sorted(suppressed)
    (distribution,) = ids_of("AWS::CloudFront::Distribution")
    for rule in (
        "AwsSolutions-CFR1",
        "AwsSolutions-CFR2",
        "AwsSolutions-CFR3",
        "AwsSolutions-CFR4",
    ):
        assert suppressed[rule] == {distribution}, rule
    s3_buckets = {lid for lid in ids_of("AWS::S3::Bucket") if lid.startswith("SpaBucket")}
    assert suppressed["AwsSolutions-S1"] == s3_buckets and len(s3_buckets) == 1
    allowed_lambda_prefixes = (
        "DashboardApi",  # the API function, its role and policy (Step 4.1 / 4.3)
        # BucketDeployment singleton (CDK-managed). The auto_delete_objects provider
        # is a raw CfnResource cdk-nag does not flag, so it carries none.
        "CustomCDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C",
    )
    for rule in ("AwsSolutions-IAM4", "AwsSolutions-IAM5", "AwsSolutions-L1"):
        stray = {lid for lid in suppressed[rule] if not lid.startswith(allowed_lambda_prefixes)}
        assert not stray, (rule, stray)


# ---------------------------------------------------------------------------
# DashboardStack Step 4.5: private S3 + CloudFront SPA hosting
# ---------------------------------------------------------------------------


def _distribution_config(templates: dict[str, Any]) -> dict[str, Any]:
    dists = templates["dashboard"].find_resources("AWS::CloudFront::Distribution")
    assert len(dists) == 1
    config: dict[str, Any] = next(iter(dists.values()))["Properties"]["DistributionConfig"]
    return config


def _api_behaviour(templates: dict[str, Any]) -> dict[str, Any]:
    behaviours = _distribution_config(templates)["CacheBehaviors"]
    assert len(behaviours) == 1
    behaviour: dict[str, Any] = behaviours[0]
    assert behaviour["PathPattern"] == "/api/*"
    return behaviour


def _origin(templates: dict[str, Any], origin_id: str) -> dict[str, Any]:
    (origin,) = [o for o in _distribution_config(templates)["Origins"] if o["Id"] == origin_id]
    return origin  # type: ignore[no-any-return]


def _spa_bucket_id(templates: dict[str, Any]) -> str:
    buckets: dict[str, Any] = templates["dashboard"].find_resources("AWS::S3::Bucket")
    assert len(buckets) == 1
    return next(iter(buckets))


# Managed policy ids (AWS-published constants).
CACHING_OPTIMIZED_ID = "658327ea-f89d-4fab-a63d-7e88639e58f6"
CACHING_DISABLED_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ALL_VIEWER_EXCEPT_HOST_ID = "b689b0a8-53d0-40ab-baf2-68738e2966ac"


def test_spa_bucket_private_ssl_encrypted(templates: dict[str, Any]) -> None:
    t = templates["dashboard"]
    bucket = t.find_resources("AWS::S3::Bucket")[_spa_bucket_id(templates)]
    props = bucket["Properties"]
    assert props["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    sse = props["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert sse[0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    assert "VersioningConfiguration" not in props
    assert bucket["DeletionPolicy"] == "Delete"
    # enforce_ssl: a Deny on aws:SecureTransport=false.
    policy = json.dumps(t.find_resources("AWS::S3::BucketPolicy"))
    assert '"aws:SecureTransport": "false"' in policy
    # auto_delete_objects custom resource present (static build output).
    t.resource_count_is("Custom::S3AutoDeleteObjects", 1)


def test_distribution_https_only_http2and3_price200(templates: dict[str, Any]) -> None:
    from aws_cdk import aws_cloudfront as cloudfront
    from stacks.dashboard_stack import SPA_PRICE_CLASS

    config = _distribution_config(templates)
    assert config["PriceClass"] == "PriceClass_200"  # includes India edges (design §8)
    assert SPA_PRICE_CLASS == cloudfront.PriceClass.PRICE_CLASS_200
    assert config["HttpVersion"] == "http2and3"
    assert config["DefaultRootObject"] == "index.html"
    assert config["Enabled"] is True
    assert "Aliases" not in config  # default *.cloudfront.net certificate
    assert config["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    assert _api_behaviour(templates)["ViewerProtocolPolicy"] == "redirect-to-https"


def test_default_behaviour_s3_oac_with_rewrite_function(templates: dict[str, Any]) -> None:
    t = templates["dashboard"]
    default = _distribution_config(templates)["DefaultCacheBehavior"]
    assert default["CachePolicyId"] == CACHING_OPTIMIZED_ID
    origin = _origin(templates, default["TargetOriginId"])
    assert "S3OriginConfig" in origin
    assert origin["S3OriginConfig"].get("OriginAccessIdentity", "") == ""
    assert "OriginAccessControlId" in origin
    t.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    domain = json.dumps(origin["DomainName"])
    assert _spa_bucket_id(templates) in domain
    # Exactly one viewer-request function, on this behaviour.
    (assoc,) = default["FunctionAssociations"]
    assert assoc["EventType"] == "viewer-request"
    functions = t.find_resources("AWS::CloudFront::Function")
    assert len(functions) == 1
    fn_id, fn = next(iter(functions.items()))
    assert assoc["FunctionARN"] == {"Fn::GetAtt": [fn_id, "FunctionARN"]}
    assert fn["Properties"]["FunctionConfig"]["Runtime"] == "cloudfront-js-2.0"
    source = (Path(__file__).resolve().parents[2] / "infrastructure" / "spa_rewrite.js").read_text(
        encoding="utf-8"
    )
    assert fn["Properties"]["FunctionCode"] == source


def test_api_behaviour_forwards_all_viewer_except_host(templates: dict[str, Any]) -> None:
    # Review Focus 3: Authorization (and query strings) reach API Gateway; Host does not.
    api = _api_behaviour(templates)
    assert api["OriginRequestPolicyId"] == ALL_VIEWER_EXCEPT_HOST_ID
    assert api["CachePolicyId"] == CACHING_DISABLED_ID
    assert api["AllowedMethods"] == [
        "GET",
        "HEAD",
        "OPTIONS",
        "PUT",
        "PATCH",
        "POST",
        "DELETE",
    ]
    origin = _origin(templates, api["TargetOriginId"])
    assert origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
    # Domain = host part of the HttpApi endpoint (Fn::Select 2 of split on "/").
    domain = origin["DomainName"]
    assert domain["Fn::Select"][0] == 2
    split = domain["Fn::Select"][1]["Fn::Split"]
    assert split[0] == "/"
    http_api_id = next(iter(templates["dashboard"].find_resources("AWS::ApiGatewayV2::Api")))
    assert split[1] == {"Fn::GetAtt": [http_api_id, "ApiEndpoint"]}
    # $default stage serves at the root: /api/... passes through unchanged.
    assert origin.get("OriginPath", "") == ""


def test_api_behaviour_has_no_function_association(templates: dict[str, Any]) -> None:
    # Review Focus 1: the API's real 403/404 must reach the SPA unchanged.
    api = _api_behaviour(templates)
    assert "FunctionAssociations" not in api
    assert "LambdaFunctionAssociations" not in api


def test_no_distribution_custom_error_responses(templates: dict[str, Any]) -> None:
    assert "CustomErrorResponses" not in _distribution_config(templates)


def _headers_policies(templates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    policies = templates["dashboard"].find_resources("AWS::CloudFront::ResponseHeadersPolicy")
    return {lid: p["Properties"]["ResponseHeadersPolicyConfig"] for lid, p in policies.items()}


def test_security_headers_policy_hsts_csp(templates: dict[str, Any]) -> None:
    policies = _headers_policies(templates)
    default = _distribution_config(templates)["DefaultCacheBehavior"]
    api = _api_behaviour(templates)
    spa_id = default["ResponseHeadersPolicyId"]["Ref"]
    api_id = api["ResponseHeadersPolicyId"]["Ref"]
    for lid in (spa_id, api_id):
        sec = policies[lid]["SecurityHeadersConfig"]
        assert sec["StrictTransportSecurity"] == {
            "AccessControlMaxAgeSec": 31536000,
            "IncludeSubdomains": True,
            "Override": True,
        }
        assert sec["ContentTypeOptions"] == {"Override": True}
        assert sec["ReferrerPolicy"] == {"ReferrerPolicy": "no-referrer", "Override": True}
        assert sec["FrameOptions"] == {"FrameOption": "DENY", "Override": True}
    assert "ContentSecurityPolicy" not in policies[api_id]["SecurityHeadersConfig"]
    csp = policies[spa_id]["SecurityHeadersConfig"]["ContentSecurityPolicy"]
    assert csp["Override"] is True
    rendered = json.dumps(csp["ContentSecurityPolicy"])
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self' https://cognito-idp.",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self' https://",
    ):
        assert directive in rendered, directive
    assert "unsafe-inline" not in rendered and "unsafe-eval" not in rendered
    # The Hosted UI host is built from the Cognito domain (connect-src + form-action).
    domain_id = next(iter(templates["dashboard"].find_resources("AWS::Cognito::UserPoolDomain")))
    assert rendered.count(domain_id) == 2
    assert rendered.count(".auth.") == 2 and "amazoncognito.com" in rendered


def _deployments(templates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = templates["dashboard"].find_resources(
        "Custom::CDKBucketDeployment"
    )
    return found


def test_bucket_deployment_with_config_json_and_invalidation(templates: dict[str, Any]) -> None:
    from stacks.dashboard_stack import SPA_ASSET_CACHE_CONTROL, SPA_HTML_CACHE_CONTROL

    assert SPA_HTML_CACHE_CONTROL == "no-cache"
    assert SPA_ASSET_CACHE_CONTROL == "public, max-age=31536000, immutable"
    deployments = _deployments(templates)
    assert len(deployments) == 2
    by_cache = {
        d["Properties"]["SystemMetadata"]["cache-control"]: (lid, d)
        for lid, d in deployments.items()
    }
    assert set(by_cache) == {SPA_HTML_CACHE_CONTROL, SPA_ASSET_CACHE_CONTROL}
    assets_id, assets = by_cache[SPA_ASSET_CACHE_CONTROL]
    _, html = by_cache[SPA_HTML_CACHE_CONTROL]
    bucket_id = _spa_bucket_id(templates)
    for d in (assets, html):
        assert d["Properties"]["DestinationBucketName"] == {"Ref": bucket_id}
    # Hashed assets: only assets/*, never pruned (cached old index.html may still
    # reference last deploy's chunks; the other deployment's files are untouched).
    assert assets["Properties"]["Prune"] is False
    assert assets["Properties"]["Exclude"] == ["*"]
    assert assets["Properties"]["Include"] == ["assets/*"]
    assert "DistributionId" not in assets["Properties"]
    # index.html / config.json / root files: everything except assets/*, pruned
    # (excluded files are never deleted by the sync), plus the invalidation.
    assert html["Properties"].get("Prune", True) is True
    assert html["Properties"]["Exclude"] == ["assets/*"]
    dist_id = next(iter(templates["dashboard"].find_resources("AWS::CloudFront::Distribution")))
    assert html["Properties"]["DistributionId"] == {"Ref": dist_id}
    assert html["Properties"]["DistributionPaths"] == ["/*"]
    # The html deployment (with the invalidation) runs after the assets upload.
    assert assets_id in html.get("DependsOn", [])
    # Sources: the dist asset + the generated config.json, which is the html
    # deployment's only extra source.
    assert len(html["Properties"]["SourceObjectKeys"]) == 2
    assert len(assets["Properties"]["SourceObjectKeys"]) == 1
    assert html["Properties"]["SourceObjectKeys"][0] == assets["Properties"]["SourceObjectKeys"][0]
    config, markers = _runtime_config(templates)
    assert set(config) == {
        "region",
        "userPoolId",
        "clientId",
        "cognitoDomain",
        "apiBase",
        "redirectUri",
        "logoutUri",
        "version",
    }
    assert markers  # the token values (resolved by the deployment at deploy time)


_MARKER = re.compile(r"<<marker:0xbaba:(\d+)>>")


def _runtime_config(templates: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The staged ``config.json`` (tokens -> marker ids) + the deployment's markers."""
    (html,) = [
        d
        for d in _deployments(templates).values()
        if d["Properties"]["SystemMetadata"]["cache-control"] == "no-cache"
    ]
    config_key = json.dumps(html["Properties"]["SourceObjectKeys"][1])
    (path,) = [
        p
        for p in Path(templates["dashboard_outdir"]).glob("asset.*/config.json")
        if p.parent.name.removeprefix("asset.") in config_key
    ]
    config: dict[str, Any] = json.loads(
        _MARKER.sub(lambda m: f'"MARKER_{m.group(1)}"', path.read_text(encoding="utf-8"))
    )
    marker_map = html["Properties"]["SourceMarkers"][1]
    markers = {f"MARKER_{_MARKER.fullmatch(k).group(1)}": v for k, v in marker_map.items()}  # type: ignore[union-attr]
    return config, markers


def test_config_json_contents(templates: dict[str, Any]) -> None:
    # Keys + values the SPA loader requires (Task 1): same-origin redirect/logout
    # URIs on the distribution domain, apiBase "/api", the APP_VERSION.
    config, markers = _runtime_config(templates)
    t = templates["dashboard"]
    assert config["apiBase"] == "/api"
    assert config["version"] == "t3st000"

    def value(key: str) -> str:
        return json.dumps(markers[config[key]])

    dist_id = next(iter(t.find_resources("AWS::CloudFront::Distribution")))
    pool_id = next(iter(t.find_resources("AWS::Cognito::UserPool")))
    client_id = next(iter(t.find_resources("AWS::Cognito::UserPoolClient")))
    domain_id = next(iter(t.find_resources("AWS::Cognito::UserPoolDomain")))
    assert "AWS::Region" in value("region")
    assert pool_id in value("userPoolId")
    assert client_id in value("clientId")
    assert domain_id in value("cognitoDomain") and "amazoncognito.com" in value("cognitoDomain")
    assert value("redirectUri") == json.dumps(
        {"Fn::Join": ["", ['"https://', {"Fn::GetAtt": [dist_id, "DomainName"]}, '/callback"']]}
    )
    assert value("logoutUri") == json.dumps(
        {"Fn::Join": ["", ['"https://', {"Fn::GetAtt": [dist_id, "DomainName"]}, '/"']]}
    )


def test_client_callbacks_include_cloudfront_and_helper(templates: dict[str, Any]) -> None:
    props = _client_props(templates)
    dist_id = next(iter(templates["dashboard"].find_resources("AWS::CloudFront::Distribution")))
    domain = {"Fn::GetAtt": [dist_id, "DomainName"]}
    assert props["CallbackURLs"] == [
        "http://localhost:8765/callback",
        {"Fn::Join": ["", ["https://", domain, "/callback"]]},
    ]
    assert props["LogoutURLs"] == [
        "http://localhost:8765/",
        {"Fn::Join": ["", ["https://", domain, "/"]]},
    ]


def test_dashboard_outputs_include_url(templates: dict[str, Any]) -> None:
    outputs = templates["dashboard"].find_outputs("*")
    for name in ("DashboardUrl", "DashboardDistributionId", "DashboardSpaBucketName"):
        assert name in outputs, name
    dist_id = next(iter(templates["dashboard"].find_resources("AWS::CloudFront::Distribution")))
    assert outputs["DashboardUrl"]["Value"] == {
        "Fn::Join": ["", ["https://", {"Fn::GetAtt": [dist_id, "DomainName"]}]]
    }
    assert outputs["DashboardDistributionId"]["Value"] == {"Ref": dist_id}
    assert outputs["DashboardSpaBucketName"]["Value"] == {"Ref": _spa_bucket_id(templates)}


SPA_REWRITE_TABLE = {
    "/": "/index.html",
    "/callback": "/index.html",
    "/signals": "/index.html",
    "/signals/abc": "/index.html",
    "/signals/abc/": "/index.html",
    "/assets/x.js": "/assets/x.js",
    "/assets/index-D1ezsjbz.css": "/assets/index-D1ezsjbz.css",
    "/config.json": "/config.json",
    "/favicon.svg": "/favicon.svg",
    "/index.html": "/index.html",
}


def test_spa_rewrite_function_table() -> None:
    # Review Focus 5: run the real CloudFront Function source in Node.
    source_path = Path(__file__).resolve().parents[2] / "infrastructure" / "spa_rewrite.js"
    source = source_path.read_text(encoding="utf-8")
    # Source-level guards (always run): a JS 2.0 handler, no module system, and
    # the "no dot in the last segment" rule.
    assert "function handler(event)" in source
    assert "module.exports" not in source and "require(" not in source
    assert 'lastIndexOf("/")' in source and 'indexOf(".")' in source
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH: the source-level guards above still ran")
    harness = (
        source
        + "\nconst table = JSON.parse(process.argv[1]);\n"
        + "const out = {};\n"
        + "for (const uri of Object.keys(table)) {\n"
        + "  out[uri] = handler({request: {uri: uri, method: 'GET', headers: {}}}).uri;\n"
        + "}\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        [node, "-e", harness, json.dumps(SPA_REWRITE_TABLE)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(result.stdout) == SPA_REWRITE_TABLE


def _load_app_module() -> Any:
    """Import ``infrastructure/app.py`` without synthesizing (``main()`` is guarded)."""
    import importlib.util

    app_py = Path(__file__).resolve().parents[2] / "infrastructure" / "app.py"
    spec = importlib.util.spec_from_file_location("_infra_app_under_test", app_py)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_app_requires_spa_dist(tmp_path: Path) -> None:
    # Review Focus 4: a missing build fails synth loudly, with the build hint.
    app_module = _load_app_module()
    with pytest.raises(SystemExit) as excinfo:
        app_module.resolve_spa_dist(tmp_path)
    assert "dashboard-ui/dist/index.html not found" in str(excinfo.value)
    assert "npm ci && npm run build" in str(excinfo.value)
    # An empty dist directory (no index.html) is still missing.
    (tmp_path / "dashboard-ui" / "dist").mkdir(parents=True)
    with pytest.raises(SystemExit):
        app_module.resolve_spa_dist(tmp_path)
    (tmp_path / "dashboard-ui" / "dist" / "index.html").write_text("<html></html>", "utf-8")
    assert app_module.resolve_spa_dist(tmp_path) == tmp_path / "dashboard-ui" / "dist"


# ---------------------------------------------------------------------------
# Step 4.7 Part B: the dashboard URL reaches the scan Lambda for Telegram deep links
# (design §7: a static SSM parameter, no cross-stack reference in either direction)
# ---------------------------------------------------------------------------

DASHBOARD_URL_PARAM = "/crypto-signals/dashboard-base-url"


def _compute_ssm_statements(templates: dict[str, Any]) -> list[dict[str, Any]]:
    policies = templates["compute"].find_resources("AWS::IAM::Policy")
    out: list[dict[str, Any]] = []
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            if "ssm:GetParameter" in (actions if isinstance(actions, list) else [actions]):
                out.append(statement)
    return out


def test_dashboard_stack_publishes_its_url_as_a_plain_ssm_string(
    templates: dict[str, Any],
) -> None:
    params = templates["dashboard"].find_resources("AWS::SSM::Parameter")
    assert len(params) == 1
    props = next(iter(params.values()))["Properties"]
    assert props["Name"] == DASHBOARD_URL_PARAM
    assert props["Type"] == "String"  # CloudFormation cannot create SecureString
    assert props["Tier"] == "Standard"
    # The value is the CloudFront origin: https:// + the distribution's domain name.
    value = json.dumps(props["Value"])
    assert "https://" in value and "DomainName" in value


def test_scan_lambda_is_given_the_url_parameter_name(templates: dict[str, Any]) -> None:
    templates["compute"].has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": assertions.Match.object_like(
                    {"DASHBOARD_URL_PARAM_NAME": DASHBOARD_URL_PARAM}
                )
            }
        },
    )


def test_scan_role_reads_exactly_the_three_named_parameters(templates: dict[str, Any]) -> None:
    statements = _compute_ssm_statements(templates)
    assert len(statements) == 1, "one shared read-only SSM statement"
    resources = statements[0]["Resource"]
    rendered = [json.dumps(r) for r in (resources if isinstance(resources, list) else [resources])]
    assert len(rendered) == 3
    for suffix in (
        "parameter/crypto-signals/anthropic-api-key",
        "parameter/crypto-signals/telegram-bot-token",
        "parameter/crypto-signals/dashboard-base-url",
    ):
        assert sum(suffix in r for r in rendered) == 1, suffix
    assert not any('"*"' in r or r.rstrip('"}]').endswith("*") for r in rendered)
    assert statements[0]["Effect"] == "Allow"


def test_no_cross_stack_reference_for_the_dashboard_url(templates: dict[str, Any]) -> None:
    compute = json.dumps(templates["compute"].to_json())
    assert "Dashboard:" not in compute, "Compute must not import anything from Dashboard"
    dashboard = templates["dashboard"].to_json()
    exported = [name for name, out in dashboard.get("Outputs", {}).items() if "Export" in out]
    assert exported == [], "Dashboard must not export values for other stacks"


def test_env_var_name_matches_the_app_constant() -> None:
    from stacks.parameters import DASHBOARD_BASE_URL_PARAM_NAME, DASHBOARD_URL_PARAM_ENV

    from src.config.dashboard_url import DASHBOARD_URL_PARAM_NAME_ENV

    assert DASHBOARD_URL_PARAM_ENV == DASHBOARD_URL_PARAM_NAME_ENV
    assert DASHBOARD_BASE_URL_PARAM_NAME == DASHBOARD_URL_PARAM
