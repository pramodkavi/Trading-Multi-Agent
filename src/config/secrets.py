"""Runtime secret hydration from AWS SSM Parameter Store (Step 2.12).

In the cloud the scan / notifier Lambdas are given the *names* of their SSM
SecureString parameters as environment variables (``ANTHROPIC_PARAM_NAME`` /
``TELEGRAM_PARAM_NAME``) -- never the secret values, which are not baked into the
template (NFR-3.1/3.2). At start we fetch the values with
``ssm:GetParameter(WithDecryption=True)`` and place them in the plain environment
variables ``Settings`` already reads (``ANTHROPIC_API_KEY`` etc.), so the rest of
the app is unchanged whether it runs locally (values straight from ``.env``) or in
Lambda (values resolved here).

Step 2.12 moved these keys off AWS Secrets Manager (~$0.40/secret/month) onto SSM
Parameter Store SecureString (free standard tier). The Aurora DB credential is a
separate Secrets Manager secret reached via the Data API and is unaffected.

Two design points:

* **Pure core, thin side-effecting wrapper.** ``resolve_secrets`` reads an env
  mapping and returns the values to inject *without* mutating anything -- so it
  is testable with no global state. ``hydrate_secrets_env`` applies the result
  to ``os.environ`` with ``setdefault`` (an explicit env var always wins, so
  local/dev overrides are never clobbered).

* **Flexible payload shape.** Each parameter may be a plain string *or* JSON, so
  the operator (Step 2.12) is not locked into one shape:
    - Anthropic parameter: the API key as a plain string, or ``{"api_key": "..."}``.
    - Telegram parameter: the bot token as a plain string, or
      ``{"bot_token": "...", "chat_id": "..."}`` -- the JSON form is preferred
      because it carries the (non-secret but required) chat id alongside the
      token, so no extra env var is needed.

Idempotent: if a target env var is already set (warm Lambda container, or local
dev), the corresponding parameter is not fetched at all.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

ANTHROPIC_PARAM_NAME_ENV = "ANTHROPIC_PARAM_NAME"
TELEGRAM_PARAM_NAME_ENV = "TELEGRAM_PARAM_NAME"

# Keys we accept inside a JSON parameter payload, in priority order.
_ANTHROPIC_KEY_FIELDS = ("api_key", "anthropic_api_key")
_TELEGRAM_TOKEN_FIELDS = ("bot_token", "token", "telegram_bot_token")
_TELEGRAM_CHAT_FIELDS = ("chat_id", "telegram_chat_id")


def _ssm_client(region_name: str | None = None) -> Any:
    """Build a boto3 SSM client lazily (no AWS import until needed)."""
    import boto3

    return boto3.client("ssm", region_name=region_name)


def _fetch_parameter_value(client: Any, name: str) -> str:
    """Return the decrypted value of SSM parameter ``name``."""
    response = client.get_parameter(Name=name, WithDecryption=True)
    parameter = response.get("Parameter") or {}
    value = parameter.get("Value")
    if value is None:
        raise ValueError(f"SSM parameter {name} has no Value")
    return str(value)


def _maybe_json(payload: str) -> Any:
    """Parse ``payload`` as JSON if it looks like an object/array; else return the str."""
    stripped = payload.strip()
    if stripped[:1] in "{[":
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return payload
    return payload


def _scalar(payload: str, json_fields: tuple[str, ...]) -> str | None:
    """Extract a scalar value: the whole string if plain, else a JSON field."""
    parsed = _maybe_json(payload)
    if isinstance(parsed, dict):
        for field in json_fields:
            value = parsed.get(field)
            if value is not None:
                return str(value)
        return None
    return payload.strip() or None


def _json_field(payload: str, json_fields: tuple[str, ...]) -> str | None:
    """Extract a field only when the payload is JSON; a plain string yields None.

    Used for the Telegram chat id: a plain-string Telegram parameter is the bot
    token, which must never be mistaken for the chat id.
    """
    parsed = _maybe_json(payload)
    if isinstance(parsed, dict):
        for field in json_fields:
            value = parsed.get(field)
            if value is not None:
                return str(value)
    return None


def resolve_secrets(
    *,
    env: Mapping[str, str] | None = None,
    client: Any = None,
) -> dict[str, str]:
    """Resolve secret-derived config values from SSM Parameter Store.

    Reads the parameter names from ``env`` (defaults to ``os.environ``), fetches
    the values for any whose target env var is not already set, and returns a
    dict of ``{env_var: value}`` to inject. Does NOT mutate anything -- the
    caller decides how to apply the result.

    A parameter is only fetched when needed, so with no names configured (local
    dev) this is a no-op that never constructs a boto3 client.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    resolved: dict[str, str] = {}

    anthropic_name = environ.get(ANTHROPIC_PARAM_NAME_ENV)
    telegram_name = environ.get(TELEGRAM_PARAM_NAME_ENV)

    need_anthropic = bool(anthropic_name) and "ANTHROPIC_API_KEY" not in environ
    need_telegram = bool(telegram_name) and (
        "TELEGRAM_BOT_TOKEN" not in environ or "TELEGRAM_CHAT_ID" not in environ
    )
    if not (need_anthropic or need_telegram):
        return resolved

    client = client or _ssm_client()

    if need_anthropic and anthropic_name is not None:
        payload = _fetch_parameter_value(client, anthropic_name)
        api_key = _scalar(payload, _ANTHROPIC_KEY_FIELDS)
        if api_key is not None:
            resolved["ANTHROPIC_API_KEY"] = api_key

    if need_telegram and telegram_name is not None:
        payload = _fetch_parameter_value(client, telegram_name)
        if "TELEGRAM_BOT_TOKEN" not in environ:
            token = _scalar(payload, _TELEGRAM_TOKEN_FIELDS)
            if token is not None:
                resolved["TELEGRAM_BOT_TOKEN"] = token
        if "TELEGRAM_CHAT_ID" not in environ:
            chat_id = _json_field(payload, _TELEGRAM_CHAT_FIELDS)
            if chat_id is not None:
                resolved["TELEGRAM_CHAT_ID"] = chat_id

    return resolved


def hydrate_secrets_env(*, client: Any = None) -> None:
    """Fetch parameters named by the *_PARAM_NAME env vars and inject into ``os.environ``.

    Call this once at Lambda start, BEFORE the first ``get_settings()`` (which is
    cached). Uses ``setdefault`` so an explicitly-set env var always wins.
    """
    resolved = resolve_secrets(client=client)
    for key, value in resolved.items():
        os.environ.setdefault(key, value)
    if resolved:
        # Log only the names that were hydrated -- never the values.
        logger.info("hydrated from SSM Parameter Store: %s", sorted(resolved))
