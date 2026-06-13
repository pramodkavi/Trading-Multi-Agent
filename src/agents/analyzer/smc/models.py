"""Pydantic result models for the SMC structure layer (Step 2.1a).

Every detector emits typed, frozen models rather than dicts so downstream code
(the scoring/assembly step 2.1d, the Historian, the Judge) consumes a stable,
validated shape. Mirrors the `extra="forbid", frozen=True` convention used by
`src/providers/base.py`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SwingType(StrEnum):
    """Whether a fractal pivot is a swing high or a swing low."""

    HIGH = "HIGH"
    LOW = "LOW"


class SwingLabel(StrEnum):
    """Structural label of a swing relative to the previous same-type swing.

    HH (higher high) / LH (lower high) for swing highs; HL (higher low) /
    LL (lower low) for swing lows. The first swing of each type has no label
    (None) because there is nothing to compare it against.
    """

    HH = "HH"
    LH = "LH"
    HL = "HL"
    LL = "LL"


class StructureEventType(StrEnum):
    """A structural break event.

    BOS (Break of Structure) = continuation of the prevailing trend.
    CHoCH (Change of Character) = the first break *against* an established
    trend, i.e. a regime-change warning.
    """

    BOS_BULLISH = "BOS_BULLISH"
    BOS_BEARISH = "BOS_BEARISH"
    CHOCH_BULLISH = "CHOCH_BULLISH"
    CHOCH_BEARISH = "CHOCH_BEARISH"


class MarketPhase(StrEnum):
    """Current HTF market phase derived from the BOS/CHoCH state machine."""

    UPTREND = "UPTREND"
    DOWNTREND = "DOWNTREND"
    CONSOLIDATION = "CONSOLIDATION"


class Zone(StrEnum):
    """Where current price sits within the dealing range (SPEC §1.5 hard rule).

    Long only in DISCOUNT, short only in PREMIUM. EQUILIBRIUM is the no-trade
    band straddling the 50% midpoint.
    """

    PREMIUM = "PREMIUM"
    DISCOUNT = "DISCOUNT"
    EQUILIBRIUM = "EQUILIBRIUM"


class LegDirection(StrEnum):
    """Direction of the impulse leg that defines the current dealing range.

    BULLISH = the leg ran low -> high (most recent extreme is the high), so the
    OTE retracement sits in the DISCOUNT half. BEARISH = high -> low, OTE in the
    PREMIUM half. Getting this right is the fix for the reference script's bug,
    which always computed OTE as if the leg were bullish.
    """

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class SwingPoint(BaseModel):
    """A confirmed fractal swing pivot.

    `confirmed_at_index` makes as-of correctness explicit and auditable: a
    real-time observer cannot know this pivot exists until `index + lookback`
    candles have closed. Break detection must never use a swing before this bar.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0, description="Candle index of the pivot in the series.")
    open_time: datetime = Field(description="UTC open time of the pivot candle.")
    price: float = Field(gt=0, description="Pivot price (the high for a high, the low for a low).")
    swing_type: SwingType
    label: SwingLabel | None = Field(
        default=None,
        description="HH/HL/LH/LL vs the previous same-type swing; None for the first.",
    )
    confirmed_at_index: int = Field(
        ge=0,
        description="Index at which this pivot first became confirmable (index + lookback).",
    )


class StructureEvent(BaseModel):
    """A BOS or CHoCH event: a body close beyond a confirmed, unbroken swing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: StructureEventType
    index: int = Field(ge=0, description="Candle index where the breaking close occurred.")
    open_time: datetime = Field(description="UTC open time of the breaking candle.")
    broken_level: float = Field(gt=0, description="Price of the swing that was broken.")
    close_price: float = Field(gt=0, description="Close of the breaking candle.")
    broken_swing_index: int = Field(ge=0, description="Index of the swing pivot that was broken.")


class DealingRange(BaseModel):
    """The current Premium/Discount array with directional OTE band.

    Defined by the most recent confirmed swing high and swing low. The OTE band
    (61.8%-78.6% retracement) is placed in the DISCOUNT half for a bullish leg
    and the PREMIUM half for a bearish leg.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    range_high: float = Field(gt=0)
    range_low: float = Field(gt=0)
    equilibrium: float = Field(gt=0, description="50% midpoint of the range.")
    leg_direction: LegDirection
    ote_lower: float = Field(gt=0, description="Lower bound of the 61.8-78.6% OTE band.")
    ote_upper: float = Field(gt=0, description="Upper bound of the OTE band.")

    @model_validator(mode="after")
    def _validate_geometry(self) -> DealingRange:
        if self.range_high <= self.range_low:
            raise ValueError(f"range_high {self.range_high} must exceed range_low {self.range_low}")
        if self.ote_lower > self.ote_upper:
            raise ValueError(f"ote_lower {self.ote_lower} must be <= ote_upper {self.ote_upper}")
        if not (self.range_low <= self.ote_lower <= self.ote_upper <= self.range_high):
            raise ValueError("OTE band must fall within [range_low, range_high]")
        return self


