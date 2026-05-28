"""Manual smoke test: send one message to a Telegram bot.

Per SPEC §4 Step 1.10: "Add a manual test script to actually send a message".
Not run by pytest -- this is the script the operator runs once after
configuring a new bot to verify end-to-end delivery.

Usage:
    export TELEGRAM_BOT_TOKEN="<token from @BotFather>"
    export TELEGRAM_CHAT_ID="<your chat id; @userinfobot reports it>"
    python scripts/send_test_telegram.py
    python scripts/send_test_telegram.py --skip-formatted    # only send 'hello'

The script sends two messages:
    1. A simple 'hello' to confirm the bot can reach the chat.
    2. A formatted SignalProposal so the operator can eyeball the layout.

If either fails, the operator sees the typed NotifierError variant in the
error path, which is the same exception the production agents would see.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING
from uuid import uuid4

from src.common.models import SignalDirection, SignalProposal
from src.notifications import (
    NotifierError,
    TelegramNotifier,
    escape_markdown_v2,
    format_new_signal,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "TELEGRAM_CHAT_ID"


def _example_proposal() -> SignalProposal:
    """Build a plausible-looking BTCUSDT LONG so the operator can eyeball the layout."""
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
        tags=["slice-1-stub", "htf-bias-only", "bias-uptrend"],
        confluence_narrative=(
            "4H structure shows higher highs and higher lows after sweep "
            "of equal lows at 67,000. SMC: bullish OB tap."
        ),
    )


async def _run(*, token: str, chat_id: str | int, skip_formatted: bool) -> None:
    async with TelegramNotifier(token=token, chat_id=chat_id) as notifier:
        hello = escape_markdown_v2("Hello from crypto-signals-system! Bot is configured correctly.")
        logger.info("Sending hello message")
        await notifier.send(hello)

        if not skip_formatted:
            proposal = _example_proposal()
            logger.info("Sending example formatted signal")
            await notifier.send(format_new_signal(proposal))


def _resolve_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(
            f"Set {name} before running this script. "
            "Get a token from @BotFather; find your chat id via @userinfobot."
        )
    return value


def _coerce_chat_id(raw: str) -> str | int:
    """Telegram accepts numeric ids or '@channel' handles; preserve either."""
    if raw.startswith("@"):
        return raw
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(
            f"{CHAT_ID_ENV} must be a numeric id or '@channel' handle, got {raw!r}"
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="send_test_telegram",
        description="Send one or two test messages to a configured Telegram bot.",
    )
    parser.add_argument(
        "--skip-formatted",
        action="store_true",
        help="Only send the 'hello' message; skip the formatted-signal demo.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    token = _resolve_env(TOKEN_ENV)
    chat_id = _coerce_chat_id(_resolve_env(CHAT_ID_ENV))

    try:
        asyncio.run(
            _run(
                token=token,
                chat_id=chat_id,
                skip_formatted=args.skip_formatted,
            )
        )
    except NotifierError as exc:
        logger.error("Telegram delivery failed: %s (notifier=%s)", exc, exc.notifier)
        return 1

    logger.info("Done. Check your Telegram chat.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
