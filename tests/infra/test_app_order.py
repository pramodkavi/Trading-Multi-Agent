"""infrastructure/app.py declares DashboardStack LAST (design §3.5 / anti-goal §4.4).

Pinned by reading the source rather than executing ``app.py`` (which synthesizes
and pip-installs both dependency layers).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore")

APP_PY = Path(__file__).resolve().parents[2] / "infrastructure" / "app.py"
EXPECTED = [
    "CryptoSignals-Network",
    "CryptoSignals-Data",
    "CryptoSignals-Compute",
    "CryptoSignals-Scheduling",
    "CryptoSignals-Monitoring",
    "CryptoSignals-Dashboard",
]


def test_stack_declaration_order_ends_with_dashboard() -> None:
    text = APP_PY.read_text(encoding="utf-8")
    found = re.findall(r'"(CryptoSignals-[A-Za-z]+)"', text)
    assert found == EXPECTED


def test_dashboard_stack_has_no_explicit_dependency_on_pipeline_stacks() -> None:
    assert "add_dependency" not in APP_PY.read_text(encoding="utf-8")
