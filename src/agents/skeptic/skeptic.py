"""Skeptic agent: independent macro fetch + adversarial objection (Step 2.5).

SPEC §3.1 role 3 / FR-1.5 / §4 Step 2.5. The Skeptic looks at MACRO and
cross-asset context the Analyzer never saw -- the broad US dollar, interest
rates, equities, and market volatility -- and tries to *invalidate* the
proposal. It emits a ``SkepticObjection`` (severity + reasoning citing specific
data points) for the Judge to weigh.

Pipeline within the node:

    1. Fetch macro from the injected providers (FRED + Twelve Data) in parallel.
    2. Merge the partial ``MacroContext`` results into one snapshot. If *no*
       provider served any data, return a ``NoMacroData`` sentinel instead of an
       objection -- the Judge reads that as "downgrade confidence to medium"
       (FR-4.3 graceful degradation), NOT as "no objection".
    3. Build a prompt from the proposal + macro snapshot and call Claude via
       ``structured_completion`` with the ``SkepticObjection`` output schema.

Like the Historian, this is a node *factory* (``make_skeptic_node``): the
providers and Anthropic client are injected via closure so nothing has to live
in the checkpointed ``AgentState``. The graph edge wiring (analyzer -> historian
-> skeptic -> judge) is added in Step 2.7; this step only ships the node.

Macro proxy caveat (see the Step 2.3 cost decision): on the Twelve Data free
tier the S&P / VIX *indices* are paywalled, so ``build_macro_providers`` wires
the SPY / VIXY *ETF proxies*. The Skeptic's system prompt is written so the LLM
treats equity/volatility figures as coarse, possibly-proxy regime indicators and
never compares them against absolute thresholds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final

from src.agents.skeptic.models import SkepticObjection
from src.common.llm import DEFAULT_MODEL, structured_completion
from src.common.models import SignalProposal
from src.providers import (
    DataProvider,
    FREDProvider,
    MacroContext,
    NoMacroData,
    TwelveDataProvider,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable, Sequence

    from anthropic import AsyncAnthropic

    from src.agents.orchestration.graph import AgentState
    from src.config import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Free-tier proxy symbols (Step 2.3 cost decision, user-confirmed)
# ---------------------------------------------------------------------------

SPX_PROXY_SYMBOL: Final[str] = "SPY"
"""S&P 500 stand-in on the Twelve Data free tier (the SPX index is paywalled).
SPY tracks the S&P 500 at ~1/10 its level -- a level the Skeptic must read
DIRECTIONALLY / as a regime cue, never against an absolute S&P threshold."""

VIX_PROXY_SYMBOL: Final[str] = "VIXY"
"""Volatility stand-in on the free tier. VIXY is a VIX-FUTURES ETF, not spot
VIX -- so it is NOT comparable to the familiar 'VIX 20' levels. The Skeptic
treats it as a coarse risk-on/off cue only."""


SKEPTIC_SYSTEM_PROMPT: Final[str] = """\
You are the Skeptic in a multi-agent, signal-only crypto trading system. The \
system never places trades; it sends a human a Telegram alert they may act on \
manually. Your role is adversarial and independent: another agent (the \
Analyzer) has proposed a trade using Smart Money Concepts price-structure \
analysis. Your job is to look at MACRO and CROSS-ASSET context the Analyzer \
never saw -- the broad US dollar, interest rates, equities, and market \
volatility -- and find the single STRONGEST reason this specific trade might \
fail.

Operating rules:
- Reason ONLY from the macro context and the proposal's own stated rationale \
that you are given. Never invent data points you were not provided. If a macro \
field is missing, say so and lower your confidence -- absence of data is never \
confirmation.
- You are given a single point-in-time SNAPSHOT of each macro series, not a \
history, so you cannot compute trends. Reason qualitatively about the \
cross-asset risk regime the levels imply; do not fabricate a direction \
('rising' / 'falling') you cannot observe.
- The equity and volatility figures may be ETF PROXIES (e.g. SPY for the S&P \
500, VIXY for volatility) rather than the indices themselves. Treat them as \
coarse regime indicators -- NEVER compare them against absolute thresholds \
(do not say things like 'VIX above 20').
- Crypto tendencies, to use as WEAK priors and not laws: a strong broad US \
dollar and elevated rates are liquidity headwinds for crypto; risk-on equity \
strength tends to support crypto; elevated volatility signals risk-off.
- FORBIDDEN: do not reference RSI, MACD, Bollinger Bands, or moving averages -- \
these indicators are banned in this system.
- Be honest, not contrarian for its own sake. If macro is broadly neutral or \
supportive, say so: emit your best available objection at LOW severity with \
recommends_against = false. Reserve HIGH severity for a clear, strong macro \
contradiction that should likely stop the alert. With only a single snapshot \
of proxy data, most objections are LOW or MEDIUM.
- A human with real money reads your output. Calibrate severity to the \
evidence you actually have.

