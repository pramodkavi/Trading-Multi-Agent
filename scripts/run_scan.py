"""End-to-end scan runner (Slice 2 Step 2.7 live adoption).

Wires the full per-signal pipeline into one pass and proves the production
stack works against real external services:

    config (Step 1.11)        -> load Settings from .env / SSM Parameter Store
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

Parallelism (Step 2.13):
    The Lambda path (`_run_symbols`) scans the whole watchlist concurrently with
    asyncio.gather, so the Skeptic + Judge LLM calls overlap across symbols and
    one symbol failing is isolated to its own result entry. A shared
    ScanReservationLedger keeps the §1.6 stateful caps (max concurrent / daily
    cap / correlated exposure) exact when several symbols publish in one scan.
    The local asyncpg store pools connections so concurrent symbols never share
    one; the cloud Data API store is already stateless and concurrency-safe.

Telegram trigger:
    A message is sent on every outcome (published signal, analyzer skip, or a
    Judge veto) so the operator always sees the end-to-end result. The message
    shape is chosen from the Judge's ruling.

Per-agent accounting (Step 4.0, design §5.2 / NFR-2.3):
    The Skeptic and Judge nodes return an ``LlmUsage`` alongside their output
    (``skeptic_usage`` / ``judge_usage`` in state); ``_persist`` writes its
    latency / tokens / cost onto that agent's agent_runs row. The Analyzer and
    Historian make no LLM call, so their rows carry 0 / {} / NULL.

Deferred (Step 2.7 follow-ups, see docs/PROJECT_STATE.md): a local
AsyncPostgresSaver checkpointer (the Data API Lambda runs without one) and
multi-timeframe / derivatives fetching.

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
from uuid import uuid4

from anthropic import AsyncAnthropic

from src.agents.critic.resolver import OutcomeResolver
from src.agents.forecaster import Forecaster
from src.agents.historian import HistorianRepository
from src.agents.judge import Judge
from src.agents.orchestration import ScanReservationLedger, build_pipeline_graph
from src.agents.skeptic import Skeptic, SkepticObjection, build_macro_providers
from src.common import session_for
from src.common.models import (
    AgentRole,
    JudgeRuling,
    ScanContext,
    ScanSession,
    SignalProposal,
    SkipDecision,
)
from src.config import get_settings, hydrate_secrets_env
from src.config.dashboard_url import get_dashboard_base_url
from src.notifications import (
    TelegramNotifier,
    escape_markdown_v2,
    format_new_signal,
    format_skip,
    link_line,
)
from src.notifications import signal_url as build_signal_url
from src.persistence import create_store, run_data_api_migration
from src.providers import BinanceProvider, NoMacroData, ProviderError, Timeframe

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence
    from uuid import UUID

    from pydantic import BaseModel

    from src.agents.orchestration import AgentState
    from src.common.models import LlmUsage
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

# {"mode": "resolve"} chunking (Critic v0 design §5.5). One chunk of 25 decisions
# is at most 25 seven-day 5m candle fetches (cached per symbol/window within a
# run), which fits comfortably inside the Lambda timeout; 100 is the hard ceiling
# so an operator's manual backfill invocation can't run the function into it.
RESOLVE_CHUNK_DEFAULT: int = 25
RESOLVE_CHUNK_MAX: int = 100


# ---------------------------------------------------------------------------
# Pipeline construction
# ---------------------------------------------------------------------------


def build_pipeline(
    *,
    settings: Settings,
    store: SignalStore,
    client: AsyncAnthropic,
    reservations: ScanReservationLedger | None = None,
) -> tuple[Any, list[DataProvider]]:
    """Construct the compiled pipeline graph and the macro providers it owns.

    Built once per process and reused across symbols. Returns the compiled
    graph plus the Skeptic's macro providers so the caller can ``aclose`` them
    (they each hold an httpx client). With no FRED / Twelve Data keys configured
    the provider list is empty and the Skeptic degrades to NoMacroData (FR-4.3).

    ``reservations`` (Step 2.13) is the per-batch ledger that keeps the §1.6
    stateful caps exact across the concurrently-scanned watchlist; ``None`` (the
    single-symbol CLI path) leaves the risk gate behaving as in Step 2.11.
    """
    macro_providers = build_macro_providers(settings)
    graph = build_pipeline_graph(
        store=store,
        historian=HistorianRepository(store),
        skeptic=Skeptic(macro_providers, client=client),
        judge=Judge(client=client),
        reservations=reservations,
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
    usage: LlmUsage | None = None,
) -> None:
    """Write one agent_run row carrying an agent's structured output + LLM usage.

    The reasoning itself is what FR-1.7 requires; ``mode="json"`` makes UUIDs /
    datetimes / enums JSON-safe for the JSONB column.

    ``usage`` (Step 4.0, design §5.2 / NFR-2.3) is the ``LlmUsage`` of the
    agent's Claude call: ``latency_ms`` and ``cost_usd`` go to their own
    columns, the rest (model / tokens / attempts) to the ``token_usage`` JSONB.
    It is ``None`` for the deterministic agents (Analyzer, Historian) and for
    the Skeptic's NoMacroData short circuit: those rows get ``latency_ms=0``,
    ``token_usage=None`` (the store writes ``{}``) and ``cost_usd=None`` (SQL
    NULL -- never 0.0, which would silently under-report spend).
    """
    await store.log_agent_run(
        scan_id=ctx.scan_id,
        agent_role=role,
        strategy=ctx.strategy,
        input_hash=_input_hash(f"{ctx.scan_id}:{role.value}:{symbol}"),
        output=output.model_dump(mode="json"),
        latency_ms=usage.latency_ms if usage is not None else 0,
        token_usage=usage.token_usage() if usage is not None else None,
        cost_usd=usage.cost_usd if usage is not None else None,
    )


async def _persist(
    *,
    store: SignalStore,
    ctx: ScanContext,
    symbol: str,
    state: AgentState,
) -> UUID:
    """Write the signal row and the per-agent reasoning chain (FR-1.7); returns the signal id.

    The parent scan_runs row is created by the caller before the work starts
    (FK ordering); this writes the signal plus one agent_run per agent that
    ran. On a skip only the analyzer ran, so only its row is written. The
    Skeptic / Judge rows carry their LLM usage from state (Step 4.0, NFR-2.3).
    """
    proposal = state.get("proposal")
    if proposal is None:
        raise RuntimeError("graph produced no proposal/skip; cannot persist")

    # `proposal` is the signal row we write (a forced SkipDecision when the risk
    # gate rejected the setup -> a SKIPPED row, FR-1.3). The ANALYZER agent_run,
    # though, must record what the Analyzer actually produced: on a gate skip
    # that is `rejected_proposal` (the original SignalProposal); otherwise it is
    # `proposal` itself (a published proposal, or the Analyzer's own skip).
    signal_id = await store.create_signal(proposal)
    analyzer_output = state.get("rejected_proposal") or proposal
    await _log_agent(store, ctx, symbol, AgentRole.ANALYZER, analyzer_output)

    report = state.get("historian_report")
    if report is not None:
        await _log_agent(store, ctx, symbol, AgentRole.HISTORIAN, report)
    objection = state.get("skeptic_objection")
    if objection is not None:
        await _log_agent(
            store, ctx, symbol, AgentRole.SKEPTIC, objection, state.get("skeptic_usage")
        )
    judge_decision = state.get("judge_decision")
    if judge_decision is not None:
        await _log_agent(
            store, ctx, symbol, AgentRole.JUDGE, judge_decision, state.get("judge_usage")
        )

    # Step 2.8: a published signal becomes a tracked active setup the Forecaster
    # will follow (only on PUBLISH / PUBLISH_WITH_CAVEAT; skips and vetoes don't).
    if isinstance(proposal, SignalProposal) and state.get("decision") in _PUBLISH_RULINGS:
        await store.open_active_setup(signal_id=signal_id)
    return signal_id


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


def _format_judge_skip(state: AgentState, signal_url: str | None = None) -> str:
    """Operator note when the Judge vetoed a real proposal (decision == SKIP)."""
    proposal = state.get("proposal")
    judge_decision = state.get("judge_decision")
    symbol = proposal.symbol if isinstance(proposal, SignalProposal) else "?"
    reasoning = judge_decision.reasoning if judge_decision is not None else "Judge ruled SKIP."
    lines = [
        "*\U0001f50e JUDGED SKIP*",
        "",
        f"*Symbol:* `{escape_markdown_v2(symbol)}`",
        "",
        f"*Why:* {escape_markdown_v2(reasoning)}",
    ]
    link = link_line(signal_url)
    if link is not None:
        lines.extend(["", link])
    return "\n".join(lines)


def compose_message(state: AgentState, signal_url: str | None = None) -> str:
    """Build the Telegram MarkdownV2 body for the scan outcome.

    Shape is chosen from the Judge's ruling:
        PUBLISH / PUBLISH_WITH_CAVEAT -> the full signal (FR-5.2), enriched with
            the Historian win rate, the Skeptic objection, and (with caveat) the
            Judge's caveat line.
        analyzer SkipDecision         -> a skip note.
        Judge veto on a real proposal -> a "judged skip" note.

    ``signal_url`` (Step 4.7 Part B) is the dashboard page of this decision; every
    message kind appends it as a link, and nothing changes when it is ``None``.
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
            signal_url=signal_url,
        )

    if isinstance(proposal, SkipDecision):
        return format_skip(proposal, signal_url=signal_url)

    # A SignalProposal the Judge ruled SKIP (or any other non-publish outcome).
    return _format_judge_skip(state, signal_url)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _published(state: AgentState) -> bool:
    """True when the scan ended in a published signal (mirrors ``_persist``).

    Must match the condition under which ``_persist`` opens an active setup, so a
    Step-2.13 reservation is released exactly when the symbol did NOT publish.
    """
    return (
        isinstance(state.get("proposal"), SignalProposal)
        and state.get("decision") in _PUBLISH_RULINGS
    )


