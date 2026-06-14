"""Pydantic output model for the Judge agent (Slice 2 Step 2.6).

The Judge is the final arbiter of the per-signal pipeline (SPEC §3.1 role 4 /
FR-1.6). It consumes three inputs -- the Analyzer's ``SignalProposal``, the
Historian's empirical ``HistorianReport``, and the Skeptic's ``SkepticObjection``
(or ``NoMacroData``) -- and produces exactly one ruling: PUBLISH,
PUBLISH_WITH_CAVEAT, or SKIP. Every ruling carries written reasoning that is
appended to the journal (FR-1.6 / FR-1.7).

``JudgeDecision`` is the schema the LLM is forced to emit via
``structured_completion``; the field descriptions double as instructions to the
model. It is a boundary type the Telegram dispatcher (FR-5.2) and the dashboard
(FR-7.2) read, so it is frozen and forbids extra fields.

The hard numeric risk rules (§1.6: 1% risk, 1:3 R:R, leverage cap, ...) are NOT
the Judge's job -- they are enforced programmatically in risk_gates.py
(Step 2.11). The Judge is the *qualitative* arbiter that weighs structural
quality against empirical track record and macro objection.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.common.models import JudgeConfidence, JudgeRuling


class JudgeDecision(BaseModel):
    """The Judge's terminal ruling on a proposal, with written reasoning.

    ``confidence`` is the Judge's confidence in the ruling, distinct from the
    ruling itself (one can SKIP at HIGH confidence or PUBLISH at LOW). Per FR-4.3
    it is capped at MEDIUM when the Skeptic could not fetch macro context. The
    ``caveat`` is the one-line warning shown in the Telegram alert and is
    required exactly when the ruling is PUBLISH_WITH_CAVEAT.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    ruling: JudgeRuling = Field(
        description="PUBLISH (send as a high-conviction signal), "
        "PUBLISH_WITH_CAVEAT (send, prefixed with the caveat; recipient takes "
        "reduced size), or SKIP (do not publish; reasoning still journaled).",
    )
    confidence: JudgeConfidence = Field(
        description="LOW / MEDIUM / HIGH confidence in this ruling. Cap at MEDIUM "
        "when macro context was unavailable. Small or absent historian samples "
        "should also temper confidence.",
    )
    reasoning: str = Field(
        min_length=20,
        max_length=2000,
        description="The written rationale for the ruling, citing the SPECIFIC "
        "inputs you weighed: the proposal's R:R and structure, the historian's "
        "empirical win rate AND sample size, and the skeptic's objection "
        "severity. This text is journaled and may be shown to the user.",
    )
    caveat: str | None = Field(
        default=None,
        max_length=200,
        description="One-line warning for the Telegram alert. REQUIRED when "
        "ruling is PUBLISH_WITH_CAVEAT (usually the skeptic's objection in brief); "
        "leave null for PUBLISH and SKIP.",
    )

    @model_validator(mode="after")
    def _caveat_matches_ruling(self) -> JudgeDecision:
        """PUBLISH_WITH_CAVEAT must carry a non-empty caveat; nothing else needs one.

        Enforced at the schema boundary so a malformed ruling triggers a
        structured-output retry (the LLM self-corrects) rather than silently
        publishing a 'with caveat' signal that has no caveat to show.
        """
        if self.ruling is JudgeRuling.PUBLISH_WITH_CAVEAT and not (
            self.caveat and self.caveat.strip()
        ):
            raise ValueError("ruling PUBLISH_WITH_CAVEAT requires a non-empty caveat")
        return self
