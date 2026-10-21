"""Shared fixtures for the unit suite.

Mangum (`src.dashboard.api.app.handler`) resolves the main thread's current event
loop when the handler is built at import (creating one if none is set) and drives
every invocation on whatever loop is current at call time -- exactly what the
Lambda runtime provides: one loop per container. Under pytest-asyncio that loop is
unset after the first async test and would be garbage-collected unclosed (a
ResourceWarning, which this suite promotes to an error), so capture it here at
conftest import, re-install it for direct `handler(...)` calls, and close it once
at session end.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

_HANDLER_LOOP = asyncio.get_event_loop()


@pytest.fixture(scope="session", autouse=True)
def _close_handler_loop() -> Iterator[None]:
    yield
    _HANDLER_LOOP.close()
    asyncio.set_event_loop(None)


@pytest.fixture
def lambda_event_loop() -> Iterator[None]:
    """Make the import-time loop current again for a direct `handler(...)` call."""
    asyncio.set_event_loop(_HANDLER_LOOP)
    try:
        yield
    finally:
        asyncio.set_event_loop(None)
