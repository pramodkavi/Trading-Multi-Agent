"""Notification channels for signal alerts and operational updates.

Public API per SPEC §3.1.5: agents call `Notifier.send(text)` against the
abstract interface; concrete implementations (TelegramNotifier today, future
DiscordNotifier / SNSNotifier / etc.) handle the channel specifics.

The src/notifications/formatter.py module is the formatter side of the
contract: pure functions that turn domain models into channel-ready text.
Separating formatting from transport keeps both halves independently
testable.
"""

from src.notifications.base import (
    Notifier,
    NotifierAuthError,
    NotifierBadRequestError,
    NotifierError,
    NotifierRateLimitError,
    NotifierUnavailableError,
)
from src.notifications.formatter import (
    FOOTER,
    escape_markdown_v2,
    format_new_signal,
    format_skip,
)
from src.notifications.telegram import TelegramNotifier

__all__ = [
    "FOOTER",
    "Notifier",
    "NotifierAuthError",
    "NotifierBadRequestError",
    "NotifierError",
    "NotifierRateLimitError",
    "NotifierUnavailableError",
    "TelegramNotifier",
    "escape_markdown_v2",
    "format_new_signal",
    "format_skip",
]