Emit your verdict by calling the provided tool exactly once."""


# ---------------------------------------------------------------------------
# Macro merging + prompt rendering
# ---------------------------------------------------------------------------


def _merge_macro(contexts: list[MacroContext]) -> MacroContext:
    """Merge partial per-provider snapshots into one combined ``MacroContext``.

    Each provider populates only the fields it owns (FRED: dxy / us10y / fed
    funds; Twelve Data: spx / vix) and leaves the rest None, so a simple
    first-non-None merge reconstructs the full picture. ``fetched_at`` takes the
    most recent timestamp across the inputs.
    """
    fetched_at = max(ctx.fetched_at for ctx in contexts)

    def first(attr: str) -> float | None:
        for ctx in contexts:
            value = getattr(ctx, attr)
            if value is not None:
                return float(value)
        return None

    return MacroContext(
        fetched_at=fetched_at,
        dxy=first("dxy"),
        us10y_yield=first("us10y_yield"),
        spx=first("spx"),
        vix=first("vix"),
        fed_funds=first("fed_funds"),
    )


def _fmt(label: str, value: float | None, unit: str = "") -> str:
    """One macro line: a value with optional unit, or an explicit '(unavailable)'."""
    return f"{label}: {value}{unit}" if value is not None else f"{label}: (unavailable)"


def _render_proposal(proposal: SignalProposal) -> str:
    tp2 = f" | TP2 {proposal.take_profit_2}" if proposal.take_profit_2 is not None else ""
    tags = ", ".join(proposal.tags) if proposal.tags else "(none)"
    features = ", ".join(f"{k}={v}" for k, v in sorted(proposal.features.items())) or "(none)"
    return (
        "PROPOSAL UNDER REVIEW\n"
        f"Symbol: {proposal.symbol}\n"
        f"Direction: {proposal.direction.value}\n"
        f"Strategy: {proposal.strategy}\n"
        f"Entry {proposal.entry_price} | Stop-loss {proposal.stop_loss} | "
        f"TP1 {proposal.take_profit_1}{tp2}\n"
        f"Risk:Reward {proposal.risk_reward_ratio} | Leverage {proposal.leverage}x | "
        f"Risk {proposal.risk_percent}%\n"
        f"Tags: {tags}\n"
        f"Key features: {features}\n"
        "Analyzer's confluence narrative:\n"
        f"{proposal.confluence_narrative}"
    )


def _render_macro(macro: MacroContext) -> str:
    lines = [
        "MACRO / CROSS-ASSET CONTEXT (independent of the Analyzer; latest snapshot)",
        f"Fetched at: {macro.fetched_at.isoformat()}",
        _fmt("Broad USD index (DXY proxy)", macro.dxy),
        _fmt("US 10-year Treasury yield", macro.us10y_yield, "%"),
        _fmt("Effective Fed Funds rate", macro.fed_funds, "%"),
        _fmt("S&P 500 level (may be SPY ETF proxy)", macro.spx),
        _fmt("Market volatility (may be VIXY ETF proxy)", macro.vix),
    ]
    return "\n".join(lines)


def _build_user_prompt(proposal: SignalProposal, macro: MacroContext) -> str:
    return (
        f"{_render_proposal(proposal)}\n\n"
        f"{_render_macro(macro)}\n\n"
        "YOUR TASK\n"
        "Identify the single strongest macro / cross-asset objection to this "
        "proposal, rate its severity, and explain it citing the specific data "
        "points above. Then call the tool with your verdict."
    )


# ---------------------------------------------------------------------------
# Skeptic
# ---------------------------------------------------------------------------


class Skeptic:
    """Macro fetch + adversarial LLM evaluation, decoupled from the graph.

    Holds the macro providers and the Anthropic client/model so the LangGraph
    node (built by ``make_skeptic_node``) stays a thin closure. The providers
    are *borrowed*, not owned: the Skeptic never closes them -- their lifecycle
    belongs to whoever constructed them (the scan runner in Step 2.7).
    """

    def __init__(
        self,
        macro_providers: Sequence[DataProvider],
        *,
        client: AsyncAnthropic | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._macro_providers = tuple(macro_providers)
        self._client = client
        self._model = model

    async def gather_macro(self) -> MacroContext | NoMacroData:
        """Fetch every provider's macro slice in parallel and merge them.

        Returns a merged ``MacroContext`` if *any* provider served data, else a
        ``NoMacroData`` sentinel carrying the collected failure reasons (FR-4.3).
        Provider exceptions are tolerated (the providers already degrade to
        NoMacroData internally, but a programming error must not crash the scan).
        """
        if not self._macro_providers:
            return NoMacroData(provider="skeptic", reason="no macro providers configured")

        results = await asyncio.gather(
            *(p.fetch_macro_context() for p in self._macro_providers),
            return_exceptions=True,
        )

        contexts: list[MacroContext] = []
        reasons: list[str] = []
        for provider, result in zip(self._macro_providers, results, strict=True):
            if isinstance(result, MacroContext):
                contexts.append(result)
            elif isinstance(result, NoMacroData):
                reasons.append(f"{result.provider}: {result.reason}")
            elif isinstance(result, BaseException):
                reasons.append(f"{provider.name}: {type(result).__name__}: {result}")

        if not contexts:
            reason = "; ".join(reasons) or "all macro providers returned no data"
            logger.warning("Skeptic degrading to NoMacroData: %s", reason)
            return NoMacroData(provider="skeptic", reason=reason[:500])

        return _merge_macro(contexts)

    async def evaluate(self, proposal: SignalProposal) -> SkepticObjection | NoMacroData:
        """Produce the strongest macro objection to ``proposal``.

        Returns ``NoMacroData`` (no LLM call) when macro is wholly unavailable;
        otherwise calls Claude with the ``SkepticObjection`` schema and returns
        the validated objection.
        """
        macro = await self.gather_macro()
        if isinstance(macro, NoMacroData):
            return macro

        result = await structured_completion(
            output_schema=SkepticObjection,
            system=SKEPTIC_SYSTEM_PROMPT,
            user=_build_user_prompt(proposal, macro),
            model=self._model,
            client=self._client,
            tool_name="emit_objection",
            tool_description=(
                "Record your strongest macro / cross-asset objection to the proposal, "
                "with an honestly-calibrated severity."
            ),
        )
        logger.debug(
            "Skeptic objection for %s %s: severity=%s cost_usd=%s",
            proposal.symbol,
            proposal.direction.value,
            result.output.severity.value,
            result.cost_usd,
        )
        return result.output


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------


def make_skeptic_node(skeptic: Skeptic) -> Callable[[AgentState], Awaitable[AgentState]]:
    """Build the ``skeptic`` LangGraph node bound to a ``Skeptic`` instance.

    A factory (mirroring ``make_historian_node``) so the providers / client are
    injected via closure rather than living in the checkpointed AgentState. The
    node is a no-op for SkipDecisions: there is no proposal to object to. The
    edge analyzer -> ... -> skeptic is wired in Step 2.7.
    """

    async def skeptic_node(state: AgentState) -> AgentState:
        proposal = state.get("proposal")
        if not isinstance(proposal, SignalProposal):
            return {"skeptic_objection": None}
        objection = await skeptic.evaluate(proposal)
        return {"skeptic_objection": objection}

    return skeptic_node


# ---------------------------------------------------------------------------
# Construction from settings
# ---------------------------------------------------------------------------


def build_macro_providers(settings: Settings) -> list[DataProvider]:
    """Build the configured macro providers from settings (caller owns aclose).

    Returns FRED and/or Twelve Data providers for whichever API keys are present;
    an empty list when neither is configured (the Skeptic then degrades to
    NoMacroData -- the correct behaviour for an environment without macro keys).
    Twelve Data is wired with the SPY / VIXY free-tier ETF proxies per the Step
    2.3 cost decision.

    The returned providers each own an httpx client; the caller is responsible
    for awaiting ``aclose()`` on them (the Step 2.7 scan runner does this).
    """
    providers: list[DataProvider] = []
    if settings.fred_api_key is not None:
        providers.append(FREDProvider(api_key=settings.fred_api_key.get_secret_value()))
    if settings.twelve_data_api_key is not None:
        providers.append(
            TwelveDataProvider(
                api_key=settings.twelve_data_api_key.get_secret_value(),
                spx_symbol=SPX_PROXY_SYMBOL,
                vix_symbol=VIX_PROXY_SYMBOL,
            )
        )
    return providers
