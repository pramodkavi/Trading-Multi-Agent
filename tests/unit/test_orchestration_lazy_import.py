"""`src.agents.orchestration` is a lazy package (PEP 562) — dashboard cold start.

The dashboard Lambda imports `src.agents.orchestration.risk_gates` for the pure
SPEC §1.6 rules (design §5.3) and must not pay for LangGraph + the Anthropic SDK
at cold start (design §3.2). A fresh interpreter proves it: importing the
submodule must leave `langgraph` / `anthropic` out of `sys.modules`, while the
package-level names the scan pipeline uses keep resolving to the same objects.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import src.agents.orchestration as orchestration

_REPO_ROOT = Path(__file__).resolve().parents[2]  # `import src...` from any start dir

_PROBE = (
    "import sys, src.agents.orchestration.risk_gates as r; "
    "print('langgraph' in sys.modules, 'anthropic' in sys.modules)"
)


def test_importing_risk_gates_does_not_load_langgraph_or_anthropic() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=_REPO_ROOT,
    )
    assert result.stdout.split() == ["False", "False"], result.stdout


def test_package_names_resolve_lazily_to_the_submodule_objects() -> None:
    from src.agents.orchestration import (
        AgentState,
        RiskContext,
        ScanReservationLedger,
        graph,
        reservations,
        risk_gates,
    )

    assert AgentState is graph.AgentState
    assert ScanReservationLedger is reservations.ScanReservationLedger
    assert RiskContext is risk_gates.RiskContext


def test_every_public_name_is_lazily_resolvable() -> None:
    for name in orchestration.__all__:
        assert getattr(orchestration, name) is not None, name
        assert name in dir(orchestration), name


def test_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        orchestration.nope  # noqa: B018 - the attribute access is the assertion
