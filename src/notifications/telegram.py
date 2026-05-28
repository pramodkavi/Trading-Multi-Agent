"""TelegramNotifier: httpx-based notification channel for the Telegram Bot API.

Per SPEC §3.1.5 FR-5.1, we use direct httpx calls -- no python-telegram-bot
or aiogram wrappers. The Bot API surface we need is one endpoint
(`sendMessage`), so a library wrapper would only add maintenance overhead.

Lifecycle:
    async with TelegramNotifier(token=..., chat_id=...) as notifier:
        await notifier.send(text)
    # client closed automatically

The notifier owns its `httpx.AsyncClient`. Step 1.12 (scan runner) will pass
the constructed notifier into the orchestration code as a context manager
so the same HTTP connection pool is reused across the scan.

Error translation:
    Telegram returns HTTP 200 with a JSON body `{"ok": false, ...}` on
    application errors, *and* it returns non-200 codes in some cases. We
    handle both paths:
    - 401 / 403 -> NotifierAuthError (bad token / bot blocked)
    - 400      -> NotifierBadRequestError (bad chat_id, malformed Markdown)
    - 429      -> NotifierRateLimitError, with retry_after_seconds populated
                  from `Retry-After` header or `parameters.retry_after` body
    - 5xx      -> NotifierUnavailableError
    - body {"ok": false} -> map by error_code (Telegram uses HTTP-style codes
                            inside the body too)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

import httpx

from src.notifications.base import (
    Notifier,
    NotifierAuthError,
    NotifierBadRequestError,
    NotifierError,
    NotifierRateLimitError,
    NotifierUnavailableError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL: Final[str] = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0
DEFAULT_PARSE_MODE: Final[str] = "MarkdownV2"


class TelegramNotifier(Notifier):
    """Concrete Notifier that posts to the Telegram Bot API.

    Construction does not validate the token -- the first `send()` call is
    what surfaces auth failures. This keeps the constructor pure (no I/O)
    and aligns with the Step 1.6 / 1.4 pattern of deferring side effects
    until the first method call.
    """

    name = "telegram"

    def __init__(
        self,
        *,
        token: str,
        chat_id: str | int,
        client: httpx.AsyncClient | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        parse_mode: str = DEFAULT_PARSE_MODE,
    ) -> None:
        """Construct a TelegramNotifier.

        Args:
            token: bot token from @BotFather. Treat as a secret -- never
                log or expose. Production sourced from AWS Secrets Manager
                via the config layer (Step 1.11).
            chat_id: target chat. Accepts int (user / group) or string
                (channel handle like '@my_channel') as Telegram's API does.
            client: optional pre-built httpx.AsyncClient. Tests inject a
                mock here. Production passes None to get a fresh one with
                our timeout.
            base_url: override for tests / on-prem deployments. Defaults to
                https://api.telegram.org.
            timeout_seconds: per-request timeout. 10s is generous; Telegram
                usually responds well under 1s.
            parse_mode: MarkdownV2 by default. Pass an empty string to
                disable parsing (plain text).
        """
        if not token:
            raise ValueError("token must be a non-empty string")
        self._token = token
        self._chat_id = chat_id
        self._base_url = base_url.rstrip("/")
        self._parse_mode = parse_mode
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(
            timeout=timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, text: str) -> None:
        """Post a message to the configured chat.

        Raises:
            NotifierAuthError | NotifierBadRequestError
            | NotifierRateLimitError | NotifierUnavailableError as
            documented on the Notifier base class.
        """
        url = f"{self._base_url}/bot{self._token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if self._parse_mode:
            payload["parse_mode"] = self._parse_mode

        try:
            response = await self._client.post(url, json=payload)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise NotifierUnavailableError(
                f"network error contacting Telegram: {exc}",
                notifier=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            raise NotifierUnavailableError(
                f"unexpected httpx error: {exc}",
                notifier=self.name,
            ) from exc

        self._raise_for_response(response)

    # ------------------------------------------------------------------
    # Response translation
    # ------------------------------------------------------------------

    def _raise_for_response(self, response: httpx.Response) -> None:
        """Inspect the response and raise the right NotifierError variant.

        Telegram is inconsistent: most errors arrive as non-200 with a JSON
        body containing `{ok: false, error_code: int, description: str}`.
        Some 200 responses also have `{ok: false}` (rare but real). We
        handle both paths through one decision tree.
        """
        # Try to extract the Telegram payload regardless of status code.
        # If JSON parsing fails, fall back to the status code alone.
        body: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = parsed
        except (ValueError, httpx.DecodingError):
            body = {}

        ok = body.get("ok", response.status_code == 200)
        if ok:
            return  # success path

        description = body.get("description") or response.text or "(no body)"
        # Telegram's body-level error_code mirrors the HTTP status in most
        # cases; prefer it when present so retry_after-bearing 429s with
        # exotic HTTP statuses still map correctly.
        error_code = body.get("error_code", response.status_code)

        if error_code in (401, 403):
            raise NotifierAuthError(
                f"Telegram rejected credentials ({error_code}): {description}",
                notifier=self.name,
            )
        if error_code == 400:
            raise NotifierBadRequestError(
                f"Telegram rejected the request as malformed: {description}",
                notifier=self.name,
            )
        if error_code == 429:
            raise NotifierRateLimitError(
                f"Telegram rate limit hit: {description}",
                notifier=self.name,
                retry_after_seconds=_extract_retry_after(body, response),
            )
        if error_code >= 500:
            raise NotifierUnavailableError(
                f"Telegram server error ({error_code}): {description}",
                notifier=self.name,
            )

        # Anything else (e.g., 4xx not handled above): treat as Bad Request
        # so the operator sees the description and we don't accidentally
        # retry something Telegram already rejected.
        raise NotifierError(
            f"Telegram returned error_code={error_code}: {description}",
            notifier=self.name,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_retry_after(body: dict[str, Any], response: httpx.Response) -> float | None:
    """Return Telegram's retry-after window in seconds, or None.

    Telegram puts the value in two possible places:
        1. body.parameters.retry_after (int seconds)
        2. Retry-After response header (int seconds, RFC 7231)
    Prefer the body value when present -- it is more precise.
    """
    parameters = body.get("parameters")
    if isinstance(parameters, dict):
        retry = parameters.get("retry_after")
        if isinstance(retry, int | float):
            return float(retry)
    header = response.headers.get("retry-after")
    if header is not None:
        try:
            return float(header)
        except ValueError:
            return None
    return None
