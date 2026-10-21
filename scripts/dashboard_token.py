"""Get a Cognito access token for the dashboard API (authorization code + PKCE).

Slice 4 Step 4.4. The operator runs this on their own machine to obtain an
access token from the dashboard user pool's Hosted UI and, optionally, to call
one dashboard API URL with it (e.g. to verify the JWT authorizer end to end).

Flow:
    1. Generate a PKCE S256 pair (64-char verifier) and a random ``state``.
    2. Open the browser at ``{domain}/oauth2/authorize`` (the URL is also
       printed to stderr in case no browser opens).
    3. Wait (120 s) for the one redirect to ``http://localhost:<port>/callback``
       on a one-shot HTTP server bound to 127.0.0.1 only; reject a mismatched
       ``state``.
    4. ``POST {domain}/oauth2/token`` (form-encoded, public client, no secret).
    5. Print the **access token** to stdout -- nothing is written to disk.

Usage:
    python -m scripts.dashboard_token \\
        --domain https://crypto-signals-dash-<account>.auth.ap-south-1.amazoncognito.com \\
        --client-id <DashboardUserPoolClientId> [--port 8765] [--call <api-url>]

``--domain`` and ``--client-id`` are the ``DashboardCognitoDomain`` and
``DashboardUserPoolClientId`` stack outputs. ``--port`` must match the
registered callback (``http://localhost:8765/callback``). With ``--call`` the
helper performs ``GET <api-url>`` with ``Authorization: Bearer <token>`` and
reports the status and the first 500 characters of the body on stderr, so
stdout stays exactly the token (``TOKEN=$(python -m scripts.dashboard_token ...)``).

Errors exit non-zero with a one-line message on stderr and never echo the token.

This module ships in the Lambda code asset, so it imports the standard library
only and does nothing at import time.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import socketserver
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler
from typing import Any

DEFAULT_PORT = 8765
DEFAULT_TIMEOUT_S = 120.0
HTTP_TIMEOUT_S = 30.0
SCOPES = "openid email profile"
CALLBACK_PATH = "/callback"
BODY_PREVIEW_CHARS = 500

_DONE_PAGE = (
    b"<!doctype html><html><head><title>Dashboard sign-in</title></head>"
    b"<body><p>Sign-in finished. You can close this tab and return to the terminal.</p>"
    b"</body></html>"
)
_FAILED_PAGE = (
    b"<!doctype html><html><head><title>Dashboard sign-in</title></head>"
    b"<body><p>Sign-in failed. See the terminal. You can close this tab.</p>"
    b"</body></html>"
)

Opener = Callable[..., Any]
BrowserOpener = Callable[[str], bool]
WaitFn = Callable[[int, str, float], str]


class HelperError(Exception):
    """A user-facing failure; the message is printed as one line (never a token)."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)``: 64-char verifier, S256 base64url challenge (no '=')."""
    verifier = secrets.token_urlsafe(48)  # 48 bytes -> exactly 64 URL-safe chars
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def authorize_url(
    domain: str, client_id: str, redirect_uri: str, challenge: str, state: str
) -> str:
    """Build the Hosted UI ``/oauth2/authorize`` URL for the code + PKCE grant."""
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{domain.rstrip('/')}/oauth2/authorize?{query}"


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def parse_callback(path: str, expected_state: str) -> str:
    """Return the ``code`` from a callback request path, or raise :class:`HelperError`."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    error = _first(query, "error")
    if error is not None:
        description = _first(query, "error_description") or ""
        detail = f"{error} {description}".strip()[:200]
        raise HelperError(f"authorization failed: {detail}")
    state = _first(query, "state")
    if state is None or not hmac.compare_digest(state, expected_state):
        raise HelperError("callback state mismatch (possible CSRF); aborting")
    code = _first(query, "code")
    if not code:
        raise HelperError("callback carried no authorization code")
    return code


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
    except (ValueError, OSError):
        return f"HTTP {exc.code}"
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return f"HTTP {exc.code} {payload['error']}"
    return f"HTTP {exc.code}"


def exchange_code(
    domain: str,
    client_id: str,
    code: str,
    redirect_uri: str,
    verifier: str,
    *,
    opener: Opener = urllib.request.urlopen,
) -> dict[str, Any]:
    """Exchange the authorization code at ``POST {domain}/oauth2/token``."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
    ).encode("ascii")
    req = urllib.request.Request(
        f"{domain.rstrip('/')}/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with opener(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise HelperError(f"token exchange failed: {_error_detail(exc)}") from None
    except urllib.error.URLError as exc:
        raise HelperError(f"token exchange failed: {exc.reason}") from None
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HelperError("token exchange returned a non-JSON body") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
        raise HelperError("token exchange response has no access_token")
    return payload


def call_api(url: str, token: str, *, opener: Opener = urllib.request.urlopen) -> tuple[int, str]:
    """``GET url`` with the bearer token; return ``(status, first 500 chars of body)``.

    HTTP error statuses (401/403/...) are returned, not raised -- they are the
    interesting answers when verifying the authorizer.
    """
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
    try:
        with opener(req, timeout=HTTP_TIMEOUT_S) as resp:
            status = int(resp.status)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except urllib.error.URLError as exc:
        raise HelperError(f"API call failed: {exc.reason}") from None
    return status, raw.decode("utf-8", "replace")[:BODY_PREVIEW_CHARS]


class _CallbackServer(socketserver.TCPServer):
    # TCPServer (not HTTPServer) avoids the getfqdn() reverse lookup on bind.
    allow_reuse_address = False
    expected_state: str
    code: str | None
    failure: HelperError | None


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self) -> None:
        if urllib.parse.urlsplit(self.path).path != CALLBACK_PATH:
            self.send_error(404)
            return
        try:
            self.server.code = parse_callback(self.path, self.server.expected_state)
        except HelperError as exc:
            self.server.failure = exc
            self._respond(400, _FAILED_PAGE)
            return
        self._respond(200, _DONE_PAGE)

    def _respond(self, status: int, page: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format: str, *args: Any) -> None:
        return  # keep the code out of stderr


