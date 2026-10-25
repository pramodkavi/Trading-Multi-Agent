"""Unit tests for src.config.dashboard_url (Step 4.7 Part B).

The reader fetches the dashboard's base URL from SSM once per container and must NEVER raise or
slow a scan: any failure, or an unusable value, just means "no link".
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from src.config import dashboard_url
from src.config.dashboard_url import (
    DASHBOARD_URL_PARAM_NAME_ENV,
    get_dashboard_base_url,
    reset_dashboard_url_cache,
)

PARAM = "/crypto-signals/dashboard-base-url"
ENV = {DASHBOARD_URL_PARAM_NAME_ENV: PARAM}
GOOD = "https://d111.cloudfront.net"


class FakeSsm:
    def __init__(self, value: Any = GOOD, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def get_parameter(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"Parameter": {"Value": self.value}}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "nope"}}, "GetParameter")


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    reset_dashboard_url_cache()
    yield
    reset_dashboard_url_cache()


def test_unset_env_gives_none_and_never_builds_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("an SSM client must not be built when the env var is unset")

    monkeypatch.setattr(dashboard_url, "_ssm_client", boom)
    assert get_dashboard_base_url(env={}) is None


def test_reads_the_parameter_without_decryption() -> None:
    ssm = FakeSsm()
    assert get_dashboard_base_url(env=ENV, client=ssm) == GOOD
    assert ssm.calls == [{"Name": PARAM}]


def test_value_is_trimmed() -> None:
    assert get_dashboard_base_url(env=ENV, client=FakeSsm(value=f"  {GOOD}\n")) == GOOD


def test_reads_once_per_container() -> None:
    ssm = FakeSsm()
    for _ in range(3):
        assert get_dashboard_base_url(env=ENV, client=ssm) == GOOD
    assert len(ssm.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        _client_error("ParameterNotFound"),
        _client_error("AccessDeniedException"),
        BotoCoreError(),
        RuntimeError("anything else"),
    ],
)
def test_failures_give_none_and_never_raise(error: Exception) -> None:
    assert get_dashboard_base_url(env=ENV, client=FakeSsm(error=error)) is None


def test_a_failure_is_cached_too() -> None:
    ssm = FakeSsm(error=_client_error("ParameterNotFound"))
    assert get_dashboard_base_url(env=ENV, client=ssm) is None
    assert get_dashboard_base_url(env=ENV, client=ssm) is None
    assert len(ssm.calls) == 1


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "javascript:alert(1)",
        "http://insecure.example",
        "https://",
        "https://has space.example",
        "https://" + "a" * 250 + ".example",
        None,
        12345,
    ],
)
def test_unusable_values_give_none(value: Any) -> None:
    assert get_dashboard_base_url(env=ENV, client=FakeSsm(value=value)) is None


def test_logs_the_exception_type_only_never_the_value(caplog: pytest.LogCaptureFixture) -> None:
    secret_like = "https://leaky.example/token=abc123"
    with caplog.at_level(logging.WARNING, logger="src.config.dashboard_url"):
        get_dashboard_base_url(env=ENV, client=FakeSsm(value="http://" + secret_like))
        reset_dashboard_url_cache()
        get_dashboard_base_url(
            env=ENV, client=FakeSsm(error=_client_error("AccessDeniedException"))
        )
    text = caplog.text
    assert "abc123" not in text
    assert "leaky" not in text
    assert "ClientError" in text
