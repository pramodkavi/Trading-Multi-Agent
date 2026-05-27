"""Anthropic SDK wrapper with Pydantic-validated structured outputs.

Per SPEC §4 Step 1.6 and §3.3.1 NFR-1.4 / §3.3.2 NFR-2.3:

- Every agent LLM call goes through `structured_completion()` here.
- Structured output uses Anthropic's tool-use mechanism: the caller's Pydantic
  schema is exposed as a single tool's `input_schema`, the API is forced to
  emit a `tool_use` block, and the returned `input` dict is Pydantic-validated.
- If validation fails, we re-prompt with the actual validation errors so
  Claude can self-correct. Up to 3 retries total (configurable) with
  exponential backoff. After exhausting retries, raises StructuredOutputError.
- Transient API errors (rate limit, 5xx, connection) get their own retry
  budget with exponential backoff — separate concern from validation retries.
- Every call returns a StructuredCompletionResult carrying token counts and
  a computed USD cost (lookup table; None for unrecognized models so we don't
  silently report zero).
- A `dry_run` argument bypasses the network entirely and returns the supplied
  fixture — lets every agent test path stay fully offline by default.

What this module deliberately does NOT do:
- Prompt caching (`cache_control`) — deferred to Slice 2 where Skeptic/Judge
  prompts are large enough to benefit. SPEC §4 Step 1.6 does not mandate it.
- Langfuse tracing — Step 1.6 scope. Hooks are added at agent boundaries in
  Step 1.7+ where they have meaningful trace context.
- Streaming — agents need final structured output, not partial token streams.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from anthropic.types import MessageParam, ToolChoiceToolParam, ToolParam

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables — exported as constants so callers / tests can override
# ---------------------------------------------------------------------------

DEFAULT_MODEL: str = "claude-sonnet-4-5"
"""Production model per SPEC §2.1. Override via parameter for tests / future
model bumps. The Critic (SPEC §3.4) might recommend a model change later;
expose this rather than burying it inline."""

DEFAULT_MAX_VALIDATION_RETRIES: int = 3
"""SPEC §3.3.1 NFR-1.4: 'Malformed outputs trigger up to 3 retries with
exponential backoff before failing the agent.' Total attempts = retries + 1.
"""

DEFAULT_MAX_TRANSIENT_RETRIES: int = 3
"""Distinct retry budget for transient API errors (rate-limit, 5xx, network).
Separate so a flaky network doesn't burn the validation-retry budget."""

DEFAULT_BACKOFF_BASE_SECONDS: float = 1.0
"""Initial backoff for exponential retry: base * 2**attempt. attempt 0 -> 1s,
attempt 1 -> 2s, attempt 2 -> 4s, attempt 3 -> 8s."""

DEFAULT_MAX_OUTPUT_TOKENS: int = 4096
"""Anthropic's parameter, not ours. 4096 is comfortably above any structured
agent response in this system. Increase for the Critic's weekly markdown."""


# Approximate USD pricing per million tokens as of 2026-05.
# Keys are model IDs (or model-family prefixes). Values are (input, output).
# Source: Anthropic public pricing. None means 'don't compute cost; we don't
# know the rate' — better than silently reporting $0.
_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-7": (15.0, 75.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    """Raised when the LLM cannot produce a schema-valid output after retries.

    Carries the last validation error and the count of attempts made so the
    caller can log it for debugging. Distinct from anthropic's exception
    hierarchy: agents should `except StructuredOutputError` to handle the
    'model couldn't comply' case separately from transient API failures.
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        last_error: ValidationError | str,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


