"""Slice 4 Step 4.2 — `DashboardSettings` (design §3.3): small, never loads the agent `Settings`.

Isolation: every test constructs with `_env_file=None` so the real gitignored `.env` is
never read, and clears the env vars the model reads so the host environment cannot leak in.
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from src.config.settings import DEFAULT_WATCHLIST
from src.dashboard.api.settings import DashboardSettings, get_dashboard_settings

_ENV_KEYS = (
    "DB_CLUSTER_ARN",
    "DB_SECRET_ARN",
    "DB_NAME",
    "AWS_REGION",
    "SCAN_SYMBOLS",
    "LOG_LEVEL",
    "LANGFUSE_BASE_URL",
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DATABASE_URL",
)

_CLUSTER = "arn:aws:rds:ap-south-1:123456789012:cluster:signals"
_SECRET = "arn:aws:secretsmanager:ap-south-1:123456789012:secret:signals-db-abc123"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    get_dashboard_settings.cache_clear()


def _set_arns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_CLUSTER_ARN", _CLUSTER)
    monkeypatch.setenv("DB_SECRET_ARN", _SECRET)


def test_settings_parse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_arns(monkeypatch)
    monkeypatch.setenv("SCAN_SYMBOLS", "btcusdt, ethusdt")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf.example")
    s = DashboardSettings(_env_file=None)
    assert s.db_cluster_arn == _CLUSTER
    assert s.db_secret_arn == _SECRET
    assert s.db_name == "signals"
    assert s.aws_region is None
    assert s.scan_symbols == ["BTCUSDT", "ETHUSDT"]
    assert s.langfuse_base_url == "https://lf.example"


def test_settings_default_watchlist_and_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_arns(monkeypatch)
    s = DashboardSettings(_env_file=None)
    assert s.scan_symbols == DEFAULT_WATCHLIST
    assert s.scan_symbols is not DEFAULT_WATCHLIST  # own copy, never aliases the constant
    assert s.log_level == "INFO"
    assert s.langfuse_base_url is None


def test_settings_log_level_normalised_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_arns(monkeypatch)
    monkeypatch.setenv("LOG_LEVEL", " debug ")
    assert DashboardSettings(_env_file=None).log_level == "DEBUG"
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
    with pytest.raises(ValidationError, match="log_level"):
        DashboardSettings(_env_file=None)


def test_settings_region_and_db_name_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_arns(monkeypatch)
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    monkeypatch.setenv("DB_NAME", "signals_test")
    s = DashboardSettings(_env_file=None)
    assert s.aws_region == "ap-south-1"
    assert s.db_name == "signals_test"


def test_settings_missing_arns_is_a_validation_error() -> None:
    with pytest.raises(ValidationError) as excinfo:
        DashboardSettings(_env_file=None)
    missing = {err["loc"][0] for err in excinfo.value.errors()}
    assert {"db_cluster_arn", "db_secret_arn"} <= missing


def test_settings_empty_symbols_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_arns(monkeypatch)
    monkeypatch.setenv("SCAN_SYMBOLS", " , ")
    with pytest.raises(ValidationError, match="at least one symbol"):
        DashboardSettings(_env_file=None)


def test_settings_never_requires_agent_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    # Design §3.3: the dashboard Lambda has no Anthropic / Telegram secrets at all.
    _set_arns(monkeypatch)
    assert "ANTHROPIC_API_KEY" not in os.environ
    s = DashboardSettings(_env_file=None)
    assert not hasattr(s, "anthropic_api_key")
    assert not hasattr(s, "telegram_bot_token")
    assert not hasattr(s, "telegram_chat_id")


def test_get_dashboard_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_arns(monkeypatch)
    first = get_dashboard_settings()
    assert first is get_dashboard_settings()
    get_dashboard_settings.cache_clear()
    assert get_dashboard_settings() is not first
