"""DataProvider interface and shared boundary types for all market data sources.

Per SPEC §2.3 and §3.1.4 FR-4.1, every external data source (Binance, FRED,
Twelve Data, future on-chain sources) sits behind this uniform interface and
returns normalized Pydantic models. Agents — including the Skeptic, which
fetches its own macro context — must never `import ccxt` or any other vendor
library directly. They go through this abstraction.

Concrete provider modules (`src/providers/binance.py`, etc.) implement
DataProvider and translate vendor-specific exceptions into the typed hierarchy
defined here. That lets downstream agents `except ProviderUnavailableError`
without ever knowing which vendor produced it.

Multi-timeframe by design: MarketSnapshot.klines is a `dict[Timeframe, list[Kline]]`
even though Slice 1 Step 1.4 only populates the `H4` key. Step 2.2 adds more
timeframes (D1, H1, M15, M5) without breaking any consumer's signature.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for every provider failure.

    Catch this at the orchestration boundary to handle *any* provider issue
    uniformly. Catch a subclass to react to a specific failure mode (e.g.,
    rate limiting deserves backoff and retry; an invalid response is a bug).

    Carries the provider name so logs can show which source failed without
    requiring agents to introspect the exception type.
    """

    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderUnavailableError(ProviderError):
    """Provider is reachable but cannot serve the request (5xx, region block,
    DNS failure, maintenance). Per FR-4.3, the Skeptic degrades gracefully on
    this — the Judge interprets absence as 'downgrade confidence to medium'
    rather than treating it as 'no objection'.
    """


class ProviderRateLimitError(ProviderError):
    """Provider rejected the request because the caller exceeded its quota.

    The caller may retry after a delay. Binance returns weight-exceeded as
    HTTP 418/429; CCXT raises `RateLimitExceeded` or `DDoSProtection`.
    """


class ProviderTimeoutError(ProviderError):
    """The request did not complete within the provider's timeout window.

    Distinct from Unavailable so the orchestration layer can pick a different
    backoff strategy (timeouts often clear faster than outages).
    """


class ProviderInvalidResponseError(ProviderError):
    """The provider returned data we cannot parse or that violates expected shape.

    This is usually a bug (vendor API change) rather than a transient failure.
    Should not be retried; should be surfaced loudly.
    """


# ---------------------------------------------------------------------------
# Domain types — what providers return
# ---------------------------------------------------------------------------


class Timeframe(StrEnum):
    """Candle timeframes referenced in SPEC §1.5 Layer 1.

    Enum values match the CCXT timeframe strings ('1d', '4h', '1h', '15m',
    '5m'), which keeps the BinanceProvider implementation a thin pass-through:
    `await exchange.fetch_ohlcv(symbol, timeframe.value, ...)`.
    """

    D1 = "1d"
    H4 = "4h"
    H1 = "1h"
    M15 = "15m"
    M5 = "5m"


