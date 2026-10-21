"""Unit tests for ``scripts/dashboard_token.py`` (Slice 4 Step 4.4, Task 3).

No real browser, no fixed-port bind, no network: the opener, the browser
launcher and the callback wait are injected fakes. The single real-socket test
binds an ephemeral port on 127.0.0.1 and is closed deterministically.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from scripts import dashboard_token as dt

DOMAIN = "https://crypto-signals-dash-123456789012.auth.ap-south-1.amazoncognito.com"
CLIENT_ID = "abc123clientid"
REDIRECT = "http://localhost:8765/callback"
TOKEN = "eyJraWQiOiJ0ZXN0In0.SECRET-ACCESS-TOKEN.sig"


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _RecordingOpener:
    def __init__(self, responses: list[_FakeResponse | Exception]) -> None:
        self.requests: list[urllib.request.Request] = []
        self._responses = responses

    def __call__(self, req: urllib.request.Request, timeout: float | None = None) -> Any:
        self.requests.append(req)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _token_response() -> _FakeResponse:
    body = (
        '{"access_token": "' + TOKEN + '", "id_token": "id.tok.en", '
        '"refresh_token": "refresh", "token_type": "Bearer", "expires_in": 3600}'
    )
    return _FakeResponse(200, body.encode())


def test_pkce_pair_s256() -> None:
    verifier, challenge = dt.pkce_pair()
    assert len(verifier) == 64
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert challenge == expected
    assert "=" not in challenge
    assert dt.pkce_pair()[0] != verifier


def test_authorize_url_params() -> None:
    url = dt.authorize_url(DOMAIN + "/", CLIENT_ID, REDIRECT, "chal", "st4te")
    parts = urllib.parse.urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == DOMAIN + "/oauth2/authorize"
    assert "scope=openid+email+profile" in parts.query
    q = urllib.parse.parse_qs(parts.query)
    assert q == {
        "response_type": ["code"],
        "client_id": [CLIENT_ID],
        "redirect_uri": [REDIRECT],
        "scope": ["openid email profile"],
        "code_challenge": ["chal"],
        "code_challenge_method": ["S256"],
        "state": ["st4te"],
    }


def test_callback_returns_code() -> None:
    assert dt.parse_callback("/callback?code=the-code&state=s1", "s1") == "the-code"


def test_callback_state_mismatch_rejected() -> None:
    with pytest.raises(dt.HelperError, match="state"):
        dt.parse_callback("/callback?code=the-code&state=attacker", "s1")
    with pytest.raises(dt.HelperError, match="state"):
        dt.parse_callback("/callback?code=the-code", "s1")


def test_callback_error_param() -> None:
    with pytest.raises(dt.HelperError, match="access_denied"):
        dt.parse_callback("/callback?error=access_denied&state=s1", "s1")


def test_callback_missing_code() -> None:
    with pytest.raises(dt.HelperError, match="code"):
        dt.parse_callback("/callback?state=s1", "s1")


def test_exchange_code_request_shape() -> None:
    opener = _RecordingOpener([_token_response()])
    out = dt.exchange_code(DOMAIN, CLIENT_ID, "the-code", REDIRECT, "verif", opener=opener)
    assert out["access_token"] == TOKEN
    (req,) = opener.requests
    assert req.full_url == DOMAIN + "/oauth2/token"
    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert isinstance(req.data, bytes)
    assert urllib.parse.parse_qs(req.data.decode()) == {
        "grant_type": ["authorization_code"],
        "client_id": [CLIENT_ID],
        "code": ["the-code"],
        "redirect_uri": [REDIRECT],
        "code_verifier": ["verif"],
    }


def test_exchange_code_http_error_is_helper_error() -> None:
    err = urllib.error.HTTPError(
        DOMAIN + "/oauth2/token", 400, "Bad Request", None, io.BytesIO(b'{"error":"invalid_grant"}')
    )
    opener = _RecordingOpener([err])
    with pytest.raises(dt.HelperError, match="invalid_grant"):
        dt.exchange_code(DOMAIN, CLIENT_ID, "c", REDIRECT, "v", opener=opener)


def test_call_api_sends_bearer() -> None:
    opener = _RecordingOpener([_FakeResponse(200, b"x" * 800)])
    status, body = dt.call_api("https://api.example/api/signals", TOKEN, opener=opener)
    assert status == 200
    assert body == "x" * 500
    (req,) = opener.requests
    assert req.get_method() == "GET"
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"


def test_call_api_http_error_returns_status() -> None:
    err = urllib.error.HTTPError(
        "https://api.example/api/x", 401, "Unauthorized", None, io.BytesIO(b'{"message":"no"}')
    )
    opener = _RecordingOpener([err])
    assert dt.call_api("https://api.example/api/x", TOKEN, opener=opener) == (
        401,
        '{"message":"no"}',
    )


class _FakeListener:
    def __init__(self, events: list[str], state: str) -> None:
        self.events = events
        self.state = state

    def __enter__(self) -> _FakeListener:
        return self

    def __exit__(self, *exc: object) -> None:
        self.events.append("close")

    def wait(self, timeout_s: float = 120.0) -> str:
        self.events.append(f"wait:{timeout_s:g}")
        return "the-code"


def _run_main(
    argv: list[str],
    opener: _RecordingOpener,
    capsys: pytest.CaptureFixture[str],
    events: list[str] | None = None,
) -> tuple[int, str, str, list[str]]:
    opened: list[str] = []
    events = events if events is not None else []
    seen_state: dict[str, str] = {}

    def fake_listen(port: int, expected_state: str) -> _FakeListener:
        assert port == 8765
        seen_state["state"] = expected_state
        events.append("bind")
        return _FakeListener(events, expected_state)

    def fake_browser(url: str) -> bool:
        opened.append(url)
        events.append("browser")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert q["state"][0] == seen_state["state"]
        return True

    rc = dt.main(argv, opener=opener, open_browser=fake_browser, listen=fake_listen)
    out, err = capsys.readouterr()
    return rc, out, err, opened


def test_main_prints_only_token(capsys: pytest.CaptureFixture[str]) -> None:
    opener = _RecordingOpener([_token_response()])
    rc, out, err, opened = _run_main(["--domain", DOMAIN, "--client-id", CLIENT_ID], opener, capsys)
    assert rc == 0
    assert out == TOKEN + "\n"
    assert TOKEN not in err
    assert len(opened) == 1
    assert opened[0].startswith(DOMAIN + "/oauth2/authorize?")
    (req,) = opener.requests
    assert isinstance(req.data, bytes)
    body = urllib.parse.parse_qs(req.data.decode())
    assert body["redirect_uri"] == [REDIRECT]
    # the verifier sent at exchange matches the challenge sent at authorize
    challenge = urllib.parse.parse_qs(urllib.parse.urlsplit(opened[0]).query)["code_challenge"][0]
    verifier = body["code_verifier"][0]
    digest = hashlib.sha256(verifier.encode()).digest()
    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == challenge


def test_main_call_reports_status_without_token(capsys: pytest.CaptureFixture[str]) -> None:
    opener = _RecordingOpener([_token_response(), _FakeResponse(200, b'{"ok": true}')])
    rc, out, err, _ = _run_main(
        ["--domain", DOMAIN, "--client-id", CLIENT_ID, "--call", "https://api.example/api/x"],
        opener,
        capsys,
    )
    assert rc == 0
    assert out == TOKEN + "\n"
    assert "200" in err
    assert '{"ok": true}' in err
    assert TOKEN not in err
    assert opener.requests[1].get_header("Authorization") == f"Bearer {TOKEN}"


def test_errors_do_not_echo_token(capsys: pytest.CaptureFixture[str]) -> None:
    # An unexpected failure whose message happens to carry the token must be redacted.
    opener = _RecordingOpener([_token_response(), RuntimeError(f"boom with {TOKEN} inside")])
    rc, out, err, _ = _run_main(
        ["--domain", DOMAIN, "--client-id", CLIENT_ID, "--call", "https://api.example/api/x"],
        opener,
        capsys,
    )
    assert rc != 0
    assert TOKEN not in err
    assert "the-code" not in err
    assert "Traceback" not in err
    last = err.strip().splitlines()[-1]
    assert last.startswith("error:")
    assert "boom" in last
    assert "<redacted>" in last
    # the token was already printed for the operator before --call failed
    assert out == TOKEN + "\n"


def test_main_binds_before_opening_browser(capsys: pytest.CaptureFixture[str]) -> None:
    events: list[str] = []
    rc, _, _, _ = _run_main(
        ["--domain", DOMAIN, "--client-id", CLIENT_ID],
        _RecordingOpener([_token_response()]),
        capsys,
        events,
    )
    assert rc == 0
    assert events == ["bind", "browser", "wait:120", "close"]


def test_main_port_conflict_fails_before_browser(capsys: pytest.CaptureFixture[str]) -> None:
    opened: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        busy = squatter.getsockname()[1]
        rc = dt.main(
            ["--domain", DOMAIN, "--client-id", CLIENT_ID, "--port", str(busy)],
            opener=_RecordingOpener([]),
            open_browser=lambda url: bool(opened.append(url)) or True,
        )
    out, err = capsys.readouterr()
    assert rc == 1
    assert opened == []
    assert out == ""
    assert err.strip().splitlines()[-1].startswith("error: cannot listen on 127.0.0.1:")


def test_main_rejects_non_https_call(capsys: pytest.CaptureFixture[str]) -> None:
    events: list[str] = []
    rc, out, err, opened = _run_main(
        ["--domain", DOMAIN, "--client-id", CLIENT_ID, "--call", "http://api.example/api/x"],
        _RecordingOpener([]),
        capsys,
        events,
    )
    assert rc == 2
    assert out == ""
    assert opened == []
    assert events == []
    assert "--call must be an https://" in err


def test_call_body_preview_strips_escape_sequences(capsys: pytest.CaptureFixture[str]) -> None:
    body = b'{"msg": "\x1b[31mred\x1b]0;title\x07\r\nnext"}'
    opener = _RecordingOpener([_token_response(), _FakeResponse(200, body)])
    rc, _, err, _ = _run_main(
        ["--domain", DOMAIN, "--client-id", CLIENT_ID, "--call", "https://api.example/api/x"],
        opener,
        capsys,
    )
    assert rc == 0
    assert "\x1b" not in err
    assert "\x07" not in err
    assert "\r" not in err
    assert "red" in err
    assert "next" in err


def test_callback_error_description_strips_escape_sequences() -> None:
    with pytest.raises(dt.HelperError) as info:
        dt.parse_callback("/callback?error=bad%1B%5B2J&error_description=x%07y&state=s1", "s1")
    assert "\x1b" not in str(info.value)
    assert "\x07" not in str(info.value)
    assert "bad" in str(info.value)


def test_callback_non_ascii_state_is_mismatch_not_typeerror() -> None:
    with pytest.raises(dt.HelperError, match="state mismatch"):
        dt.parse_callback("/callback?code=c&state=%C3%A9", "s1")


def test_main_token_exchange_failure_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    err = urllib.error.HTTPError(
        DOMAIN + "/oauth2/token", 400, "Bad Request", None, io.BytesIO(b'{"error":"invalid_grant"}')
    )
    rc, out, errout, _ = _run_main(
        ["--domain", DOMAIN, "--client-id", CLIENT_ID], _RecordingOpener([err]), capsys
    )
    assert rc == 1
    assert out == ""
    assert "invalid_grant" in errout


def test_main_rejects_non_https_domain(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, err, opened = _run_main(
        ["--domain", "http://evil.example", "--client-id", CLIENT_ID], _RecordingOpener([]), capsys
    )
    assert rc == 2
    assert out == ""
    assert opened == []
    assert "https" in err


def test_wait_for_code_real_socket_ephemeral_port() -> None:
    ports: queue.Queue[int] = queue.Queue()
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["code"] = dt.wait_for_code(0, "s1", timeout_s=10, on_ready=ports.put)
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            result["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    port = ports.get(timeout=5)
    url = f"http://127.0.0.1:{port}/callback?code=real-code&state=s1"
    with urllib.request.urlopen(url, timeout=5) as resp:
        page = resp.read().decode()
        status = resp.status
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert result == {"code": "real-code"}
    assert status == 200
    assert "close this tab" in page
    assert "real-code" not in page


def _serve_in_thread(timeout_s: float) -> tuple[threading.Thread, int, dict[str, Any]]:
    ports: queue.Queue[int] = queue.Queue()
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["code"] = dt.wait_for_code(0, "s1", timeout_s=timeout_s, on_ready=ports.put)
        except Exception as exc:  # pragma: no cover - surfaced by the asserts
            result["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    return worker, ports.get(timeout=5), result


def test_wait_for_code_favicon_then_callback() -> None:
    worker, port, result = _serve_in_thread(10)
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=5)
    assert info.value.code == 404
    info.value.close()
    url = f"http://127.0.0.1:{port}/callback?code=real-code&state=s1"
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert resp.status == 200
        resp.read()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert result == {"code": "real-code"}


def test_wait_for_code_idle_connection_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dt._CallbackHandler, "timeout", 0.3)
    worker, port, result = _serve_in_thread(10)
    start = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=5):
        # idle: sends nothing; the server must time this connection out and move on
        url = f"http://127.0.0.1:{port}/callback?code=real-code&state=s1"
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            resp.read()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert result == {"code": "real-code"}
    assert time.monotonic() - start < 5


def test_handler_has_connection_timeout() -> None:
    assert dt._CallbackHandler.timeout == dt.CONNECTION_TIMEOUT_S == 5.0


def test_http_error_body_is_closed() -> None:
    fp = io.BytesIO(b'{"error":"invalid_grant"}')
    err = urllib.error.HTTPError(DOMAIN + "/oauth2/token", 400, "Bad Request", None, fp)
    with pytest.raises(dt.HelperError):
        dt.exchange_code(DOMAIN, CLIENT_ID, "c", REDIRECT, "v", opener=_RecordingOpener([err]))
    assert fp.closed
    fp2 = io.BytesIO(b"denied")
    err2 = urllib.error.HTTPError("https://api.example/x", 403, "Forbidden", None, fp2)
    assert dt.call_api("https://api.example/x", TOKEN, opener=_RecordingOpener([err2])) == (
        403,
        "denied",
    )
    assert fp2.closed


def test_wait_for_code_times_out() -> None:
    with pytest.raises(dt.HelperError, match="timed out"):
        dt.wait_for_code(0, "s1", timeout_s=0.2)


def test_module_imports_stdlib_only() -> None:
    source = Path(dt.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports not allowed"
            assert node.module is not None
            names.add(node.module.split(".")[0])
    assert names, "expected some imports"
    non_stdlib = {n for n in names if n not in sys.stdlib_module_names and n != "__future__"}
    assert non_stdlib == set()
