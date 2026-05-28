"""Unit tests for src.notifications.telegram.TelegramNotifier.

These tests do not touch the network. They verify the boundary:
- successful 200 -> no exception
- 400/401/403/429/5xx -> mapped to the right NotifierError subclass
- 200 body with {ok: false} -> mapped per body error_code
- retry_after extraction from body.parameters or Retry-After header
- network errors (timeout / connect) -> NotifierUnavailableError
- async context manager closes the underlying client

The real Telegram contract is exercised manually via
scripts/send_test_telegram.py (no marked integration test -- that would
require a live bot token we don't want to ship credentials for).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.notifications import (
    NotifierAuthError,
    NotifierBadRequestError,
    NotifierError,
    NotifierRateLimitError,
    NotifierUnavailableError,
    TelegramNotifier,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    status_code: int,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a MagicMock that mimics httpx.Response for the bits we read."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.headers = headers or {}
    response.text = "{}" if body is None else str(body)
    response.json = MagicMock(return_value=body if body is not None else {"ok": True})
    return response


def _make_notifier(
    response: MagicMock | None = None,
    side_effect: BaseException | None = None,
) -> tuple[TelegramNotifier, AsyncMock]:
    """Build a TelegramNotifier with a mocked httpx.AsyncClient."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(return_value=response, side_effect=side_effect)
    client.aclose = AsyncMock()
    notifier = TelegramNotifier(
        token="test-token",
        chat_id=12345,
        client=client,
    )
    return notifier, client.post


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_empty_token_rejected(self) -> None:
        with pytest.raises(ValueError, match="token"):
            TelegramNotifier(token="", chat_id=1)

    def test_string_chat_id_accepted(self) -> None:
        notifier = TelegramNotifier(token="t", chat_id="@my_channel")
        # No exception; lazy construction means no network call yet.
        assert notifier.name == "telegram"


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


class TestSuccessfulSend:
    async def test_posts_to_correct_url_with_payload(self) -> None:
        response = _make_response(200, {"ok": True, "result": {"message_id": 99}})
        notifier, post = _make_notifier(response)

        await notifier.send("hello")

        post.assert_awaited_once()
        call_args = post.await_args
        # First positional is the URL
        assert call_args.args[0] == "https://api.telegram.org/bottest-token/sendMessage"
        payload = call_args.kwargs["json"]
        assert payload["chat_id"] == 12345
        assert payload["text"] == "hello"
        assert payload["parse_mode"] == "MarkdownV2"
        assert payload["disable_web_page_preview"] is True

    async def test_no_parse_mode_when_disabled(self) -> None:
        response = _make_response(200, {"ok": True, "result": {"message_id": 99}})
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=response)
        client.aclose = AsyncMock()
        notifier = TelegramNotifier(
            token="t",
            chat_id=1,
            client=client,
            parse_mode="",
        )
        await notifier.send("hi")
        payload = client.post.await_args.kwargs["json"]
        assert "parse_mode" not in payload


