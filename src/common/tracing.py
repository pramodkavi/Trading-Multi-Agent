"""Optional Langfuse tracing seam for LangGraph nodes (Slice 2 Step 2.7).

Tracing is OFF by default and a transparent no-op: ``trace_node`` returns the
node callable unchanged unless BOTH ``LANGFUSE_PUBLIC_KEY`` and
``LANGFUSE_SECRET_KEY`` are present in the environment AND the optional
``langfuse`` package is installed (the ``tracing`` extra). This keeps the core
install lean and incurs zero cost / infra until the operator opts in -- either a
self-hosted Langfuse or its free cloud tier (SPEC §2 observability / Step 2.7).

The wrapper is deliberately defensive: if tracing is requested but the SDK is
missing it logs once and degrades to a no-op, so a misconfigured environment can
never break a live scan. The default (credentials absent) path imports nothing
and adds no overhead.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, TypeVar, cast

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# A LangGraph node: an async callable from state to a (partial) state.
F = TypeVar("F", bound="Callable[..., Awaitable[Any]]")


def tracing_enabled() -> bool:
    """True when Langfuse credentials are present in the environment."""
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def trace_node(name: str, fn: F) -> F:
    """Wrap an async LangGraph node with a Langfuse span, or return it unchanged.

    No-op (returns ``fn`` as-is) when tracing is disabled or the SDK is
    unavailable, so the default code path carries no tracing overhead and no
    hard dependency on ``langfuse``.
    """
    if not tracing_enabled():
        return fn
    observe = _load_observe()
    if observe is None:
        return fn
    return cast("F", observe(name=f"node.{name}")(fn))


def _load_observe() -> Any:
    """Return langfuse's ``observe`` decorator (v3 or v2), or None if unavailable.

    Imported lazily and guarded so the dependency is only touched when tracing
    is actually switched on. Supports both the v3 top-level ``langfuse.observe``
    and the v2 ``langfuse.decorators.observe``.
    """
    try:
        import langfuse

        return langfuse.observe  # langfuse v3
    except (ImportError, AttributeError):
        pass
    try:
        from langfuse.decorators import observe  # langfuse v2

        return observe
    except ImportError:
        logger.warning(
            "LANGFUSE_* set but the `langfuse` package is not installed; node tracing is "
            "disabled. Install the `tracing` extra (pip install -e '.[tracing]') to enable it."
        )
        return None
