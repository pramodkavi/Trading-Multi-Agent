"""End-to-end scan runner (Slice 2 Step 2.7 live adoption).

Wires the full per-signal pipeline into one pass and proves the production
stack works against real external services:

    config (Step 1.11)        -> load Settings from .env / Secrets Manager
    Binance (Step 2.2)        -> fetch 4H klines for one symbol
    LangGraph (Step 2.7)      -> run the full pipeline graph:
                                 analyzer -> historian -> skeptic -> judge
    Anthropic (Step 1.6)      -> the Skeptic + Judge nodes make the LIVE Claude
                                 calls (the Slice-1 commentary stand-in is gone)
    Postgres (Steps 1.8-1.9)  -> persist scan_runs + signals + the full
                                 reasoning chain to agent_runs (FR-1.7)
    Telegram (Step 1.10)      -> deliver the Judge's decision to the operator

Pipeline placement:
    The graph is built ONCE per process (it embeds the store-backed Historian
    and the Skeptic/Judge bound to one Anthropic client) and reused across
    symbols. A SkipDecision short-circuits the conditional edge, so skips cost
    no LLM calls (see src/agents/orchestration/graph.py).

Telegram trigger:
    A message is sent on every outcome (published signal, analyzer skip, or a
    Judge veto) so the operator always sees the end-to-end result. The message
    shape is chosen from the Judge's ruling.

Deferred (Step 2.7 follow-ups, see docs/PROJECT_STATE.md): a local
AsyncPostgresSaver checkpointer (the Data API Lambda runs without one),
per-agent token/cost accounting on the agent_runs rows (Langfuse covers
observability when enabled), and multi-timeframe / derivatives fetching.

Usage:
    # .env must provide ANTHROPIC_API_KEY, TELEGRAM_*, DATABASE_URL.
    # FRED_API_KEY / TWELVE_DATA_API_KEY are optional (Skeptic macro context).
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
from typing import TYPE_CHECKING, Any

from anthropic import AsyncAnthropic

from src.agents.forecaster import Forecaster
from src.agents.historian import HistorianRepository
from src.agents.judge import Judge
from src.agents.orchestration import build_pipeline_graph
from src.agents.skeptic import Skeptic, SkepticObjection, build_macro_providers
from src.common.models import (
    AgentRole,
    JudgeRuling,
    ScanContext,
    ScanSession,
    SignalProposal,
    SkipDecision,
)
from src.config import get_settings, hydrate_secrets_env
from src.notifications import (
    TelegramNotifier,
    escape_markdown_v2,
    format_new_signal,
    format_skip,
)
from src.persistence import create_store
from src.providers import BinanceProvider, NoMacroData, Timeframe

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from pydantic import BaseModel

    from src.agents.orchestration import AgentState
    from src.config import Settings
    from src.notifications import Notifier
    from src.persistence import SignalStore
    from src.providers import DataProvider

logger = logging.getLogger(__name__)

# Fetch enough 4H history for the analyzer's swing-pivot detection
# (MIN_KLINES_REQUIRED=30 plus comfortable headroom for the pivot window).
CANDLE_LIMIT: int = 200

# Rulings that publish a signal to the operator (vs SKIP).
_PUBLISH_RULINGS: frozenset[JudgeRuling] = frozenset(
    {JudgeRuling.PUBLISH, JudgeRuling.PUBLISH_WITH_CAVEAT}
)


# ---------------------------------------------------------------------------
# Pipeline construction
# ---------------------------------------------------------------------------


def build_pipeline(
    *,
    settings: Settings,
    store: SignalStore,
    client: AsyncAnthropic,
) -> tuple[Any, list[DataProvider]]:
    """Construct the compiled pipeline graph and the macro providers it owns.

    Built once per process and reused across symbols. Returns the compiled
    graph plus the Skeptic's macro providers so the caller can ``aclose`` them
    (they each hold an httpx client). With no FRED / Twelve Data keys configured
    the provider list is empty and the Skeptic degrades to NoMacroData (FR-4.3).
    """
    macro_providers = build_macro_providers(settings)
    graph = build_pipeline_graph(
        historian=HistorianRepository(store),
        skeptic=Skeptic(macro_providers, client=client),
        judge=Judge(client=client),
    )
    return graph, macro_providers


def _initial_state(ctx: ScanContext, snapshot: Any) -> AgentState:
    return {
        "scan_context": ctx,
        "snapshot": snapshot,
        "proposal": None,
        "decision": None,
    }


# ---------------------------------------------------------------------------
# Persistence (FR-1.7: persist the full reasoning chain, including skips)
# ---------------------------------------------------------------------------


def _input_hash(text: str) -> str:
    """Stable SHA-256 used as the agent_run input_hash."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _log_agent(
    store: SignalStore,
    ctx: ScanContext,
    symbol: str,
    role: AgentRole,
    output: BaseModel,
) -> None:
    """Write one agent_run row carrying an agent's structured output.

    Token/cost/latency are omitted for now (Langfuse provides per-call
    observability when enabled); the reasoning itself is what FR-1.7 requires.
    ``mode="json"`` makes UUIDs / datetimes / enums JSON-safe for the JSONB
    column.
    """
    await store.log_agent_run(
        scan_id=ctx.scan_id,
        agent_role=role,
        strategy=ctx.strategy,
        input_hash=_input_hash(f"{ctx.scan_id}:{role.value}:{symbol}"),
        output=output.model_dump(mode="json"),
        latency_ms=0,
    )


