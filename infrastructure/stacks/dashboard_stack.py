"""DashboardStack: the read-only dashboard API Lambda behind an HTTP API (Slice 4).

Design: docs/superpowers/specs/2026-10-18-serverless-dashboard-design.md.
Step 4.1 ships the scaffold -- enough to deploy, hit ``GET /api/health`` and see
which commit is live. Later steps add resources to THIS stack; nothing here is
throwaway.

What it is (design §3.2):

- One **zip-packaged Lambda** (FastAPI wrapped by Mangum, handler
  ``src.dashboard.api.app.handler``) built from the same ``src/`` + ``scripts/``
  code asset as the scan Lambda, so the dashboard source is just as readable and
  editable in the console. It runs **outside any VPC** and reaches Aurora over
  the RDS Data API, exactly like the scan Lambda -- no VPC, no NAT, Aurora still
  scales to zero.
- Its **own dependency layer** (design §3.3): the scan layer is ~190 MB of a
  240 MB guard and must not carry a web framework, so this stack builds
  ``deps_layer(..., extras=("dashboard",))`` = base dependencies + ``fastapi`` +
  ``mangum``. Built in THIS stack for the same reason the Monitoring notifier
  builds its own: a LayerVersion ARN imported across stacks is a CloudFormation
  export, and every dependency bump would fail ``cdk deploy --all`` with
  "Cannot update export ... in use".
- An **API Gateway HTTP API** (not a REST API; design §13): ~$1 per million
  requests, and in Step 4.4 the Cognito **JWT authorizer** runs before the Lambda
  so there is no auth code in Python and unauthenticated calls cost nothing. A
  Lambda Function URL was rejected because it has no built-in JWT authorizer.
  The API is served on the ``$default`` stage (no stage prefix in paths), with
  access logging to its own log group and a conservative throttle.

Why ``GET /api/health`` is open and DB-free (design §6.1): it is the deploy
smoke test and returns only ``{"status": "ok", "version": <APP_VERSION>}``. It
must never touch the database or any AWS API -- an unauthenticated probe that
woke Aurora would let any scanner keep it awake (~$25/month). It is the ONLY
unauthenticated route; since Step 4.4 every other ``/api/*`` path is served by
``ANY /api/{proxy+}`` behind the Cognito JWT authorizer (API Gateway prefers
the more specific health route).

Why the role is so small (design §8): ``grant_data_api_access`` on the one
cluster + its credential secret, CloudWatch Logs write and -- since Step 4.3 --
read-only CloudWatch for the alarm strip and the P7 Lambda metrics
(``DescribeAlarms`` on the ``CryptoSignals-Monitoring-*`` alarms only,
``ListMetrics`` + ``GetMetricData`` on ``*`` because they have no resource-level
permissions). **No** SSM access (the dashboard needs no third-party keys), no
``lambda:*`` (it must not read the scan function's configuration -- the scan
function's name is discovered from its metrics), no S3 in Phase 1.

Why it is declared LAST and coupled only to DataStack (design §3.5 / anti-goal
§4.4): a broken dashboard deploy must never break or delay a pipeline deploy, so
this stack imports nothing from Compute / Scheduling / Monitoring -- the tests
pin that (``Fn::ImportValue`` names all start with ``Data:``).

Step 4.2 shipped the read-only query module and the first data routes
(``/api/overview``, ``/api/setups/open``, ``/api/signals...``) -- in the FastAPI
app only, deliberately unrouted here (no auth yet); the one change 4.2 made to
this stack is the JSON access-log format (``ACCESS_LOG_FORMAT``). Step 4.3
added the funnel / analytics / Critic / system-health / market routes, again
FastAPI-only; its only change here is the read-only CloudWatch IAM above. What
the next steps add here: 4.4 the Cognito user pool + JWT authorizer and the
``ANY /api/{proxy+}`` route that makes the data routes reachable; 4.5 the SPA
(private S3 bucket + CloudFront, which also fronts ``/api/*`` so SPA and API
share one origin); 4.7 the SSM parameter publishing the base URL for the
Telegram deep links (design §7).

Step 4.4 (done) added the one-user Cognito pool (admin-created only, LITE plan,
TOTP MFA required, RETAINED with deletion protection), its classic Hosted UI
domain, the public PKCE app client (callback = the local token helper
``scripts/dashboard_token.py``; 4.5 adds the CloudFront URLs), the
``HttpUserPoolAuthorizer`` and the ``ANY /api/{proxy+}`` route that uses it.
The app adds a second layer (``src/dashboard/api/auth.py``): under Lambda, a
non-health request without JWT claims is a 401. Runbook: docs/operations.md §8.7.
"""

