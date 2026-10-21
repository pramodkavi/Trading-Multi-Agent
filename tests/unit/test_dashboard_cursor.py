"""Opaque keyset cursor for the dashboard journal (Slice 4 Step 4.2, design §3.4).

The cursor is `base64url(json.dumps([created_at_iso, str(id)]))`; anything that does
not decode to exactly that shape must raise `BadCursor` so the router can answer
400 `bad_cursor` instead of leaking a traceback (plan Review Focus #1).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from src.dashboard.api.cursor import BadCursor, decode_cursor, encode_cursor

NOW = datetime(2026, 10, 20, 8, 3, 52, 123456, tzinfo=UTC)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode("ascii")


def test_cursor_round_trip() -> None:
    signal_id = uuid4()
    raw = encode_cursor(NOW, signal_id)
    assert decode_cursor(raw) == (NOW, signal_id)


def test_cursor_is_urlsafe_and_unpadded() -> None:
    raw = encode_cursor(NOW, uuid4())
    assert "=" not in raw
    assert "+" not in raw and "/" not in raw
    # The encoding is the documented shape, not a private format.
    padded = raw + "=" * (-len(raw) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded))
    assert decoded == [NOW.isoformat(), str(UUID(decoded[1]))]


def test_cursor_round_trip_preserves_non_utc_offset() -> None:
    ts = datetime(2026, 10, 20, 13, 3, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    created_at, _ = decode_cursor(encode_cursor(ts, uuid4()))
    assert created_at == ts


def test_decode_cursor_accepts_padded_input() -> None:
    signal_id = uuid4()
    raw = encode_cursor(NOW, signal_id)
    padded = raw + "=" * (-len(raw) % 4)
    assert decode_cursor(padded) == (NOW, signal_id)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("zzz", id="bad-base64"),
        pytest.param("", id="empty"),
        pytest.param(_b64("not json"), id="bad-json"),
        pytest.param(_b64("[1]"), id="wrong-element-count"),
        pytest.param(_b64('["a","b","c"]'), id="too-many-elements"),
        pytest.param(_b64('{"a": 1}'), id="not-a-list"),
        pytest.param(_b64('["not-a-date","x"]'), id="non-iso-timestamp"),
        pytest.param(_b64('["2026-10-20T08:03:52","x"]'), id="bad-uuid"),
        pytest.param(
            _b64(f'["2026-10-20T08:03:52","{uuid4()}"]'),
            id="naive-timestamp",
        ),
        pytest.param(_b64(f'[1, "{uuid4()}"]'), id="timestamp-not-a-string"),
        pytest.param(_b64(f'["{NOW.isoformat()}", 7]'), id="id-not-a-string"),
        pytest.param("éééé", id="non-ascii"),
    ],
)
def test_decode_cursor_rejects_garbage(raw: str) -> None:
    with pytest.raises(BadCursor):
        decode_cursor(raw)


def test_bad_cursor_is_a_value_error() -> None:
    assert issubclass(BadCursor, ValueError)


def test_decode_cursor_rejects_oversized_input_before_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 100 kB of valid base64: rejected on length alone -- no decode / parse work is
    # spent on it (a 200-char cursor is the real shape; 512 is the cap).
    huge = "A" * 100_000

    def _no_decode(*_a: object, **_k: object) -> bytes:
        raise AssertionError("an oversized cursor must be rejected before base64 decoding")

    monkeypatch.setattr(base64, "urlsafe_b64decode", _no_decode)
    with pytest.raises(BadCursor, match="too long"):
        decode_cursor(huge)
    # The cap is a limit on the untrusted input, not on legitimate cursors.
    assert len(encode_cursor(NOW, uuid4())) < 512


def test_decode_cursor_accepts_input_at_the_cap() -> None:
    # Exactly 512 characters is still decoded (and then fails on shape, not length).
    at_cap = _b64("x" * 383)[:512]
    assert len(at_cap) == 512
    with pytest.raises(BadCursor) as excinfo:
        decode_cursor(at_cap)
    assert "too long" not in str(excinfo.value)


def test_decode_cursor_rejects_deeply_nested_json() -> None:
    # Deep nesting inside the cap: never a RecursionError out of the router.
    nested = _b64("[" * 380)
    assert len(nested) <= 512
    with pytest.raises(BadCursor):
        decode_cursor(nested)


def test_decode_cursor_maps_recursion_error_to_bad_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    # The JSON decoder raises RecursionError (not ValueError) past the interpreter's
    # recursion limit; it must surface as BadCursor -> 400, not a 500.
    from src.dashboard.api import cursor as cursor_module

    def _blow_up(*_a: object, **_k: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(cursor_module.json, "loads", _blow_up)
    with pytest.raises(BadCursor):
        decode_cursor(encode_cursor(NOW, uuid4()))
