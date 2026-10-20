"""Unit tests for LlmUsage (Slice 4 Step 4.0, design §5.2).

The carrier that moves a StructuredCompletionResult's observability data
(NFR-2.3) from the Skeptic / Judge through LangGraph state to agent_runs.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from src.common.llm import StructuredCompletionResult
from src.common.models import LlmUsage


class _Out(BaseModel):
    x: int = 1


def test_from_result_copies_every_field() -> None:
    result = StructuredCompletionResult[_Out](
        output=_Out(),
        model="claude-sonnet-4-5",
        tokens_in=100,
        tokens_out=40,
        cost_usd=0.0009,
        attempts=1,
        latency_ms=1234,
    )
    usage = LlmUsage.from_result(result)
    assert usage == LlmUsage(
        model="claude-sonnet-4-5",
        tokens_in=100,
        tokens_out=40,
        cost_usd=0.0009,
        attempts=1,
        latency_ms=1234,
    )


def test_from_result_preserves_none_cost() -> None:
    # Unknown model -> structured_completion reports cost_usd=None; the carrier
    # must keep None (-> SQL NULL) rather than coercing to 0.0.
    result = StructuredCompletionResult[_Out](
        output=_Out(),
        model="claude-unknown-9",
        tokens_in=1,
        tokens_out=1,
        cost_usd=None,
        attempts=1,
        latency_ms=3,
    )
    assert LlmUsage.from_result(result).cost_usd is None


def test_token_usage_payload_excludes_cost_and_latency() -> None:
    usage = LlmUsage(model="m", tokens_in=1, tokens_out=2, cost_usd=None, attempts=1, latency_ms=5)
    assert usage.token_usage() == {"model": "m", "tokens_in": 1, "tokens_out": 2, "attempts": 1}


def test_is_frozen() -> None:
    usage = LlmUsage(model="m", tokens_in=1, tokens_out=2, cost_usd=None, attempts=1, latency_ms=5)
    with pytest.raises(ValidationError):
        usage.tokens_in = 9  # type: ignore[misc]


@pytest.mark.parametrize("field", ["tokens_in", "tokens_out", "latency_ms", "attempts"])
def test_counters_reject_negatives(field: str) -> None:
    base = {
        "model": "m",
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": None,
        "attempts": 0,
        "latency_ms": 0,
    }
    with pytest.raises(ValidationError):
        LlmUsage(**{**base, field: -1})