async def _persist(
    *,
    store: SignalStore,
    ctx: ScanContext,
    symbol: str,
    state: AgentState,
) -> None:
    """Write the signal row and the per-agent reasoning chain (FR-1.7).

    The parent scan_runs row is created by the caller before the work starts
    (FK ordering); this writes the signal plus one agent_run per agent that
    ran. On a skip only the analyzer ran, so only its row is written.
    """
    proposal = state.get("proposal")
    if proposal is None:
        raise RuntimeError("graph produced no proposal/skip; cannot persist")

    signal_id = await store.create_signal(proposal)
    await _log_agent(store, ctx, symbol, AgentRole.ANALYZER, proposal)

    report = state.get("historian_report")
    if report is not None:
        await _log_agent(store, ctx, symbol, AgentRole.HISTORIAN, report)
    objection = state.get("skeptic_objection")
    if objection is not None:
        await _log_agent(store, ctx, symbol, AgentRole.SKEPTIC, objection)
    judge_decision = state.get("judge_decision")
    if judge_decision is not None:
        await _log_agent(store, ctx, symbol, AgentRole.JUDGE, judge_decision)

    # Step 2.8: a published signal becomes a tracked active setup the Forecaster
    # will follow (only on PUBLISH / PUBLISH_WITH_CAVEAT; skips and vetoes don't).
    if isinstance(proposal, SignalProposal) and state.get("decision") in _PUBLISH_RULINGS:
        await store.open_active_setup(signal_id=signal_id)


# ---------------------------------------------------------------------------
# Message composition
# ---------------------------------------------------------------------------


def _skeptic_fields(objection: object) -> tuple[str | None, str | None]:
    """Pull (objection text, severity) for the alert from the Skeptic output."""
    if isinstance(objection, SkepticObjection):
        return objection.headline, objection.severity.value
    if isinstance(objection, NoMacroData):
        return (f"Macro context unavailable ({objection.reason}); confidence reduced.", None)
    return (None, None)


def _format_judge_skip(state: AgentState) -> str:
    """Operator note when the Judge vetoed a real proposal (decision == SKIP)."""
    proposal = state.get("proposal")
    judge_decision = state.get("judge_decision")
    symbol = proposal.symbol if isinstance(proposal, SignalProposal) else "?"
    reasoning = judge_decision.reasoning if judge_decision is not None else "Judge ruled SKIP."
    return "\n".join(
        [
            "*\U0001f50e JUDGED SKIP*",
            "",
            f"*Symbol:* `{escape_markdown_v2(symbol)}`",
            "",
            f"*Why:* {escape_markdown_v2(reasoning)}",
        ]
    )