from __future__ import annotations

import json
from typing import Any

import jsii
from aws_cdk import (
    ArnFormat,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from cdk_nag import NagSuppressions
from constructs import Construct
from lambda_assets import ARCHITECTURE, RUNTIME, code_asset, deps_layer

# FastAPI + Mangum cold start comfortably fits 512 MB; the scan Lambda's 1024 MB
# is sized for the agent pipeline, not a request handler.
API_MEMORY_MB = 512
# Below API Gateway's hard 30 s integration timeout (design §3.4): the Lambda
# must give up first so the client gets a structured 503 (db_resuming), not a
# gateway 504.
API_TIMEOUT_SECONDS = 25
# Stage-wide throttle: one operator polling every 60 s needs a fraction of this;
# it bounds what an unauthenticated scanner can cost before the authorizer lands.
THROTTLE_RATE_RPS = 10
THROTTLE_BURST = 20
# The one unauthenticated route (design §6.1).
HEALTH_PATH = "/api/health"
# Step 4.4: every other /api/* path, behind the Cognito JWT authorizer. API
# Gateway picks the most specific route, so GET /api/health keeps winning.
PROXY_PATH = "/api/{proxy+}"
# Cognito prefix domain = f"{COGNITO_DOMAIN_PREFIX_BASE}-{account id}" (globally
# unique per account; lowercase alnum + hyphen as Cognito requires).
COGNITO_DOMAIN_PREFIX_BASE = "crypto-signals-dash"
# The local PKCE token helper (scripts/dashboard_token.py) listens here; Step 4.5
# adds the CloudFront URLs for the SPA.
TOKEN_HELPER_CALLBACK = "http://localhost:8765/callback"
TOKEN_HELPER_LOGOUT = "http://localhost:8765/"
# MonitoringStack's CDK-generated alarm names start with its stack name; the
# dashboard may describe only those (mirrors `src.dashboard.api.cloudwatch.ALARM_PREFIX`).
MONITORING_ALARM_PREFIX = "CryptoSignals-Monitoring-"

# HTTP-API access-log line, one JSON object per request. CDK's default (Common
# Log) format uses the REST-era ``$context.resourcePath``, which an HTTP API
# logs as ``-`` (live-verified on Step 4.1: ``"GET - HTTP/1.1" 200 35``), so the
# path of a rejected or slow request was invisible. ``$context.routeKey`` /
# ``$context.path`` are the HTTP-API variables; ``integrationError`` is the only
# record of a Lambda-side failure when the integration (not the app) 5xx's, and
# from Step 4.4 these lines are also the only record of requests the JWT
# authorizer rejects before the Lambda runs. Values are $context variables, not
# user input, so the JSON cannot be broken by a request.
ACCESS_LOG_FORMAT = json.dumps(
    {
        "requestId": "$context.requestId",
        "ip": "$context.identity.sourceIp",
        "requestTime": "$context.requestTime",
        "method": "$context.httpMethod",
        "routeKey": "$context.routeKey",
        "path": "$context.path",
        "status": "$context.status",
        "responseLength": "$context.responseLength",
        "integrationLatency": "$context.integrationLatency",
        "integrationError": "$context.integrationErrorMessage",
    }
)


@jsii.implements(apigwv2.IAccessLogSettings)
class _AccessLogSettings:
    """Stage access-log settings: a destination + the HTTP-API JSON format above.

    ``HttpStage.access_log_settings`` takes the jsii *interface*
    ``IAccessLogSettings`` (``destination`` + optional ``format``), and
    aws-cdk-lib ships no concrete class for it in this version -- a bare
    ``LogGroupLogDestination`` or a dict fails at synth with
    ``Cannot read properties of undefined (reading 'bind')``. Same pattern as the
    ``ILocalBundling`` implementations in ``lambda_assets.py``.
    """

    def __init__(self, destination: apigwv2.IAccessLogDestination) -> None:
        self._destination = destination

    @property
    def destination(self) -> apigwv2.IAccessLogDestination:
        return self._destination

    @property
    def format(self) -> apigw.AccessLogFormat:
        return apigw.AccessLogFormat.custom(ACCESS_LOG_FORMAT)


class DashboardStack(Stack):
    """Dashboard API Lambda (zip + own layer) behind an HTTP API, Data-only coupling."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cluster: rds.DatabaseCluster,
        db_name: str = "signals",
        app_version: str = "local",
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if cluster.secret is None:  # pragma: no cover - DataStack always generates one
            raise ValueError("cluster must have a generated credentials secret for the Data API")

        # ---- Log groups (explicit, so no log-retention custom resource) ------
        self.log_group = logs.LogGroup(
            self,
            "DashboardApiLogs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # API Gateway access logs: who hit which route with what status -- the
        # only record of requests the authorizer (Step 4.4) rejects before the
        # Lambda runs.
        self.access_log_group = logs.LogGroup(
            self,
            "DashboardApiAccessLogs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---- Dependency layer: base + the [dashboard] extra (design §3.3) ----
        self.deps_layer = deps_layer(self, "DepsLayer", extras=("dashboard",))

        # ---- The dashboard API Lambda (zip code asset + layer) ---------------
        self.function = lambda_.Function(
            self,
            "DashboardApi",
            runtime=RUNTIME,
            architecture=ARCHITECTURE,
            handler="src.dashboard.api.app.handler",
            code=code_asset(),
            layers=[self.deps_layer],
            memory_size=API_MEMORY_MB,
            timeout=Duration.seconds(API_TIMEOUT_SECONDS),
            log_group=self.log_group,
            environment={
                "PERSISTENCE_BACKEND": "dataapi",
                "DB_CLUSTER_ARN": cluster.cluster_arn,
                "DB_SECRET_ARN": cluster.secret.secret_arn,
                "DB_NAME": db_name,
                # Reported by /api/health so the deploy smoke test shows which
                # commit is live (resolved in app.py: GITHUB_SHA / git / "local").
                "APP_VERSION": app_version,
                "LOG_LEVEL": "INFO",
            },
            description=(
                "Crypto-signals dashboard API (FastAPI via Mangum, read-only). Invoked by "
                "the dashboard HTTP API. Zip-packaged so the source is editable in the "
                "console; deps live in this stack's DepsLayer (base + [dashboard] extra)."
            ),
        )

        # ---- Least-privilege grant (design §8) -------------------------------
        # RDS Data API to the one cluster + read of its credentials secret.
        # Deliberately no SSM, no S3, no lambda:*.
        cluster.grant_data_api_access(self.function)
        # Step 4.3: read-only CloudWatch for the alarm strip + the P7 Lambda metrics.
        # DescribeAlarms is limited to MonitoringStack's alarms by name prefix ...
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadMonitoringAlarms",
                actions=["cloudwatch:DescribeAlarms"],
                resources=[
                    self.format_arn(
                        service="cloudwatch",
                        resource="alarm",
                        resource_name=f"{MONITORING_ALARM_PREFIX}*",
                        arn_format=ArnFormat.COLON_RESOURCE_NAME,
                    )
                ],
            )
        )
        # ... ListMetrics / GetMetricData support no resource-level permissions, so
        # they can only be granted on `*` (suppressed as AwsSolutions-IAM5 below).
        # The scan function's name is discovered from its metrics, not read from
        # the function (no lambda:GetFunctionConfiguration).
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadLambdaMetrics",
                actions=["cloudwatch:ListMetrics", "cloudwatch:GetMetricData"],
                resources=["*"],
            )
        )

        # ---- HTTP API + $default stage + the health route (design §3.2 / §13) -
        # create_default_stage=False so the stage is declared explicitly below
        # with access logging and throttling (the implicit default has neither).
        self.http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name="crypto-signals-dashboard",
            create_default_stage=False,
            description=(
                "Crypto-signals dashboard API. Step 4.1: GET /api/health only (open); "
                "Step 4.4 puts every other route behind a Cognito JWT authorizer."
            ),
        )
        self.stage = self.http_api.add_stage(
            "DefaultStage",
            stage_name="$default",
            auto_deploy=True,
            access_log_settings=_AccessLogSettings(
                apigwv2.LogGroupLogDestination(self.access_log_group)
            ),
            throttle=apigwv2.ThrottleSettings(
                rate_limit=THROTTLE_RATE_RPS,
                burst_limit=THROTTLE_BURST,
            ),
        )
        # One integration object shared by both routes: CDK binds it once per API,
        # so there is one AWS::ApiGatewayV2::Integration -- but it adds one Lambda
        # permission per route (2 here: health + proxy, each scoped to its route).
        self.integration = integrations.HttpLambdaIntegration("HealthIntegration", self.function)
        health_routes = self.http_api.add_routes(
            path=HEALTH_PATH,
            methods=[apigwv2.HttpMethod.GET],
            integration=self.integration,
        )
        # One method -> exactly one route; kept so the nag suppression below
        # targets the health route alone, never the API or the stack.
        self.health_route = health_routes[0]

        # ---- Cognito: one-user pool, Hosted UI domain, public PKCE client ----
        # (Step 4.4, design §8.) Admin-created users only; email sign-in; the
        # LITE tier (threat protection is a paid add-on); TOTP MFA REQUIRED, never
        # SMS (no SNS role / cost) -- operator decision 2026-10-21: optional MFA
        # had no enrolment path on the classic Hosted UI, while required MFA makes
        # the Hosted UI enrol an authenticator app at first sign-in. RETAINED + deletion protection (operator
        # decision 2026-10-21): an accidental stack delete must not wipe the
        # account -- a recreated stack gets a new empty pool and the orphan is free.
        self.user_pool = cognito.UserPool(
            self,
            "DashboardUserPool",
            user_pool_name="crypto-signals-dashboard",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            sign_in_case_sensitive=False,
            feature_plan=cognito.FeaturePlan.LITE,
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.user_pool_domain = self.user_pool.add_domain(
            "DashboardHostedUi",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"{COGNITO_DOMAIN_PREFIX_BASE}-{self.account}"
            ),
            managed_login_version=cognito.ManagedLoginVersion.CLASSIC_HOSTED_UI,
        )
        # Public client (a browser SPA / the local helper cannot keep a secret):
        # authorization code + PKCE only, SRP for the Hosted UI, no password flow.
        self.user_pool_client = self.user_pool.add_client(
            "DashboardSpaClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[TOKEN_HELPER_CALLBACK],
                logout_urls=[TOKEN_HELPER_LOGOUT],
            ),
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
        )

        # ---- JWT authorizer + the authenticated proxy route ------------------
        # API Gateway validates issuer, audience (client_id) and expiry before the
        # Lambda runs, so a rejected request costs nothing and touches no DB.
        self.jwt_authorizer = authorizers.HttpUserPoolAuthorizer(
            "DashboardJwtAuthorizer",
            self.user_pool,
            user_pool_clients=[self.user_pool_client],
        )
        proxy_routes = self.http_api.add_routes(
            path=PROXY_PATH,
            methods=[apigwv2.HttpMethod.ANY],
            integration=self.integration,
            authorizer=self.jwt_authorizer,
        )
        self.proxy_route = proxy_routes[0]

        # ---- Outputs ---------------------------------------------------------
        # No default stage, so there is no `.url`; api_endpoint is the base URL
        # and $default serves at its root.
        CfnOutput(self, "DashboardApiUrl", value=self.http_api.api_endpoint)
        CfnOutput(self, "DashboardFunctionName", value=self.function.function_name)
        CfnOutput(self, "DashboardUserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(
            self, "DashboardUserPoolClientId", value=self.user_pool_client.user_pool_client_id
        )
        # Hosted UI base URL: https://<prefix>.auth.<region>.amazoncognito.com
        CfnOutput(self, "DashboardCognitoDomain", value=self.user_pool_domain.base_url())
        CfnOutput(
            self,
            "DashboardJwtIssuer",
            value=f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool.user_pool_id}",
        )

        Tags.of(self).add("project", "crypto-signals")
        Tags.of(self).add("layer", "dashboard")

        self._apply_nag_suppressions()

    def _apply_nag_suppressions(self) -> None:
        """Justified cdk-nag suppressions for intentional Step 4.1 choices (design §8)."""
        NagSuppressions.add_resource_suppressions(
            self.function,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "The function uses the AWS-managed AWSLambdaBasicExecutionRole "
                        "for CloudWatch Logs write only -- the standard, minimal Lambda "
                        "logging policy. Its only other access is the Data-API grant below."
                    ),
                },
                # grant_data_api_access emits exact-ARN statements only (verified
                # Compliant in the synth nag report); the only wildcards are the two
                # read-only CloudWatch statements (Step 4.3), scoped by appliesTo so
                # any other future IAM5 finding still surfaces.
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Read-only CloudWatch for the dashboard (design §8): "
                        "cloudwatch:ListMetrics and cloudwatch:GetMetricData have no "
                        "resource-level permissions, so they can only be granted on '*'; "
                        "cloudwatch:DescribeAlarms is limited to the "
                        "CryptoSignals-Monitoring-* alarm name prefix. No write actions."
                    ),
                    "appliesTo": [
                        "Resource::*",
                        # Partition / region / account render as `<AWS::...>` tokens
                        # (which contain colons) or literals, depending on the env.
                        {
                            "regex": (
                                r"/^Resource::arn:(<[^>]+>|[^:]+):cloudwatch:(<[^>]+>|[^:]+):"
                                r"(<[^>]+>|[^:]+):alarm:CryptoSignals-Monitoring-\*$/g"
                            )
                        },
                    ],
                },
                {
                    "id": "AwsSolutions-L1",
                    "reason": (
                        "python3.11 is deliberate: the project toolchain, tests, mypy and the "
                        "layer's manylinux wheels are pinned to 3.11 (pyproject requires-python, "
                        "CI matrix). A runtime bump is its own step, re-validating the wheel set."
                    ),
                },
            ],
            apply_to_children=True,
        )
        # The health route is the ONE route that stays open; suppressed on the
        # route construct only (not the API, not the stack), so any route added
        # later without an authorizer is flagged at synth.
        NagSuppressions.add_resource_suppressions(
            self.health_route,
            [
                {
                    "id": "AwsSolutions-APIG4",
                    "reason": (
                        "GET /api/health is the deploy smoke test and is deliberately "
                        "unauthenticated; it touches no database or AWS API (design §6.1). "
                        "Every other route (ANY /api/{proxy+}) is behind the Cognito JWT "
                        "authorizer (Step 4.4)."
                    ),
                },
            ],
        )
        # The proxy route needs no APIG4 suppression -- the authorizer satisfies it.
        # Cognito: only the deliberate cost choices (LITE plan / no advanced
        # security), on the pool only. COG2 (MFA) is satisfied: TOTP is required.
        NagSuppressions.add_resource_suppressions(
            self.user_pool,
            [
                {
                    "id": "AwsSolutions-COG3",
                    "reason": "paid tier; single user (design §3.2 / §9)",
                },
                {
                    "id": "AwsSolutions-COG8",
                    "reason": (
                        "The Plus feature plan is Cognito's paid threat-protection tier "
                        "(billed per active user). This pool holds one operator account with "
                        "self-signup disabled; the design deliberately runs LITE with advanced "
                        "security off (design §3.2 / §9; same rationale as AwsSolutions-COG3)."
                    ),
                },
            ],
        )
