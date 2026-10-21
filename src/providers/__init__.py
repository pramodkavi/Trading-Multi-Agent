"""Market-data providers.

Public API per SPEC §2.3 / §3.1.4: every external data source implements
`DataProvider` and returns normalized models. Agents import only from this
package — never from `ccxt` or any other vendor library directly.

Why the vendor-backed names are lazy (PEP 562): the dashboard Lambda imports
``src.providers.base`` (``Timeframe``, ``ProviderError``) and must not pay for
ccxt + aiohttp (``binance.py``) or httpx (``macro.py``) at cold start (design
§3.2) -- importing ANY ``src.providers.*`` submodule runs this ``__init__``
first. So the light models (``base``) and ``rate_limit`` (stdlib only) stay
eager, and ``BinanceProvider`` / the macro providers are resolved from their
submodule on first attribute access: ``from src.providers import
BinanceProvider`` still works for the scan pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.providers.base import (
    DataProvider,
    Kline,
    MacroContext,
    MarketSnapshot,
    NoMacroData,
    ProviderError,
    ProviderInvalidResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    Timeframe,
)
from src.providers.rate_limit import TokenBucket

if TYPE_CHECKING:  # pragma: no cover - typing only; runtime resolution is lazy
    from src.providers.binance import BinanceProvider
    from src.providers.macro import FREDProvider, MacroProvider, TwelveDataProvider

__all__ = [
    "BinanceProvider",
    "DataProvider",
    "FREDProvider",
    "Kline",
    "MacroContext",
    "MacroProvider",
    "MarketSnapshot",
    "NoMacroData",
    "ProviderError",
    "ProviderInvalidResponseError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "Timeframe",
    "TokenBucket",
    "TwelveDataProvider",
]

# Public name -> submodule that defines it (resolved on first access).
_LAZY: dict[str, str] = {
    "BinanceProvider": "binance",
    "FREDProvider": "macro",
    "MacroProvider": "macro",
    "TwelveDataProvider": "macro",
}


def __getattr__(name: str) -> Any:
    try:
        module = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib

    return getattr(importlib.import_module(f"src.providers.{module}"), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
