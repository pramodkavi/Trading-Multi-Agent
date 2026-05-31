"""Unit tests for scripts.run_scan with all external services mocked.

No network, no DB, no Telegram, no Anthropic. We inject:
- a mock DataProvider returning a synthetic MarketSnapshot,
- a mock AsyncAnthropic client returning a valid MarketCommentary tool call,
- a mock asyncpg connection (execute is all the write path needs),
- a mock Notifier.

The genuine live run is exercised manually by invoking scripts/run_scan.py;
there is no marked integration test because it would require real credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.run_scan import (
    MarketCommentary,
    compose_message,
    generate_commentary,
    run_one_symbol,
)
from src.common.models import (
    ScanStatus,
    SignalProposal,
    SkipDecision,
    SkipReason,
)
from src.config import Settings
from src.providers import Kline, MarketSnapshot, Timeframe

# ---------------------------------------------------------------------------
# Synthetic market data (mirrors test_smc_analyzer factories)
# ---------------------------------------------------------------------------

_ANCHOR = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)


def _kline(idx: int, *, open_: float, high: float, low: float, close: float) -> Kline:
    return Kline(
        open_time=_ANCHOR + timedelta(minutes=idx * 240),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100.0,
    )


def _flat(idx: int, *, level: float) -> Kline:
    return _kline(idx, open_=level, high=level + 0.1, low=level - 0.1, close=level)


def _swing_high(idx: int, *, peak: float, base: float) -> Kline:
    return _kline(idx, open_=base, high=peak, low=base - 0.1, close=base)


def _swing_low(idx: int, *, trough: float, base: float) -> Kline:
    return _kline(idx, open_=base, high=base + 0.1, low=trough, close=base)


def _bullish_series() -> list[Kline]:
    candles: list[Kline] = [_flat(i, level=100.0) for i in range(5)]
    candles.append(_swing_low(5, trough=99.0, base=100.0))
    candles.extend(_flat(i, level=101.0) for i in range(6, 10))
    candles.append(_swing_high(10, peak=103.0, base=101.5))
    candles.extend(_flat(i, level=102.0) for i in range(11, 15))
    candles.append(_swing_low(15, trough=101.0, base=102.0))
    candles.extend(_flat(i, level=103.0) for i in range(16, 20))
    candles.append(_swing_high(20, peak=106.0, base=103.5))
    candles.extend(_flat(i, level=104.0) for i in range(21, 30))
    return candles


def _ranging_series() -> list[Kline]:
    candles: list[Kline] = [_flat(i, level=100.0) for i in range(5)]
    candles.append(_swing_low(5, trough=99.0, base=100.0))
    candles.extend(_flat(i, level=100.5) for i in range(6, 10))
    candles.append(_swing_high(10, peak=102.0, base=100.5))
    candles.extend(_flat(i, level=100.0) for i in range(11, 15))
    candles.append(_swing_low(15, trough=98.0, base=100.0))
    candles.extend(_flat(i, level=101.0) for i in range(16, 20))
    candles.append(_swing_high(20, peak=103.0, base=101.0))
    candles.extend(_flat(i, level=101.0) for i in range(21, 30))
    return candles


def _snapshot(candles: list[Kline], symbol: str = "BTCUSDT") -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        venue="binance",
        fetched_at=datetime(2026, 5, 31, 0, 0, 0, tzinfo=UTC),
        klines={Timeframe.H4: candles},
    )


# ---------------------------------------------------------------------------
# Mock builders
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        anthropic_api_key="sk-ant-test",
        telegram_bot_token="123:ABC",
        database_url="postgresql://u:p@localhost:5433/db",
        telegram_chat_id="111",
        _env_file=None,
    )


def _commentary_response(
    *,
    commentary: str = "Structure is constructive but unconfirmed on lower timeframes.",
    key_risk: str = "A failure to hold the swing low invalidates the read.",
    tokens_in: int = 120,
    tokens_out: int = 60,
) -> SimpleNamespace:
    block = SimpleNamespace(
        type="tool_use",
        name="emit_structured_output",
        id="toolu_x",
        input={"commentary": commentary, "key_risk": key_risk},
    )
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out),
    )


def _anthropic_client(response: SimpleNamespace | None = None) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=response or _commentary_response())
    return client


def _provider(snapshot: MarketSnapshot) -> MagicMock:
    provider = MagicMock()
    provider.fetch_market_snapshot = AsyncMock(return_value=snapshot)
    provider.aclose = AsyncMock()
    return provider


def _conn() -> MagicMock:
    conn = MagicMock()
    conn.execute = AsyncMock()
    return conn


def _notifier() -> MagicMock:
    notifier = MagicMock()
    notifier.send = AsyncMock()
    notifier.aclose = AsyncMock()
    return notifier


# ---------------------------------------------------------------------------
# generate_commentary
# ---------------------------------------------------------------------------


class TestGenerateCommentary:
    async def test_returns_validated_commentary(self) -> None:
        state = {
            "snapshot": _snapshot(_bullish_series()),
            "proposal": None,
            "decision": None,
        }
        # Build a real proposal-bearing state by running the graph path is
        # unnecessary here; the summary handles missing proposal gracefully.
        result = await generate_commentary(
            settings=_settings(),
            symbol="BTCUSDT",
            state=state,  # type: ignore[arg-type]
            client=_anthropic_client(),
        )
        assert isinstance(result.output, MarketCommentary)
        assert result.tokens_in == 120
        assert result.tokens_out == 60

    async def test_prompt_includes_symbol_and_decision(self) -> None:
        client = _anthropic_client()
        state = {"snapshot": _snapshot(_bullish_series()), "proposal": None, "decision": None}
        await generate_commentary(
            settings=_settings(),
            symbol="ETHUSDT",
            state=state,  # type: ignore[arg-type]
            client=client,
        )
        sent_user_msg = client.messages.create.await_args.kwargs["messages"][0]["content"]
        assert "ETHUSDT" in sent_user_msg


# ---------------------------------------------------------------------------
# compose_message
# ---------------------------------------------------------------------------


class TestComposeMessage:
    def _proposal(self) -> SignalProposal:
        from uuid import uuid4

        return SignalProposal(
            scan_id=uuid4(),
            strategy="smc",
            symbol="BTCUSDT",
            direction="LONG",  # type: ignore[arg-type]
            entry_price=100.0,
            stop_loss=95.0,
            take_profit_1=115.0,
            risk_reward_ratio=3.0,
            leverage=5.0,
            risk_percent=1.0,
            confluence_narrative="Bullish OB tap with liquidity sweep below equal lows.",
        )

    def _skip(self) -> SkipDecision:
        from uuid import uuid4

        return SkipDecision(
            scan_id=uuid4(),
            strategy="smc",
            symbol="BTCUSDT",
            reason=SkipReason.NO_CLEAR_BIAS,
            details="Consolidation; no actionable bias.",
        )

    def test_proposal_message_includes_signal_and_note(self) -> None:
        commentary = MarketCommentary(
            commentary="Trend intact above the reclaimed level.",
            key_risk="Macro headwinds could cap upside.",
        )
        state = {"proposal": self._proposal(), "decision": None, "snapshot": None}
        msg = compose_message(state, commentary)  # type: ignore[arg-type]
        assert "NEW SIGNAL" in msg
        assert "Analyst note:" in msg
        assert "Key risk:" in msg

    def test_skip_message_includes_skip_and_note(self) -> None:
        commentary = MarketCommentary(
            commentary="No edge; price mid-range.",
            key_risk="Chasing here risks a fakeout.",
        )
        state = {"proposal": self._skip(), "decision": None, "snapshot": None}
        msg = compose_message(state, commentary)  # type: ignore[arg-type]
        assert "SKIP" in msg
        assert "Analyst note:" in msg

    def test_commentary_is_markdown_escaped(self) -> None:
        commentary = MarketCommentary(
            commentary="Watch 1.0 support.",  # the '.' must be escaped
            key_risk="Risk is real.",
        )
        state = {"proposal": self._skip(), "decision": None, "snapshot": None}
        msg = compose_message(state, commentary)  # type: ignore[arg-type]
        assert "1\\.0" in msg


# ---------------------------------------------------------------------------
# run_one_symbol — full orchestration
# ---------------------------------------------------------------------------


class TestRunOneSymbol:
    async def test_bullish_publishes_persists_and_notifies(self) -> None:
        provider = _provider(_snapshot(_bullish_series()))
        conn = _conn()
        notifier = _notifier()
        client = _anthropic_client()

        ctx = await run_one_symbol(
            settings=_settings(),
            symbol="BTCUSDT",
            provider=provider,
            conn=conn,
            notifier=notifier,
            anthropic_client=client,
        )

        # Provider fetched the snapshot.
        provider.fetch_market_snapshot.assert_awaited_once()
        # LLM was called.
        client.messages.create.assert_awaited_once()
        # Telegram sent.
        notifier.send.assert_awaited_once()
        # DB writes happened: start_scan, create_signal, log_run, complete_scan
        # all funnel through conn.execute.
        assert conn.execute.await_count >= 4
        assert ctx.strategy == "smc"

    async def test_ranging_skips_but_still_persists_and_notifies(self) -> None:
        provider = _provider(_snapshot(_ranging_series()))
        conn = _conn()
        notifier = _notifier()
        client = _anthropic_client()

        await run_one_symbol(
            settings=_settings(),
            symbol="BTCUSDT",
            provider=provider,
            conn=conn,
            notifier=notifier,
            anthropic_client=client,
        )
        # Even on a skip, the LLM is called and a message is sent.
        client.messages.create.assert_awaited_once()
        notifier.send.assert_awaited_once()

    async def test_no_notifier_skips_telegram(self) -> None:
        provider = _provider(_snapshot(_bullish_series()))
        conn = _conn()
        client = _anthropic_client()

        await run_one_symbol(
            settings=_settings(),
            symbol="BTCUSDT",
            provider=provider,
            conn=conn,
            notifier=None,
            anthropic_client=client,
        )
        # No notifier -> no send, but DB + LLM still happen.
        client.messages.create.assert_awaited_once()
        assert conn.execute.await_count >= 4

    async def test_provider_failure_marks_scan_failed(self) -> None:
        provider = MagicMock()
        provider.fetch_market_snapshot = AsyncMock(side_effect=RuntimeError("binance down"))
        conn = _conn()
        notifier = _notifier()

        with pytest.raises(RuntimeError, match="binance down"):
            await run_one_symbol(
                settings=_settings(),
                symbol="BTCUSDT",
                provider=provider,
                conn=conn,
                notifier=notifier,
                anthropic_client=_anthropic_client(),
            )
        # start_scan + fail_scan both executed; no notify on failure.
        assert conn.execute.await_count >= 2
        notifier.send.assert_not_awaited()

    async def test_fail_scan_sql_uses_failed_status(self) -> None:
        provider = MagicMock()
        provider.fetch_market_snapshot = AsyncMock(side_effect=RuntimeError("boom"))
        conn = _conn()

        with pytest.raises(RuntimeError):
            await run_one_symbol(
                settings=_settings(),
                symbol="BTCUSDT",
                provider=provider,
                conn=conn,
                notifier=None,
                anthropic_client=_anthropic_client(),
            )
        # One of the execute calls must carry the FAILED status value.
        all_args = [call.args for call in conn.execute.await_args_list]
        flat = [a for args in all_args for a in args]
        assert ScanStatus.FAILED.value in flat
