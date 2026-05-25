"""Tests for src.common.models.skip_decision.SkipDecision."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.common.models import SkipDecision, SkipReason


def _valid_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "reason": SkipReason.NO_CLEAR_BIAS,
        "details": "HTF state machine returned CONSOLIDATION; no actionable bias.",
    }
    base.update(overrides)
    return base


class TestSkipDecisionValid:
    def test_minimal_construction(self) -> None:
        sd = SkipDecision(**_valid_kwargs())
        assert sd.reason is SkipReason.NO_CLEAR_BIAS
        assert sd.violated_rule is None

    def test_rule_violation_records_rule_id(self) -> None:
        sd = SkipDecision(
            **_valid_kwargs(
                reason=SkipReason.INSUFFICIENT_RR,
                details="Computed R:R 2.4, required >= 3.0.",
                violated_rule="RULE_2_MIN_RR",
            )
        )
        assert sd.violated_rule == "RULE_2_MIN_RR"


class TestSkipDecisionValidation:
    def test_blank_details_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SkipDecision(**_valid_kwargs(details=""))

    def test_too_short_details_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SkipDecision(**_valid_kwargs(details="bad"))

    def test_too_long_details_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SkipDecision(**_valid_kwargs(details="x" * 3000))

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SkipDecision(**_valid_kwargs(unexpected="bad"))

    def test_naive_created_at_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            SkipDecision(**_valid_kwargs(created_at=datetime(2026, 1, 1, 0, 0, 0)))

    def test_frozen_after_construction(self) -> None:
        sd = SkipDecision(**_valid_kwargs())
        with pytest.raises(ValidationError):
            sd.symbol = "ETHUSDT"  # type: ignore[misc]


class TestSkipReasonCatalog:
    def test_all_spec_hard_rules_have_reason(self) -> None:
        """Every SPEC §1.6 hard rule must have a corresponding SkipReason."""
        expected = {
            "PREMIUM_DISCOUNT_VIOLATION",  # SPEC §1.5
            "INSUFFICIENT_RR",  # rule 2
            "EXCESSIVE_RISK",  # rule 1
            "LEVERAGE_CAP",  # rule 8
            "CONCURRENT_SETUPS_LIMIT",  # rule 4
            "DAILY_SIGNAL_CAP",  # rule 5
            "LOSS_STREAK_PAUSE",  # rule 6
            "SESSION_BLOCKED",  # rule 7
            "CORRELATED_EXPOSURE",  # rule 9
            "FUNDING_COST_PROHIBITIVE",  # rule 10
        }
        actual = {r.name for r in SkipReason}
        missing = expected - actual
        assert not missing, f"missing SkipReason members for hard rules: {missing}"
