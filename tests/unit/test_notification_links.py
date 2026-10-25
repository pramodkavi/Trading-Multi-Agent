"""Unit tests for the dashboard deep-link helpers and their use in the formatters (Step 4.7 Part B).

Coverage targets:
- ``signal_url`` builds ``<base>/signals/<id>`` only from a usable https base URL.
- ``link_line`` renders a MarkdownV2 inline link and escapes the reserved characters Telegram
  requires inside the link target.
- The formatters append the link only when given a URL, and without one their output is unchanged.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from src.agents.forecaster import ForecasterUpdate
from src.common.models import (
    ForecastStatus,
    SignalDirection,
    SignalOutcome,
    SignalProposal,
    SkipDecision,
    SkipReason,
)
from src.notifications import (
    FOOTER,
    escape_markdown_v2,
    format_forecaster_update,
    format_new_signal,
    format_skip,
    link_line,
    signal_url,
)
from src.notifications.formatter import _MARKDOWN_V2_RESERVED

SIGNAL_ID = UUID("11111111-1111-4111-8111-111111111111")
BASE = "https://d111.cloudfront.net"
URL = f"{BASE}/signals/{SIGNAL_ID}"
LINK = f"[Open in dashboard]({URL})"


def _proposal() -> SignalProposal:
    return SignalProposal(
        scan_id=uuid4(),
        strategy="smc",
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        entry_price=68450.5,
        stop_loss=67200.0,
        take_profit_1=72200.0,
        risk_reward_ratio=3.0,
        leverage=5.0,
        risk_percent=1.0,
        confluence_narrative="Bullish OB tap with liquidity sweep below equal lows.",
    )


def _skip() -> SkipDecision:
    return SkipDecision(
        scan_id=uuid4(),
        strategy="smc",
        symbol="BTCUSDT",
        reason=SkipReason.NO_CLEAR_BIAS,
        details="Consolidation; bias unclear within freshness window.",
    )


def _update(status: ForecastStatus = ForecastStatus.AT_RISK) -> ForecasterUpdate:
    outcome = SignalOutcome.LOSS if status is ForecastStatus.INVALIDATED else None
    return ForecasterUpdate(
        status=status,
        reasoning="Price is nearing the stop at 67200 and the structure is weakening.",
        outcome=outcome,
    )


class TestSignalUrl:
    @pytest.mark.parametrize("base", [None, "", "   "])
    def test_no_base_url_gives_no_link(self, base: str | None) -> None:
        assert signal_url(base, SIGNAL_ID) is None

    @pytest.mark.parametrize(
        "base",
        [
            "http://x.example",
            "javascript:alert(1)",
            "ftp://x.example",
            "https://",
            "d111.cloudfront.net",
        ],
    )
    def test_only_https_urls_with_a_host_are_accepted(self, base: str) -> None:
        assert signal_url(base, SIGNAL_ID) is None

    @pytest.mark.parametrize("base", [BASE, f"{BASE}/", f"{BASE}//"])
    def test_exactly_one_slash_before_signals(self, base: str) -> None:
        assert signal_url(base, SIGNAL_ID) == URL


class TestLinkLine:
    def test_none_gives_no_line(self) -> None:
        assert link_line(None) is None

    def test_renders_a_markdown_v2_inline_link(self) -> None:
        assert link_line(URL) == LINK

    def test_escapes_backslash_and_closing_paren_in_the_target(self) -> None:
        line = link_line("https://x.example/a)b\\c")
        assert line == "[Open in dashboard](https://x.example/a\\)b\\\\c)"

    def test_reserved_characters_in_the_target_never_raise(self) -> None:
        hostile = "https://x.example/" + "".join(sorted(_MARKDOWN_V2_RESERVED)) + "\\"
        line = link_line(hostile)
        assert line is not None
        assert line.startswith("[Open in dashboard](")


class TestFormatters:
    def test_new_signal_link_sits_above_the_footer_and_footer_stays_last(self) -> None:
        text = format_new_signal(_proposal(), signal_url=URL)
        assert LINK in text
        assert text.index(LINK) < text.index(FOOTER)
        assert text.endswith(f"_{FOOTER}_")

    def test_new_signal_without_url_is_unchanged(self) -> None:
        proposal = _proposal()
        plain = format_new_signal(proposal)
        assert format_new_signal(proposal, signal_url=None) == plain
        assert "dashboard" not in plain
        assert format_new_signal(proposal, signal_url=URL).replace(f"\n{LINK}\n", "") == plain

    def test_skip_link_is_appended_and_absent_without_url(self) -> None:
        skip = _skip()
        plain = format_skip(skip)
        with_link = format_skip(skip, signal_url=URL)
        assert with_link == f"{plain}\n\n{LINK}"
        assert format_skip(skip, signal_url=None) == plain

    @pytest.mark.parametrize("status", [ForecastStatus.AT_RISK, ForecastStatus.INVALIDATED])
    def test_forecaster_update_link_is_appended_and_absent_without_url(
        self, status: ForecastStatus
    ) -> None:
        proposal, update = _proposal(), _update(status)
        plain = format_forecaster_update(proposal, update)
        assert format_forecaster_update(proposal, update, signal_url=URL) == f"{plain}\n\n{LINK}"
        assert format_forecaster_update(proposal, update, signal_url=None) == plain

    def test_escape_helper_still_escapes_the_label_alphabet(self) -> None:
        assert escape_markdown_v2("Open in dashboard") == "Open in dashboard"
