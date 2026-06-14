"""Judge agent: final arbiter of the per-signal pipeline (Step 2.6).

SPEC §3.1 role 4 / FR-1.6. The Judge weighs three already-gathered inputs --
the Analyzer's ``SignalProposal``, the Historian's empirical ``HistorianReport``,
and the Skeptic's ``SkepticObjection`` (or ``NoMacroData``) -- and rules
PUBLISH / PUBLISH_WITH_CAVEAT / SKIP with written reasoning.

Unlike the Skeptic it fetches nothing; it arbitrates what the upstream agents
produced. Two behaviours are enforced deterministically rather than left to the
LLM:

- FR-4.3 macro-unavailable cap: if the Skeptic returned ``NoMacroData``, the
  Judge's confidence is capped at MEDIUM after the LLM call (the system prompt
  also asks for this, but the cap guarantees it regardless of compliance).
- The ``decision`` state field is set to the ruling enum so the existing
  downstream consumers (the Telegram dispatcher / scan runner) keep working
  unchanged.

Like the Historian and Skeptic this is a node *factory* (``make_judge_node``):
the Anthropic client/model is injected via closure. The graph edge wiring
(analyzer -> historian -> skeptic -> judge -> notify_or_skip) is Step 2.7; this
step only ships the node, so the live path stays analyzer -> END.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from src.agents.judge.models import JudgeDecision
from src.agents.skeptic import SkepticObjection
from src.common.llm import DEFAULT_MODEL, structured_completion
from src.common.models import JudgeConfidence, JudgeRuling, SignalProposal
from src.providers import NoMacroData

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

    from anthropic import AsyncAnthropic

    from src.agents.historian import HistorianReport
    from src.agents.orchestration.graph import AgentState

logger = logging.getLogger(__name__)


SkepticInput = SkepticObjection | NoMacroData | None


JUDGE_SYSTEM_PROMPT: Final[str] = """\
You are the Judge, the final arbiter in a multi-agent, signal-only crypto \
trading system. The system never places trades; a PUBLISH ruling sends a human \
a Telegram alert they act on MANUALLY with real money. You are given three \
inputs about one proposed trade and must return exactly one ruling: PUBLISH, \
PUBLISH_WITH_CAVEAT, or SKIP.

The asymmetry that governs your decision: a missed signal costs nothing; a bad \
published signal costs the user real money. Favour precision over recall. When \
genuinely uncertain, prefer PUBLISH_WITH_CAVEAT or SKIP over PUBLISH.