class Kline(BaseModel):
    """A single OHLCV candle.

    Normalized from CCXT's `[timestamp_ms, open, high, low, close, volume]`
    list-of-lists format. Storing as a Pydantic model (not a tuple) means
    downstream code can use `candle.high` instead of `candle[2]`.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    open_time: datetime = Field(
        description="UTC timestamp of the candle's open. Must be timezone-aware.",
    )
    open: float = Field(gt=0, description="Open price.")
    high: float = Field(gt=0, description="High price during the candle period.")
    low: float = Field(gt=0, description="Low price during the candle period.")
    close: float = Field(gt=0, description="Close price.")
    volume: float = Field(ge=0, description="Base-asset volume traded during the period.")

    @field_validator("open_time")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("open_time must be timezone-aware (use UTC)")
        return v

    @model_validator(mode="after")
    def _validate_ohlc_consistency(self) -> Kline:
        """High must dominate, low must be dominated by every other price.

        Catches gross data-corruption from the provider (e.g., a high lower
        than the open). Cheaper to reject here than to discover at analysis
        time when an SMC sweep detector silently misfires.
        """
        if self.high < max(self.open, self.close):
            raise ValueError(
                f"high {self.high} must be >= max(open={self.open}, close={self.close})"
            )
        if self.low > min(self.open, self.close):
            raise ValueError(f"low {self.low} must be <= min(open={self.open}, close={self.close})")
        if self.high < self.low:
            raise ValueError(f"high {self.high} cannot be less than low {self.low}")
        return self


class MarketSnapshot(BaseModel):
    """A point-in-time, multi-timeframe view of one trading symbol.

    The Analyzer consumes this for the SMC 5-layer protocol (SPEC §1.5).
    Slice 1 only populates `klines[Timeframe.H4]`; Step 2.2 fills the rest.

    Funding rate and open interest are Optional because not every venue
    provides them and not every snapshot needs them. The Analyzer's
    derivatives confluence gate (Layer 3 Gate 4) checks for presence.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    symbol: str = Field(
        min_length=1,
        max_length=20,
        description="Market symbol in CCXT/Binance format, e.g., 'BTCUSDT'.",
    )
    venue: str = Field(
        min_length=1,
        max_length=32,
        description="Provider/exchange name, e.g., 'binance'. Used to attribute "
        "downstream traces to a specific data source.",
    )
    fetched_at: datetime = Field(
        description="UTC wall-clock time the snapshot was taken. Must be timezone-aware. "
        "Used to detect stale data in retries and to align with scan boundaries.",
    )
    klines: dict[Timeframe, list[Kline]] = Field(
        description="Per-timeframe candle history, most-recent last. Slice 1 fills "
        "only Timeframe.H4; Step 2.2 adds D1/H1/M15/M5. Empty dict not allowed.",
    )
    funding_rate: float | None = Field(
        default=None,
        description="Latest perpetual funding rate as a decimal (e.g., 0.0001 = 0.01%). "
        "None when the venue does not expose it or the call was scoped without it.",
    )
    open_interest: float | None = Field(
        default=None,
        ge=0,
        description="Latest open-interest figure in base-asset units. None when not fetched.",
    )

    @field_validator("fetched_at")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware (use UTC)")
        return v

    @field_validator("klines")
    @classmethod
    def _klines_non_empty(cls, v: dict[Timeframe, list[Kline]]) -> dict[Timeframe, list[Kline]]:
        if not v:
            raise ValueError("klines must contain at least one timeframe")
        for tf, candles in v.items():
            if not candles:
                raise ValueError(f"klines[{tf.value}] must contain at least one candle")
        return v

    @model_validator(mode="after")
    def _klines_chronological(self) -> MarketSnapshot:
        """Within each timeframe, candles must be sorted ascending by open_time.

        The Analyzer assumes most-recent-last ordering. Asserting it at the
        boundary saves every downstream consumer from re-sorting defensively.
        """
        for tf, candles in self.klines.items():
            for prev, curr in pairwise(candles):
                if curr.open_time <= prev.open_time:
                    raise ValueError(
                        f"klines[{tf.value}] not strictly ascending by open_time "
                        f"({prev.open_time.isoformat()} -> {curr.open_time.isoformat()})"
                    )
        return self


