"""Shared SSM Parameter Store names for the third-party API credentials.

Step 2.12 moved the third-party API keys (Anthropic / Telegram / FRED / Twelve
Data) from AWS Secrets Manager to **SSM Parameter Store SecureString** parameters
-- they cost nothing (standard tier) versus ~$0.40/secret/month, which is a
meaningful slice of this project's tight monthly budget. The Aurora cluster's
own credential secret stays in Secrets Manager (it is generated and managed by
the RDS construct and required by the Data API; it is not one of these keys).

These names are the single source of truth shared by the stacks:

* ``ComputeStack`` passes the names the scan Lambda reads at runtime
  (``ANTHROPIC_PARAM_NAME`` / ``TELEGRAM_PARAM_NAME``) and grants ``GetParameter``
  on their ARNs.
* ``MonitoringStack`` passes ``TELEGRAM_PARAM_NAME`` to the alarm-notifier Lambda
  and grants it ``GetParameter`` on that one parameter.

**Important provisioning note:** CloudFormation (and therefore CDK) *cannot create
SecureString parameters*. The parameters are created out-of-band with a single
``aws ssm put-parameter --type SecureString`` per key (see ``docs/operations.md``);
the stacks only *reference* them for the IAM grants and the env wiring. They must
exist before the Lambda first runs.
"""

from __future__ import annotations

# All parameters live under one path prefix so a single IAM wildcard (if ever
# needed) or a console filter scopes cleanly to this system.
SSM_PARAM_PREFIX = "/crypto-signals"

ANTHROPIC_PARAM_NAME = f"{SSM_PARAM_PREFIX}/anthropic-api-key"
TELEGRAM_PARAM_NAME = f"{SSM_PARAM_PREFIX}/telegram-bot-token"
FRED_PARAM_NAME = f"{SSM_PARAM_PREFIX}/fred-api-key"
TWELVE_DATA_PARAM_NAME = f"{SSM_PARAM_PREFIX}/twelve-data-api-key"

# Env-var names the application's secret-hydration layer (src/config/secrets.py)
# reads to learn WHICH parameter to fetch. The Lambda is given the parameter
# *names* here; the secret *values* are fetched at runtime, never baked in.
ANTHROPIC_PARAM_ENV = "ANTHROPIC_PARAM_NAME"
TELEGRAM_PARAM_ENV = "TELEGRAM_PARAM_NAME"
