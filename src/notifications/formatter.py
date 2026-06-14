r"""MarkdownV2 message formatters for the signal pipeline.

Per SPEC §3.1.5 FR-5.2, the alert format must include:
- symbol, direction, entry zone, invalidation, targets, R:R
- Historian win-rate statistic (when available)
- Skeptic objection (when present)
- "Signal only - manual execution required" footer

FR-5.3 says Forecaster updates must be distinguishable from new signals via
prefix and formatting. Slice 1 builds only `format_new_signal` and
`format_skip`; `format_forecaster_update` arrives with Slice 2 Step 2.9 but
the design here leaves room (separate function -> separate text, no shared
template).

Slice 1 caveats:
- Historian win-rate is `None` (no Historian agent yet -- Slice 2 Step 2.4).
- Skeptic objection is `None` (no Skeptic agent yet -- Slice 2 Step 2.5).
- Both are accepted as optional parameters so the call site doesn't change
  when Slice 2 starts populating them.

MarkdownV2 escaping discipline:
- Telegram's MarkdownV2 reserves these literal characters everywhere a
  formatting context would otherwise consume them:
      _  *  [  ]  (  )  ~  `  >  #  +  -  =  |  {  }  .  !
  When a price is rendered like `68450.5`, the literal `.` must be escaped
  to `68450\.5` or Telegram returns "can't parse entities".
- The discipline: NEVER interpolate dynamic content directly. Always go
  through `escape_markdown_v2()`. Tests guard this by feeding values
  containing every reserved char and asserting the rendered text round-trips.
- Reserved chars *inside* a code span (between backticks) need not be
  escaped, but escaping them does no harm. To keep one rule, we escape
  everywhere.

References:
- https://core.telegram.org/bots/api#markdownv2-style
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.common.models import ForecastStatus, SignalDirection, SignalProposal, SkipDecision

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Imported for typing only: the forecaster package imports this module, so a
    # runtime import of its model here would create a cycle. Field access in the
    # function body is duck-typed and needs no runtime import.
    from src.agents.forecaster import ForecasterUpdate


# ---------------------------------------------------------------------------
# MarkdownV2 escaping
# ---------------------------------------------------------------------------

# All characters Telegram MarkdownV2 treats as formatting tokens. Every
# occurrence in dynamic content must be preceded by a single backslash.
_MARKDOWN_V2_RESERVED: frozenset[str] = frozenset("_*[]()~`>#+-=|{}.!")


def escape_markdown_v2(text: str) -> str:
    """Escape every MarkdownV2-reserved character in `text` with a backslash.

    Idempotent in spirit but not literally: calling twice would also escape
    the backslashes we inserted. Always apply to raw input, not to already-
    formatted output.
    """
    out: list[str] = []
    for char in text:
        if char in _MARKDOWN_V2_RESERVED:
            out.append("\\")
        out.append(char)
    return "".join(out)


def _format_price(value: float, *, precision: int = 2) -> str:
    """Render a numeric price and escape it for MarkdownV2.

    Centralising the precision rule means every field uses the same rounding,
    which makes the message visually consistent. SignalProposal currently
    stores floats; Slice 2 may switch to Decimal -- if so, this is the one
    function to update.
    """
    rendered = f"{value:.{precision}f}"
    return escape_markdown_v2(rendered)


# ---------------------------------------------------------------------------
# Public formatters
# ---------------------------------------------------------------------------

FOOTER: str = escape_markdown_v2("Signal only - manual execution required")
"""SPEC FR-5.2 mandates this footer verbatim on every alert."""


def format_new_signal(
    proposal: SignalProposal,
    *,
    historian_win_rate: float | None = None,
    historian_sample_size: int | None = None,
    skeptic_objection: str | None = None,
    skeptic_severity: str | None = None,
    caveat: str | None = None,
) -> str:
    """Render a SignalProposal as a Telegram MarkdownV2 message.

    The historian/skeptic/caveat kwargs are populated by the Slice 2 pipeline
    (Step 2.7 live adoption): the win rate + objection come from the Historian
    and Skeptic, and `caveat` is the Judge's one-liner on a PUBLISH_WITH_CAVEAT
    ruling. All default to None so a bare proposal still renders.

    Args:
        proposal: the published trade idea.
        historian_win_rate: 0.0-1.0 win rate from the historian's retrieval.
        historian_sample_size: how many similar historical setups were used.
        skeptic_objection: the skeptic's strongest objection text.
        skeptic_severity: 'low' / 'medium' / 'high'.
        caveat: the Judge's caveat shown on a PUBLISH_WITH_CAVEAT ruling.

    Returns:
        A Telegram-ready MarkdownV2 string. Always ends with the mandated
        "Signal only" footer.
    """
    direction_emoji = "[UP]" if proposal.direction is SignalDirection.LONG else "[DOWN]"
    header = f"*⚡ NEW SIGNAL {direction_emoji}*"  # zap glyph

    # Core fields per FR-5.2.
    lines: list[str] = [
        header,
        "",
        f"*Symbol:* `{escape_markdown_v2(proposal.symbol)}`",
        f"*Strategy:* `{escape_markdown_v2(proposal.strategy)}`",
        f"*Direction:* {escape_markdown_v2(proposal.direction.value)}",
        f"*Entry:* `{_format_price(proposal.entry_price)}`",
        f"*Invalidation \\(SL\\):* `{_format_price(proposal.stop_loss)}`",
        f"*TP1:* `{_format_price(proposal.take_profit_1)}`",
    ]
    if proposal.take_profit_2 is not None:
        lines.append(f"*TP2:* `{_format_price(proposal.take_profit_2)}`")

    rr_rendered = escape_markdown_v2(f"1:{proposal.risk_reward_ratio:.1f}")
    risk_rendered = escape_markdown_v2(f"{proposal.risk_percent:.2f}%")
    leverage_rendered = escape_markdown_v2(f"{proposal.leverage:.0f}x")
    lines.extend(
        [
            f"*R:R:* {rr_rendered}",
            f"*Risk:* {risk_rendered} · *Lev:* {leverage_rendered}",
        ]
    )

    # Judge caveat (PUBLISH_WITH_CAVEAT). Placed high so the warning is seen
    # before the trade levels are acted on.
    if caveat:
        lines.extend(["", f"*⚠ Caveat:* {escape_markdown_v2(caveat)}"])

    # Historian section (Slice 2+).
    if historian_win_rate is not None:
        win_pct = escape_markdown_v2(f"{historian_win_rate * 100:.1f}%")
        sample_chunk = (
            f" \\(n\\={historian_sample_size}\\)" if historian_sample_size is not None else ""
        )
        lines.extend(["", f"*Historian win rate:* {win_pct}{sample_chunk}"])

    # Skeptic section (Slice 2+).
    if skeptic_objection is not None:
        severity_tag = (
            f" \\[{escape_markdown_v2(skeptic_severity.upper())}\\]"
            if skeptic_severity is not None
            else ""
        )
        lines.extend(
            [
                "",
                f"*Skeptic objection{severity_tag}:*",
                f">{escape_markdown_v2(skeptic_objection)}",
            ]
        )

    # Confluence narrative (always present).
    lines.extend(
        [
            "",
            f"*Why:* {escape_markdown_v2(proposal.confluence_narrative)}",
        ]
    )

    # Tags (slice-1-stub, htf-bias-only, etc. surface here for transparency).
    if proposal.tags:
        tag_chunk = " ".join(f"`{escape_markdown_v2(t)}`" for t in proposal.tags)
        lines.extend(["", f"*Tags:* {tag_chunk}"])

    # Mandated footer.
    lines.extend(["", f"_{FOOTER}_"])
    return "\n".join(lines)


def format_forecaster_update(proposal: SignalProposal, update: ForecasterUpdate) -> str:
    """Render a Forecaster verdict on an open setup (FR-2.1 / FR-5.3).

    Distinguished from a NEW SIGNAL by its header so the operator can tell a
    follow-up update apart at a glance. Sent on AT_RISK (a warning) and
    INVALIDATED (a close, carrying the terminal outcome); STILL_VALID does not
    normally reach Telegram.
    """
    status = update.status
    if status is ForecastStatus.AT_RISK:
        header = "*⚠️ SETUP AT RISK*"
    elif status is ForecastStatus.INVALIDATED:
        header = "*🔚 SETUP CLOSED*"
    else:
        header = "*🔄 SETUP UPDATE*"

    lines: list[str] = [
        header,
        "",
        f"*Symbol:* `{escape_markdown_v2(proposal.symbol)}`",
        f"*Direction:* {escape_markdown_v2(proposal.direction.value)}",
        f"*Status:* `{escape_markdown_v2(status.value)}`",
    ]
    if update.outcome is not None:
        lines.append(f"*Outcome:* `{escape_markdown_v2(update.outcome.value)}`")
    lines.extend(["", f"*Why:* {escape_markdown_v2(update.reasoning)}"])
    return "\n".join(lines)


def format_skip(skip: SkipDecision) -> str:
    """Render a SkipDecision for operator transparency.

    Skip messages are not part of FR-5.2 (that mandates alert format for
    PUBLISHED signals). They are useful in Slice 1 for the operator to
    verify the scanner is running. Slice 2 will likely silence routine
    NO_CLEAR_BIAS skips and only surface gate failures via Telegram.
    """
    lines: list[str] = [
        "*\U0001f44b SKIP*",
        "",
        f"*Symbol:* `{escape_markdown_v2(skip.symbol)}`",
        f"*Strategy:* `{escape_markdown_v2(skip.strategy)}`",
        f"*Reason:* `{escape_markdown_v2(skip.reason.value)}`",
    ]
    if skip.violated_rule is not None:
        lines.append(f"*Rule:* `{escape_markdown_v2(skip.violated_rule)}`")
    lines.extend(
        [
            "",
            f"*Details:* {escape_markdown_v2(skip.details)}",
        ]
    )
    return "\n".join(lines)