async def run_one_symbol(
    *,
    symbol: str,
    provider: DataProvider,
    store: SignalStore,
    graph: Any,
    notifier: Notifier | None,
    reservations: ScanReservationLedger | None = None,
    session: ScanSession | None = None,
    triggered_by: str = "scheduler",
    clock: Callable[[], datetime] | None = None,
    dashboard_base_url: str | None = None,
) -> ScanContext:
    """Run one full scan for one symbol through the pipeline; returns its ScanContext.

    Lifecycle: start_scan -> fetch -> pipeline graph -> persist -> notify ->
    complete_scan. Any exception flips the scan row to FAILED and re-raises so
    the caller surfaces a non-zero exit. The compiled ``graph`` (built once by
    the caller) holds the Historian / Skeptic / Judge; persistence reaches the
    backend-neutral ``SignalStore`` (Step 1.17) the graph's Historian also uses.

    ``session`` / ``clock`` (Step 4.0, design §14 D1): the ScanContext session
    is derived from the UTC clock at scan start via ``session_for`` so the risk
    gate's rule 7 (no new signals in ASIAN / COOLDOWN) sees the real window and
    ``scan_runs.session`` records it. ``session`` is an explicit override (the
    Lambda event's ``"session"`` key, e.g. ``AD_HOC`` for a console smoke test);
    ``clock`` defaults to the real UTC clock and exists so tests can pin the
    instant. ``started_at`` is taken from the same instant so the two agree.

    ``reservations`` (Step 2.13): when the watchlist is scanned in parallel the
    risk gate reserves a §1.6 slot for a proposal that clears the stateful caps.
    If this symbol does not end up publishing (a Judge veto) or the scan fails,
    we release that reservation so it does not wrongly block a sibling symbol. A
    publish keeps the reservation — its active_setups row now reflects it.

    ``dashboard_base_url`` (Step 4.7 Part B): when set, the Telegram message ends with a
    link to this decision's dashboard page (``<base>/signals/<signal id>``).
    """
    now = (clock or _utcnow)()
    ctx = ScanContext(
        session=session if session is not None else session_for(now),
        symbols=[symbol],
        strategy="smc",
        triggered_by=triggered_by,
        started_at=now,
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
        try:
            snapshot = await provider.fetch_market_snapshot(
                symbol, [Timeframe.H4], limit=CANDLE_LIMIT
            )
        except ProviderError as exc:
            # Emit the PROVIDER_ERROR marker the MonitoringStack metric filter
            # counts (-> ProviderErrors metric -> provider-error alarm, NFR-2.2),
            # then re-raise so the scan still flips to FAILED and surfaces normally.
            logger.warning(
                "PROVIDER_ERROR provider=%s symbol=%s error=%s: %s",
                type(provider).__name__,
                symbol,
                type(exc).__name__,
                exc,
            )
            raise
        logger.info("fetched %d 4H candles", len(snapshot.klines[Timeframe.H4]))

        state = await graph.ainvoke(_initial_state(ctx, snapshot))
        decision = state.get("decision")
        logger.info("judge decision: %s", decision.value if decision else "UNKNOWN")

        signal_id = await _persist(store=store, ctx=ctx, symbol=symbol, state=state)
        logger.info("persisted signal + reasoning chain for scan %s", ctx.scan_id)

        # Step 2.13: a reservation that did not become a published setup (the gate
        # reserved a slot but the Judge vetoed) must be released so it stops
        # counting against sibling symbols' gates. Idempotent if nothing reserved.
        if reservations is not None and not _published(state):
            await reservations.release(ctx.scan_id)

        if notifier is not None:
            link = build_signal_url(dashboard_base_url, signal_id)
            await notifier.send(compose_message(state, signal_url=link))
            logger.info("telegram message sent")

        await store.complete_scan(scan_id=ctx.scan_id, completed_at=_utcnow())
        logger.info("scan %s completed", ctx.scan_id)
    except Exception as exc:
        logger.exception("scan %s failed", ctx.scan_id)
        if reservations is not None:
            # The scan failed -> it cannot publish, so free any reserved slot.
            await reservations.release(ctx.scan_id)
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


async def _scan_symbol_isolated(
    *,
    symbol: str,
    provider: DataProvider,
    store: SignalStore,
    graph: Any,
    notifier: Notifier | None,
    reservations: ScanReservationLedger,
    session: ScanSession | None,
    triggered_by: str,
    dashboard_base_url: str | None = None,
) -> dict[str, Any]:
    """Run one symbol and convert its outcome into a result entry, never raising.

    The per-symbol error isolation seam (Step 2.13): a failure here is captured
    in this symbol's result dict and returned, so ``asyncio.gather`` over the
    watchlist never sees an exception and one symbol failing cannot abort the
    others (``run_one_symbol`` has already flipped that scan's row to FAILED and
    released its reservation before re-raising). ``session`` / ``triggered_by``
    are passed straight through to ``run_one_symbol`` (Step 4.0 D1).
    """
    try:
        ctx = await run_one_symbol(
            symbol=symbol,
            provider=provider,
            store=store,
            graph=graph,
            notifier=notifier,
            reservations=reservations,
            session=session,
            triggered_by=triggered_by,
            dashboard_base_url=dashboard_base_url,
        )
        return {"symbol": symbol, "scan_id": str(ctx.scan_id), "status": "ok"}
    except Exception as exc:
        logger.exception("lambda scan failed for %s", symbol)
        return {"symbol": symbol, "status": "error", "error": f"{type(exc).__name__}: {exc}"}


async def _run_symbols(
    *,
    settings: Settings,
    symbols: list[str],
    notify: bool,
    session: ScanSession | None,
) -> list[dict[str, Any]]:
    """Scan the watchlist **in parallel**, sharing one store / provider / pipeline.

    Step 2.13: the per-symbol pipelines run concurrently via ``asyncio.gather``
    (the expensive Skeptic + Judge LLM calls overlap across symbols), each behind
    the ``_scan_symbol_isolated`` error boundary so one symbol's failure cannot
    abort the others. ``gather`` preserves input order, so the returned results
    line up with ``symbols``.

    ``session`` (Step 4.0 D1) is the event override; ``None`` (every scheduled
    invocation) lets each symbol derive its session from the UTC clock. The
    Lambda path is the scheduler's, so every scan is ``triggered_by="scheduler"``.

    A single :class:`ScanReservationLedger` is shared across the batch so the
    §1.6 stateful caps (max concurrent, daily cap, correlated exposure) stay
    exact even when several symbols publish in the same scan: the risk gate
    serialises its read → evaluate → reserve under the ledger lock and counts
    sibling symbols' pending publishes.

    The store backend is chosen by ``settings.persistence_backend`` -- the cloud
    ``dataapi`` store is stateless and concurrency-safe; the local ``asyncpg``
    store pools connections (Step 2.13) so concurrent symbols never share one.
    The pipeline graph + Anthropic client are built once and reused.
    """
    store = await create_store(settings)
    results: list[dict[str, Any]] = []
    try:
        provider = BinanceProvider()
        client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
        reservations = ScanReservationLedger()
        graph, macro_providers = build_pipeline(
            settings=settings, store=store, client=client, reservations=reservations
        )
        notifier: Notifier | None = (
            TelegramNotifier(
                token=settings.telegram_bot_token.get_secret_value(),
                chat_id=settings.telegram_chat_id,
            )
            if notify
            else None
        )
        # Read once per scan run (cached per container); never raises -- no URL, no links.
        dashboard_base_url = get_dashboard_base_url() if notify else None
        try:
            results = list(
                await asyncio.gather(
                    *(
                        _scan_symbol_isolated(
                            symbol=symbol,
                            provider=provider,
                            store=store,
                            graph=graph,
                            notifier=notifier,
                            reservations=reservations,
                            session=session,
                            triggered_by="scheduler",
                            dashboard_base_url=dashboard_base_url,
                        )
                        for symbol in symbols
                    )
                )
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
                store=store,
                provider=provider,
                notifier=notifier,
                client=client,
                dashboard_base_url=get_dashboard_base_url() if notify else None,
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


async def _run_resolver(*, settings: Settings, chunk_size: int) -> dict[str, Any]:
    """Resolve one chunk of decisions against real price action ({"mode": "resolve"}).

    Critic v0 Layer 1 (design §5). Runs in this Lambda because Binance geofences
    GitHub's US runners (HTTP 451); the review layer (GitHub Actions) only reads
    the ``decision_resolutions`` rows this writes. Needs no Anthropic client and
    no Telegram notifier -- resolution is deterministic replay, not an agent.

    Returns the ``ResolveSummary`` counts as a JSON-serialisable dict so a
    backfill loop over the invocation can stop when ``remaining`` hits zero.
    """
    store = await create_store(settings)
    try:
        provider = BinanceProvider()
        try:
            summary = await OutcomeResolver(store=store, provider=provider).run(
                chunk_size=chunk_size
            )
        finally:
            await provider.aclose()
    finally:
        await store.aclose()
    logger.info(
        "resolver run complete: resolved=%d deferred=%d remaining=%d (chunk_size=%d)",
        summary.resolved,
        summary.deferred,
        summary.remaining,
        chunk_size,
    )
    return {
        "ok": True,
        "mode": "resolve",
        "resolved": summary.resolved,
        "deferred": summary.deferred,
        "remaining": summary.remaining,
    }


async def _run_migration(settings: Settings) -> dict[str, Any]:
    """Apply the idempotent schema to Aurora via the Data API ({"mode": "migrate"}).

    A manual escape hatch for when the CD pre-deploy migration step did not run
    (e.g. the OIDC deploy role can't reach CloudFormation / the Data API, so the
    step's describe-stacks guard skips it). The Lambda's OWN execution role has
    rds-data access to the cluster plus the DB secret (``grant_data_api_access``),
    so it can apply ``schema.sql`` itself. Idempotent -- the schema is all
    ``CREATE/ALTER ... IF NOT EXISTS``, so re-running is safe.

    Requires the dataapi backend (cluster + secret ARNs). We first issue one
    retried no-op read through the store so Aurora's scale-to-zero resume is
    ridden out by ``_execute``'s retry, then apply the (un-retried) migration
    statements against the now-awake cluster.
    """
    if settings.db_cluster_arn is None or settings.db_secret_arn is None:
        raise RuntimeError(
            "mode=migrate requires the dataapi backend (db_cluster_arn + db_secret_arn)"
        )

    # Wake the cluster through the retried Data API path (get_scan_run is a
    # harmless SELECT) so the migration statements don't hit DatabaseResumingException.
    store = await create_store(settings)
    try:
        await store.get_scan_run(uuid4())
    finally:
        await store.aclose()

    count = await asyncio.to_thread(
        run_data_api_migration,
        cluster_arn=settings.db_cluster_arn,
        secret_arn=settings.db_secret_arn,
        database=settings.db_name,
    )
    logger.info("schema migration applied: %d statement(s)", count)
    return {"ok": True, "mode": "migrate", "statements": count}


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


def _session_from_event(event: dict[str, Any]) -> ScanSession | None:
    """Resolve the optional ``{"session": "..."}`` override (Step 4.0 D1).

    Returns ``None`` when absent (the scheduled EventBridge payload never sets
    it) so ``run_one_symbol`` derives the session from the UTC clock. The value
    is case-insensitive. An unknown value raises ``ValueError`` on purpose: an
    operator typo must fail the invocation loudly rather than silently bypass
    SPEC §1.6 rule 7.
    """
    raw = event.get("session")
    if raw is None:
        return None
    return ScanSession(str(raw).upper())


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

    # Critic v0 Layer 1: {"mode": "resolve"} replays unresolved decisions against
    # 5m candles and writes decision_resolutions (+ signals.outcome for published).
    # chunk_size defaults to RESOLVE_CHUNK_DEFAULT and is clamped to 1..RESOLVE_CHUNK_MAX.
    if payload.get("mode") == "resolve":
        raw = payload.get("chunk_size", RESOLVE_CHUNK_DEFAULT)
        chunk = max(1, min(RESOLVE_CHUNK_MAX, int(raw)))
        return await _run_resolver(settings=settings, chunk_size=chunk)

    # Manual escape hatch: {"mode": "migrate"} applies the DB schema via the Data
    # API using the Lambda's own role (for when the CD migration step skipped).
    if payload.get("mode") == "migrate":
        return await _run_migration(settings)

    symbols = _symbols_from_event(payload, settings)
    session = _session_from_event(payload)
    logger.info(
        "lambda scan starting for %s (notify=%s, session=%s)",
        symbols,
        notify,
        session.value if session is not None else "from-clock",
    )

    results = await _run_symbols(settings=settings, symbols=symbols, notify=notify, session=session)
    ok = all(r["status"] == "ok" for r in results)
    return {"ok": ok, "scans": results}


def _load_settings() -> Settings:
    """Hydrate secrets from SSM Parameter Store, then build the cached Settings.

    Order matters: ``get_settings`` is ``lru_cache``d, so the secret values must
    be in the environment before its first call -- otherwise the cache locks in
    a Settings validated against a half-empty environment. Locally this is a
    no-op (no parameter names set) and Settings reads straight from ``.env``.
    """
    hydrate_secrets_env()
    return get_settings()


def lambda_handler(event: dict[str, Any] | None, context: object) -> dict[str, Any]:
    """AWS Lambda entry point: run the scan and return a structured result.

    Triggered by EventBridge Scheduler (Step 1.19). Resolves secret values from
    SSM Parameter Store (the ComputeStack injects only the parameter names, never
    the values), loads configuration, runs the scan for the configured symbol(s),
    and returns a JSON-serialisable summary. ``ok`` is False if any symbol
    errored, so the invocation surfaces failures to CloudWatch without losing
    the successful scans.

    Event ``mode`` selects the entry point: ``"forecaster"`` re-evaluates open
    setups; ``"resolve"`` scores one chunk of past decisions against real price
    action (Critic v0 Layer 1, see ``_run_resolver``; optional ``chunk_size``);
    ``"migrate"`` applies the DB schema via the Data API (manual escape hatch,
    see ``_run_migration``); absent/any other value runs the per-signal scan
    over the event's symbols (or the watchlist).

    Scan event keys: ``symbols`` | ``symbol`` (default: the watchlist),
    ``notify`` (default true), ``mode`` (above), and ``session`` (Step 4.0 D1:
    an explicit ScanSession such as ``"AD_HOC"`` to bypass the rule-7 window
    for a manual smoke test; absent on every scheduled invocation, in which
    case the session is derived from the UTC clock; an unknown value fails the
    invocation).
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
            # The CLI is a dev tool: it derives the session from the real clock
            # (no override) so an operator sees the rule-7 window as the Lambda
            # would, and is labelled as a manual trigger (Step 4.0 D1).
            await run_one_symbol(
                symbol=target_symbol,
                provider=provider,
                store=store,
                graph=graph,
                notifier=notifier,
                triggered_by="manual",
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
