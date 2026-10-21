"""`src.providers` is a lazy package (PEP 562) — dashboard cold start.

The dashboard imports `src.providers.base` (`Timeframe`, `ProviderError`), which
runs the package `__init__` first. That must not load ccxt / aiohttp (the Binance
provider) or httpx (the macro providers): `/api/health` shares the import and its
cold start would pay ~3.5 s. A fresh interpreter proves it, while the
package-level names the scan pipeline uses keep resolving to the same objects.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import src.providers as providers

# The repo root, so `import src...` resolves the same in the subprocess wherever
# pytest was started from.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_HEAVY = ("ccxt", "aiohttp", "httpx", "boto3")
_PROBE = f"import sys, src.dashboard.api.app; print(*[m in sys.modules for m in {_HEAVY!r}])"


def test_dashboard_app_import_does_not_load_heavy_clients() -> None:
    # ccxt / aiohttp: the Binance provider; httpx: the macro providers; boto3: the
    # dashboard needs botocore only (its clients are built lazily per container).
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=_REPO_ROOT,
    )
    loaded = dict(zip(_HEAVY, result.stdout.split(), strict=True))
    assert loaded == dict.fromkeys(_HEAVY, "False"), result.stdout


def test_package_names_resolve_lazily_to_the_submodule_objects() -> None:
    from src.providers import (
        BinanceProvider,
        FREDProvider,
        MacroProvider,
        TwelveDataProvider,
        binance,
        macro,
    )

    assert BinanceProvider is binance.BinanceProvider
    assert FREDProvider is macro.FREDProvider
    assert MacroProvider is macro.MacroProvider
    assert TwelveDataProvider is macro.TwelveDataProvider


def test_every_public_name_is_resolvable() -> None:
    for name in providers.__all__:
        assert getattr(providers, name) is not None, name
        assert name in dir(providers), name


def test_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        providers.nope  # noqa: B018 - the attribute access is the assertion
