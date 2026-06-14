"""Pydantic output model for the Forecaster agent (Slice 2 Step 2.9).

The Forecaster re-evaluates each OPEN setup every scan (SPEC §3.1.2 FR-2.1) and
emits a ``ForecasterUpdate``: a verdict (STILL_VALID / AT_RISK / INVALIDATED)
plus reasoning, and -- when it closes a setup -- the terminal ``SignalOutcome``
to log on the journal.

This is the schema the LLM is forced to emit via ``structured_completion``; the
field descriptions double as instructions. It is frozen and forbids extras
because the Telegram update (FR-5.3) and the dashboard read it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.common.models import ForecastStatus, SignalOutcome


class ForecasterUpdate(BaseModel):
    """The Forecaster's verdict on one open setup.

    ``outcome`` is required exactly when ``status`` is INVALIDATED (the terminal
    result to stamp on the signal -- WIN if the target was reached, LOSS if the
    stop was hit, INVALIDATED if the structural premise broke, EXPIRED if the
    setup never resolved in its window) and must be absent otherwise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    status: ForecastStatus = Field(
        description="STILL_VALID (on track, no action), AT_RISK (premise "
        "threatened -- warn the operator), or INVALIDATED (resolved or broken -- "
        "close it and set `outcome`).",
    )
    reasoning: str = Field(
        min_length=20,
        max_length=2000,
        description="Why this verdict, citing the SPECIFIC current price levels "
        "versus the setup's entry / stop / target. Never reference forbidden "
        "indicators (RSI, MACD, Bollinger, moving averages); never invent data.",
    )
    outcome: SignalOutcome | None = Field(
        default=None,
        description="REQUIRED when status is INVALIDATED: the terminal outcome to "
        "log (WIN / LOSS / BREAKEVEN / INVALIDATED / EXPIRED). Leave null for "
        "STILL_VALID and AT_RISK.",
    )

    @model_validator(mode="after")
    def _outcome_matches_status(self) -> ForecasterUpdate:
        """A close (INVALIDATED) must carry an outcome; nothing else may.

        Enforced at the boundary so a malformed verdict triggers a
        structured-output retry rather than a setup closed with no logged result
        (or a live setup spuriously carrying one).
        """
        if self.status is ForecastStatus.INVALIDATED and self.outcome is None:
            raise ValueError("status INVALIDATED requires an outcome to log")
        if self.status is not ForecastStatus.INVALIDATED and self.outcome is not None:
            raise ValueError("outcome may only be set when status is INVALIDATED")
        return self
