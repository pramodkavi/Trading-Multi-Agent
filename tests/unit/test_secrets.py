"""Unit tests for src.config.secrets (runtime Secrets Manager hydration).

A fake Secrets Manager client returns canned payloads; no AWS, no network.
``resolve_secrets`` is pure (takes an env mapping, returns a dict) so most tests
need not touch the real environment. The one test of ``hydrate_secrets_env``
cleans up the os.environ keys it injects.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from src.config.secrets import (
    hydrate_secrets_env,
    resolve_secrets,
)

ANTHROPIC_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:crypto-signals/anthropic"
TELEGRAM_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:crypto-signals/telegram"


class FakeSecretsClient:
    """Records SecretId lookups; returns canned SecretString payloads."""

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:  # noqa: N803 - boto3 kwarg
        self.calls.append(SecretId)
        return {"SecretString": self._secrets[SecretId]}


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_plain_string() -> None:
    client = FakeSecretsClient({ANTHROPIC_ARN: "sk-ant-xyz"})
    resolved = resolve_secrets(env={"ANTHROPIC_SECRET_ARN": ANTHROPIC_ARN}, client=client)
    assert resolved == {"ANTHROPIC_API_KEY": "sk-ant-xyz"}
    assert client.calls == [ANTHROPIC_ARN]


def test_anthropic_json_payload() -> None:
    client = FakeSecretsClient({ANTHROPIC_ARN: json.dumps({"api_key": "sk-ant-json"})})
    resolved = resolve_secrets(env={"ANTHROPIC_SECRET_ARN": ANTHROPIC_ARN}, client=client)
    assert resolved == {"ANTHROPIC_API_KEY": "sk-ant-json"}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def test_telegram_json_carries_token_and_chat_id() -> None:
    client = FakeSecretsClient(
        {TELEGRAM_ARN: json.dumps({"bot_token": "123:ABC", "chat_id": "999"})}
    )
    resolved = resolve_secrets(env={"TELEGRAM_SECRET_ARN": TELEGRAM_ARN}, client=client)
    assert resolved == {"TELEGRAM_BOT_TOKEN": "123:ABC", "TELEGRAM_CHAT_ID": "999"}


def test_telegram_plain_string_is_token_only() -> None:
    # A plain Telegram secret is the bot token; it must NOT be read as chat id.
    client = FakeSecretsClient({TELEGRAM_ARN: "123:ABC"})
    resolved = resolve_secrets(env={"TELEGRAM_SECRET_ARN": TELEGRAM_ARN}, client=client)
    assert resolved == {"TELEGRAM_BOT_TOKEN": "123:ABC"}


def test_telegram_numeric_chat_id_coerced_to_str() -> None:
    client = FakeSecretsClient({TELEGRAM_ARN: json.dumps({"bot_token": "t", "chat_id": 999})})
    resolved = resolve_secrets(env={"TELEGRAM_SECRET_ARN": TELEGRAM_ARN}, client=client)
    assert resolved["TELEGRAM_CHAT_ID"] == "999"


# ---------------------------------------------------------------------------
# Both / skip / no-op
# ---------------------------------------------------------------------------


def test_both_secrets_resolved() -> None:
    client = FakeSecretsClient(
        {
            ANTHROPIC_ARN: "sk-ant-xyz",
            TELEGRAM_ARN: json.dumps({"bot_token": "123:ABC", "chat_id": "999"}),
        }
    )
    resolved = resolve_secrets(
        env={"ANTHROPIC_SECRET_ARN": ANTHROPIC_ARN, "TELEGRAM_SECRET_ARN": TELEGRAM_ARN},
        client=client,
    )
    assert resolved == {
        "ANTHROPIC_API_KEY": "sk-ant-xyz",
        "TELEGRAM_BOT_TOKEN": "123:ABC",
        "TELEGRAM_CHAT_ID": "999",
    }


def test_skips_fetch_when_target_already_set() -> None:
    client = FakeSecretsClient({ANTHROPIC_ARN: "sk-ant-xyz"})
    resolved = resolve_secrets(
        env={"ANTHROPIC_SECRET_ARN": ANTHROPIC_ARN, "ANTHROPIC_API_KEY": "already-set"},
        client=client,
    )
    assert resolved == {}
    assert client.calls == []  # no fetch when the value is already present


def test_telegram_chat_id_filled_when_only_token_present() -> None:
    client = FakeSecretsClient(
        {TELEGRAM_ARN: json.dumps({"bot_token": "123:ABC", "chat_id": "999"})}
    )
    resolved = resolve_secrets(
        env={"TELEGRAM_SECRET_ARN": TELEGRAM_ARN, "TELEGRAM_BOT_TOKEN": "explicit"},
        client=client,
    )
    # Token already set -> kept; only the missing chat id is filled.
    assert resolved == {"TELEGRAM_CHAT_ID": "999"}


def test_no_arns_is_noop_without_client() -> None:
    # client=None and no ARNs: must return early, never construct a boto3 client.
    assert resolve_secrets(env={}, client=None) == {}


def test_binary_secret_raises() -> None:
    class BinaryClient:
        def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:  # noqa: N803
            return {"SecretBinary": b"\x00"}

    with pytest.raises(ValueError, match="no SecretString"):
        resolve_secrets(env={"ANTHROPIC_SECRET_ARN": ANTHROPIC_ARN}, client=BinaryClient())


# ---------------------------------------------------------------------------
# hydrate_secrets_env (the os.environ side effect)
# ---------------------------------------------------------------------------


def test_hydrate_sets_env_with_setdefault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_SECRET_ARN", ANTHROPIC_ARN)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = FakeSecretsClient({ANTHROPIC_ARN: "sk-ant-xyz"})
    try:
        hydrate_secrets_env(client=client)
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-xyz"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_hydrate_does_not_override_explicit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_SECRET_ARN", ANTHROPIC_ARN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "explicit-wins")
    client = FakeSecretsClient({ANTHROPIC_ARN: "sk-ant-xyz"})
    hydrate_secrets_env(client=client)
    assert os.environ["ANTHROPIC_API_KEY"] == "explicit-wins"
    assert client.calls == []
