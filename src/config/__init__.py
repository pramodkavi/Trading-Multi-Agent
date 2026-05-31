"""Application configuration.

Public API:
    Settings          -- the typed config model (Pydantic BaseSettings).
    get_settings      -- cached accessor returning the process singleton.
    DEFAULT_WATCHLIST -- SPEC default scan symbols.
"""

from src.config.settings import DEFAULT_WATCHLIST, Settings, get_settings

__all__ = ["DEFAULT_WATCHLIST", "Settings", "get_settings"]