How to weigh the three inputs:
- PROPOSAL (the Analyzer's structural case): a clean, structurally sound setup \
with a healthy reward-to-risk ratio is the baseline reason to publish. You do \
not re-derive the price analysis; take its structure as given.
- HISTORIAN (empirical track record): this is a raw win rate over a SAMPLE of \
similar past setups. Weight it by sample size -- a small sample is weak \
evidence either way, and 'no decisive precedents' means ABSENCE of evidence, \
which you treat neutrally, NOT as a bad sign. A strong win rate over a healthy \
sample supports PUBLISH; a poor win rate over a healthy sample supports SKIP.
- SKEPTIC (macro objection, severity-rated): a HIGH-severity, well-reasoned \
objection is a strong push toward SKIP. MEDIUM usually warrants \
PUBLISH_WITH_CAVEAT, using the objection as the caveat. LOW is minor. If the \
Skeptic reports MACRO DATA UNAVAILABLE, do NOT read that as 'no objection': cap \
your confidence at MEDIUM and lean cautious.

Calibration guide:
- strong proposal + supportive history + weak objection  -> PUBLISH (high confidence)
- strong proposal + weak/poor history + strong objection  -> SKIP
- borderline or mixed evidence                            -> PUBLISH_WITH_CAVEAT

Rules:
- Your reasoning must cite the SPECIFIC inputs you weighed (the actual win rate \
and sample size, the objection severity, the proposal's reward-to-risk). It is \
journaled and may be shown to the user.
- The hard numeric risk rules (max 1% risk, minimum 1:3 reward-to-risk, \
leverage cap) are enforced separately by code -- do not re-check them; focus on \
the qualitative weighing.
- Never reference forbidden indicators (RSI, MACD, Bollinger Bands, moving \
averages). Never invent data not given to you.
- If you rule PUBLISH_WITH_CAVEAT you MUST provide a one-line caveat for the \
alert; leave it empty for PUBLISH and SKIP.

Emit your ruling by calling the provided tool exactly once."""


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _render_proposal(proposal: SignalProposal) -> str:
    tp2 = f" | TP2 {proposal.take_profit_2}" if proposal.take_profit_2 is not None else ""
    tags = ", ".join(proposal.tags) if proposal.tags else "(none)"
    return (
        "PROPOSAL\n"
        f"Symbol: {proposal.symbol} | Direction: {proposal.direction.value} | "
        f"Strategy: {proposal.strategy}\n"
        f"Entry {proposal.entry_price} | Stop-loss {proposal.stop_loss} | "
        f"TP1 {proposal.take_profit_1}{tp2}\n"
        f"Reward-to-risk {proposal.risk_reward_ratio} | Leverage {proposal.leverage}x | "
        f"Risk {proposal.risk_percent}%\n"
        f"Tags: {tags}\n"
        f"Analyzer narrative: {proposal.confluence_narrative}"
    )


def _render_historian(report: HistorianReport | None) -> str:
    if report is None:
        return "HISTORIAN TRACK RECORD\n(No historian report available.)"
    if report.win_rate is None:
        win_rate = "n/a (no decisive win/loss precedents)"
    else:
        win_rate = f"{report.win_rate * 100:.0f}% over {report.decisive} decisive outcomes"
    return (
        "HISTORIAN TRACK RECORD\n"
        f"Similar past setups: {report.sample_size} "
        f"({report.wins}W / {report.losses}L / {report.breakeven}BE / "
        f"{report.inconclusive} inconclusive)\n"
        f"Empirical win rate: {win_rate}\n"
        f"Summary: {report.summary}"
    )


def _render_skeptic(objection: SkepticInput) -> str:
    if objection is None:
        return "SKEPTIC OBJECTION\n(No skeptic objection available.)"
    if isinstance(objection, NoMacroData):
        return (
            "SKEPTIC OBJECTION\n"
            f"MACRO DATA UNAVAILABLE -- the Skeptic could not fetch macro context "
            f"({objection.reason}). Treat this as a reason for CAUTION (cap confidence "
            "at MEDIUM), NOT as an all-clear."
        )
    cited = "; ".join(objection.cited_macro) if objection.cited_macro else "(none)"
    return (
        "SKEPTIC OBJECTION\n"
        f"Severity: {objection.severity.value} | Recommends against: "
        f"{objection.recommends_against}\n"
        f"Headline: {objection.headline}\n"
        f"Reasoning: {objection.reasoning}\n"
        f"Cited macro: {cited}"
    )


def _build_user_prompt(
    proposal: SignalProposal,
    historian_report: HistorianReport | None,
    skeptic_objection: SkepticInput,
) -> str:
    return (
        f"{_render_proposal(proposal)}\n\n"
        f"{_render_historian(historian_report)}\n\n"
        f"{_render_skeptic(skeptic_objection)}\n\n"
        "YOUR TASK\n"
        "Weigh these three inputs and rule PUBLISH, PUBLISH_WITH_CAVEAT, or SKIP. "
        "Set your confidence, justify the ruling citing the specific numbers "
        "above, and (only for PUBLISH_WITH_CAVEAT) give a one-line caveat. Then "
        "call the tool with your ruling."
    )


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


class Judge:
    """Arbitrates proposal + historian + skeptic into a ruling via one LLM call.

    Holds only the Anthropic client/model -- it fetches no data. Construct once
    and reuse across scans; the LangGraph node (``make_judge_node``) wraps it.
    """

    def __init__(
        self,
        *,
        client: AsyncAnthropic | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._client = client
        self._model = model

    async def evaluate(
        self,
        proposal: SignalProposal,
        historian_report: HistorianReport | None,
        skeptic_objection: SkepticInput,
    ) -> JudgeDecision:
        """Produce the terminal ruling for ``proposal``.

        Calls Claude with the ``JudgeDecision`` schema, then applies the FR-4.3
        macro-unavailable confidence cap deterministically.
        """
        result = await structured_completion(
            output_schema=JudgeDecision,
            system=JUDGE_SYSTEM_PROMPT,
            user=_build_user_prompt(proposal, historian_report, skeptic_objection),
            model=self._model,
            client=self._client,
            tool_name="emit_ruling",
            tool_description="Record your terminal ruling on the proposal with written reasoning.",
        )
        decision = _cap_confidence_if_macro_unavailable(result.output, skeptic_objection)
        logger.debug(
            "Judge ruling for %s %s: %s (confidence=%s, cost_usd=%s)",
            proposal.symbol,
            proposal.direction.value,
            decision.ruling.value,
            decision.confidence.value,
            result.cost_usd,
        )
        return decision


def _cap_confidence_if_macro_unavailable(
    decision: JudgeDecision,
    skeptic_objection: SkepticInput,
) -> JudgeDecision:
    """Enforce FR-4.3: macro unavailable -> confidence capped at MEDIUM.

    A frozen-model copy (rather than mutation) so the contract that
    JudgeDecision is immutable holds. No-op unless the Skeptic returned
    NoMacroData and the LLM nonetheless claimed HIGH confidence.
    """
    if isinstance(skeptic_objection, NoMacroData) and decision.confidence is JudgeConfidence.HIGH:
        logger.info("Capping Judge confidence HIGH->MEDIUM: macro context was unavailable (FR-4.3)")
        return decision.model_copy(update={"confidence": JudgeConfidence.MEDIUM})
    return decision


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------


def make_judge_node(judge: Judge) -> Callable[[AgentState], Awaitable[AgentState]]:
    """Build the ``judge`` LangGraph node bound to a ``Judge`` instance.

    A factory (mirroring make_historian_node / make_skeptic_node) so the client
    is injected via closure. For a non-proposal (SkipDecision / None) there is
    nothing to publish, so the node rules SKIP without an LLM call. Otherwise it
    reads the historian report + skeptic objection already in state, evaluates,
    and sets BOTH ``judge_decision`` (the full object) and ``decision`` (the
    ruling enum the existing dispatcher consumes). Edge wiring is Step 2.7.
    """

    async def judge_node(state: AgentState) -> AgentState:
        proposal = state.get("proposal")
        if not isinstance(proposal, SignalProposal):
            return {"judge_decision": None, "decision": JudgeRuling.SKIP}
        decision = await judge.evaluate(
            proposal,
            state.get("historian_report"),
            state.get("skeptic_objection"),
        )
        return {"judge_decision": decision, "decision": decision.ruling}

    return judge_node