# ---------------------------------------------------------------------------
# HTTP error -> typed exception mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:
    async def test_400_maps_to_bad_request(self) -> None:
        response = _make_response(
            400,
            {"ok": False, "error_code": 400, "description": "Bad chat_id"},
        )
        notifier, _ = _make_notifier(response)
        with pytest.raises(NotifierBadRequestError, match="Bad chat_id"):
            await notifier.send("x")

    async def test_401_maps_to_auth(self) -> None:
        response = _make_response(
            401,
            {"ok": False, "error_code": 401, "description": "Unauthorized"},
        )
        notifier, _ = _make_notifier(response)
        with pytest.raises(NotifierAuthError):
            await notifier.send("x")

    async def test_403_maps_to_auth(self) -> None:
        response = _make_response(
            403,
            {"ok": False, "error_code": 403, "description": "Bot blocked by user"},
        )
        notifier, _ = _make_notifier(response)
        with pytest.raises(NotifierAuthError):
            await notifier.send("x")

    async def test_429_maps_to_rate_limit_with_retry_after_from_body(self) -> None:
        response = _make_response(
            429,
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 7},
            },
        )
        notifier, _ = _make_notifier(response)
        with pytest.raises(NotifierRateLimitError) as exc_info:
            await notifier.send("x")
        assert exc_info.value.retry_after_seconds == 7.0

    async def test_429_falls_back_to_retry_after_header(self) -> None:
        response = _make_response(
            429,
            {"ok": False, "error_code": 429, "description": "rate-limited"},
            headers={"retry-after": "3"},
        )
        notifier, _ = _make_notifier(response)
        with pytest.raises(NotifierRateLimitError) as exc_info:
            await notifier.send("x")
        assert exc_info.value.retry_after_seconds == 3.0

    async def test_429_without_retry_after(self) -> None:
        response = _make_response(
            429,
            {"ok": False, "error_code": 429, "description": "rate-limited"},
        )
        notifier, _ = _make_notifier(response)
        with pytest.raises(NotifierRateLimitError) as exc_info:
            await notifier.send("x")
        assert exc_info.value.retry_after_seconds is None

    async def test_500_maps_to_unavailable(self) -> None:
        response = _make_response(
            500,
            {"ok": False, "error_code": 500, "description": "Internal error"},
        )
        notifier, _ = _make_notifier(response)
        with pytest.raises(NotifierUnavailableError):
            await notifier.send("x")

    async def test_503_maps_to_unavailable(self) -> None:
        response = _make_response(
            503,
            {"ok": False, "error_code": 503, "description": "Unavailable"},
        )
        notifier, _ = _make_notifier(response)
        with pytest.raises(NotifierUnavailableError):
            await notifier.send("x")

    async def test_200_with_ok_false_body_still_raises(self) -> None:
        # Rare but real Telegram quirk: 200 OK with {ok: false} inside.
        response = _make_response(
            200,
            {"ok": False, "error_code": 400, "description": "Markdown parse error"},
        )
        notifier, _ = _make_notifier(response)
        with pytest.raises(NotifierBadRequestError, match="Markdown"):
            await notifier.send("x")

    async def test_unknown_4xx_falls_back_to_generic_notifier_error(self) -> None:
        response = _make_response(
            418,
            {"ok": False, "error_code": 418, "description": "I'm a teapot"},
        )
        notifier, _ = _make_notifier(response)
        with pytest.raises(NotifierError) as exc_info:
            await notifier.send("x")
        # Must NOT be one of the more specific subclasses.
        specific = (
            NotifierAuthError,
            NotifierBadRequestError,
            NotifierRateLimitError,
            NotifierUnavailableError,
        )
        assert not isinstance(exc_info.value, specific)


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------


class TestNetworkErrors:
    async def test_timeout_maps_to_unavailable(self) -> None:
        notifier, _ = _make_notifier(side_effect=httpx.TimeoutException("slow"))
        with pytest.raises(NotifierUnavailableError, match="network error"):
            await notifier.send("x")

    async def test_connect_error_maps_to_unavailable(self) -> None:
        notifier, _ = _make_notifier(side_effect=httpx.ConnectError("dns"))
        with pytest.raises(NotifierUnavailableError):
            await notifier.send("x")

    async def test_other_http_error_maps_to_unavailable(self) -> None:
        # Catch-all branch for unexpected httpx errors.
        notifier, _ = _make_notifier(side_effect=httpx.HTTPError("weird"))
        with pytest.raises(NotifierUnavailableError):
            await notifier.send("x")


# ---------------------------------------------------------------------------
# Body-parsing edge cases
# ---------------------------------------------------------------------------


class TestBodyParsing:
    async def test_non_json_body_with_5xx_still_maps_to_unavailable(self) -> None:
        # Build a response whose .json() raises (non-JSON body) and status_code=503.
        response = MagicMock(spec=httpx.Response)
        response.status_code = 503
        response.headers = {}
        response.text = "<html>503 Bad Gateway</html>"
        response.json = MagicMock(side_effect=ValueError("not json"))
        notifier, _ = _make_notifier(response)
        with pytest.raises(NotifierUnavailableError):
            await notifier.send("x")

    async def test_non_json_body_with_400_maps_to_bad_request(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.status_code = 400
        response.headers = {}
        response.text = "Bad Request"
        response.json = MagicMock(side_effect=ValueError("not json"))
        notifier, _ = _make_notifier(response)
        with pytest.raises(NotifierBadRequestError):
            await notifier.send("x")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_aclose_calls_underlying_aclose(self) -> None:
        response = _make_response(200, {"ok": True, "result": {}})
        notifier, _ = _make_notifier(response)
        # Pull the mock client out so we can assert on aclose.
        mock_client = notifier._client
        await notifier.aclose()
        mock_client.aclose.assert_awaited_once()  # type: ignore[union-attr]

    async def test_context_manager_closes_on_exit(self) -> None:
        response = _make_response(200, {"ok": True, "result": {}})
        client = MagicMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=response)
        client.aclose = AsyncMock()
        async with TelegramNotifier(token="t", chat_id=1, client=client) as notifier:
            await notifier.send("hi")
        client.aclose.assert_awaited_once()
