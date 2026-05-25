"""Tests for src.common.models.signal_proposal.SignalProposal.

Coverage targets per SPEC.md Step 1.3 acceptance criteria:
- rejects invalid R:R
- rejects out-of-range numeric fields
- cross-field consistency (entry/SL/TP geometry by direction)
- timezone-aware created_at required
- model `frozen` semantics for extras
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.common.models import SignalDirection, SignalProposal


def _long_kwargs(**overrides: object) -> dict[str, object]:
    """Construct a known-valid LONG proposal kwargs dict.

    Entry 100, SL 95 (risk 5), TP1 115 (reward 15) → R:R = 3.0 exactly.
    Easy mental math for downstream tests to mutate.
    """
    base: dict[str, object] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "direction": SignalDirection.LONG,
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit_1": 115.0,
        "risk_reward_ratio": 3.0,
        "leverage": 5.0,
        "risk_percent": 1.0,
        "confluence_narrative": "Bullish OB tap with liquidity sweep below equal lows.",
    }
    base.update(overrides)
    return base


def _short_kwargs(**overrides: object) -> dict[str, object]:
    """Known-valid SHORT proposal: entry 100, SL 105 (risk 5), TP1 85 (reward 15)."""
    base: dict[str, object] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "ETHUSDT",
        "direction": SignalDirection.SHORT,
        "entry_price": 100.0,
        "stop_loss": 105.0,
        "take_profit_1": 85.0,
        "risk_reward_ratio": 3.0,
        "leverage": 3.0,
        "risk_percent": 0.5,
        "confluence_narrative": "Bearish OB rejection at premium with FVG above.",
    }
    base.update(overrides)
    return base


class TestSignalProposalValidLong:
    def test_minimal_long_constructs(self) -> None:
        p = SignalProposal(**_long_kwargs())
        assert p.direction is SignalDirection.LONG
        assert p.entry_price == 100.0
        assert p.risk_reward_ratio == pytest.approx(3.0)
        assert p.created_at.tzinfo is not None

    def test_tp2_above_tp1_accepted(self) -> None:
        p = SignalProposal(**_long_kwargs(take_profit_2=120.0))
        assert p.take_profit_2 == 120.0

    def test_default_tags_empty(self) -> None:
        p = SignalProposal(**_long_kwargs())
        assert p.tags == []

    def test_default_features_empty(self) -> None:
        p = SignalProposal(**_long_kwargs())
        assert p.features == {}


class TestSignalProposalValidShort:
    def test_minimal_short_constructs(self) -> None:
        p = SignalProposal(**_short_kwargs())
        assert p.direction is SignalDirection.SHORT
        assert p.stop_loss > p.entry_price
        assert p.take_profit_1 < p.entry_price

    def test_tp2_below_tp1_accepted(self) -> None:
        p = SignalProposal(**_short_kwargs(take_profit_2=75.0))
        assert p.take_profit_2 == 75.0


class TestSignalProposalGeometryLong:
    def test_long_rejects_sl_above_entry(self) -> None:
        with pytest.raises(ValidationError, match="stop_loss < entry_price"):
            SignalProposal(**_long_kwargs(stop_loss=105.0, risk_reward_ratio=3.0))

    def test_long_rejects_tp_below_entry(self) -> None:
        with pytest.raises(ValidationError, match="take_profit_1 > entry_price"):
            SignalProposal(**_long_kwargs(take_profit_1=95.0))

    def test_long_rejects_tp2_below_tp1(self) -> None:
        with pytest.raises(ValidationError, match="take_profit_2 > take_profit_1"):
            SignalProposal(**_long_kwargs(take_profit_2=110.0))


class TestSignalProposalGeometryShort:
    def test_short_rejects_sl_below_entry(self) -> None:
        with pytest.raises(ValidationError, match="stop_loss > entry_price"):
            SignalProposal(**_short_kwargs(stop_loss=95.0))

    def test_short_rejects_tp_above_entry(self) -> None:
        with pytest.raises(ValidationError, match="take_profit_1 < entry_price"):
            SignalProposal(**_short_kwargs(take_profit_1=110.0))

    def test_short_rejects_tp2_above_tp1(self) -> None:
        with pytest.raises(ValidationError, match="take_profit_2 < take_profit_1"):
            SignalProposal(**_short_kwargs(take_profit_2=90.0))


class TestSignalProposalRiskRewardConsistency:
    def test_declared_rr_must_match_geometry(self) -> None:
        # Geometry implies R:R of 3.0; claiming 5.0 must be rejected.
        with pytest.raises(ValidationError, match="inconsistent"):
            SignalProposal(**_long_kwargs(risk_reward_ratio=5.0))

    def test_within_five_percent_tolerance_accepted(self) -> None:
        # 3.1 is within ±5% of implied 3.0 → OK.
        p = SignalProposal(**_long_kwargs(risk_reward_ratio=3.1))
        assert p.risk_reward_ratio == 3.1

    def test_outside_tolerance_rejected(self) -> None:
        # 3.5 is outside ±5% of 3.0 → reject.
        with pytest.raises(ValidationError, match="inconsistent"):
            SignalProposal(**_long_kwargs(risk_reward_ratio=3.5))

    def test_zero_rr_rejected_by_field_constraint(self) -> None:
        with pytest.raises(ValidationError):
            SignalProposal(**_long_kwargs(risk_reward_ratio=0.0))

    def test_negative_rr_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignalProposal(**_long_kwargs(risk_reward_ratio=-1.0))


class TestSignalProposalNumericBounds:
    def test_negative_entry_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignalProposal(**_long_kwargs(entry_price=-100.0))

    def test_zero_entry_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignalProposal(**_long_kwargs(entry_price=0.0))

    def test_leverage_below_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignalProposal(**_long_kwargs(leverage=0.5))

    def test_leverage_above_100_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignalProposal(**_long_kwargs(leverage=150.0))

    def test_leverage_above_policy_cap_accepted_at_schema_layer(self) -> None:
        # Schema cap is 100x; policy cap of 10x is enforced in risk_gates.py.
        # This is intentional: lets risk_gates log a violation with the actual
        # leverage requested, rather than the model swallowing it silently.
        p = SignalProposal(**_long_kwargs(leverage=25.0))
        assert p.leverage == 25.0

    def test_risk_percent_above_schema_cap_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignalProposal(**_long_kwargs(risk_percent=15.0))

    def test_risk_percent_above_policy_accepted_at_schema_layer(self) -> None:
        # Policy cap 1%; schema cap 10% — same rationale as leverage.
        p = SignalProposal(**_long_kwargs(risk_percent=2.0))
        assert p.risk_percent == 2.0


class TestSignalProposalNarrativeAndTags:
    def test_narrative_too_short_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignalProposal(**_long_kwargs(confluence_narrative="too short"))

    def test_narrative_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignalProposal(**_long_kwargs(confluence_narrative="x" * 5000))

    def test_tags_accept_open_vocabulary(self) -> None:
        # Spec §3.4: Critic discovers new tags; vocabulary is intentionally open.
        p = SignalProposal(
            **_long_kwargs(tags=["bullish-ob", "novel-tag-from-critic", "london-killzone"])
        )
        assert "novel-tag-from-critic" in p.tags

    def test_features_accept_mixed_value_types(self) -> None:
        p = SignalProposal(
            **_long_kwargs(
                features={"funding_rate": 0.0001, "oi_change_pct": -2.5, "session": "london"}
            )
        )
        assert p.features["funding_rate"] == 0.0001


class TestSignalProposalMetadata:
    def test_rejects_naive_created_at(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            SignalProposal(**_long_kwargs(created_at=datetime(2026, 1, 1, 12, 0, 0)))

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            SignalProposal(**_long_kwargs(unexpected_field=42))

    def test_blank_symbol_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignalProposal(**_long_kwargs(symbol=""))

    def test_proposal_id_auto_unique(self) -> None:
        a = SignalProposal(**_long_kwargs())
        b = SignalProposal(**_long_kwargs())
        assert a.proposal_id != b.proposal_id

    def test_aware_created_at_with_explicit_utc(self) -> None:
        ts = datetime(2026, 5, 25, 13, 3, 0, tzinfo=UTC)
        p = SignalProposal(**_long_kwargs(created_at=ts))
        assert p.created_at == ts
