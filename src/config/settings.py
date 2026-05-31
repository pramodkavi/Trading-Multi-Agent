"""Application configuration via Pydantic Settings.

Per SPEC §4 Step 1.11 and §3.3.3 NFR-3.1, all configuration is loaded from
environment variables. Locally those come from a gitignored `.env` file; in
production they are injected from AWS Secrets Manager into the Fargate task
environment (Step 1.16+). The code path is identical -- only the source of
the env vars differs.

Security posture:
- Secrets (Anthropic API key, Telegram bot token, database URL) are typed as
  `SecretStr`. They render as '**********' in reprs, logs, and tracebacks;
  the real value is only accessible via `.get_secret_value()`. This is the
  single most effective guard against the most common leak vector: a stray
  `print(settings)` or an exception that dumps the whole config object.
- `get_settings()` is `lru_cache`d so the `.env` is read and validated exactly
  once per process. Every caller shares one immutable instance.

Field policy (per the Step 1.11 design decision):
- Secrets + telegram_chat_id are REQUIRED -- a missing one raises at startup
  with a clear message rather than surfacing as a cryptic 401 mid-scan.
- scan_symbols and log_level have sensible defaults (SPEC watchlist / INFO).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# SPEC Appendix B / §11: default Slice 1-2 watchlist.
DEFAULT_WATCHLIST: list[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class Settings(BaseSettings):
    """Typed application configuration loaded from environment / .env.

    Construct via `get_settings()` rather than `Settings()` directly so the
    cached singleton is reused. Direct construction is allowed (and used in
    tests) to build throwaway instances with overridden values.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # tolerate unrelated env vars (PATH, etc.)
    )

    # ---- Secrets (required) ----------------------------------------------
    anthropic_api_key: SecretStr = Field(
        description="Anthropic API key (Claude Sonnet 4.5). From Secrets Manager in prod.",
    )
    telegram_bot_token: SecretStr = Field(
        description="Telegram bot token from @BotFather. Treated as a secret.",
    )
    database_url: SecretStr = Field(
        description="Postgres DSN, e.g. postgresql://user:pw@host:5432/db. "
        "Secret because it embeds credentials.",
    )

    # ---- Required non-secret ---------------------------------------------
    telegram_chat_id: str = Field(
        min_length=1,
        description="Target Telegram chat id (numeric) or '@channel' handle.",
    )

    # ---- Operational (defaulted) -----------------------------------------
    # NoDecode disables pydantic-settings' default JSON pre-parse for this
    # complex (list) field, so the raw env string reaches _split_csv_symbols
    # below. Without it, pydantic-settings tries json.loads('BTCUSDT,...')
    # and fails before our validator runs.
    scan_symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_WATCHLIST),
        description="Watchlist symbols. Env var SCAN_SYMBOLS is a comma-separated "
        "string (e.g. 'BTCUSDT,ETHUSDT'); parsed into a list here.",
    )
    log_level: str = Field(
        default="INFO",
        description="Root logging level: DEBUG / INFO / WARNING / ERROR / CRITICAL.",
    )

    # ---- Validators -------------------------------------------------------

    @field_validator("scan_symbols", mode="before")
    @classmethod
    def _split_csv_symbols(cls, v: object) -> object:
        """Accept a comma-separated string from the env var and split to a list.

        Env vars are always strings, so SCAN_SYMBOLS='BTCUSDT,ETHUSDT' arrives
        as one string. We split on commas, strip whitespace, uppercase, and
        drop empties. A genuine list (from tests / programmatic construction)
        passes through untouched.
        """
        if isinstance(v, str):
            return [part.strip().upper() for part in v.split(",") if part.strip()]
        return v

    @field_validator("scan_symbols")
    @classmethod
    def _symbols_non_empty_unique(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("scan_symbols must contain at least one symbol")
        if len(set(v)) != len(v):
            raise ValueError("scan_symbols must not contain duplicates")
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, v: object) -> object:
        if isinstance(v, str):
            upper = v.strip().upper()
            if upper not in _VALID_LOG_LEVELS:
                raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got {v!r}")
            return upper
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings singleton.

    Cached so the `.env` is parsed and validated exactly once. Call
    `get_settings.cache_clear()` in tests that need to re-read the environment.
    """
    return Settings()  # type: ignore[call-arg]  # values come from env / .env
