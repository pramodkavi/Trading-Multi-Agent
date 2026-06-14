"""Historian agent package (Slice 2 Step 2.4).

Three-stage journal retrieval (hard filters -> tag overlap -> L2 distance) plus
the empirical track-record report the Judge and Telegram alert consume.

Public API:
    HistorianRepository  -- backend-agnostic retrieval over a SignalStore.
    make_historian_node  -- factory building the LangGraph 'historian' node.
    HistorianReport      -- empirical win-rate summary for a proposal.
    HistoricalMatch      -- one retrieved precedent with its similarity metrics.
    L2_FEATURE_KEYS      -- the scale-free numeric keys defining stage-3 similarity.
"""

from src.agents.historian.historian import (
    DEFAULT_TAG_POOL,
    DEFAULT_TOP_K,
    L2_FEATURE_KEYS,
    HistorianRepository,
    make_historian_node,
)
from src.agents.historian.models import HistorianReport, HistoricalMatch

__all__ = [
    "DEFAULT_TAG_POOL",
    "DEFAULT_TOP_K",
    "L2_FEATURE_KEYS",
    "HistorianReport",
    "HistorianRepository",
    "HistoricalMatch",
    "make_historian_node",
]
