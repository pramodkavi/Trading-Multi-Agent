"""Core Pydantic models for the multi-agent signal pipeline.

This package defines the *boundary* types every agent emits and consumes.
If you find yourself reaching for `dict[str, Any]` in agent code, you are
working around a missing model — add it here instead.

Public API:
    SignalProposal  -- the trade idea emitted by Analyzer strategies
    SkipDecision    -- structured non-action with categorical reason
    SkipReason      -- enum of categorical skip reasons (SPEC §1.6 mapping)
    JudgeRuling     -- terminal Judge decision enum
    JudgeConfidence -- LOW / MEDIUM / HIGH confidence in the Judge's ruling
    ObjectionSeverity -- LOW / MEDIUM / HIGH for the Skeptic's objection
    SignalDirection -- LONG / SHORT
    SignalStatus    -- persisted PUBLISHED / SKIPPED row status
    ScanSession     -- which scheduler window triggered the scan
    ScanStatus      -- persisted scan_runs row status (RUNNING/SUCCESS/FAILED)
    ActiveSetupStatus -- OPEN + terminal lifecycle of an active_setups row
    SetupEvaluationStatus -- ForecastStatus + ERROR for a setup_evaluations row
                       (Step 4.0, design §5.2)
    AgentRole       -- the six agent roles referenced by agent_runs
    ScanContext     -- per-scan metadata propagated through every agent
    LlmUsage        -- tokens / cost / latency of one agent LLM call, carried
                       through AgentState into agent_runs (NFR-2.3, design §5.2)
"""

from src.common.models.enums import (
    ActiveSetupStatus,
    AgentRole,
    ForecastStatus,
    JudgeConfidence,
    JudgeRuling,
    ObjectionSeverity,
    ScanSession,
    ScanStatus,
    SetupEvaluationStatus,
    SignalDirection,
    SignalOutcome,
    SignalStatus,
)
from src.common.models.llm_usage import LlmUsage
from src.common.models.scan_context import ScanContext
from src.common.models.signal_proposal import SignalProposal
from src.common.models.skip_decision import SkipDecision, SkipReason

__all__ = [
    "ActiveSetupStatus",
    "AgentRole",
    "ForecastStatus",
    "JudgeConfidence",
    "JudgeRuling",
    "LlmUsage",
    "ObjectionSeverity",
    "ScanContext",
    "ScanSession",
    "ScanStatus",
    "SetupEvaluationStatus",
    "SignalDirection",
    "SignalOutcome",
    "SignalProposal",
    "SignalStatus",
    "SkipDecision",
    "SkipReason",
]
