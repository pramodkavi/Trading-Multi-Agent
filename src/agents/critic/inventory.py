"""Stitch a signals row to its agent_runs and classify the decision (design §3).

``signals.status`` is PUBLISHED for *every* SignalProposal payload, including
Judge vetoes (see ``repositories.create_signal``). The Judge's own agent_runs
row is therefore the only trustworthy source for "was it published".

Classification, derived from the raw payload plus the agent outputs that share
the signal's ``scan_id``:

- payload has a ``"direction"`` key -> it is a SignalProposal:
    - Judge run present with ``ruling == "SKIP"`` -> ``VETOED``
    - any other ruling, or no Judge run (Slice-1-era rows) -> ``PUBLISHED``
- payload has no ``"direction"`` key -> it is a SkipDecision:
    - non-empty ``violated_rule`` -> ``GATE_REJECTED``; the rejected proposal is
      recovered from the Analyzer run output when that output has a ``"direction"``
    - otherwise -> ``NO_SETUP``

Typed re-parsing (``SignalProposal`` / ``SkipDecision``) is best-effort: rows
persist across schema evolutions, so a ``ValidationError`` degrades to ``None``
with a warning rather than failing the whole inventory. Historian / Skeptic /
Judge outputs are attached as raw dict copies whenever their run exists.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from src.agents.critic.models import DecisionKind, DecisionRecord
from src.common.models import AgentRole, JudgeRuling, SignalProposal, SkipDecision
from src.persistence.models import StoredAgentRun, StoredSignal

logger = logging.getLogger(__name__)

_SCOREABLE = frozenset({DecisionKind.PUBLISHED, DecisionKind.VETOED, DecisionKind.GATE_REJECTED})


def is_scoreable(kind: DecisionKind) -> bool:
    """True when the decision kind carries a proposal that can be price-replayed."""
    return kind in _SCOREABLE


def _first_output(runs: Sequence[StoredAgentRun], role: AgentRole) -> dict[str, Any] | None:
    """Return a copy of the first ``role`` run's output, or None if that agent did not run."""
    for run in runs:
        if run.agent_role is role:
            return dict(run.output)
    return None


def _proposal_from(payload: dict[str, Any] | None, *, signal_id: UUID) -> SignalProposal | None:
    """Best-effort parse of a proposal-shaped payload; None when absent or corrupt."""
    if payload is None or "direction" not in payload:
        return None
    try:
        return SignalProposal.model_validate(payload)
    except ValidationError as exc:
        logger.warning("signal %s: proposal payload failed validation: %s", signal_id, exc)
        return None


def _skip_from(payload: dict[str, Any], *, signal_id: UUID) -> SkipDecision | None:
    """Best-effort parse of a SkipDecision payload; None when corrupt."""
    try:
        return SkipDecision.model_validate(payload)
    except ValidationError as exc:
        logger.warning("signal %s: skip payload failed validation: %s", signal_id, exc)
        return None


def classify(signal: StoredSignal, runs: Sequence[StoredAgentRun]) -> DecisionRecord:
    """Classify one signals row given the agent_runs rows that share its ``scan_id``.

    Runs from roles other than Analyzer / Historian / Skeptic / Judge are ignored;
    if a role appears more than once the first occurrence wins.
    """
    historian = _first_output(runs, AgentRole.HISTORIAN)
    skeptic = _first_output(runs, AgentRole.SKEPTIC)
    judge = _first_output(runs, AgentRole.JUDGE)
    payload = dict(signal.payload)

    if "direction" in payload:
        ruling = judge.get("ruling") if judge is not None else None
        kind = DecisionKind.VETOED if ruling == JudgeRuling.SKIP.value else DecisionKind.PUBLISHED
        return DecisionRecord(
            signal=signal,
            kind=kind,
            proposal=_proposal_from(payload, signal_id=signal.id),
            skip=None,
            historian=historian,
            skeptic=skeptic,
            judge=judge,
        )

    skip = _skip_from(payload, signal_id=signal.id)
    if payload.get("violated_rule"):
        # A hard-rule rejection: the Analyzer's agent_run holds the proposal the
        # gate blocked (scripts/run_scan.py::_persist logs `rejected_proposal`).
        analyzer_output = _first_output(runs, AgentRole.ANALYZER)
        return DecisionRecord(
            signal=signal,
            kind=DecisionKind.GATE_REJECTED,
            proposal=_proposal_from(analyzer_output, signal_id=signal.id),
            skip=skip,
            historian=historian,
            skeptic=skeptic,
            judge=judge,
        )
    return DecisionRecord(
        signal=signal,
        kind=DecisionKind.NO_SETUP,
        proposal=None,
        skip=skip,
        historian=historian,
        skeptic=skeptic,
        judge=judge,
    )