class StructureAnalysis(BaseModel):
    """Top-level output of the structure layer for one timeframe's candle series."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: MarketPhase
    current_price: float = Field(gt=0, description="Close of the most recent candle.")
    zone: Zone | None = Field(
        default=None,
        description="Premium/Discount/Equilibrium of current price; None if no dealing range.",
    )
    swings: list[SwingPoint] = Field(description="All confirmed swings, oldest first.")
    events: list[StructureEvent] = Field(description="BOS/CHoCH events, in chronological order.")
    dealing_range: DealingRange | None = Field(default=None)
    atr: float | None = Field(
        default=None,
        ge=0,
        description="ATR as of the most recent candle (current volatility). None if too few bars.",
    )
    lookback: int = Field(gt=0, description="Fractal lookback used for swing detection.")


class FVGType(StrEnum):
    """Direction of a Fair Value Gap (3-candle imbalance)."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class FairValueGap(BaseModel):
    """A Fair Value Gap: a 3-candle imbalance where candle 1 and candle 3 do not overlap.

    Bullish FVG: candle1.high < candle3.low (a gap left by an up-displacement).
    Bearish FVG: candle1.low > candle3.high.

    `top`/`bottom` are the gap edges (top > bottom for both directions). The gap is
    *known* once candle 3 closes (`formation_index`), so it is as-of correct. The
    `mitigated`/`filled` flags reflect price action **as of the last candle in the
    series passed to the detector** (i.e. "now") — they look only at candles between
    formation and the present, never beyond.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fvg_type: FVGType
    top: float = Field(gt=0, description="Upper edge of the gap.")
    bottom: float = Field(gt=0, description="Lower edge of the gap.")
    midpoint: float = Field(gt=0, description="Consequent Encroachment (50% of the gap).")
    size: float = Field(gt=0, description="top - bottom, in price units.")
    formation_index: int = Field(ge=2, description="Index of candle 3; the gap is known here.")
    displacement_index: int = Field(ge=1, description="Index of the middle (displacement) candle.")
    open_time: datetime = Field(description="UTC open time of the displacement candle.")
    is_displacement: bool = Field(
        description="Whether the middle candle's body is impulsive (>= the ATR threshold)."
    )
    displacement_atr_multiple: float = Field(
        ge=0, description="Middle-candle body size as a multiple of the as-of ATR (0 if no ATR)."
    )
    mitigated: bool = Field(description="Price has re-entered the gap as of the last candle.")
    filled: bool = Field(description="Price has fully traversed the gap as of the last candle.")
    mitigation_index: int | None = Field(
        default=None, ge=0, description="Index of the first candle that entered the gap, if any."
    )

    @model_validator(mode="after")
    def _validate(self) -> FairValueGap:
        if self.top <= self.bottom:
            raise ValueError(f"top {self.top} must exceed bottom {self.bottom}")
        if not (self.bottom <= self.midpoint <= self.top):
            raise ValueError("midpoint must lie within [bottom, top]")
        if self.filled and not self.mitigated:
            raise ValueError("a filled gap must also be mitigated")
        if self.displacement_index >= self.formation_index:
            raise ValueError("displacement_index must precede formation_index")
        return self


class OrderBlockDirection(StrEnum):
    """Bias of an Order Block (a POI / mitigation zone)."""

    BULLISH = "BULLISH"  # a demand zone — origin of an up-move that broke structure
    BEARISH = "BEARISH"  # a supply zone — origin of a down-move that broke structure


class OrderBlock(BaseModel):
    """An Order Block: the last opposing candle before a displacement that broke structure.

    A bullish OB is the last down-close candle before an impulsive up-move that
    produced a confirmed bullish BOS/CHoCH; the bearish case is the mirror. Anchoring
    to a *confirmed* structure event (rather than scanning for displacement
    independently, as the reference script did) keeps detection as-of correct: the OB
    candle is found by looking back from the break, never forward.

    `zone_high`/`zone_low` are the full candle range of the OB (the POI; SL sits beyond
    `zone_low` for a bullish OB). `confluence_count` is a RAW count of corroborating
    factors (displacement + FVG + still-unmitigated) — NOT a calibrated probability;
    real confidence is assigned by the scoring/assembly step and earned via forward-test.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    direction: OrderBlockDirection
    ob_index: int = Field(ge=0, description="Candle index of the order-block candle.")
    open_time: datetime = Field(description="UTC open time of the order-block candle.")
    zone_high: float = Field(gt=0, description="Top of the OB zone (the candle high).")
    zone_low: float = Field(gt=0, description="Bottom of the OB zone (the candle low).")
    break_index: int = Field(ge=0, description="Index of the structure break this OB caused.")
    break_event_type: StructureEventType = Field(description="The BOS/CHoCH event anchored to.")
    has_displacement: bool = Field(description="Move past the OB was impulsive (>= ATR threshold).")
    displacement_atr_multiple: float = Field(
        ge=0, description="Size of the move past the OB as a multiple of the as-of ATR."
    )
    has_fvg: bool = Field(description="A same-direction FVG formed between the OB and the break.")
    mitigated: bool = Field(description="Price has returned into the zone as of the last candle.")
    mitigation_index: int | None = Field(
        default=None, ge=0, description="First candle (after the break) to tap the zone, if any."
    )
    confluence_count: int = Field(
        ge=0, le=3, description="Raw count of {displacement, fvg, unmitigated}; not a probability."
    )

    @model_validator(mode="after")
    def _validate(self) -> OrderBlock:
        if self.zone_high <= self.zone_low:
            raise ValueError(f"zone_high {self.zone_high} must exceed zone_low {self.zone_low}")
        if self.ob_index >= self.break_index:
            raise ValueError("ob_index must precede break_index")
        if self.mitigated != (self.mitigation_index is not None):
            raise ValueError("mitigation_index must be set iff mitigated is True")
        return self
