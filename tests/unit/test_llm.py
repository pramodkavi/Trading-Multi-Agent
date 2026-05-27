"""Tests for src.common.llm.structured_completion with mocked Anthropic SDK.

Coverage per SPEC §4 Step 1.6 acceptance:
- success on first call -> validated output + cost tracking
- ValidationError on first call -> retry -> success on second
- ValidationError on every call -> StructuredOutputError raised
- dry_run -> returns fixture without touching the network
- transient error (RateLimitError, 5xx) -> exponential backoff retry
- non-retryable error (auth) -> raised immediately
- cost calculation: known model -> dollar amount; unknown model -> None
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest
from pydantic import BaseModel, Field

from src.common.llm import (
    DEFAULT_MAX_VALIDATION_RETRIES,
    StructuredCompletionResult,
    StructuredOutputError,
    _compute_cost_usd,
    structured_completion,
)

# ---------------------------------------------------------------------------
# Test schema and mock helpers
# ---------------------------------------------------------------------------


class _Sample(BaseModel):
    """A minimal Pydantic model used as the structured-output schema in tests."""

    decision: str = Field(min_length=1, max_length=20)
    confidence: float = Field(ge=0.0, le=1.0)


def _fake_tool_use_response(
    *,
    tool_name: str = "emit_structured_output",
    input_payload: dict[str, Any] | None = None,
    tokens_in: int = 100,
    tokens_out: int = 50,
) -> SimpleNamespace:
    """Build a mock response that mimics anthropic.types.Message shape we read."""
    block = SimpleNamespace(
        type="tool_use",
        name=tool_name,
        id="toolu_abc",
        input=input_payload if input_payload is not None else {"decision": "ok", "confidence": 0.7},
    )
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out),
    )


def _make_mock_client(responses: list[Any]) -> MagicMock:
    """Wire an AsyncAnthropic mock whose .messages.create yields the given sequence."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    return client


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


class TestSuccess:
    async def test_first_call_validates_returns_result(self) -> None:
        client = _make_mock_client([_fake_tool_use_response()])
        result = await structured_completion(
            output_schema=_Sample,
            system="sys",
            user="hello",
            client=client,
        )
        assert isinstance(result, StructuredCompletionResult)
        assert result.output.decision == "ok"
        assert result.output.confidence == 0.7
        assert result.attempts == 1
        client.messages.create.assert_awaited_once()

    async def test_token_counts_and_cost_recorded(self) -> None:
        client = _make_mock_client([_fake_tool_use_response(tokens_in=1000, tokens_out=500)])
        result = await structured_completion(
            output_schema=_Sample,
            system="sys",
            user="hello",
            model="claude-sonnet-4-5",
            client=client,
        )
        assert result.tokens_in == 1000
        assert result.tokens_out == 500
        # claude-sonnet-4-5: $3/MTok in, $15/MTok out
        # 1000/1e6 * 3 + 500/1e6 * 15 = 0.003 + 0.0075 = 0.0105
        assert result.cost_usd == pytest.approx(0.0105)

    async def test_unknown_model_cost_is_none(self) -> None:
        client = _make_mock_client([_fake_tool_use_response()])
        result = await structured_completion(
            output_schema=_Sample,
            system="sys",
            user="hello",
            model="claude-future-model-9",
            client=client,
        )
        assert result.cost_usd is None

    async def test_latency_recorded(self) -> None:
        client = _make_mock_client([_fake_tool_use_response()])
        result = await structured_completion(
            output_schema=_Sample,
            system="sys",
            user="hello",
            client=client,
        )
        assert result.latency_ms >= 0


# ---------------------------------------------------------------------------
# Validation retry
# ---------------------------------------------------------------------------


