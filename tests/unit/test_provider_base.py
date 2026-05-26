"""Tests for src.providers.base — interface types and exception hierarchy.

Covers the boundary models (Kline, MarketSnapshot, MacroContext) and the
ProviderError hierarchy. The DataProvider ABC itself is exercised through
BinanceProvider tests in test_binance_provider.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.providers import (
    DataProvider,
    Kline,
    MacroContext,
    MarketSnapshot,
    ProviderError,
    ProviderInvalidResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    Timeframe,
)

# ---------------------------------------------------------------------------
# Timeframe enum
# ---------------------------------------------------------------------------


class TestTimeframe:
    def test_values_match_ccxt_strings(self) -> None:
        # CCXT's fetch_ohlcv accepts these literal strings; the enum value
        # is used as the wire-format directly.
        assert Timeframe.D1.value == "1d"
        assert Timeframe.H4.value == "4h"
        assert Timeframe.H1.value == "1h"
        assert Timeframe.M15.value == "15m"
        assert Timeframe.M5.value == "5m"

    def test_covers_spec_layer_1_timeframes(self) -> None:
        # SPEC §1.5 Layer 1 lists 1D, 4H, 1H, 15m, 5m.
        assert {t.value for t in Timeframe} == {"1d", "4h", "1h", "15m", "5m"}


# ---------------------------------------------------------------------------
# Kline
# ---------------------------------------------------------------------------


def _valid_kline_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "open_time": datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC),
        "open": 100.0,
        "high": 105.0,
        "low": 99.0,
        "close": 103.0,
        "volume": 1234.5,
    }
    base.update(overrides)
    return base


class TestKline:
    def test_valid_kline_constructs(self) -> None:
        k = Kline(**_valid_kline_kwargs())
        assert k.high == 105.0
        assert k.open_time.tzinfo is not None

    def test_naive_open_time_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            Kline(**_valid_kline_kwargs(open_time=datetime(2026, 5, 25, 12, 0, 0)))

    def test_high_below_open_rejected(self) -> None:
        with pytest.raises(ValidationError, match="high"):
            Kline(**_valid_kline_kwargs(open=110.0, high=105.0))

    def test_high_below_close_rejected(self) -> None:
        with pytest.raises(ValidationError, match="high"):
            Kline(**_valid_kline_kwargs(close=108.0, high=105.0))

    def test_low_above_open_rejected(self) -> None:
        with pytest.raises(ValidationError, match="low"):
            Kline(**_valid_kline_kwargs(open=95.0, low=99.0))

    def test_low_above_close_rejected(self) -> None:
        with pytest.raises(ValidationError, match="low"):
            Kline(**_valid_kline_kwargs(close=97.0, low=99.0))

    def test_negative_volume_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Kline(**_valid_kline_kwargs(volume=-1.0))

    def test_zero_volume_accepted(self) -> None:
        # A flat candle with no trades is unusual but legal on illiquid venues.
        k = Kline(**_valid_kline_kwargs(volume=0.0))
        assert k.volume == 0.0

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Kline(**_valid_kline_kwargs(extra="bad"))


# ---------------------------------------------------------------------------
# MarketSnapshot
# ---------------------------------------------------------------------------


def _candle(minutes: int, **overrides: object) -> Kline:
    """Build a Kline N minutes after a fixed anchor; lets us produce ordered series."""
    base_kwargs = _valid_kline_kwargs(
        open_time=datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC) + timedelta(minutes=minutes),
    )
    base_kwargs.update(overrides)
    return Kline(**base_kwargs)


def _snapshot_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "symbol": "BTCUSDT",
        "venue": "binance",
        "fetched_at": datetime(2026, 5, 25, 13, 0, 0, tzinfo=UTC),
        "klines": {Timeframe.H4: [_candle(0), _candle(240), _candle(480)]},
    }
    base.update(overrides)
    return base


class TestMarketSnapshot:
    def test_valid_snapshot_constructs(self) -> None:
        s = MarketSnapshot(**_snapshot_kwargs())
        assert s.symbol == "BTCUSDT"
        assert Timeframe.H4 in s.klines
        assert s.funding_rate is None  # not provided in Slice 1

    def test_multi_timeframe_snapshot(self) -> None:
        # Demonstrates the design point: the model already supports multi-TF.
        s = MarketSnapshot(
            **_snapshot_kwargs(
                klines={
                    Timeframe.D1: [_candle(0)],
                    Timeframe.H4: [_candle(0), _candle(240)],
                }
            )
        )
        assert set(s.klines.keys()) == {Timeframe.D1, Timeframe.H4}

    def test_empty_klines_dict_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one timeframe"):
            MarketSnapshot(**_snapshot_kwargs(klines={}))

    def test_empty_candle_list_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one candle"):
            MarketSnapshot(**_snapshot_kwargs(klines={Timeframe.H4: []}))

    def test_naive_fetched_at_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            MarketSnapshot(**_snapshot_kwargs(fetched_at=datetime(2026, 5, 25, 13, 0, 0)))

    def test_non_chronological_candles_rejected(self) -> None:
        with pytest.raises(ValidationError, match="strictly ascending"):
            MarketSnapshot(
                **_snapshot_kwargs(
                    klines={Timeframe.H4: [_candle(240), _candle(0)]}  # out of order
                )
            )

    def test_duplicate_timestamp_candles_rejected(self) -> None:
        # "Strictly ascending" — equal timestamps must not pass either.
        with pytest.raises(ValidationError, match="strictly ascending"):
            MarketSnapshot(**_snapshot_kwargs(klines={Timeframe.H4: [_candle(0), _candle(0)]}))

    def test_funding_rate_can_be_negative(self) -> None:
        # Negative funding (shorts pay longs) is a legitimate market state.
        s = MarketSnapshot(**_snapshot_kwargs(funding_rate=-0.0005))
        assert s.funding_rate == -0.0005

    def test_negative_open_interest_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MarketSnapshot(**_snapshot_kwargs(open_interest=-100.0))

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MarketSnapshot(**_snapshot_kwargs(unexpected="bad"))


# ---------------------------------------------------------------------------
# MacroContext
# ---------------------------------------------------------------------------


class TestMacroContext:
    def test_all_optional_fields_default_none(self) -> None:
        mc = MacroContext(fetched_at=datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC))
        assert mc.dxy is None
        assert mc.us10y_yield is None
        assert mc.spx is None
        assert mc.vix is None

    def test_us10y_yield_can_be_negative(self) -> None:
        # Negative yields exist in stressed regimes — we must not reject them.
        mc = MacroContext(
            fetched_at=datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC),
            us10y_yield=-0.5,
        )
        assert mc.us10y_yield == -0.5

    def test_negative_dxy_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MacroContext(
                fetched_at=datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC),
                dxy=-100.0,
            )

    def test_naive_fetched_at_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            MacroContext(fetched_at=datetime(2026, 5, 25, 12, 0, 0))


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestProviderExceptions:
    def test_all_subclasses_inherit_from_provider_error(self) -> None:
        # Single `except ProviderError` must catch every variant.
        for cls in (
            ProviderUnavailableError,
            ProviderRateLimitError,
            ProviderTimeoutError,
            ProviderInvalidResponseError,
        ):
            assert issubclass(cls, ProviderError)

    def test_provider_name_attached(self) -> None:
        exc = ProviderRateLimitError("too fast", provider="binance")
        assert exc.provider == "binance"
        assert "too fast" in str(exc)

    def test_provider_kwarg_required(self) -> None:
        with pytest.raises(TypeError):
            ProviderError("missing provider")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# DataProvider ABC
# ---------------------------------------------------------------------------


class TestDataProviderABC:
    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            DataProvider()  # type: ignore[abstract]