def wait_for_code(
    port: int,
    expected_state: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    *,
    on_ready: Callable[[int], None] | None = None,
) -> str:
    """Serve ``127.0.0.1:port`` until one ``/callback`` arrives; return its code.

    Other paths (e.g. ``/favicon.ico``) get a 404 and the wait continues. The
    server is always closed. ``on_ready`` receives the bound port (useful with
    ``port=0``).
    """
    with _CallbackServer(("127.0.0.1", port), _CallbackHandler) as server:
        server.expected_state = expected_state
        server.code = None
        server.failure = None
        if on_ready is not None:
            on_ready(int(server.server_address[1]))
        deadline = time.monotonic() + timeout_s
        while server.code is None and server.failure is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HelperError(
                    f"timed out after {timeout_s:g}s waiting for the sign-in callback"
                )
            server.timeout = remaining
            server.handle_request()
        if server.failure is not None:
            raise server.failure
        assert server.code is not None
        return server.code


def _wait_default(port: int, expected_state: str, timeout_s: float) -> str:
    return wait_for_code(port, expected_state, timeout_s)


def _redact(message: str, secret: str | None) -> str:
    if secret:
        message = message.replace(secret, "<redacted>")
    return " ".join(message.split())  # one line


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dashboard_token",
        description="Get a Cognito access token for the dashboard API (code + PKCE).",
    )
    parser.add_argument("--domain", required=True, help="DashboardCognitoDomain stack output.")
    parser.add_argument("--client-id", required=True, help="DashboardUserPoolClientId output.")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Local callback port; must match the registered callback URL (default 8765).",
    )
    parser.add_argument("--call", metavar="API_URL", help="GET this URL with the token.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    opener: Opener = urllib.request.urlopen,
    open_browser: BrowserOpener = webbrowser.open,
    wait: WaitFn = _wait_default,
) -> int:
    args = _build_parser().parse_args(argv)
    domain = str(args.domain).rstrip("/")
    if not domain.startswith("https://"):
        print("error: --domain must be an https:// URL", file=sys.stderr)
        return 2

    token: str | None = None
    try:
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        redirect_uri = f"http://localhost:{args.port}{CALLBACK_PATH}"
        url = authorize_url(domain, args.client_id, redirect_uri, challenge, state)
        print(f"Opening the sign-in page (or open it yourself):\n{url}", file=sys.stderr)
        if not open_browser(url):
            print("Could not open a browser; open the URL above manually.", file=sys.stderr)
        code = wait(args.port, state, DEFAULT_TIMEOUT_S)
        tokens = exchange_code(domain, args.client_id, code, redirect_uri, verifier, opener=opener)
        token = str(tokens["access_token"])
        print(token)
        sys.stdout.flush()
        if args.call:
            status, body = call_api(args.call, token, opener=opener)
            print(f"GET {args.call} -> HTTP {status}", file=sys.stderr)
            print(body.replace(token, "<redacted>") or "(empty body)", file=sys.stderr)
    except HelperError as exc:
        print(f"error: {_redact(str(exc), token)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # never let a traceback (possibly holding the token) escape
        print(f"error: {type(exc).__name__}: {_redact(str(exc), token)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
