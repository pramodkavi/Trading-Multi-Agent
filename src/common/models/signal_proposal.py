"""SignalProposal: the structured trade idea emitted by Analyzer strategies.

Every strategy (SMC, future funding-rate, etc.) returns either a SignalProposal
or a SkipDecision. The downstream agents (Historian, Skeptic, Judge) all
consume SignalProposal polymorphically — they do not know which strategy
produced it.

Field-level validation here enforces *shape* only (positive prices, R:R math
consistent with entry/SL/TP). Hard *policy* rules from SPEC §1.6 (min 1:3 R:R,
max 10x leverage, max 1% risk) are enforced in src/agents/orchestration/
risk_gates.py at Step 2.11. That separation lets analysts and the Critic
propose proposals that fail policy and have them rejected with a logged
violation, instead of silently swallowed at the schema layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.common.models.enums import SignalDirection


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SignalProposal(BaseModel):
    """A trade idea produced by a strategy's Analyzer.

    Carries everything downstream agents need: pricing, sizing parameters,
    qualitative tags for Historian retrieval, and a natural-language
    confluence_narrative the Critic later embeds (SPEC §3.4).
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    # ---- Identity ---------------------------------------------------------
    proposal_id: UUID = Field(
        default_factory=uuid4,
        description="Stable identifier for this proposal; preserved across "
        "the journal even if the Judge skips it.",
    )
    scan_id: UUID = Field(
        description="The scan run that produced this proposal; join key with "
        "ScanContext, scan_runs, and agent_runs.",
    )
    strategy: str = Field(
        min_length=1,
        max_length=64,
        description="Strategy registry name (e.g., 'smc'). Slice 1 always 'smc'.",
    )
    created_at: datetime = Field(
        default_factory=_utc_now,
        description="UTC timestamp of proposal creation. Must be timezone-aware.",
    )

    # ---- Instrument & direction ------------------------------------------
    symbol: str = Field(
        min_length=1,
        max_length=20,
        description="Market symbol in CCXT/Binance format, e.g., 'BTCUSDT'.",
    )
    direction: SignalDirection = Field(
        description="LONG or SHORT. Premium/Discount constraint (SPEC §1.5) "
        "is enforced in risk_gates.py, not here.",
    )

    # ---- Pricing ----------------------------------------------------------
    entry_price: float = Field(
        gt=0,
        description="Limit-entry price the user should place the order at.",
    )
    stop_loss: float = Field(
        gt=0,
        description="Hard invalidation price. Per SPEC §1.5 Layer 5, placed "
        "beyond the structural sweep wick.",
    )
    take_profit_1: float = Field(
        gt=0,
        description="First profit target. Per SPEC §1.5 Layer 5, partial close "
        "happens here and remaining position moves to breakeven.",
    )
    take_profit_2: float | None = Field(
        default=None,
        gt=0,
        description="Optional secondary target for the runner. Must be further "
        "from entry than take_profit_1 in the trade direction if present.",
    )

    # ---- Sizing & risk shape ---------------------------------------------
    risk_reward_ratio: float = Field(
        gt=0,
        description="Reward / risk computed against take_profit_1. Schema "
        "rejects <= 0; risk_gates enforces minimum 1:3 (SPEC §1.6 rule 2).",
    )
    leverage: float = Field(
        ge=1,
        le=100,
        description="Recommended leverage multiplier. Schema caps at 100x as a "
        "sanity ceiling; risk_gates enforces the policy cap of 10x.",
    )
    risk_percent: float = Field(
        gt=0,
        le=10,
        description="Percent of account equity at risk if SL hits. Schema caps "
        "at 10%; risk_gates enforces the 1% policy cap (SPEC §1.6 rule 1).",
    )

    # ---- Qualitative context ---------------------------------------------
    tags: list[str] = Field(
        default_factory=list,
        description="Free-form semantic tags used by the Historian for tag-overlap "
        "retrieval (SPEC §3.1.1 FR-1.4). Open vocabulary: the Critic discovers "
        "new tag patterns weekly (SPEC §3.4), so the set is deliberately not "
        "closed. Examples: 'bullish-ob', 'liquidity-sweep', 'london-killzone'.",
        examples=[["bullish-ob", "liquidity-sweep", "ote-confluence"]],
    )
    confluence_narrative: str = Field(
        min_length=20,
        max_length=4000,
        description="Natural-language explanation of why this setup formed. "
        "The Critic embeds this with text-embedding-3-small at Step 3.4 to "
        "discover cross-signal patterns the structured tags don't capture.",
    )
    features: dict[str, float | int | str | bool] = Field(
        default_factory=dict,
        description="Numeric/categorical feature snapshot used by the Historian's "
        "L2-distance retrieval stage (SPEC §3.1.1 FR-1.4 stage 3). Keys are "
        "strategy-specific. Persisted as JSONB.",
    )

    # ---- Cross-field consistency -----------------------------------------
    @model_validator(mode="after")
    def _validate_geometry(self) -> SignalProposal:
        """Ensure SL and TP sit on the correct sides of entry for the given direction.

        Catches schema-level mistakes (e.g., SL above entry on a LONG) at the
        boundary so downstream agents can trust the geometry. Does NOT enforce
        policy ratios — that is risk_gates.py.
        """
        if self.direction is SignalDirection.LONG:
            if self.stop_loss >= self.entry_price:
                raise ValueError(
                    "LONG proposals require stop_loss < entry_price "
                    f"(got SL={self.stop_loss}, entry={self.entry_price})"
                )
            if self.take_profit_1 <= self.entry_price:
                raise ValueError(
                    "LONG proposals require take_profit_1 > entry_price "
                    f"(got TP1={self.take_profit_1}, entry={self.entry_price})"
                )
            if self.take_profit_2 is not None and self.take_profit_2 <= self.take_profit_1:
                raise ValueError(
                    "LONG proposals require take_profit_2 > take_profit_1 "
                    f"(got TP2={self.take_profit_2}, TP1={self.take_profit_1})"
                )
        else:  # SHORT
            if self.stop_loss <= self.entry_price:
                raise ValueError(
                    "SHORT proposals require stop_loss > entry_price "
                    f"(got SL={self.stop_loss}, entry={self.entry_price})"
                )
            if self.take_profit_1 >= self.entry_price:
                raise ValueError(
                    "SHORT proposals require take_profit_1 < entry_price "
                    f"(got TP1={self.take_profit_1}, entry={self.entry_price})"
                )
            if self.take_profit_2 is not None and self.take_profit_2 >= self.take_profit_1:
                raise ValueError(
                    "SHORT proposals require take_profit_2 < take_profit_1 "
                    f"(got TP2={self.take_profit_2}, TP1={self.take_profit_1})"
                )
        return self

    @model_validator(mode="after")
    def _validate_rr_matches_geometry(self) -> SignalProposal:
        """Verify the declared risk_reward_ratio matches what entry/SL/TP1 imply.

        Tolerance ±5% to allow for rounding in the strategy's calculation, but
        catches gross inconsistencies (e.g., claimed R:R=5 with geometry that
        is actually 1:1).
        """
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.take_profit_1 - self.entry_price)
        if risk <= 0:
            raise ValueError("entry_price and stop_loss must differ")
        implied = reward / risk
        if not (implied * 0.95 <= self.risk_reward_ratio <= implied * 1.05):
            raise ValueError(
                f"risk_reward_ratio {self.risk_reward_ratio} inconsistent with "
                f"entry/SL/TP1 geometry (implied {implied:.3f}, tolerance ±5%)"
            )
        return self

    @model_validator(mode="after")
    def _validate_timezone_aware(self) -> SignalProposal:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (use UTC)")
        return self