class MacroContext(BaseModel):
    """Macro / cross-asset context used by the Skeptic agent.

    Populated by FRED + Twelve Data providers in Slice 2 (Step 2.3). Defined
    here in Slice 1 so the interface is stable from day one; concrete
    populating providers come later.

    Per FR-4.3 the Skeptic must handle a missing MacroContext gracefully.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    fetched_at: datetime = Field(
        description="UTC timestamp of the macro snapshot. Must be timezone-aware.",
    )
    dxy: float | None = Field(
        default=None,
        gt=0,
        description="DXY dollar index spot. None when FRED is unavailable.",
    )
    us10y_yield: float | None = Field(
        default=None,
        description="US 10-year Treasury yield, as a percent (e.g., 4.25 = 4.25%). "
        "Can be negative in stressed regimes.",
    )
    spx: float | None = Field(
        default=None,
        gt=0,
        description="S&P 500 index level (latest intraday). None when Twelve Data unavailable.",
    )
    vix: float | None = Field(
        default=None,
        ge=0,
        description="VIX volatility index level. None when intraday data unavailable.",
    )
    fed_funds: float | None = Field(
        default=None,
        description="Effective Federal Funds rate, as a percent (e.g., 5.33). "
        "None when FRED is unavailable. Unconstrained sign for robustness.",
    )

    @field_validator("fetched_at")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware (use UTC)")
        return v


class NoMacroData(BaseModel):
    """Sentinel returned when a macro provider cannot serve *any* data.

    Per FR-4.3 the Skeptic degrades gracefully: it treats NoMacroData as "macro
    context unavailable — downgrade confidence to medium", NOT as "no objection".
    A distinct type (rather than an all-None MacroContext) lets callers branch with
    `isinstance(result, NoMacroData)` and keeps "we have no data" unambiguous versus
    "we fetched data and these fields happened to be absent".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, description="Which provider failed (e.g., 'fred').")
    reason: str = Field(
        min_length=1, max_length=500, description="Human-readable cause for logs/dashboards."
    )


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class DataProvider(ABC):
    """Abstract interface every market-data source implements.

    Concrete providers (BinanceProvider, FREDProvider, TwelveDataProvider, ...)
    each implement the subset of methods they support and raise
    NotImplementedError for the rest. Composition over inheritance: an
    orchestrator may hold several providers and route by source type rather
    than relying on a single God-provider.

    Slice 1 only defines `fetch_market_snapshot`; macro methods are added in
    Step 2.3 when the Skeptic ships. Designing the abstract surface now keeps
    consumer code stable.
    """

    name: str  # concrete subclasses must set this; used in error messages and traces

    @abstractmethod
    async def fetch_market_snapshot(
        self,
        symbol: str,
        timeframes: list[Timeframe],
        *,
        limit: int = 200,
        include_derivatives: bool = False,
    ) -> MarketSnapshot:
        """Fetch a normalized snapshot for one symbol across one or more timeframes.

        Args:
            symbol: market symbol in venue-native format (e.g., 'BTCUSDT').
            timeframes: which timeframes to include in the snapshot.
            limit: max number of candles per timeframe (most-recent N).
            include_derivatives: when True and the venue supports it, also populate
                `funding_rate` and `open_interest` on the snapshot (Step 2.2). On a
                venue without derivatives data these stay None. Derivative fetches
                degrade gracefully — a failure leaves the field None rather than
                failing the whole snapshot.

        Raises:
            ProviderUnavailableError: venue unreachable or returned 5xx.
            ProviderRateLimitError: caller has hit the venue's rate limit.
            ProviderTimeoutError: request did not complete in time.
            ProviderInvalidResponseError: response did not parse to a MarketSnapshot.
        """

    async def fetch_macro_context(self) -> MacroContext | NoMacroData:
        """Fetch this provider's slice of macro context (Step 2.3).

        Macro providers (FRED, Twelve Data) override this; market-only providers
        (Binance) inherit the default below. Each macro provider populates only the
        fields it owns (FRED: dxy/us10y_yield/fed_funds; Twelve Data: spx/vix) and
        leaves the rest None — the Skeptic merges them. Returns a NoMacroData
        sentinel when the provider cannot serve any field (graceful degradation,
        FR-4.3), rather than raising.
        """
        raise NotImplementedError(f"{self.name} does not provide macro context")

    async def aclose(self) -> None:  # noqa: B027  (intentional no-op default)
        """Release any held async resources (HTTP sessions, etc.).

        Default no-op so providers without external state need not override.
        Async-context-manager users get this automatically via __aexit__.
        """

    async def __aenter__(self) -> DataProvider:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()
