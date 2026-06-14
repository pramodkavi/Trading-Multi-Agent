"""Market-data providers.

Public API per SPEC §2.3 / §3.1.4: every external data source implements
`DataProvider` and returns normalized models. Agents import only from this
package — never from `ccxt` or any other vendor library directly.
"""

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
from src.providers.binance import BinanceProvider
from src.providers.macro import FREDProvider, MacroProvider, TwelveDataProvider
from src.providers.rate_limit import TokenBucket

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
