"""LlmUsage: the per-call LLM observability record carried through agent state.

Slice 4 Step 4.0 (design §5.2 prerequisite) / SPEC §3.3.2 NFR-2.3. Every
``structured_completion`` call already returns a ``StructuredCompletionResult``
with token counts, a computed USD cost, and wall-clock latency -- but the
Skeptic and Judge used to return only ``.output``, so ``agent_runs`` was written
with ``latency_ms=0`` / ``token_usage={}`` / ``cost_usd=NULL``. This model is
the Pydantic carrier that moves that metadata out of the agent, through the
LangGraph ``AgentState`` (``skeptic_usage`` / ``judge_usage``), and into the
``store.log_agent_run`` call in the scan runner. The dashboard's per-step and
per-day cost views read the resulting columns.

It is a *separate* type from ``StructuredCompletionResult`` on purpose: that
dataclass is generic over the output payload and lives next to the Anthropic
wrapper; this model is payload-free, Pydantic (so it checkpoints like every
other ``AgentState`` value), and knows how the columns are split.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.common.llm import StructuredCompletionResult


class LlmUsage(BaseModel):
    """Tokens, cost, latency and attempt count for one agent's LLM call.

    ``cost_usd`` is ``None`` when ``structured_completion`` could not price the
    model (unknown model id). It must stay ``None`` all the way to the row --
    SQL NULL means "unknown", whereas 0.0 would silently under-report spend.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(description="Model id the call was made with (e.g. claude-sonnet-4-5).")
    tokens_in: int = Field(ge=0, description="Input tokens summed across validation retries.")
    tokens_out: int = Field(ge=0, description="Output tokens summed across validation retries.")
    cost_usd: float | None = Field(
        description="Computed USD cost; None when the model's pricing is unknown (-> SQL NULL)."
    )
    latency_ms: int = Field(ge=0, description="Wall-clock milliseconds for the whole call.")
    attempts: int = Field(
        ge=0, description="API round-trips made (0 for a dry-run; >1 after validation retries)."
    )

    @classmethod
    def from_result(cls, result: StructuredCompletionResult[Any]) -> LlmUsage:
        """Copy the observability fields off a ``StructuredCompletionResult``."""
        return cls(
            model=result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            attempts=result.attempts,
        )

    def token_usage(self) -> dict[str, Any]:
        """The ``agent_runs.token_usage`` JSONB payload.

        Cost and latency are deliberately excluded: they have their own typed
        columns (``cost_usd``, ``latency_ms``) so the dashboard can aggregate
        them in SQL without unpacking JSON.
        """
        return {
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "attempts": self.attempts,
        }
