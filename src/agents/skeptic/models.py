"""Pydantic output model for the Skeptic agent (Slice 2 Step 2.5).

The Skeptic independently fetches macro / cross-asset context the Analyzer
never saw (DXY, rates, equities, volatility) and tries to *invalidate* the
proposal (SPEC §3.1 role 3 / FR-1.5). Its structured output is a
``SkepticObjection`` -- the single strongest objection plus a severity rating
and concrete reasoning citing specific macro data points.

This is the Pydantic schema the LLM is forced to emit (via
``src.common.llm.structured_completion``), so the field set is deliberately
small and self-describing: the field descriptions double as instructions to
the model. It is a boundary type consumed by the Judge (Step 2.6) and surfaced
in the Telegram alert (FR-5.2 "Skeptic objection, if any"), so it is frozen and
forbids extra fields.

When macro data is *unavailable* the Skeptic does NOT emit a low-severity
objection -- it returns the provider-level ``NoMacroData`` sentinel instead
(FR-4.3), which the Judge reads as "downgrade confidence to medium". So a
``SkepticObjection`` always reflects a real (if partial) macro picture.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.common.models import ObjectionSeverity


class SkepticObjection(BaseModel):
    """The Skeptic's strongest macro / cross-asset objection to a proposal.

    The Skeptic's job is adversarial: even for a strong setup it articulates the
    *best available* objection and rates it honestly. A weak objection is
    expressed as ``severity=LOW`` with ``recommends_against=False`` -- not as a
    fabricated headwind. ``cited_macro`` keeps the model honest by forcing it to
    point at the specific data it reasoned from.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    severity: ObjectionSeverity = Field(
        description="LOW / MEDIUM / HIGH -- how strongly the macro context argues "
        "against this proposal. Reserve HIGH for a clear, strong contradiction; "
        "most single-snapshot macro objections are LOW or MEDIUM.",
    )
    recommends_against: bool = Field(
        description="True if, on balance, the macro / cross-asset picture argues "
        "against taking this trade now; False if macro is broadly neutral or "
        "supportive and the objection is merely the best available.",
    )
    headline: str = Field(
        min_length=5,
        max_length=200,
        description="One-line statement of the single strongest objection. Used "
        "verbatim as the caveat line in the Telegram alert (FR-5.2). No hedging "
        "preamble -- state the concern directly.",
    )
    reasoning: str = Field(
        min_length=20,
        max_length=2000,
        description="Full reasoning for the objection and severity, citing the "
        "SPECIFIC macro data points provided. Reason qualitatively about the "
        "cross-asset risk regime; never reference forbidden indicators (RSI, "
        "MACD, Bollinger, moving averages) and never invent data not supplied.",
    )
    cited_macro: list[str] = Field(
        default_factory=list,
        description="The specific macro data points the objection rests on, each "
        "as a short phrase (e.g. 'broad USD index 104.2', 'volatility proxy "
        "elevated'). Empty only if no macro field was usable.",
    )