def compose_message(state: AgentState) -> str:
    """Build the Telegram MarkdownV2 body for the scan outcome.

    Shape is chosen from the Judge's ruling:
        PUBLISH / PUBLISH_WITH_CAVEAT -> the full signal (FR-5.2), enriched with
            the Historian win rate, the Skeptic objection, and (with caveat) the
            Judge's caveat line.
        analyzer SkipDecision         -> a skip note.
        Judge veto on a real proposal -> a "judged skip" note.
    """
    proposal = state.get("proposal")
    decision = state.get("decision")
    judge_decision = state.get("judge_decision")

    if isinstance(proposal, SignalProposal) and decision in _PUBLISH_RULINGS:
        report = state.get("historian_report")
        skeptic_text, severity = _skeptic_fields(state.get("skeptic_objection"))
        caveat = (
            judge_decision.caveat
            if judge_decision is not None and decision is JudgeRuling.PUBLISH_WITH_CAVEAT
            else None
        )
        return format_new_signal(
            proposal,
            historian_win_rate=report.win_rate if report is not None else None,
            historian_sample_size=report.sample_size if report is not None else None,
            skeptic_objection=skeptic_text,
            skeptic_severity=severity,
            caveat=caveat,
        )

    if isinstance(proposal, SkipDecision):
        return format_skip(proposal)

    # A SignalProposal the Judge ruled SKIP (or any other non-publish outcome).
    return _format_judge_skip(state)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_one_symbol(
    *,
    symbol: str,
    provider: DataProvider,
    store: SignalStore,
    graph: Any,
    notifier: Notifier | None,
) -> ScanContext:
    """Run one full scan for one symbol through the pipeline; returns its ScanContext.

    Lifecycle: start_scan -> fetch -> pipeline graph -> persist -> notify ->
    complete_scan. Any exception flips the scan row to FAILED and re-raises so
    the caller surfaces a non-zero exit. The compiled ``graph`` (built once by
    the caller) holds the Historian / Skeptic / Judge; persistence reaches the
    backend-neutral ``SignalStore`` (Step 1.17) the graph's Historian also uses.
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

        state = await graph.ainvoke(_initial_state(ctx, snapshot))
        decision = state.get("decision")
        logger.info("judge decision: %s", decision.value if decision else "UNKNOWN")

        await _persist(store=store, ctx=ctx, symbol=symbol, state=state)
        logger.info("persisted signal + reasoning chain for scan %s", ctx.scan_id)

        if notifier is not None:
            await notifier.send(compose_message(state))
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
# AWS Lambda entry point (serverless deploy, Step 1.17)
# ---------------------------------------------------------------------------


async def _run_symbols(
    *,
    settings: Settings,
    symbols: list[str],
    notify: bool,
) -> list[dict[str, Any]]:
    """Run a scan for each symbol, sharing one store / provider / pipeline / notifier.

    Each symbol is independent: a failure on one is captured in its result entry
    and does not abort the others (``run_one_symbol`` has already flipped that
    scan's row to FAILED before re-raising). The store backend is chosen by
    ``settings.persistence_backend`` -- ``dataapi`` in the Lambda. The pipeline
    graph + Anthropic client are built once and reused across symbols.
    """
    store = await create_store(settings)
    results: list[dict[str, Any]] = []
    try:
        provider = BinanceProvider()
        client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
        graph, macro_providers = build_pipeline(settings=settings, store=store, client=client)
        notifier: Notifier | None = (
            TelegramNotifier(
                token=settings.telegram_bot_token.get_secret_value(),
                chat_id=settings.telegram_chat_id,
            )
            if notify
            else None
        )
        try:
            for symbol in symbols:
                try:
                    ctx = await run_one_symbol(
                        symbol=symbol,
                        provider=provider,
                        store=store,
                        graph=graph,
                        notifier=notifier,
                    )
                    results.append({"symbol": symbol, "scan_id": str(ctx.scan_id), "status": "ok"})
                except Exception as exc:
                    # Report this symbol's failure and keep scanning the rest.
                    logger.exception("lambda scan failed for %s", symbol)
                    results.append(
                        {
                            "symbol": symbol,
                            "status": "error",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
        finally:
            await provider.aclose()
            for macro_provider in macro_providers:
                await macro_provider.aclose()
            await client.close()
            if notifier is not None:
                await notifier.aclose()
    finally:
        await store.aclose()
    return results


async def _run_forecaster(*, settings: Settings, notify: bool) -> dict[str, Any]:
    """Re-evaluate every open setup once (Step 2.9 Forecaster, scheduled by 2.10).

    Builds the same store / provider / notifier / Anthropic client as a scan and
    runs the Forecaster loop. Returns a JSON-serialisable summary counting the
    verdicts so the invocation surfaces activity to CloudWatch.
    """
    store = await create_store(settings)
    updates: list[Any] = []
    try:
        provider = BinanceProvider()
        client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
        notifier: Notifier | None = (
            TelegramNotifier(
                token=settings.telegram_bot_token.get_secret_value(),
                chat_id=settings.telegram_chat_id,
            )
            if notify
            else None
        )
        try:
            forecaster = Forecaster(
                store=store, provider=provider, notifier=notifier, client=client
            )
            updates = await forecaster.run()
        finally:
            await provider.aclose()
            await client.close()
            if notifier is not None:
                await notifier.aclose()
    finally:
        await store.aclose()

    by_status: dict[str, int] = {}
    for update in updates:
        by_status[update.status.value] = by_status.get(update.status.value, 0) + 1
    logger.info("forecaster run complete: %d setup(s), %s", len(updates), by_status)
    return {"ok": True, "mode": "forecaster", "evaluated": len(updates), "by_status": by_status}


def _symbols_from_event(event: dict[str, Any], settings: Settings) -> list[str]:
    """Resolve the symbols to scan: event override, else the watchlist.

    Accepts ``{"symbols": [...]}`` or ``{"symbol": "BTCUSDT"}``; with neither
    (the scheduled EventBridge invocation sends an empty payload) it falls back
    to ``settings.scan_symbols``.
    """
    if event.get("symbols"):
        return [str(s) for s in event["symbols"]]
    if event.get("symbol"):
        return [str(event["symbol"])]
    return list(settings.scan_symbols)


async def _alambda(event: dict[str, Any] | None, settings: Settings) -> dict[str, Any]:
    """Async core of the Lambda handler (the awaitable part, sans event loop).

    Kept separate from ``lambda_handler`` so tests can await it under pytest's
    managed loop instead of spinning a fresh ``asyncio.run`` loop -- which on
    Windows leaves an unclosed-loop ResourceWarning.
    """
    payload = event or {}
    notify = bool(payload.get("notify", True))

    # Step 2.10: a separate EventBridge schedule invokes this same Lambda with
    # {"mode": "forecaster"} a couple minutes after each scan, to re-evaluate
    # open setups. Any other (or absent) mode runs the per-signal scan.
    if payload.get("mode") == "forecaster":
        return await _run_forecaster(settings=settings, notify=notify)

    symbols = _symbols_from_event(payload, settings)
    logger.info("lambda scan starting for %s (notify=%s)", symbols, notify)

    results = await _run_symbols(settings=settings, symbols=symbols, notify=notify)
    ok = all(r["status"] == "ok" for r in results)
    return {"ok": ok, "scans": results}


def _load_settings() -> Settings:
    """Hydrate secrets from Secrets Manager, then build the cached Settings.

    Order matters: ``get_settings`` is ``lru_cache``d, so the secret values must
    be in the environment before its first call -- otherwise the cache locks in
    a Settings validated against a half-empty environment. Locally this is a
    no-op (no secret ARNs set) and Settings reads straight from ``.env``.
    """
    hydrate_secrets_env()
    return get_settings()


def lambda_handler(event: dict[str, Any] | None, context: object) -> dict[str, Any]:
    """AWS Lambda entry point: run the scan and return a structured result.

    Triggered by EventBridge Scheduler (Step 1.19). Resolves secret values from
    Secrets Manager (the LambdaStack injects only the secret ARNs, never the
    values), loads configuration, runs the scan for the configured symbol(s),
    and returns a JSON-serialisable summary. ``ok`` is False if any symbol
    errored, so the invocation surfaces failures to CloudWatch without losing
    the successful scans.
    """
    return asyncio.run(_alambda(event, _load_settings()))


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
        client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
        graph, macro_providers = build_pipeline(settings=settings, store=store, client=client)
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
                symbol=target_symbol,
                provider=provider,
                store=store,
                graph=graph,
                notifier=notifier,
            )
        finally:
            await provider.aclose()
            for macro_provider in macro_providers:
                await macro_provider.aclose()
            await client.close()
            if notifier is not None:
                await notifier.aclose()
    finally:
        await store.aclose()

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_scan",
        description="Run one end-to-end scan for a single symbol through the full pipeline.",
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
