"""Judge agent package (Slice 2 Step 2.6).

The final arbiter of the per-signal pipeline: weighs the Analyzer's proposal,
the Historian's empirical track record, and the Skeptic's macro objection into
one ruling (PUBLISH / PUBLISH_WITH_CAVEAT / SKIP) with written reasoning
(SPEC §3.1 role 4 / FR-1.6).

Public API:
    Judge            -- arbitrates the three inputs into a ruling via one LLM call.
    make_judge_node  -- factory building the LangGraph 'judge' node.
    JudgeDecision    -- the Judge's structured ruling (ruling + confidence + reasoning).
"""

from src.agents.judge.judge import Judge, make_judge_node
from src.agents.judge.models import JudgeDecision

__all__ = [
    "Judge",
    "JudgeDecision",
    "make_judge_node",
]
