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
import sys
import threading
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


def _run_main(
    argv: list[str],
    opener: _RecordingOpener,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str, list[str]]:
    opened: list[str] = []
    seen_state: dict[str, str] = {}

    def fake_browser(url: str) -> bool:
        opened.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        seen_state["state"] = q["state"][0]
        return True

    def fake_wait(port: int, expected_state: str, timeout_s: float) -> str:
        assert port == 8765
        assert expected_state == seen_state["state"]
        return "the-code"

    rc = dt.main(argv, opener=opener, open_browser=fake_browser, wait=fake_wait)
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
    assert "Traceback" not in err
    assert len(err.strip().splitlines()) >= 1
    assert "boom" in err
    # the token was already printed for the operator before --call failed
    assert out == TOKEN + "\n"


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
