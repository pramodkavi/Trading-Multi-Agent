"""Local end-to-end scan runner (Slice 1 Step 1.12).

Wires every Slice 1 component into one pass and proves the production stack
works against real external services:

    config (Step 1.11)        -> load Settings from .env
    Binance (Step 1.4)        -> fetch 4H klines for one symbol
    LangGraph (Step 1.7)      -> run the analyzer-only graph
    Anthropic (Step 1.6)      -> a LIVE Claude Sonnet 4.5 call generates an
                                 analyst commentary on the scan result
    Postgres (Steps 1.8-1.9)  -> persist scan_runs + signals + agent_runs
    Telegram (Step 1.10)      -> deliver the result to the operator's phone

LLM placement note:
    The Slice 1 analyzer is pure-Python HTF bias detection (Step 1.5) -- it
    does not call the LLM. To satisfy the Step 1.12 requirement of a real
    Anthropic call end-to-end, the runner makes one structured_completion
    call AFTER the graph runs, producing a short analyst note. The graph
    itself stays "analyzer only" per the spec wording. Slice 2 promotes the
    LLM into proper agent nodes (Skeptic, Judge); the commentary here is the
    Slice 1 stand-in and is logged to agent_runs like any other agent call.

Telegram trigger:
    A message is sent on BOTH outcomes (published proposal OR skip) so the
    operator always sees the end-to-end result regardless of market state.

Usage:
    # .env must provide ANTHROPIC_API_KEY, TELEGRAM_*, DATABASE_URL.
    python scripts/run_scan.py                 # first symbol in SCAN_SYMBOLS
    python scripts/run_scan.py --symbol ETHUSDT
    python scripts/run_scan.py --no-notify     # skip Telegram (DB + LLM only)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from src.agents.orchestration import run_scan
from src.common.llm import structured_completion
from src.common.models import (
    AgentRole,
    ScanContext,
    ScanSession,
    SignalProposal,
    SkipDecision,
)
from src.config import get_settings
from src.notifications import (
    TelegramNotifier,
    escape_markdown_v2,
    format_new_signal,
    format_skip,
)
from src.persistence import create_store
from src.providers import BinanceProvider, Timeframe

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from src.agents.orchestration import AgentState
    from src.common.llm import StructuredCompletionResult
    from src.config import Settings
    from src.notifications import Notifier
    from src.persistence import SignalStore
    from src.providers import DataProvider

logger = logging.getLogger(__name__)

# Fetch enough 4H history for the analyzer's swing-pivot detection
# (MIN_KLINES_REQUIRED=30 plus comfortable headroom for the pivot window).
CANDLE_LIMIT: int = 200


# ---------------------------------------------------------------------------
# LLM commentary schema + prompt
# ---------------------------------------------------------------------------


class MarketCommentary(BaseModel):
    """Structured analyst note produced by a live Claude call.

    Deliberately small: this is the Slice 1 LLM touchpoint, not the full
    Skeptic/Judge reasoning that arrives in Slice 2. Field length bounds keep
    the Telegram message tidy and the token cost negligible.
    """

    commentary: str = Field(
        min_length=10,
        max_length=600,
        description="One to three sentences of plain-English analysis of the "
        "scan result. No markdown; the caller escapes it for Telegram.",
    )
    key_risk: str = Field(
        min_length=3,
        max_length=300,
        description="The single most important risk or caveat for this setup "
        "(or, on a skip, why acting now would be premature).",
    )


ANALYST_SYSTEM_PROMPT: str = (
    "You are a concise Smart Money Concepts (SMC) trading analyst. You are "
    "given the structured result of an automated 4H scan for one crypto "
    "perpetual. Write a brief, sober analyst note. Do not invent price levels "
    "or data not present in the input. Do not use markdown formatting. Be "
    "direct and avoid hype. This is a signal-only system; never tell the user "
    "to place a trade."
)


def _summarise_state_for_llm(symbol: str, state: AgentState) -> str:
    """Render the scan outcome into a compact prompt for the analyst LLM call."""
    proposal = state.get("proposal")
    decision = state.get("decision")
    lines: list[str] = [
        f"Symbol: {symbol}",
        f"Decision: {decision.value if decision is not None else 'UNKNOWN'}",
    ]

    snapshot = state.get("snapshot")
    if snapshot is not None:
        candles = snapshot.klines.get(Timeframe.H4)
        if candles:
            latest = candles[-1]
            lines.append(f"Latest 4H close: {latest.close}")
            lines.append(f"4H candles analysed: {len(candles)}")

    if isinstance(proposal, SignalProposal):
        lines.extend(
            [
                f"Direction: {proposal.direction.value}",
                f"Entry: {proposal.entry_price}",
                f"Stop loss: {proposal.stop_loss}",
                f"Take profit 1: {proposal.take_profit_1}",
                f"Risk:reward: 1:{proposal.risk_reward_ratio:.1f}",
                f"Tags: {', '.join(proposal.tags) if proposal.tags else '(none)'}",
                f"Strategy narrative: {proposal.confluence_narrative}",
            ]
        )
    elif isinstance(proposal, SkipDecision):
        lines.extend(
            [
                f"Skip reason: {proposal.reason.value}",
                f"Skip details: {proposal.details}",
            ]
        )

    return "\n".join(lines)


async def generate_commentary(
    *,
    settings: Settings,
    symbol: str,
    state: AgentState,
    client: AsyncAnthropic | None = None,
) -> StructuredCompletionResult[MarketCommentary]:
    """Make one live Anthropic call to produce an analyst note on the scan.

    The client is constructed from the configured API key unless a caller
    injects one (tests pass a mock). structured_completion handles the
    tool-use schema enforcement, validation retries, and cost accounting.
    """
    user_prompt = _summarise_state_for_llm(symbol, state)
    cli = client or AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    return await structured_completion(
        output_schema=MarketCommentary,
        system=ANALYST_SYSTEM_PROMPT,
        user=user_prompt,
        client=cli,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _input_hash(text: str) -> str:
    """Stable SHA-256 of the LLM prompt; recorded on the agent_run row."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _persist(
    *,
    store: SignalStore,
    ctx: ScanContext,
    symbol: str,
    state: AgentState,
    commentary_result: StructuredCompletionResult[MarketCommentary],
) -> None:
    """Write the signal row and the commentary agent_run.

    The parent scan_runs row is created by the caller before the work starts
    (FK ordering); this writes the two child rows.
    """
    proposal = state.get("proposal")
    if proposal is None:
        raise RuntimeError("graph produced no proposal/skip; cannot persist")

    await store.create_signal(proposal)

    await store.log_agent_run(
        scan_id=ctx.scan_id,
        agent_role=AgentRole.ANALYZER,
        strategy=ctx.strategy,
        input_hash=_input_hash(_summarise_state_for_llm(symbol, state)),
        output=commentary_result.output.model_dump(),
        latency_ms=commentary_result.latency_ms,
        token_usage={
            "input_tokens": commentary_result.tokens_in,
            "output_tokens": commentary_result.tokens_out,
            "model": commentary_result.model,
            "attempts": commentary_result.attempts,
        },
        cost_usd=commentary_result.cost_usd,
    )