@dataclass(frozen=True)
class StructuredCompletionResult(Generic[T]):
    """Outcome of one `structured_completion` invocation.

    Carries the validated payload plus the observability data NFR-2.3
    requires. Frozen so downstream code can pass it around without worrying
    about mutation.
    """

    output: T
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    attempts: int
    latency_ms: int


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def structured_completion(
    *,
    output_schema: type[T],
    system: str,
    user: str,
    model: str = DEFAULT_MODEL,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_validation_retries: int = DEFAULT_MAX_VALIDATION_RETRIES,
    max_transient_retries: int = DEFAULT_MAX_TRANSIENT_RETRIES,
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    tool_name: str = "emit_structured_output",
    tool_description: str | None = None,
    dry_run: T | None = None,
    client: AsyncAnthropic | None = None,
) -> StructuredCompletionResult[T]:
    """Call Claude and return a Pydantic-validated structured payload.

    Args:
        output_schema: Pydantic model class the response must validate against.
        system: system prompt for the call.
        user: initial user message.
        model: model ID; defaults to SPEC §2.1's Sonnet 4.5.
        max_output_tokens: Anthropic's max_tokens.
        max_validation_retries: how many times to re-prompt on ValidationError
            (total attempts = this + 1).
        max_transient_retries: how many times to retry transient API errors
            with exponential backoff before giving up.
        backoff_base_seconds: initial backoff; doubles each attempt.
        tool_name: name of the tool we declare; visible to the model.
        tool_description: optional human-language hint for the tool. Defaults
            to the schema's docstring.
        dry_run: if set, skip the network entirely and return this instance.
            Used by tests + by SPEC's `dry_run` requirement for offline testing.
        client: optional pre-built AsyncAnthropic; tests inject a mock.

    Returns:
        StructuredCompletionResult containing the validated output, token
        counts, and cost data.

    Raises:
        StructuredOutputError: validation kept failing after all retries.
        anthropic.APIError (and subclasses): transient errors exhausted retry
            budget, or non-retryable errors (e.g., AuthenticationError).
    """
    if dry_run is not None:
        # No network call, no tokens charged. Return immediately so tests
        # and offline runs are zero-cost. attempts=0 distinguishes from real calls.
        return StructuredCompletionResult[T](
            output=dry_run,
            model=model,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            attempts=0,
            latency_ms=0,
        )

    cli = client or AsyncAnthropic()
    schema_json = output_schema.model_json_schema()
    tool_payload: dict[str, Any] = {
        "name": tool_name,
        "description": tool_description or (output_schema.__doc__ or tool_name).strip(),
        "input_schema": schema_json,
    }
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]

    last_validation_error: ValidationError | None = None
    started_at = time.perf_counter()
    tokens_in_total = 0
    tokens_out_total = 0

    # Total attempts = validation retries + 1 initial try.
    for attempt in range(max_validation_retries + 1):
        response = await _call_with_transient_retry(
            client=cli,
            model=model,
            max_output_tokens=max_output_tokens,
            system=system,
            messages=messages,
            tool=tool_payload,
            max_transient_retries=max_transient_retries,
            backoff_base_seconds=backoff_base_seconds,
        )

        tokens_in_total += response.usage.input_tokens
        tokens_out_total += response.usage.output_tokens

        raw_input = _extract_tool_input(response, tool_name)
        if raw_input is None:
            # Model didn't emit the tool — unusual when tool_choice forces it,
            # but possible with malformed prompts. Treat as a validation
            # failure so the retry loop kicks in.
            validation_msg = (
                f"Model did not emit the required `{tool_name}` tool. "
                "Re-prompting with that requirement."
            )
            messages = _append_correction(messages, response, validation_msg)
            last_validation_error = validation_msg  # type: ignore[assignment]
            continue

        try:
            validated = output_schema.model_validate(raw_input)
        except ValidationError as exc:
            last_validation_error = exc
            messages = _append_correction(
                messages,
                response,
                _format_validation_error(exc),
            )
            continue

        latency_ms = int((time.perf_counter() - started_at) * 1000)
        return StructuredCompletionResult[T](
            output=validated,
            model=model,
            tokens_in=tokens_in_total,
            tokens_out=tokens_out_total,
            cost_usd=_compute_cost_usd(model, tokens_in_total, tokens_out_total),
            attempts=attempt + 1,
            latency_ms=latency_ms,
        )

    # Exhausted retries.
    assert last_validation_error is not None  # always set by the loop
    raise StructuredOutputError(
        f"Structured output failed validation after {max_validation_retries + 1} attempts",
        attempts=max_validation_retries + 1,
        last_error=last_validation_error,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _call_with_transient_retry(
    *,
    client: AsyncAnthropic,
    model: str,
    max_output_tokens: int,
    system: str,
    messages: list[dict[str, Any]],
    tool: dict[str, Any],
    max_transient_retries: int,
    backoff_base_seconds: float,
) -> Any:  # anthropic.types.Message — typed as Any to avoid SDK version coupling
    """Wrap the Anthropic call with exponential backoff on transient errors.

    Retries on APIConnectionError, RateLimitError, APIStatusError with 5xx
    status. Re-raises auth errors / 4xx immediately — those are bugs, not
    network blips. Backoff schedule: 1s, 2s, 4s, 8s ...
    """
    for attempt in range(max_transient_retries + 1):
        try:
            return await client.messages.create(
                model=model,
                max_tokens=max_output_tokens,
                system=system,
                messages=cast("list[MessageParam]", messages),
                tools=[cast("ToolParam", tool)],
                tool_choice=cast(
                    "ToolChoiceToolParam",
                    {"type": "tool", "name": tool["name"]},
                ),
            )
        except (anthropic.APIConnectionError, anthropic.RateLimitError) as exc:
            if attempt == max_transient_retries:
                raise
            wait = backoff_base_seconds * (2**attempt)
            logger.warning(
                "Anthropic transient error %s; retrying in %.1fs (attempt %d/%d)",
                type(exc).__name__,
                wait,
                attempt + 1,
                max_transient_retries,
            )
            await asyncio.sleep(wait)
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 and attempt < max_transient_retries:
                wait = backoff_base_seconds * (2**attempt)
                logger.warning(
                    "Anthropic 5xx (%d); retrying in %.1fs (attempt %d/%d)",
                    exc.status_code,
                    wait,
                    attempt + 1,
                    max_transient_retries,
                )
                await asyncio.sleep(wait)
                continue
            raise  # 4xx or budget exhausted — caller handles

    # Unreachable; loop either returns or raises.
    raise RuntimeError("transient retry loop exited without return or raise")  # pragma: no cover


def _extract_tool_input(response: Any, tool_name: str) -> Mapping[str, Any] | None:
    """Pull out the dict argument the model passed to our tool, or None.

    Anthropic's response.content is a list of typed blocks; the tool_use
    block has `type='tool_use'`, `name`, and `input` (the structured args).
    """
    for block in response.content:
        # tool_use blocks have .type == "tool_use" and .input is a dict-like
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            return getattr(block, "input", None)
    return None


def _format_validation_error(exc: ValidationError) -> str:
    """Turn a Pydantic ValidationError into a follow-up prompt fragment.

    Lists each error's location and message so the model has a concrete
    correction signal. Truncated to a reasonable length; full errors stay
    on the StructuredOutputError if all retries fail.
    """
    lines: list[str] = []
    for err in exc.errors()[:10]:
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"- {loc}: {err['msg']}")
    bullets = "\n".join(lines)
    return (
        "Your previous tool call did not pass validation. "
        "Please correct the following errors and call the tool again:\n"
        f"{bullets}"
    )