class TestValidationRetry:
    async def test_invalid_then_valid_succeeds_in_two_attempts(self) -> None:
        # First response: confidence out of range; second: corrected.
        client = _make_mock_client(
            [
                _fake_tool_use_response(input_payload={"decision": "ok", "confidence": 5.0}),
                _fake_tool_use_response(input_payload={"decision": "ok", "confidence": 0.5}),
            ]
        )
        result = await structured_completion(
            output_schema=_Sample,
            system="sys",
            user="hello",
            client=client,
        )
        assert result.attempts == 2
        assert result.output.confidence == 0.5
        assert client.messages.create.await_count == 2

    async def test_token_counts_accumulate_across_retries(self) -> None:
        client = _make_mock_client(
            [
                _fake_tool_use_response(
                    input_payload={"decision": "ok", "confidence": 5.0},
                    tokens_in=100,
                    tokens_out=20,
                ),
                _fake_tool_use_response(
                    input_payload={"decision": "ok", "confidence": 0.5},
                    tokens_in=120,
                    tokens_out=25,
                ),
            ]
        )
        result = await structured_completion(
            output_schema=_Sample,
            system="sys",
            user="hello",
            client=client,
        )
        assert result.tokens_in == 220
        assert result.tokens_out == 45

    async def test_correction_message_appended_with_validation_errors(self) -> None:
        # Verify our re-prompt actually quotes the validation issue.
        client = _make_mock_client(
            [
                _fake_tool_use_response(input_payload={"decision": "ok", "confidence": 5.0}),
                _fake_tool_use_response(input_payload={"decision": "ok", "confidence": 0.5}),
            ]
        )
        await structured_completion(
            output_schema=_Sample,
            system="sys",
            user="hello",
            client=client,
        )
        # Second call's messages should include the correction prompt.
        second_call_kwargs = client.messages.create.await_args_list[1].kwargs
        second_messages = second_call_kwargs["messages"]
        last_user_msg = second_messages[-1]
        assert last_user_msg["role"] == "user"
        assert "did not pass validation" in last_user_msg["content"]
        assert "confidence" in last_user_msg["content"]

    async def test_exhausted_retries_raises_structured_output_error(self) -> None:
        # All 4 attempts (initial + 3 retries) return invalid output.
        bad = _fake_tool_use_response(input_payload={"decision": "ok", "confidence": 99.0})
        client = _make_mock_client([bad] * (DEFAULT_MAX_VALIDATION_RETRIES + 1))
        with pytest.raises(StructuredOutputError) as exc_info:
            await structured_completion(
                output_schema=_Sample,
                system="sys",
                user="hello",
                client=client,
            )
        assert exc_info.value.attempts == DEFAULT_MAX_VALIDATION_RETRIES + 1
        assert client.messages.create.await_count == DEFAULT_MAX_VALIDATION_RETRIES + 1

    async def test_missing_tool_use_block_triggers_retry(self) -> None:
        # Response with only a text block, no tool_use. Adapter should treat
        # this as a validation failure and re-prompt.
        text_only = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="I refuse to use the tool")],
            usage=SimpleNamespace(input_tokens=20, output_tokens=10),
        )
        good = _fake_tool_use_response()
        client = _make_mock_client([text_only, good])
        result = await structured_completion(
            output_schema=_Sample,
            system="sys",
            user="hello",
            client=client,
        )
        assert result.attempts == 2


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class TestDryRun:
    async def test_dry_run_returns_fixture_without_network_call(self) -> None:
        fixture = _Sample(decision="dry", confidence=0.42)
        # Pass a client mock that would explode if called.
        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = AsyncMock(side_effect=AssertionError("network should not be hit"))

        result = await structured_completion(
            output_schema=_Sample,
            system="sys",
            user="hello",
            client=client,
            dry_run=fixture,
        )
        assert result.output is fixture
        assert result.attempts == 0
        assert result.tokens_in == 0
        assert result.tokens_out == 0
        assert result.cost_usd == 0.0
        client.messages.create.assert_not_awaited()

    async def test_dry_run_does_not_require_a_client(self) -> None:
        fixture = _Sample(decision="dry", confidence=0.5)
        # Mock the AsyncAnthropic constructor so the default-client path
        # doesn't actually instantiate (which would require an API key).
        with patch("src.common.llm.AsyncAnthropic") as mock_ctor:
            result = await structured_completion(
                output_schema=_Sample,
                system="sys",
                user="hello",
                dry_run=fixture,
            )
            mock_ctor.assert_not_called()
        assert result.output.decision == "dry"


