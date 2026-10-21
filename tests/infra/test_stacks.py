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
    policies = templates["dashboard"].find_resources("AWS::IAM::Policy")
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
    policies = templates["dashboard"].find_resources("AWS::IAM::Policy")
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


def test_dashboard_cloudwatch_grants_pass_cdk_nag() -> None:
    # The fixture does not apply the nag aspect; run it on a fresh app so a malformed
    # `appliesTo` (an unsuppressed IAM5 finding) fails here, not only in the full synth.
    import aws_cdk as core
    from aws_cdk import assertions as cdk_assertions
    from cdk_nag import AwsSolutionsChecks
    from stacks.dashboard_stack import DashboardStack
    from stacks.data_stack import DataStack
    from stacks.network_stack import NetworkStack

    app = core.App(context={"aws:cdk:bundling-stacks": []})
    network = NetworkStack(app, "Network")
    data = DataStack(app, "Data", vpc=network.vpc, db_security_group=network.db_security_group)
    dashboard = DashboardStack(app, "Dashboard", cluster=data.cluster, app_version="t")
    core.Aspects.of(dashboard).add(AwsSolutionsChecks())
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


def test_user_pool_mfa_optional_totp_only(templates: dict[str, Any]) -> None:
    props = _user_pool(templates)["Properties"]
    assert props["MfaConfiguration"] == "OPTIONAL"
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
    assert props["CallbackURLs"] == ["http://localhost:8765/callback"]
    assert props["LogoutURLs"] == ["http://localhost:8765/"]
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


def test_dashboard_nag_clean_with_cognito() -> None:
    # Full AwsSolutions pack on the dashboard stack: no error or warning at all,
    # with COG2/COG3/COG8 the only Cognito suppressions (pool only) and APIG4 on the
    # health route only (the proxy route is satisfied by the authorizer).
    import aws_cdk as core
    from aws_cdk import assertions as cdk_assertions
    from cdk_nag import AwsSolutionsChecks
    from stacks.dashboard_stack import DashboardStack
    from stacks.data_stack import DataStack
    from stacks.network_stack import NetworkStack

    app = core.App(context={"aws:cdk:bundling-stacks": []})
    network = NetworkStack(app, "Network")
    data = DataStack(app, "Data", vpc=network.vpc, db_security_group=network.db_security_group)
    dashboard = DashboardStack(app, "Dashboard", cluster=data.cluster, app_version="t")
    core.Aspects.of(dashboard).add(AwsSolutionsChecks())
    annotations = cdk_assertions.Annotations.from_stack(dashboard)
    nag = cdk_assertions.Match.string_like_regexp("AwsSolutions-")
    assert annotations.find_error("*", nag) == []
    assert annotations.find_warning("*", nag) == []

    template = cdk_assertions.Template.from_stack(dashboard).to_json()
    suppressed: dict[str, set[str]] = {}
    for logical_id, res in template["Resources"].items():
        rules = res.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])
        for rule in rules:
            suppressed.setdefault(rule["id"], set()).add(logical_id)
    pool_ids = {
        lid for lid, r in template["Resources"].items() if r["Type"] == "AWS::Cognito::UserPool"
    }
    allowed_cog = ("AwsSolutions-COG2", "AwsSolutions-COG3", "AwsSolutions-COG8")
    for rule in allowed_cog:  # pool-scoped only: on any other construct this fails
        assert suppressed[rule] == pool_ids, rule
    assert not any(
        rule.startswith("AwsSolutions-COG") and rule not in allowed_cog for rule in suppressed
    )
    (apig4,) = suppressed["AwsSolutions-APIG4"]
    assert template["Resources"][apig4]["Properties"]["RouteKey"] == "GET /api/health"
