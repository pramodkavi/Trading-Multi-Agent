"""Unit tests for scripts.alarm_notifier (CloudWatch alarm -> Telegram).

Parsing and formatting are pure functions tested directly; the handler's IO is
covered by monkeypatching the credential lookup and the async send so no AWS or
network is touched.
"""

from __future__ import annotations

import json

import pytest

from scripts import alarm_notifier


def _sns_event(message: str, subject: str | None = None) -> dict:
    record = {"Sns": {"Message": message}}
    if subject is not None:
        record["Sns"]["Subject"] = subject
    return {"Records": [record]}


# ---------------------------------------------------------------------------
# extract_alarm_messages
# ---------------------------------------------------------------------------


def test_extract_parses_json_alarm() -> None:
    event = _sns_event(json.dumps({"AlarmName": "X", "NewStateValue": "ALARM"}))
    messages = alarm_notifier.extract_alarm_messages(event)
    assert messages == [{"AlarmName": "X", "NewStateValue": "ALARM"}]


def test_extract_wraps_non_json_as_raw() -> None:
    event = _sns_event("hello not json", subject="manual test")
    messages = alarm_notifier.extract_alarm_messages(event)
    assert messages == [{"_raw": "hello not json", "_subject": "manual test"}]


def test_extract_empty_event() -> None:
    assert alarm_notifier.extract_alarm_messages({}) == []


# ---------------------------------------------------------------------------
# format_alarm
# ---------------------------------------------------------------------------


def test_format_alarm_in_alarm_state() -> None:
    text = alarm_notifier.format_alarm(
        {
            "AlarmName": "ScanFailureRateAlarm",
            "NewStateValue": "ALARM",
            "OldStateValue": "OK",
            "AlarmDescription": "Scan failure rate > 10%",
            "NewStateReason": "Threshold crossed: 1 datapoint (50.0) > 10.0",
            "Region": "Asia Pacific (Mumbai)",
            "StateChangeTime": "2026-06-15T08:03:00Z",
        }
    )
    assert "ScanFailureRateAlarm" in text
    assert "OK -> ALARM" in text
    assert "Scan failure rate > 10%" in text
    assert "Threshold crossed" in text
    assert "Mumbai" in text
    assert text.startswith("\U0001f6a8")  # 🚨 for ALARM


def test_format_alarm_ok_state_uses_check_emoji() -> None:
    text = alarm_notifier.format_alarm(
        {"AlarmName": "X", "NewStateValue": "OK", "OldStateValue": "ALARM"}
    )
    assert text.startswith("✅")
    assert "ALARM -> OK" in text


def test_format_alarm_raw_message() -> None:
    text = alarm_notifier.format_alarm({"_raw": "ping", "_subject": "test"})
    assert "ping" in text
    assert "test" in text


def test_format_alarm_truncates() -> None:
    text = alarm_notifier.format_alarm(
        {"AlarmName": "X", "NewStateValue": "ALARM", "NewStateReason": "y" * 9000}
    )
    assert len(text) <= 3500


# ---------------------------------------------------------------------------
# _telegram_credentials
# ---------------------------------------------------------------------------


def test_credentials_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_PARAM_NAME", raising=False)
    monkeypatch.delenv("ANTHROPIC_PARAM_NAME", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    assert alarm_notifier._telegram_credentials() == ("tok", "chat")


def test_credentials_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "TELEGRAM_PARAM_NAME",
        "ANTHROPIC_PARAM_NAME",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="Telegram credentials"):
        alarm_notifier._telegram_credentials()


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------


def test_handler_no_records_sends_nothing() -> None:
    assert alarm_notifier.lambda_handler({}, None) == {"ok": True, "sent": 0}


def test_handler_forwards_each_alarm(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_send_all(texts: list[str], *, token: str, chat_id: str) -> None:
        captured["texts"] = texts
        captured["token"] = token
        captured["chat_id"] = chat_id

    monkeypatch.setattr(alarm_notifier, "_telegram_credentials", lambda: ("tok", "chat"))
    monkeypatch.setattr(alarm_notifier, "_send_all", fake_send_all)

    event = _sns_event(
        json.dumps({"AlarmName": "AuroraCpuAlarm", "NewStateValue": "ALARM", "OldStateValue": "OK"})
    )
    result = alarm_notifier.lambda_handler(event, None)

    assert result == {"ok": True, "sent": 1}
    texts = captured["texts"]
    assert isinstance(texts, list)
    assert "AuroraCpuAlarm" in texts[0]
    assert captured["token"] == "tok"
    assert captured["chat_id"] == "chat"
