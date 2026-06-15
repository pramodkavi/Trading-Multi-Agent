"""Unit tests for src.config.secrets (runtime SSM Parameter Store hydration).

A fake SSM client returns canned parameter values; no AWS, no network.
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

ANTHROPIC_PARAM = "/crypto-signals/anthropic-api-key"
TELEGRAM_PARAM = "/crypto-signals/telegram-bot-token"


class FakeSsmClient:
    """Records Name lookups; returns canned parameter values."""

    def __init__(self, params: dict[str, str]) -> None:
        self._params = params
        self.calls: list[str] = []

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, Any]:  # noqa: N803 - boto3 kwargs
        assert WithDecryption is True  # SecureString values must be decrypted
        self.calls.append(Name)
        return {"Parameter": {"Name": Name, "Value": self._params[Name]}}


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_plain_string() -> None:
    client = FakeSsmClient({ANTHROPIC_PARAM: "sk-ant-xyz"})
    resolved = resolve_secrets(env={"ANTHROPIC_PARAM_NAME": ANTHROPIC_PARAM}, client=client)
    assert resolved == {"ANTHROPIC_API_KEY": "sk-ant-xyz"}
    assert client.calls == [ANTHROPIC_PARAM]


def test_anthropic_json_payload() -> None:
    client = FakeSsmClient({ANTHROPIC_PARAM: json.dumps({"api_key": "sk-ant-json"})})
    resolved = resolve_secrets(env={"ANTHROPIC_PARAM_NAME": ANTHROPIC_PARAM}, client=client)
    assert resolved == {"ANTHROPIC_API_KEY": "sk-ant-json"}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def test_telegram_json_carries_token_and_chat_id() -> None:
    client = FakeSsmClient({TELEGRAM_PARAM: json.dumps({"bot_token": "123:ABC", "chat_id": "999"})})
    resolved = resolve_secrets(env={"TELEGRAM_PARAM_NAME": TELEGRAM_PARAM}, client=client)
    assert resolved == {"TELEGRAM_BOT_TOKEN": "123:ABC", "TELEGRAM_CHAT_ID": "999"}


def test_telegram_plain_string_is_token_only() -> None:
    # A plain Telegram parameter is the bot token; it must NOT be read as chat id.
    client = FakeSsmClient({TELEGRAM_PARAM: "123:ABC"})
    resolved = resolve_secrets(env={"TELEGRAM_PARAM_NAME": TELEGRAM_PARAM}, client=client)
    assert resolved == {"TELEGRAM_BOT_TOKEN": "123:ABC"}


def test_telegram_numeric_chat_id_coerced_to_str() -> None:
    client = FakeSsmClient({TELEGRAM_PARAM: json.dumps({"bot_token": "t", "chat_id": 999})})
    resolved = resolve_secrets(env={"TELEGRAM_PARAM_NAME": TELEGRAM_PARAM}, client=client)
    assert resolved["TELEGRAM_CHAT_ID"] == "999"


# ---------------------------------------------------------------------------
# Both / skip / no-op
# ---------------------------------------------------------------------------


def test_both_secrets_resolved() -> None:
    client = FakeSsmClient(
        {
            ANTHROPIC_PARAM: "sk-ant-xyz",
            TELEGRAM_PARAM: json.dumps({"bot_token": "123:ABC", "chat_id": "999"}),
        }
    )
    resolved = resolve_secrets(
        env={"ANTHROPIC_PARAM_NAME": ANTHROPIC_PARAM, "TELEGRAM_PARAM_NAME": TELEGRAM_PARAM},
        client=client,
    )
    assert resolved == {
        "ANTHROPIC_API_KEY": "sk-ant-xyz",
        "TELEGRAM_BOT_TOKEN": "123:ABC",
        "TELEGRAM_CHAT_ID": "999",
    }


def test_skips_fetch_when_target_already_set() -> None:
    client = FakeSsmClient({ANTHROPIC_PARAM: "sk-ant-xyz"})
    resolved = resolve_secrets(
        env={"ANTHROPIC_PARAM_NAME": ANTHROPIC_PARAM, "ANTHROPIC_API_KEY": "already-set"},
        client=client,
    )
    assert resolved == {}
    assert client.calls == []  # no fetch when the value is already present


def test_telegram_chat_id_filled_when_only_token_present() -> None:
    client = FakeSsmClient({TELEGRAM_PARAM: json.dumps({"bot_token": "123:ABC", "chat_id": "999"})})
    resolved = resolve_secrets(
        env={"TELEGRAM_PARAM_NAME": TELEGRAM_PARAM, "TELEGRAM_BOT_TOKEN": "explicit"},
        client=client,
    )
    # Token already set -> kept; only the missing chat id is filled.
    assert resolved == {"TELEGRAM_CHAT_ID": "999"}


def test_no_param_names_is_noop_without_client() -> None:
    # client=None and no parameter names: must return early, never construct a boto3 client.
    assert resolve_secrets(env={}, client=None) == {}


def test_missing_value_raises() -> None:
    class EmptyClient:
        def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, Any]:  # noqa: N803
            return {"Parameter": {"Name": Name}}  # no "Value"

    with pytest.raises(ValueError, match="has no Value"):
        resolve_secrets(env={"ANTHROPIC_PARAM_NAME": ANTHROPIC_PARAM}, client=EmptyClient())


# ---------------------------------------------------------------------------
# hydrate_secrets_env (the os.environ side effect)
# ---------------------------------------------------------------------------


def test_hydrate_sets_env_with_setdefault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_PARAM_NAME", ANTHROPIC_PARAM)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = FakeSsmClient({ANTHROPIC_PARAM: "sk-ant-xyz"})
    try:
        hydrate_secrets_env(client=client)
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-xyz"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_hydrate_does_not_override_explicit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_PARAM_NAME", ANTHROPIC_PARAM)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "explicit-wins")
    client = FakeSsmClient({ANTHROPIC_PARAM: "sk-ant-xyz"})
    hydrate_secrets_env(client=client)
    assert os.environ["ANTHROPIC_API_KEY"] == "explicit-wins"
    assert client.calls == []