# ---------------------------------------------------------------------------
# Message composition
# ---------------------------------------------------------------------------


def compose_message(state: AgentState, commentary: MarketCommentary) -> str:
    """Build the Telegram MarkdownV2 body for either outcome + the LLM note."""
    proposal = state.get("proposal")
    if isinstance(proposal, SignalProposal):
        head = format_new_signal(proposal)
    elif isinstance(proposal, SkipDecision):
        head = format_skip(proposal)
    else:  # pragma: no cover - guarded earlier
        head = escape_markdown_v2("No analyzer result produced.")

    note = (
        "\n\n"
        f"*Analyst note:* {escape_markdown_v2(commentary.commentary)}\n"
        f"*Key risk:* {escape_markdown_v2(commentary.key_risk)}"
    )
    return head + note


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_one_symbol(
    *,
    settings: Settings,
    symbol: str,
    provider: DataProvider,
    store: SignalStore,
    notifier: Notifier | None,
    anthropic_client: AsyncAnthropic | None = None,
) -> ScanContext:
    """Run one full scan for one symbol; returns the ScanContext used.

    Lifecycle: start_scan -> fetch -> graph -> live LLM -> persist -> notify
    -> complete_scan. Any exception flips the scan row to FAILED and re-raises
    so the caller surfaces a non-zero exit.

    Persistence is reached through the backend-neutral ``SignalStore`` (Step
    1.17): the same code path serves local asyncpg and the cloud Data API.
    """
    ctx = ScanContext(
        session=ScanSession.AD_HOC,
        symbols=[symbol],
        strategy="smc",
        triggered_by="manual",
    )
    await store.start_scan(
        scan_id=ctx.scan_id,
        started_at=ctx.started_at,
        session=ctx.session.value,
        strategy=ctx.strategy,
        symbols=[symbol],
    )
    logger.info("scan %s started for %s", ctx.scan_id, symbol)

    try:
        snapshot = await provider.fetch_market_snapshot(symbol, [Timeframe.H4], limit=CANDLE_LIMIT)
        logger.info("fetched %d 4H candles", len(snapshot.klines[Timeframe.H4]))

        state = await run_scan(scan_context=ctx, snapshot=snapshot)
        decision = state.get("decision")
        logger.info("analyzer decision: %s", decision.value if decision else "UNKNOWN")

        commentary_result = await generate_commentary(
            settings=settings,
            symbol=symbol,
            state=state,
            client=anthropic_client,
        )
        logger.info(
            "LLM commentary: %d in / %d out tokens, cost=%s, %dms",
            commentary_result.tokens_in,
            commentary_result.tokens_out,
            commentary_result.cost_usd,
            commentary_result.latency_ms,
        )

        await _persist(
            store=store,
            ctx=ctx,
            symbol=symbol,
            state=state,
            commentary_result=commentary_result,
        )
        logger.info("persisted signal + agent_run for scan %s", ctx.scan_id)

        if notifier is not None:
            await notifier.send(compose_message(state, commentary_result.output))
            logger.info("telegram message sent")

        await store.complete_scan(scan_id=ctx.scan_id, completed_at=_utcnow())
        logger.info("scan %s completed", ctx.scan_id)
    except Exception as exc:
        logger.exception("scan %s failed", ctx.scan_id)
        await store.fail_scan(
            scan_id=ctx.scan_id,
            completed_at=_utcnow(),
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise

    return ctx


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _amain(*, symbol: str | None, notify: bool) -> int:
    settings = get_settings()
    target_symbol = symbol or settings.scan_symbols[0]
    logger.info("running scan for %s (notify=%s)", target_symbol, notify)

    store = await create_store(settings)
    try:
        provider = BinanceProvider()
        notifier: Notifier | None = (
            TelegramNotifier(
                token=settings.telegram_bot_token.get_secret_value(),
                chat_id=settings.telegram_chat_id,
            )
            if notify
            else None
        )
        try:
            await run_one_symbol(
                settings=settings,
                symbol=target_symbol,
                provider=provider,
                store=store,
                notifier=notifier,
            )
        finally:
            await provider.aclose()
            if notifier is not None:
                await notifier.aclose()
    finally:
        await store.aclose()

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_scan",
        description="Run one end-to-end Slice 1 scan for a single symbol.",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="Symbol to scan (default: first of SCAN_SYMBOLS).",
    )
    parser.add_argument(
        "--no-notify",
        dest="notify",
        action="store_false",
        help="Skip Telegram delivery (still hits Binance, Anthropic, Postgres).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return asyncio.run(_amain(symbol=args.symbol, notify=args.notify))


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
