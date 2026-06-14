"""Macro / cross-asset data providers for the Skeptic agent (Step 2.3).

Two providers, both behind the `DataProvider` interface, both returning a
normalized `MacroContext` (or a `NoMacroData` sentinel on total failure):

- `FREDProvider`     — DXY (broad USD index), US 10-year yield, Fed Funds rate.
- `TwelveDataProvider` — S&P 500 (SPX) and VIX intraday levels.

Each populates only the fields it owns and leaves the rest None; the Skeptic
(Step 2.5) fetches both in parallel and merges them. Per FR-4.3, a provider that
cannot serve *any* of its fields returns `NoMacroData` rather than raising, so a
macro outage downgrades the Skeptic's confidence instead of crashing the scan.

These are REST APIs (not ccxt), so they use httpx directly behind the same typed
ProviderError hierarchy. The httpx client is injectable for testing (the unit
tests drive it with `httpx.MockTransport`); when not injected, the provider owns
and closes its own client.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from src.providers.base import (
    DataProvider,
    MacroContext,
    MarketSnapshot,
    NoMacroData,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    Timeframe,
)

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0


def _value_or_none(result: float | None | BaseException) -> float | None:
    """Collapse a gathered best-effort result: exceptions and Nones become None."""
    if isinstance(result, BaseException):
        return None
    return result


class MacroProvider(DataProvider):
    """Shared base for REST macro providers: httpx plumbing + error translation.

    Concrete subclasses set `name`, the API base URL, and implement
    `fetch_macro_context`. `fetch_market_snapshot` is unsupported here (macro
    providers do not serve klines) and raises NotImplementedError.
    """

    base_url: str

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(timeout=timeout)
            self._owns_client = True

    async def aclose(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def fetch_market_snapshot(
        self,
        symbol: str,
        timeframes: list[Timeframe],
        *,
        limit: int = 200,
        include_derivatives: bool = False,
    ) -> MarketSnapshot:
        raise NotImplementedError(f"{self.name} is a macro provider; it has no market snapshots")

    async def _get_json(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        """GET `path` and return parsed JSON, mapping httpx failures to ProviderError."""
        try:
            response = await self._client.get(
                f"{self.base_url}{path}", params=params, timeout=self._timeout
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc), provider=self.name) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise ProviderRateLimitError(str(exc), provider=self.name) from exc
            raise ProviderUnavailableError(str(exc), provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(str(exc), provider=self.name) from exc

        if not isinstance(data, dict):
            raise ProviderUnavailableError(
                f"expected a JSON object, got {type(data).__name__}", provider=self.name
            )
        return data


class FREDProvider(MacroProvider):
    """FRED (St. Louis Fed): DXY proxy, US 10-year yield, Fed Funds rate.

    DXY note: FRED does not publish the ICE Dollar Index; `DTWEXBGS` (Nominal Broad
    U.S. Dollar Index) is the standard FRED dollar measure and a reasonable proxy.
    Series ids are overridable via __init__ for flexibility.
    """

    name = "fred"
    base_url = "https://api.stlouisfed.org/fred"

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        dxy_series: str = "DTWEXBGS",
        us10y_series: str = "DGS10",
        fed_funds_series: str = "DFF",
    ) -> None:
        super().__init__(api_key=api_key, client=client, timeout=timeout)
        self._dxy_series = dxy_series
        self._us10y_series = us10y_series
        self._fed_funds_series = fed_funds_series

    async def fetch_macro_context(self) -> MacroContext | NoMacroData:
        dxy, us10y, fed_funds = await asyncio.gather(
            self._latest_observation(self._dxy_series),
            self._latest_observation(self._us10y_series),
            self._latest_observation(self._fed_funds_series),
            return_exceptions=True,
        )
        dxy_v = _value_or_none(dxy)
        us10y_v = _value_or_none(us10y)
        fed_funds_v = _value_or_none(fed_funds)

        if dxy_v is None and us10y_v is None and fed_funds_v is None:
            return NoMacroData(provider=self.name, reason="all FRED series fetches failed")

        try:
            return MacroContext(
                fetched_at=datetime.now(UTC),
                dxy=dxy_v,
                us10y_yield=us10y_v,
                fed_funds=fed_funds_v,
            )
        except ValueError as exc:  # implausible value (e.g. non-positive DXY)
            return NoMacroData(provider=self.name, reason=f"FRED values failed validation: {exc}")

    async def _latest_observation(self, series_id: str) -> float | None:
        """Most recent numeric observation for a FRED series, or None if missing."""
        data = await self._get_json(
            "/series/observations",
            params={
                "series_id": series_id,
                "api_key": self._api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
            },
        )
        observations = data.get("observations")
        if not isinstance(observations, list) or not observations:
            return None
        value = observations[0].get("value")
        # FRED encodes a missing observation as ".".
        if value is None or value == ".":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


class TwelveDataProvider(MacroProvider):
    """Twelve Data: S&P 500 (SPX) and VIX intraday price levels."""

    name = "twelvedata"
    base_url = "https://api.twelvedata.com"

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        spx_symbol: str = "SPX",
        vix_symbol: str = "VIX",
    ) -> None:
        super().__init__(api_key=api_key, client=client, timeout=timeout)
        self._spx_symbol = spx_symbol
        self._vix_symbol = vix_symbol

    async def fetch_macro_context(self) -> MacroContext | NoMacroData:
        spx, vix = await asyncio.gather(
            self._latest_price(self._spx_symbol),
            self._latest_price(self._vix_symbol),
            return_exceptions=True,
        )
        spx_v = _value_or_none(spx)
        vix_v = _value_or_none(vix)

        if spx_v is None and vix_v is None:
            return NoMacroData(provider=self.name, reason="all Twelve Data price fetches failed")

        try:
            return MacroContext(fetched_at=datetime.now(UTC), spx=spx_v, vix=vix_v)
        except ValueError as exc:
            return NoMacroData(
                provider=self.name, reason=f"Twelve Data values failed validation: {exc}"
            )

    async def _latest_price(self, symbol: str) -> float | None:
        data = await self._get_json("/price", params={"symbol": symbol, "apikey": self._api_key})
        # Twelve Data signals errors in the body: {"status":"error","message":...}.
        if data.get("status") == "error":
            raise ProviderUnavailableError(
                str(data.get("message", "Twelve Data error")), provider=self.name
            )
        price = data.get("price")
        if price is None:
            return None
        try:
            return float(price)
        except (TypeError, ValueError):
            return None
