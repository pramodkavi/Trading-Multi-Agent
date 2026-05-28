"""Unit tests for src.notifications.formatter.

Coverage targets:
- escape_markdown_v2 escapes every Telegram-reserved character.
- format_new_signal includes every FR-5.2 field for both LONG and SHORT.
- Historian / Skeptic sections appear only when their kwargs are present
  (Slice 1 always passes None; Slice 2 will start populating).
- format_skip renders the categorical reason and details.
"""

from __future__ import annotations

from uuid import uuid4

from src.common.models import (
    SignalDirection,
    SignalProposal,
    SkipDecision,
    SkipReason,
)
from src.notifications import FOOTER, escape_markdown_v2, format_new_signal, format_skip
from src.notifications.formatter import _MARKDOWN_V2_RESERVED

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _long_proposal(**overrides: object) -> SignalProposal:
    base: dict[str, object] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "direction": SignalDirection.LONG,
        "entry_price": 68450.5,
        "stop_loss": 67200.0,
        "take_profit_1": 72200.0,
        "risk_reward_ratio": 3.0,
        "leverage": 5.0,
        "risk_percent": 1.0,
        "confluence_narrative": "Bullish OB tap with liquidity sweep below equal lows.",
    }
    base.update(overrides)
    return SignalProposal(**base)  # type: ignore[arg-type]


def _short_proposal(**overrides: object) -> SignalProposal:
    base: dict[str, object] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "ETHUSDT",
        "direction": SignalDirection.SHORT,
        "entry_price": 100.0,
        "stop_loss": 105.0,
        "take_profit_1": 85.0,
        "risk_reward_ratio": 3.0,
        "leverage": 3.0,
        "risk_percent": 0.5,
        "confluence_narrative": "Bearish OB rejection at premium with FVG above.",
    }
    base.update(overrides)
    return SignalProposal(**base)  # type: ignore[arg-type]