# ---------------------------------------------------------------------------
# Transient-error retry with backoff
# ---------------------------------------------------------------------------


def _rate_limit_error() -> anthropic.RateLimitError:
    """Construct a RateLimitError without needing a real HTTP response."""
    return anthropic.RateLimitError(
        message="rate limited",
        response=httpx.Response(status_code=429, request=httpx.Request("POST", "https://x")),
        body=None,
    )


def _api_status_500() -> anthropic.APIStatusError:
    return anthropic.APIStatusError(
        message="server error",
        response=httpx.Response(status_code=500, request=httpx.Request("POST", "https://x")),
        body=None,
    )


def _api_auth_error() -> anthropic.AuthenticationError:
    return anthropic.AuthenticationError(
        message="bad key",
        response=httpx.Response(status_code=401, request=httpx.Request("POST", "https://x")),
        body=None,
    )


class TestTransientRetry:
    async def test_rate_limit_then_success(self) -> None:
        client = _make_mock_client([_rate_limit_error(), _fake_tool_use_response()])
        with patch("src.common.llm.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            result = await structured_completion(
                output_schema=_Sample,
                system="sys",
                user="hello",
                client=client,
                backoff_base_seconds=0.0,  # also keep tests instant
            )
        assert result.attempts == 1
        # asyncio.sleep was called once between the two HTTP attempts.
        mock_sleep.assert_awaited_once()

    async def test_five_hundred_then_success(self) -> None:
        client = _make_mock_client([_api_status_500(), _fake_tool_use_response()])
        with patch("src.common.llm.asyncio.sleep", new=AsyncMock()):
            result = await structured_completion(
                output_schema=_Sample,
                system="sys",
                user="hello",
                client=client,
                backoff_base_seconds=0.0,
            )
        assert result.attempts == 1
        assert client.messages.create.await_count == 2

    async def test_auth_error_not_retried(self) -> None:
        client = _make_mock_client([_api_auth_error()])
        with (
            patch("src.common.llm.asyncio.sleep", new=AsyncMock()),
            pytest.raises(anthropic.AuthenticationError),
        ):
            await structured_completion(
                output_schema=_Sample,
                system="sys",
                user="hello",
                client=client,
            )
        assert client.messages.create.await_count == 1

    async def test_transient_retry_budget_exhausted(self) -> None:
        # 4 attempts (initial + 3 retries) all 5xx -> raises.
        client = _make_mock_client([_api_status_500()] * 4)
        with (
            patch("src.common.llm.asyncio.sleep", new=AsyncMock()),
            pytest.raises(anthropic.APIStatusError),
        ):
            await structured_completion(
                output_schema=_Sample,
                system="sys",
                user="hello",
                client=client,
                max_transient_retries=3,
            )
        assert client.messages.create.await_count == 4


# ---------------------------------------------------------------------------
# Cost calculation unit tests
# ---------------------------------------------------------------------------


class TestCostCalculation:
    def test_exact_model_lookup(self) -> None:
        # 1M input + 1M output for sonnet-4-5 = $3 + $15 = $18
        cost = _compute_cost_usd("claude-sonnet-4-5", 1_000_000, 1_000_000)
        assert cost == pytest.approx(18.0)

    def test_prefix_match_for_dated_variant(self) -> None:
        # 'claude-sonnet-4-5-20250929' should match 'claude-sonnet-4-5' prefix.
        cost = _compute_cost_usd("claude-sonnet-4-5-20250929", 1_000_000, 0)
        assert cost == pytest.approx(3.0)

    def test_unknown_model_returns_none(self) -> None:
        assert _compute_cost_usd("totally-made-up-model", 1000, 1000) is None

    def test_zero_tokens(self) -> None:
        assert _compute_cost_usd("claude-sonnet-4-5", 0, 0) == 0.0

    def test_opus_pricing(self) -> None:
        # Opus 4.7: $15 in, $75 out -> 1k each = 0.015 + 0.075 = 0.09
        cost = _compute_cost_usd("claude-opus-4-7", 1_000_000, 1_000_000)
        assert cost == pytest.approx(90.0)
