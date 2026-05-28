"""Notifier abstract base + typed exception hierarchy.

Per SPEC §3.1.5 FR-5.1, signal alerts are delivered through a `Notifier`
interface so the agent code never imports a specific channel client. Slice 1
ships only the TelegramNotifier; Discord / SNS / email can be added later as
new concrete classes without touching the agents that call `await notifier.send(text)`.

Why a thin `send(text)` rather than typed `send_signal(...)` methods:
- Formatting (cheap, pure, easy to unit-test) is separated from transport
  (slow, mocked at the boundary). The src/notifications/formatter.py module
  produces the text; this interface only delivers it.
- Adding a new message variant (Slice 2's Forecaster updates per FR-5.3,
  Slice 3's Critic weekly summary) requires only a new formatter function,
  not a new method on every Notifier implementation.

Exception hierarchy follows the same pattern as DataProvider (Step 1.4): a
single base, subclassed by failure mode. Lets callers either catch
NotifierError broadly or react to specific transient vs permanent failures.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class NotifierError(Exception):
    """Base class for every notifier failure.

    Carries the notifier name so logs can attribute the failure without the
    caller introspecting types.
    """

    def __init__(self, message: str, *, notifier: str) -> None:
        super().__init__(message)
        self.notifier = notifier


class NotifierAuthError(NotifierError):
    """Credentials rejected by the channel (401 / 403 from Telegram).

    Indicates a misconfigured bot token, a revoked bot, or that the bot was
    blocked / kicked by the target chat. Not retryable.
    """


class NotifierBadRequestError(NotifierError):
    """The channel rejected the request as malformed (Telegram 400).

    Usually a Markdown parse error or an invalid chat_id. Not retryable;
    surface so the operator can fix the formatter or config.
    """


class NotifierRateLimitError(NotifierError):
    """Channel rate limit exceeded (Telegram 429).

    Caller may retry after a delay. Telegram returns a retry_after field;
    we expose it so the orchestration layer can respect it.
    """

    def __init__(
        self,
        message: str,
        *,
        notifier: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, notifier=notifier)
        self.retry_after_seconds = retry_after_seconds


class NotifierUnavailableError(NotifierError):
    """Channel reachable but cannot serve the request (5xx, DNS, timeout).

    Retryable by callers with appropriate backoff.
    """


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class Notifier(ABC):
    """Interface every notification channel implements.

    Concrete implementations (TelegramNotifier, future DiscordNotifier, etc.)
    each own their HTTP client and translate channel-specific errors into the
    typed hierarchy above. Agents only need to import this base.
    """

    name: str  # concrete subclasses set this; used in error messages and traces

    @abstractmethod
    async def send(self, text: str) -> None:
        """Deliver a pre-formatted message to the channel.

        Args:
            text: ready-to-send message body. Formatting (MarkdownV2 escaping,
                  layout) is the caller's responsibility -- see
                  src/notifications/formatter.py.

        Raises:
            NotifierAuthError: credentials rejected by the channel.
            NotifierBadRequestError: channel rejected the body as malformed.
            NotifierRateLimitError: caller exceeded the channel's rate limit.
            NotifierUnavailableError: channel unreachable or returned 5xx.
        """

    async def aclose(self) -> None:  # noqa: B027  (intentional no-op default)
        """Release any held async resources (HTTP clients, etc.).

        Default no-op; implementations that own a client override.
        """

    async def __aenter__(self) -> Notifier:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()