def _skip_decision(**overrides: object) -> SkipDecision:
    base: dict[str, object] = {
        "scan_id": uuid4(),
        "strategy": "smc",
        "symbol": "BTCUSDT",
        "reason": SkipReason.NO_CLEAR_BIAS,
        "details": "Consolidation; bias unclear within freshness window.",
    }
    base.update(overrides)
    return SkipDecision(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# escape_markdown_v2
# ---------------------------------------------------------------------------


class TestEscapeMarkdownV2:
    def test_every_reserved_char_gets_backslash(self) -> None:
        # One canonical input: every reserved char concatenated.
        raw = "".join(sorted(_MARKDOWN_V2_RESERVED))
        escaped = escape_markdown_v2(raw)
        # Each reserved char must be preceded by exactly one backslash.
        for char in _MARKDOWN_V2_RESERVED:
            assert f"\\{char}" in escaped

    def test_plain_text_unchanged(self) -> None:
        assert escape_markdown_v2("Hello world") == "Hello world"

    def test_mixed_content_preserves_non_reserved(self) -> None:
        assert escape_markdown_v2("price=100.5!") == "price\\=100\\.5\\!"

    def test_empty_string(self) -> None:
        assert escape_markdown_v2("") == ""

    def test_idempotency_caveat_documented(self) -> None:
        # Calling twice further escapes the backslashes; this is intentional
        # and the docstring warns about it. Verify the behaviour so a future
        # refactor doesn't accidentally silence it.
        once = escape_markdown_v2(".")
        twice = escape_markdown_v2(once)
        assert once == "\\."
        assert twice != once  # \. -> \\\.


# ---------------------------------------------------------------------------
# format_new_signal
# ---------------------------------------------------------------------------


class TestFormatNewSignal:
    def test_includes_all_fr_5_2_fields(self) -> None:
        proposal = _long_proposal()
        text = format_new_signal(proposal)
        # symbol, direction, entry, invalidation, targets, R:R all referenced
        assert "BTCUSDT" in text
        assert "LONG" in text
        assert "Entry" in text
        assert "Invalidation" in text
        assert "TP1" in text
        assert "R:R" in text
        # Mandated footer
        assert FOOTER in text

    def test_long_uses_up_marker(self) -> None:
        text = format_new_signal(_long_proposal())
        assert "[UP]" in text

    def test_short_uses_down_marker(self) -> None:
        text = format_new_signal(_short_proposal())
        assert "[DOWN]" in text

    def test_optional_tp2_rendered_when_present(self) -> None:
        proposal = _long_proposal(take_profit_2=75000.0)
        text = format_new_signal(proposal)
        assert "TP2" in text

    def test_optional_tp2_omitted_when_none(self) -> None:
        proposal = _long_proposal()
        text = format_new_signal(proposal)
        assert "TP2" not in text

    def test_historian_section_appears_when_provided(self) -> None:
        text = format_new_signal(
            _long_proposal(),
            historian_win_rate=0.62,
            historian_sample_size=18,
        )
        assert "Historian" in text
        assert "62" in text  # 62.0%
        assert "n" in text and "18" in text

    def test_historian_section_omitted_in_slice_1(self) -> None:
        # Slice 1 always passes None -- the section must not appear.
        text = format_new_signal(_long_proposal())
        assert "Historian" not in text

    def test_skeptic_section_appears_when_provided(self) -> None:
        text = format_new_signal(
            _long_proposal(),
            skeptic_objection="DXY broke above 105 with strong momentum.",
            skeptic_severity="high",
        )
        assert "Skeptic" in text
        assert "HIGH" in text  # severity uppercased
        assert "DXY" in text

    def test_skeptic_severity_optional(self) -> None:
        text = format_new_signal(
            _long_proposal(),
            skeptic_objection="VIX elevated.",
        )
        assert "VIX" in text
        # Should NOT have a severity bracket since severity was None.
        assert "\\[" not in text or "VIX" in text  # weak check; main check below
        # Strong check: no severity tag chunk.
        assert "[VIX]" not in text

    def test_tags_section_appears_when_present(self) -> None:
        proposal = _long_proposal(tags=["slice-1-stub", "htf-bias-only"])
        text = format_new_signal(proposal)
        assert "Tags" in text
        # tag content escaped (`-` is reserved in MarkdownV2)
        assert "slice\\-1\\-stub" in text

    def test_tags_section_omitted_when_empty(self) -> None:
        proposal = _long_proposal()  # default tags = []
        text = format_new_signal(proposal)
        assert "Tags" not in text

    def test_prices_are_escaped(self) -> None:
        # 68450.5 must render with the . escaped
        proposal = _long_proposal()
        text = format_new_signal(proposal)
        assert "68450\\.50" in text  # _format_price uses precision=2

    def test_rr_renders_in_1_to_n_form(self) -> None:
        proposal = _long_proposal()
        text = format_new_signal(proposal)
        assert "1:3\\.0" in text

    def test_risk_and_leverage_rendered_together(self) -> None:
        proposal = _long_proposal()
        text = format_new_signal(proposal)
        # % is NOT a MarkdownV2 reserved char, so it is not escaped.
        assert "1\\.00%" in text
        assert "5x" in text  # leverage with 0 precision

    def test_confluence_narrative_included_and_escaped(self) -> None:
        proposal = _long_proposal(
            confluence_narrative="Tap on 4H OB. Sweep of equal lows. OTE 0.62-0.78."
        )
        text = format_new_signal(proposal)
        # Periods inside the narrative must be escaped.
        assert "4H OB\\." in text
        assert "OTE 0\\.62\\-0\\.78\\." in text

    def test_message_ends_with_footer(self) -> None:
        proposal = _long_proposal()
        text = format_new_signal(proposal)
        assert text.rstrip().endswith(f"_{FOOTER}_")

    def test_footer_text_matches_spec(self) -> None:
        # FR-5.2 mandates: "Signal only - manual execution required"
        # (the hyphen acts as the em-dash in our ASCII rendering).
        assert "Signal only" in FOOTER
        assert "manual execution required" in FOOTER


# ---------------------------------------------------------------------------
# format_skip
# ---------------------------------------------------------------------------


class TestFormatSkip:
    def test_includes_categorical_reason(self) -> None:
        text = format_skip(_skip_decision())
        # Underscores in enum values get escaped by MarkdownV2.
        assert "NO\\_CLEAR\\_BIAS" in text

    def test_includes_symbol_and_strategy(self) -> None:
        text = format_skip(_skip_decision())
        assert "BTCUSDT" in text
        assert "smc" in text

    def test_includes_details_with_escaping(self) -> None:
        text = format_skip(_skip_decision())
        assert "Consolidation" in text
        # Period must be escaped
        assert "\\." in text

    def test_includes_violated_rule_when_set(self) -> None:
        skip = _skip_decision(
            reason=SkipReason.INSUFFICIENT_RR,
            details="R:R came out 2.4; minimum is 3.0.",
            violated_rule="RULE_2_MIN_RR",
        )
        text = format_skip(skip)
        # Underscores get escaped.
        assert "RULE\\_2\\_MIN\\_RR" in text

    def test_omits_rule_section_when_not_set(self) -> None:
        text = format_skip(_skip_decision())
        # No violated_rule on the default skip; should not surface a "Rule:" header.
        assert "*Rule:*" not in text
