"""Unit tests for src.config.settings.

Isolation: every test constructs Settings with `_env_file=None` so the real
gitignored `.env` is never read. Required values are supplied either as direct
kwargs (highest precedence) or via monkeypatched environment variables,
depending on what the test exercises. This keeps the suite hermetic and
CI-safe (no `.env` exists in CI).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from src.config import DEFAULT_WATCHLIST, Settings, get_settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _required_kwargs(**overrides: Any) -> dict[str, Any]:
    """Minimal set of required fields for a valid Settings, plus _env_file=None."""
    base: dict[str, Any] = {
        "anthropic_api_key": "sk-ant-test",
        "telegram_bot_token": "123:ABC",
        "database_url": "postgresql://u:p@localhost:5432/db",
        "telegram_chat_id": "8300889332",
        "_env_file": None,  # isolate from the real .env
    }
    base.update(overrides)
    return base


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:ENV")
    monkeypatch.setenv("DATABASE_URL", "postgresql://e:e@localhost:5432/edb")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")


# ---------------------------------------------------------------------------
# Valid construction
# ---------------------------------------------------------------------------


class TestValidConstruction:
    def test_minimal_required_fields(self) -> None:
        s = Settings(**_required_kwargs())
        assert s.telegram_chat_id == "8300889332"
        assert s.scan_symbols == DEFAULT_WATCHLIST
        assert s.log_level == "INFO"

    def test_secrets_are_secretstr(self) -> None:
        s = Settings(**_required_kwargs())
        assert isinstance(s.anthropic_api_key, SecretStr)
        assert isinstance(s.telegram_bot_token, SecretStr)
        assert isinstance(s.database_url, SecretStr)

    def test_secret_value_retrievable(self) -> None:
        s = Settings(**_required_kwargs(anthropic_api_key="sk-ant-secret"))
        assert s.anthropic_api_key.get_secret_value() == "sk-ant-secret"


# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------


class TestSecretMasking:
    def test_repr_masks_all_secrets(self) -> None:
        s = Settings(**_required_kwargs(anthropic_api_key="sk-ant-supersecret"))
        rendered = repr(s)
        assert "sk-ant-supersecret" not in rendered
        assert "**********" in rendered

    def test_str_masks_secrets(self) -> None:
        s = Settings(**_required_kwargs(database_url="postgresql://u:hunter2@h/db"))
        assert "hunter2" not in str(s)

    def test_model_dump_keeps_secretstr_wrapper(self) -> None:
        # model_dump() returns the SecretStr object, not the raw value, unless
        # we opt in. This is the default safe behaviour.
        s = Settings(**_required_kwargs())
        dumped = s.model_dump()
        assert isinstance(dumped["anthropic_api_key"], SecretStr)


# ---------------------------------------------------------------------------
# Required-field enforcement
# ---------------------------------------------------------------------------


class TestRequiredFields:
    def test_missing_anthropic_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clear env so the field has no source, and isolate from .env.
        for var in (
            "ANTHROPIC_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            "DATABASE_URL",
            "TELEGRAM_CHAT_ID",
        ):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ValidationError) as exc_info:
            Settings(
                telegram_bot_token="x",
                database_url="y",
                telegram_chat_id="z",
                _env_file=None,
            )
        assert "anthropic_api_key" in str(exc_info.value)

    def test_missing_chat_id_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "ANTHROPIC_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            "DATABASE_URL",
            "TELEGRAM_CHAT_ID",
        ):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ValidationError):
            Settings(
                anthropic_api_key="x",
                telegram_bot_token="y",
                database_url="z",
                _env_file=None,
            )

    def test_blank_chat_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(**_required_kwargs(telegram_chat_id=""))


# ---------------------------------------------------------------------------
# scan_symbols parsing
# ---------------------------------------------------------------------------


class TestScanSymbols:
    def test_default_is_spec_watchlist(self) -> None:
        s = Settings(**_required_kwargs())
        assert s.scan_symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

    def test_csv_string_from_env_split(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_required_env(monkeypatch)
        monkeypatch.setenv("SCAN_SYMBOLS", "btcusdt, ethusdt ,solusdt")
        s = Settings(_env_file=None)
        assert s.scan_symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    def test_list_passed_directly_untouched(self) -> None:
        s = Settings(**_required_kwargs(scan_symbols=["XRPUSDT", "ADAUSDT"]))
        assert s.scan_symbols == ["XRPUSDT", "ADAUSDT"]

    def test_empty_env_string_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_required_env(monkeypatch)
        monkeypatch.setenv("SCAN_SYMBOLS", "   ")
        with pytest.raises(ValidationError, match="at least one symbol"):
            Settings(_env_file=None)

    def test_duplicate_symbols_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicates"):
            Settings(**_required_kwargs(scan_symbols=["BTCUSDT", "BTCUSDT"]))

    def test_single_symbol_csv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_required_env(monkeypatch)
        monkeypatch.setenv("SCAN_SYMBOLS", "BTCUSDT")
        s = Settings(_env_file=None)
        assert s.scan_symbols == ["BTCUSDT"]


# ---------------------------------------------------------------------------
# log_level
# ---------------------------------------------------------------------------


class TestLogLevel:
    def test_default_info(self) -> None:
        s = Settings(**_required_kwargs())
        assert s.log_level == "INFO"

    def test_lowercase_normalised(self) -> None:
        s = Settings(**_required_kwargs(log_level="debug"))
        assert s.log_level == "DEBUG"

    def test_invalid_level_rejected(self) -> None:
        with pytest.raises(ValidationError, match="log_level must be one of"):
            Settings(**_required_kwargs(log_level="VERBOSE"))

    def test_all_valid_levels_accepted(self) -> None:
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            s = Settings(**_required_kwargs(log_level=level))
            assert s.log_level == level


# ---------------------------------------------------------------------------
# Environment-variable loading & precedence
# ---------------------------------------------------------------------------


class TestEnvLoading:
    def test_reads_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_required_env(monkeypatch)
        s = Settings(_env_file=None)
        assert s.anthropic_api_key.get_secret_value() == "sk-ant-env"
        assert s.telegram_chat_id == "111"

    def test_case_insensitive_env_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # case_sensitive=False means lowercase env vars also work.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("anthropic_api_key", "sk-ant-lower")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:1")
        monkeypatch.setenv("DATABASE_URL", "postgresql://a:b@h/d")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
        s = Settings(_env_file=None)
        assert s.anthropic_api_key.get_secret_value() == "sk-ant-lower"

    def test_init_kwarg_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_required_env(monkeypatch)
        s = Settings(telegram_chat_id="override", _env_file=None)
        assert s.telegram_chat_id == "override"

    def test_unrelated_env_vars_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_required_env(monkeypatch)
        monkeypatch.setenv("SOME_UNRELATED_VAR", "noise")
        # extra='ignore' means this does not raise.
        s = Settings(_env_file=None)
        assert s.telegram_chat_id == "111"


# ---------------------------------------------------------------------------
# get_settings caching
# ---------------------------------------------------------------------------


class TestGetSettingsCache:
    def test_returns_same_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_required_env(monkeypatch)
        get_settings.cache_clear()
        try:
            a = get_settings()
            b = get_settings()
            assert a is b
        finally:
            get_settings.cache_clear()

    def test_cache_clear_rereads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_required_env(monkeypatch)
        get_settings.cache_clear()
        try:
            a = get_settings()
            assert a.telegram_chat_id == "111"
            monkeypatch.setenv("TELEGRAM_CHAT_ID", "222")
            # Without clearing, the cached instance is returned unchanged.
            assert get_settings().telegram_chat_id == "111"
            get_settings.cache_clear()
            assert get_settings().telegram_chat_id == "222"
        finally:
            get_settings.cache_clear()
