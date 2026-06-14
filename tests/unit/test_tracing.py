"""Unit tests for the optional Langfuse tracing seam (Slice 2 Step 2.7).

Tracing is off by default; these verify the env detection and that the default
path is a transparent no-op (the wrapped node is the SAME object, so there is
zero overhead and no hard langfuse dependency). The enabled+SDK-present path is
not exercised here (langfuse is an optional extra, not installed in CI).
"""

from __future__ import annotations

import importlib.util
from typing import Any

import pytest

from src.common.tracing import trace_node, tracing_enabled

_LANGFUSE_INSTALLED = importlib.util.find_spec("langfuse") is not None


async def _noop_node(state: dict[str, Any]) -> dict[str, Any]:
    return state


def test_tracing_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert tracing_enabled() is False


def test_tracing_requires_both_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert tracing_enabled() is False
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    assert tracing_enabled() is True


def test_trace_node_is_passthrough_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    # Exact-same object back: no wrapping, no overhead.
    assert trace_node("analyzer", _noop_node) is _noop_node


@pytest.mark.skipif(_LANGFUSE_INSTALLED, reason="langfuse installed; SDK-missing path n/a")
def test_trace_node_noop_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Credentials present but langfuse not installed -> graceful no-op.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    assert trace_node("analyzer", _noop_node) is _noop_node
