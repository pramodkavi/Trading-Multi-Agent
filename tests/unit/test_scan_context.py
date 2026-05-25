"""Tests for src.common.models.scan_context.ScanContext."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.common.models import ScanContext, ScanSession


def _valid_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "session": ScanSession.LONDON,
        "symbols": ["BTCUSDT"],
        "strategy": "smc",
    }
    base.update(overrides)
    return base


class TestScanContextValidConstruction:
    def test_minimal_construction_succeeds(self) -> None:
        ctx = ScanContext(**_valid_kwargs())
        assert ctx.session is ScanSession.LONDON
        assert ctx.symbols == ["BTCUSDT"]
        assert ctx.strategy == "smc"
        assert ctx.triggered_by == "scheduler"
        assert ctx.started_at.tzinfo is not None

    def test_scan_id_is_auto_generated_unique(self) -> None:
        a = ScanContext(**_valid_kwargs())
        b = ScanContext(**_valid_kwargs())
        assert a.scan_id != b.scan_id

    def test_explicit_scan_id_preserved(self) -> None:
        sid = uuid4()
        ctx = ScanContext(scan_id=sid, **_valid_kwargs())
        assert ctx.scan_id == sid


class TestScanContextValidation:
    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            ScanContext(started_at=datetime(2026, 1, 1, 12, 0, 0), **_valid_kwargs())

    def test_accepts_utc_aware_datetime(self) -> None:
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        ctx = ScanContext(started_at=ts, **_valid_kwargs())
        assert ctx.started_at == ts

    def test_empty_symbols_list_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScanContext(**_valid_kwargs(symbols=[]))

    def test_duplicate_symbols_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            ScanContext(**_valid_kwargs(symbols=["BTCUSDT", "BTCUSDT"]))

    def test_symbols_normalized_uppercase(self) -> None:
        ctx = ScanContext(**_valid_kwargs(symbols=["btcusdt", "ethusdt"]))
        assert ctx.symbols == ["BTCUSDT", "ETHUSDT"]

    def test_blank_symbol_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScanContext(**_valid_kwargs(symbols=["BTCUSDT", "  "]))

    def test_blank_strategy_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScanContext(**_valid_kwargs(strategy=""))

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScanContext(extra_field="bad", **_valid_kwargs())

    def test_frozen_after_construction(self) -> None:
        ctx = ScanContext(**_valid_kwargs())
        with pytest.raises(ValidationError):
            ctx.strategy = "different"  # type: ignore[misc]
