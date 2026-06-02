"""Runtime secret hydration from AWS Secrets Manager (Step 1.18b).

In the cloud the scan Lambda is given the *ARNs* of its secrets as environment
variables (``ANTHROPIC_SECRET_ARN`` / ``TELEGRAM_SECRET_ARN``) -- never the
secret values, which are not baked into the template (NFR-3.1/3.2). At Lambda
start we fetch the values from Secrets Manager and place them in the plain
environment variables ``Settings`` already reads (``ANTHROPIC_API_KEY`` etc.),
so the rest of the app is unchanged whether it runs locally (values straight
from ``.env``) or in Lambda (values resolved here).

Two design points:

* **Pure core, thin side-effecting wrapper.** ``resolve_secrets`` reads an env
  mapping and returns the values to inject *without* mutating anything -- so it
  is testable with no global state. ``hydrate_secrets_env`` applies the result
  to ``os.environ`` with ``setdefault`` (an explicit env var always wins, so
  local/dev overrides are never clobbered).

* **Flexible payload shape.** Each secret may be a plain string *or* JSON, so
  the operator (Step 2.12) is not locked into one shape:
    - Anthropic secret: the API key as a plain string, or ``{"api_key": "..."}``.
    - Telegram secret: the bot token as a plain string, or
      ``{"bot_token": "...", "chat_id": "..."}`` -- the JSON form is preferred
      because it carries the (non-secret but required) chat id alongside the
      token, so no extra env var is needed.

Idempotent: if a target env var is already set (warm Lambda container, or local
dev), the corresponding secret is not fetched at all.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

ANTHROPIC_SECRET_ARN_ENV = "ANTHROPIC_SECRET_ARN"
TELEGRAM_SECRET_ARN_ENV = "TELEGRAM_SECRET_ARN"

# Keys we accept inside a JSON secret payload, in priority order.
_ANTHROPIC_KEY_FIELDS = ("api_key", "anthropic_api_key")
_TELEGRAM_TOKEN_FIELDS = ("bot_token", "token", "telegram_bot_token")
_TELEGRAM_CHAT_FIELDS = ("chat_id", "telegram_chat_id")


def _secretsmanager_client(region_name: str | None = None) -> Any:
    """Build a boto3 Secrets Manager client lazily (no AWS import until needed)."""
    import boto3

    return boto3.client("secretsmanager", region_name=region_name)


def _fetch_secret_string(client: Any, arn: str) -> str:
    """Return the SecretString for ``arn``; raises if the secret is binary-only."""
    response = client.get_secret_value(SecretId=arn)
    secret_string = response.get("SecretString")
    if secret_string is None:
        raise ValueError(f"secret {arn} has no SecretString (binary secrets are unsupported)")
    return str(secret_string)


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

    Used for the Telegram chat id: a plain-string Telegram secret is the bot
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
    """Resolve secret-derived config values from Secrets Manager.

    Reads the secret ARNs from ``env`` (defaults to ``os.environ``), fetches the
    values for any whose target env var is not already set, and returns a dict
    of ``{env_var: value}`` to inject. Does NOT mutate anything -- the caller
    decides how to apply the result.

    A secret is only fetched when needed, so with no ARNs (local dev) this is a
    no-op that never constructs a boto3 client.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    resolved: dict[str, str] = {}

    anthropic_arn = environ.get(ANTHROPIC_SECRET_ARN_ENV)
    telegram_arn = environ.get(TELEGRAM_SECRET_ARN_ENV)

    need_anthropic = bool(anthropic_arn) and "ANTHROPIC_API_KEY" not in environ
    need_telegram = bool(telegram_arn) and (
        "TELEGRAM_BOT_TOKEN" not in environ or "TELEGRAM_CHAT_ID" not in environ
    )
    if not (need_anthropic or need_telegram):
        return resolved

    client = client or _secretsmanager_client()

    if need_anthropic and anthropic_arn is not None:
        payload = _fetch_secret_string(client, anthropic_arn)
        api_key = _scalar(payload, _ANTHROPIC_KEY_FIELDS)
        if api_key is not None:
            resolved["ANTHROPIC_API_KEY"] = api_key

    if need_telegram and telegram_arn is not None:
        payload = _fetch_secret_string(client, telegram_arn)
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
    """Fetch secrets named by the ARN env vars and inject them into ``os.environ``.

    Call this once at Lambda start, BEFORE the first ``get_settings()`` (which is
    cached). Uses ``setdefault`` so an explicitly-set env var always wins.
    """
    resolved = resolve_secrets(client=client)
    for key, value in resolved.items():
        os.environ.setdefault(key, value)
    if resolved:
        # Log only the names that were hydrated -- never the values.
        logger.info("hydrated from Secrets Manager: %s", sorted(resolved))
