"""Alarm-notifier Lambda: post CloudWatch alarms to Telegram (Step 2.12).

Subscribed to the alarm SNS topic (MonitoringStack). CloudWatch publishes alarm
state changes to SNS as a JSON string in each record's ``Sns.Message``; this
handler parses them, formats a short plain-text message, and sends it to the
operator's existing Telegram bot.

Reuses the scan container image (CMD overridden to this handler) so there is one
image to build/patch. It reads the Telegram token + chat id from the same SSM
SecureString parameter the scan Lambda uses (``TELEGRAM_PARAM_NAME``), via the
shared ``src.config.secrets`` hydration -- it does NOT load full ``Settings``
(which would require the Anthropic key / DB config the notifier has no business
holding).

Design: parsing (``extract_alarm_messages``) and formatting (``format_alarm``)
are pure functions, unit-tested without AWS or network; ``lambda_handler`` is the
thin side-effecting shell.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from src.config.secrets import resolve_secrets
from src.notifications import TelegramNotifier

logger = logging.getLogger(__name__)

# Telegram caps a message at 4096 chars; keep well under with margin for safety.
_MAX_TELEGRAM_CHARS = 3500

_STATE_EMOJI = {
    "ALARM": "\U0001f6a8",  # 🚨
    "OK": "✅",  # ✅
    "INSUFFICIENT_DATA": "⚠️",  # ⚠️
}


def extract_alarm_messages(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse the SNS event into a list of alarm payload dicts.

    Each SNS record carries the CloudWatch alarm JSON as a string in
    ``Sns.Message``. A message that is not JSON (e.g. a manual test publish) is
    wrapped as ``{"_raw": <message>}`` so the handler still forwards something
    useful instead of crashing.
    """
    messages: list[dict[str, Any]] = []
    for record in event.get("Records", []):
        sns = record.get("Sns") or {}
        raw = sns.get("Message", "")
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            messages.append(parsed)
        else:
            messages.append({"_raw": str(raw), "_subject": sns.get("Subject")})
    return messages


def format_alarm(alarm: dict[str, Any]) -> str:
    """Build the plain-text Telegram body for one alarm payload.

    Plain text (the notifier sends with parse_mode disabled) so alarm reasons --
    which contain ``()``, ``>``, ``.`` etc. -- need no MarkdownV2 escaping.
    """
    if "_raw" in alarm:
        # info glyph built from code points (literal U+2139 trips ruff RUF001).
        info = chr(0x2139) + chr(0xFE0F)
        subject = alarm.get("_subject")
        head = f"{info} SNS message: {subject}" if subject else f"{info} SNS message"
        body = f"{head}\n\n{alarm['_raw']}"
        return body[:_MAX_TELEGRAM_CHARS]

    name = alarm.get("AlarmName", "(unknown alarm)")
    new_state = alarm.get("NewStateValue", "?")
    old_state = alarm.get("OldStateValue", "?")
    emoji = _STATE_EMOJI.get(str(new_state), "\U0001f4e2")  # 📢 fallback
    lines = [
        f"{emoji} CloudWatch alarm: {name}",
        f"State: {old_state} -> {new_state}",
    ]
    description = alarm.get("AlarmDescription")
    if description:
        lines.append(str(description))
    reason = alarm.get("NewStateReason")
    if reason:
        lines.append("")
        lines.append(str(reason))
    region = alarm.get("Region")
    when = alarm.get("StateChangeTime")
    footer = " | ".join(part for part in (region, when) if part)
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)[:_MAX_TELEGRAM_CHARS]


def _telegram_credentials() -> tuple[str, str]:
    """Resolve the Telegram bot token + chat id (SSM in Lambda, env locally).

    Raises RuntimeError if neither source provides them, so a misconfiguration
    surfaces loudly in the notifier's own CloudWatch logs.
    """
    resolved = resolve_secrets()
    token = resolved.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = resolved.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "Telegram credentials unavailable: set TELEGRAM_PARAM_NAME (SSM) or "
            "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID directly."
        )
    return token, chat_id


async def _send_all(texts: list[str], *, token: str, chat_id: str) -> None:
    notifier = TelegramNotifier(token=token, chat_id=chat_id, parse_mode="")
    try:
        for text in texts:
            await notifier.send(text)
    finally:
        await notifier.aclose()


def lambda_handler(event: dict[str, Any] | None, context: object) -> dict[str, Any]:
    """AWS Lambda entry point: forward each alarm in the SNS event to Telegram."""
    payload = event or {}
    alarms = extract_alarm_messages(payload)
    if not alarms:
        logger.info("alarm-notifier invoked with no SNS records; nothing to send")
        return {"ok": True, "sent": 0}

    texts = [format_alarm(alarm) for alarm in alarms]
    token, chat_id = _telegram_credentials()
    asyncio.run(_send_all(texts, token=token, chat_id=chat_id))
    logger.info("alarm-notifier forwarded %d alarm(s) to Telegram", len(texts))
    return {"ok": True, "sent": len(texts)}
