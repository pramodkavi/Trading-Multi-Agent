"""Dashboard Lambda configuration (design §3.3).

The agent `src.config.settings.Settings` *requires* the Anthropic and Telegram
secrets, which the dashboard Lambda neither has nor needs. The dashboard gets
this small model instead and never loads the agent `Settings`: it reads the
Aurora Data API ARNs, the database name, the watchlist (same `SCAN_SYMBOLS` CSV
the scan Lambda reads, parsed by the shared `parse_symbol_csv`), the log level
and an optional Langfuse base URL for trace deep links.

`get_dashboard_settings()` is `lru_cache`d, so the environment is read and
validated once per container -- on first use, not at import. Missing ARNs
therefore surface as a `ValidationError` from the first request that needs the
store, never from importing the handler module.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from src.config.settings import DEFAULT_WATCHLIST, parse_symbol_csv

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class DashboardSettings(BaseSettings):
    """Typed dashboard configuration loaded from environment / .env.

    Construct via `get_dashboard_settings()` so the cached instance is reused;
    direct construction (with `_env_file=None`) is for tests.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # tolerate unrelated env vars (PATH, APP_VERSION, ...)
    )

    # ---- Aurora Data API (required) --------------------------------------
    db_cluster_arn: str = Field(
        min_length=1,
        description="Aurora cluster ARN for the RDS Data API (injected by DashboardStack).",
    )
    db_secret_arn: str = Field(
        min_length=1,
        description="Secrets Manager ARN of the Aurora credentials secret used by the Data API.",
    )
    db_name: str = Field(
        default="signals",
        min_length=1,
        description="Logical database name targeted by the Data API.",
    )
    aws_region: str | None = Field(
        default=None,
        description="Region for the rds-data client; None lets boto3 resolve it "
        "(AWS_REGION is set by the Lambda runtime).",
    )

    # ---- Operational (defaulted) -----------------------------------------
    # NoDecode: same reason as in `Settings` -- stop pydantic-settings from
    # json.loads-ing the CSV before `parse_symbol_csv` sees it.
    scan_symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_WATCHLIST),
        description="Watchlist symbols. Env var SCAN_SYMBOLS is a comma-separated string.",
    )
    log_level: str = Field(
        default="INFO",
        description="Root logging level: DEBUG / INFO / WARNING / ERROR / CRITICAL.",
    )
    langfuse_base_url: str | None = Field(
        default=None,
        description="Langfuse host for trace deep links in the UI; None hides the links.",
    )

    # ---- Validators -------------------------------------------------------

    @field_validator("scan_symbols", mode="before")
    @classmethod
    def _split_csv_symbols(cls, v: object) -> object:
        return parse_symbol_csv(v)

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
def get_dashboard_settings() -> DashboardSettings:
    """Return the per-container `DashboardSettings` (read + validated once).

    Call `get_dashboard_settings.cache_clear()` in tests that re-read the env.
    """
    return DashboardSettings()  # type: ignore[call-arg]  # values come from env / .env
