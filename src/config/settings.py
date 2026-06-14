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
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
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
    database_url: SecretStr | None = Field(
        default=None,
        description="Postgres DSN, e.g. postgresql://user:pw@host:5432/db. "
        "Secret because it embeds credentials. Required only when "
        "persistence_backend='asyncpg' (local dev); the cloud Lambda uses the "
        "Data API and leaves this unset.",
    )

    # ---- Macro provider keys (optional; Skeptic, Step 2.5) ----------------
    # Optional because the Skeptic degrades gracefully when macro is unavailable
    # (FR-4.3): with neither key set, no macro provider is built and the Skeptic
    # returns NoMacroData rather than failing startup. FRED is fully free; Twelve
    # Data uses the free tier with SPY/VIXY ETF proxies (Step 2.3 cost decision).
    fred_api_key: SecretStr | None = Field(
        default=None,
        description="FRED (St. Louis Fed) API key for DXY proxy / US10Y / Fed Funds. "
        "Free key from fredaccount.stlouisfed.org/apikeys. None disables FRED.",
    )
    twelve_data_api_key: SecretStr | None = Field(
        default=None,
        description="Twelve Data API key for the S&P 500 / volatility ETF proxies "
        "(SPY / VIXY on the free tier). None disables Twelve Data.",
    )

    # ---- Required non-secret ---------------------------------------------
    telegram_chat_id: str = Field(
        min_length=1,
        description="Target Telegram chat id (numeric) or '@channel' handle.",
    )

    # ---- Persistence backend selection (Step 1.17) -----------------------
    persistence_backend: Literal["asyncpg", "dataapi"] = Field(
        default="asyncpg",
        description="Which SignalStore backend to build: 'asyncpg' (local "
        "Postgres socket) or 'dataapi' (Aurora RDS Data API, cloud Lambda).",
    )
    db_cluster_arn: str | None = Field(
        default=None,
        description="Aurora cluster ARN for the Data API. Required when "
        "persistence_backend='dataapi'.",
    )
    db_secret_arn: str | None = Field(
        default=None,
        description="Secrets Manager ARN of the Aurora credentials secret used "
        "by the Data API. Required when persistence_backend='dataapi'.",
    )
    db_name: str = Field(
        default="signals",
        min_length=1,
        description="Logical database name targeted by the Data API.",
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

    @model_validator(mode="after")
    def _backend_requirements(self) -> Settings:
        """Enforce the fields the selected persistence backend depends on.

        Done as a cross-field check (rather than making every field required)
        because the two deployment targets need different things: the local
        asyncpg path needs a DSN, while the cloud Data API path needs the
        cluster + secret ARNs and never sees a DSN. Failing here -- at startup
        -- beats a confusing connection error on the first scan.
        """
        if self.persistence_backend == "asyncpg":
            if self.database_url is None:
                raise ValueError("persistence_backend='asyncpg' requires database_url")
        elif self.persistence_backend == "dataapi" and (
            self.db_cluster_arn is None or self.db_secret_arn is None
        ):
            raise ValueError(
                "persistence_backend='dataapi' requires db_cluster_arn and db_secret_arn"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings singleton.

    Cached so the `.env` is parsed and validated exactly once. Call
    `get_settings.cache_clear()` in tests that need to re-read the environment.
    """
    return Settings()  # type: ignore[call-arg]  # values come from env / .env
