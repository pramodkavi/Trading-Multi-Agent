"""Unit tests for the Skeptic agent (Slice 2 Step 2.5).

Covers, all offline (no network):
- macro merge + parallel gather (success, all-unavailable, exception-tolerant)
- evaluate(): LLM path with a mocked Anthropic client; macro-unavailable short
  circuit (no LLM call) returning NoMacroData (FR-4.3)
- the LangGraph node: skip for non-proposals; objection / NoMacroData for proposals
- prompt construction (proposal + macro + proxy caveats) and system-prompt guards
- build_macro_providers() wiring from Settings (incl. the SPY / VIXY proxies)
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.agents.skeptic import (
    SPX_PROXY_SYMBOL,
    VIX_PROXY_SYMBOL,
    Skeptic,
    SkepticObjection,
    build_macro_providers,
    make_skeptic_node,
)
from src.agents.skeptic.skeptic import (
    SKEPTIC_SYSTEM_PROMPT,
    _build_user_prompt,
    _merge_macro,
)
from src.common.models import (
    ObjectionSeverity,
    SignalDirection,
    SignalProposal,
    SkipDecision,
    SkipReason,
)
from src.config import Settings
from src.providers import (
    DataProvider,
    FREDProvider,
    MacroContext,
    MarketSnapshot,
    NoMacroData,
    ProviderUnavailableError,
    Timeframe,
    TwelveDataProvider,
)

# ---------------------------------------------------------------------------
# Fakes / builders
# ---------------------------------------------------------------------------


class FakeMacroProvider(DataProvider):
    """Returns a preset MacroContext / NoMacroData, or raises a preset exception."""

    def __init__(self, result: MacroContext | NoMacroData | BaseException, *, name: str) -> None:
        self.name = name
        self._result = result
        self.calls = 0

    async def fetch_market_snapshot(
        self,
        symbol: str,
        timeframes: list[Timeframe],
        *,
        limit: int = 200,
        include_derivatives: bool = False,
    ) -> MarketSnapshot:
        raise NotImplementedError

    async def fetch_macro_context(self) -> MacroContext | NoMacroData:
        self.calls += 1
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _fake_tool_response(
    payload: dict[str, Any],
    *,
    tool_name: str = "emit_objection",
    tokens_in: int = 120,
    tokens_out: int = 60,
) -> SimpleNamespace:
    """Mimic the anthropic Message shape that structured_completion reads."""
    block = SimpleNamespace(type="tool_use", name=tool_name, id="toolu_x", input=payload)
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out),
    )


def _mock_client(responses: list[Any]) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    return client


_VALID_OBJECTION: dict[str, Any] = {
    "severity": "MEDIUM",
    "recommends_against": True,
    "headline": "Strong dollar is a liquidity headwind for this long.",
    "reasoning": "The broad USD index level is elevated, which historically pressures "
    "crypto longs; the volatility proxy suggests a risk-off lean.",
    "cited_macro": ["broad USD index 104.2", "volatility proxy 18.4"],
}


def _t(hour: int = 12) -> datetime:
    return datetime(2026, 6, 1, hour, tzinfo=UTC)


def _fred_ctx() -> MacroContext:
    return MacroContext(fetched_at=_t(11), dxy=104.2, us10y_yield=4.25, fed_funds=5.33)


def _twelve_ctx() -> MacroContext:
    return MacroContext(fetched_at=_t(12), spx=540.1, vix=18.4)


def make_proposal(**overrides: Any) -> SignalProposal:
    base: dict[str, Any] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "direction": SignalDirection.LONG,
        "entry_price": 100.0,
        "stop_loss": 97.0,
        "take_profit_1": 109.0,
        "risk_reward_ratio": 3.0,
        "leverage": 3.0,
        "risk_percent": 1.0,
        "tags": ["smc", "bullish-ob", "liquidity-sweep", "discount"],
        "confluence_narrative": "Bullish OB in discount with a liquidity sweep below equal lows.",
        "features": {
            "primary_poi_type": "order_block",
            "confluence_score": 4,
            "ob_confluence_count": 2,
        },
    }
    base.update(overrides)
    return SignalProposal(**base)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "anthropic_api_key": "sk-ant-test",
        "telegram_bot_token": "123:ABC",
        "database_url": "postgresql://u:p@localhost:5432/db",
        "telegram_chat_id": "111",
        "_env_file": None,  # isolate from the real .env
    }
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Macro merge
# ---------------------------------------------------------------------------


def test_merge_macro_combines_partial_snapshots() -> None:
    merged = _merge_macro([_fred_ctx(), _twelve_ctx()])
    assert merged.dxy == 104.2
    assert merged.us10y_yield == 4.25
    assert merged.fed_funds == 5.33
    assert merged.spx == 540.1
    assert merged.vix == 18.4
    # Most recent timestamp wins.
    assert merged.fetched_at == _t(12)


def test_merge_macro_first_non_none_wins() -> None:
    a = MacroContext(fetched_at=_t(10), dxy=100.0)
    b = MacroContext(fetched_at=_t(11), dxy=999.0, vix=20.0)
    merged = _merge_macro([a, b])
    assert merged.dxy == 100.0  # first non-None
    assert merged.vix == 20.0  # only b had it


# ---------------------------------------------------------------------------
# gather_macro
# ---------------------------------------------------------------------------


async def test_gather_macro_merges_available() -> None:
    skeptic = Skeptic(
        [
            FakeMacroProvider(_fred_ctx(), name="fred"),
            FakeMacroProvider(_twelve_ctx(), name="twelvedata"),
        ]
    )
    macro = await skeptic.gather_macro()
    assert isinstance(macro, MacroContext)
    assert macro.dxy == 104.2
    assert macro.spx == 540.1


async def test_gather_macro_all_unavailable_returns_nomacrodata() -> None:
    skeptic = Skeptic(
        [
            FakeMacroProvider(NoMacroData(provider="fred", reason="down"), name="fred"),
            FakeMacroProvider(NoMacroData(provider="twelvedata", reason="429"), name="twelvedata"),
        ]
    )
    macro = await skeptic.gather_macro()
    assert isinstance(macro, NoMacroData)
    assert "fred" in macro.reason
    assert "twelvedata" in macro.reason


async def test_gather_macro_no_providers_returns_nomacrodata() -> None:
    macro = await Skeptic([]).gather_macro()
    assert isinstance(macro, NoMacroData)
    assert "no macro providers" in macro.reason


async def test_gather_macro_tolerates_exception_uses_survivor() -> None:
    skeptic = Skeptic(
        [
            FakeMacroProvider(ProviderUnavailableError("boom", provider="fred"), name="fred"),
            FakeMacroProvider(_twelve_ctx(), name="twelvedata"),
        ]
    )
    macro = await skeptic.gather_macro()
    assert isinstance(macro, MacroContext)
    assert macro.spx == 540.1
    assert macro.dxy is None


async def test_gather_macro_all_exceptions_returns_nomacrodata() -> None:
    skeptic = Skeptic(
        [FakeMacroProvider(ProviderUnavailableError("boom", provider="fred"), name="fred")]
    )
    macro = await skeptic.gather_macro()
    assert isinstance(macro, NoMacroData)
    assert "fred" in macro.reason


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


async def test_evaluate_returns_objection() -> None:
    client = _mock_client([_fake_tool_response(_VALID_OBJECTION)])
    skeptic = Skeptic([FakeMacroProvider(_fred_ctx(), name="fred")], client=client)
    result = await skeptic.evaluate(make_proposal())
    assert isinstance(result, SkepticObjection)
    assert result.severity is ObjectionSeverity.MEDIUM
    assert result.recommends_against is True
    assert "broad USD index 104.2" in result.cited_macro
    client.messages.create.assert_awaited_once()


async def test_evaluate_macro_unavailable_skips_llm() -> None:
    client = _mock_client([_fake_tool_response(_VALID_OBJECTION)])
    skeptic = Skeptic(
        [FakeMacroProvider(NoMacroData(provider="fred", reason="down"), name="fred")],
        client=client,
    )
    result = await skeptic.evaluate(make_proposal())
    assert isinstance(result, NoMacroData)
    client.messages.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------


async def test_node_skips_non_proposal() -> None:
    client = _mock_client([_fake_tool_response(_VALID_OBJECTION)])
    fake = FakeMacroProvider(_fred_ctx(), name="fred")
    node = make_skeptic_node(Skeptic([fake], client=client))

    skip = SkipDecision(
        scan_id=uuid4(),
        strategy="smc",
        symbol="BTCUSDT",
        reason=SkipReason.NO_CLEAR_BIAS,
        details="no setup",
    )
    for proposal in (None, skip):
        out = await node({"proposal": proposal})
        assert out == {"skeptic_objection": None}
    # Never fetched macro or called the LLM for a non-proposal.
    assert fake.calls == 0
    client.messages.create.assert_not_awaited()


async def test_node_proposal_with_macro_returns_objection() -> None:
    client = _mock_client([_fake_tool_response(_VALID_OBJECTION)])
    node = make_skeptic_node(Skeptic([FakeMacroProvider(_fred_ctx(), name="fred")], client=client))
    out = await node({"proposal": make_proposal()})
    objection = out["skeptic_objection"]
    assert isinstance(objection, SkepticObjection)
    assert objection.severity is ObjectionSeverity.MEDIUM


async def test_node_proposal_no_macro_returns_nomacrodata() -> None:
    node = make_skeptic_node(Skeptic([]))  # no providers -> NoMacroData
    out = await node({"proposal": make_proposal()})
    assert isinstance(out["skeptic_objection"], NoMacroData)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def test_user_prompt_includes_proposal_macro_and_proxy_caveats() -> None:
    prompt = _build_user_prompt(make_proposal(), _merge_macro([_fred_ctx(), _twelve_ctx()]))
    # Proposal facts.
    assert "BTCUSDT" in prompt
    assert "LONG" in prompt
    assert "liquidity sweep" in prompt
    # Macro facts + the proxy caveats the Skeptic must heed.
    assert "104.2" in prompt
    assert "540.1" in prompt
    assert "SPY ETF proxy" in prompt
    assert "VIXY ETF proxy" in prompt
    assert "snapshot" in prompt


def test_user_prompt_marks_unavailable_macro_fields() -> None:
    prompt = _build_user_prompt(make_proposal(), _fred_ctx())  # no spx / vix
    assert "(unavailable)" in prompt


def test_system_prompt_forbids_indicators_and_absolute_thresholds() -> None:
    assert "RSI" in SKEPTIC_SYSTEM_PROMPT
    assert "MACD" in SKEPTIC_SYSTEM_PROMPT
    assert "signal-only" in SKEPTIC_SYSTEM_PROMPT
    assert "absolute thresholds" in SKEPTIC_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# build_macro_providers
# ---------------------------------------------------------------------------


def test_build_macro_providers_empty_when_no_keys() -> None:
    assert build_macro_providers(_settings()) == []


async def test_build_macro_providers_wires_both_with_proxies() -> None:
    settings = _settings(fred_api_key="fred-key", twelve_data_api_key="td-key")
    providers = build_macro_providers(settings)
    try:
        assert len(providers) == 2
        assert isinstance(providers[0], FREDProvider)
        assert isinstance(providers[1], TwelveDataProvider)
        # Free-tier ETF proxies wired per the Step 2.3 cost decision.
        assert providers[1]._spx_symbol == SPX_PROXY_SYMBOL
        assert providers[1]._vix_symbol == VIX_PROXY_SYMBOL
    finally:
        for provider in providers:
            await provider.aclose()


async def test_build_macro_providers_fred_only() -> None:
    providers = build_macro_providers(_settings(fred_api_key="fred-key"))
    try:
        assert len(providers) == 1
        assert isinstance(providers[0], FREDProvider)
    finally:
        for provider in providers:
            await provider.aclose()