def _append_correction(
    messages: list[dict[str, Any]],
    response: Any,
    correction: str,
) -> list[dict[str, Any]]:
    """Build a new messages list that includes the rejected assistant turn
    followed by our correction request. Preserves the conversation so the
    model sees what it produced AND why we rejected it.
    """
    # Round-trip the assistant's previous content back into the conversation.
    assistant_content = [
        {
            "type": getattr(block, "type", "text"),
            **(
                {"text": getattr(block, "text", "")}
                if getattr(block, "type", None) == "text"
                else {}
            ),
            **(
                {
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}),
                }
                if getattr(block, "type", None) == "tool_use"
                else {}
            ),
        }
        for block in response.content
    ]
    new_messages = list(messages)
    new_messages.append({"role": "assistant", "content": assistant_content})
    new_messages.append({"role": "user", "content": correction})
    return new_messages


def _compute_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Look up per-MTok pricing and compute USD cost; None on unknown model.

    Returning None (not 0.0) for unknown models avoids silently reporting
    zero cost when the table is stale. Caller can decide whether to log or
    proceed.
    """
    rates = _PRICING_USD_PER_MTOK.get(model)
    if rates is None:
        # Best-effort prefix match: e.g., 'claude-sonnet-4-5-20260514' matches 'claude-sonnet-4-5'.
        for key, value in _PRICING_USD_PER_MTOK.items():
            if model.startswith(key):
                rates = value
                break
    if rates is None:
        logger.info("Unknown model '%s'; cost_usd will be None", model)
        return None
    rate_in, rate_out = rates
    return (tokens_in / 1_000_000) * rate_in + (tokens_out / 1_000_000) * rate_out
