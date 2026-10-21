"""Decision classification for the dashboard (design §5.1, §14 D2).

``signals.status`` is ``PUBLISHED`` for *every* ``SignalProposal`` row, Judge vetoes
included, so "what happened to this decision" is **derived**, never stored. The
Critic (``src.agents.critic.inventory``) owns that derivation; this module is a thin
re-export of it plus two companions that let the journal classify **in SQL**:

- :func:`decision_kind_from_columns` -- the same rule evaluated on the three
  columns the journal projection already carries (``s.direction``,
  ``s.payload->>'violated_rule'``, the Judge run's ``output->>'ruling'``).
- :data:`DECISION_KIND_SQL` -- the ``CASE`` twin of that function, used both as a
  projected column and as the ``decision_kind`` filter predicate so filtering
  happens server-side instead of after paging.

The parity test (``tests/unit/test_dashboard_classify.py``) pins all eight
``(direction, violated_rule, judge_ruling)`` combinations against ``classify``.
Note the one representational difference: ``classify`` keys on the *payload* having
a ``"direction"`` key, the column variants on ``signals.direction IS NOT NULL`` --
``create_signal`` writes the column from that same payload key, so they agree.
"""

from __future__ import annotations

from src.agents.critic import DecisionKind, DecisionRecord
from src.agents.critic.inventory import classify
from src.common.models import JudgeRuling

__all__ = [
    "DECISION_KIND_SQL",
    "DecisionKind",
    "DecisionRecord",
    "classify",
    "decision_kind_from_columns",
]

# SQL twin of ``decision_kind_from_columns``. Aliases are part of the contract:
# ``s`` is the ``signals`` row and ``j.ruling`` the LATERAL Judge subquery in
# ``queries.py`` (``SELECT a.output->>'ruling' AS ruling FROM agent_runs a ...``).
# Contains no bind parameters, so it can be embedded in any statement.
DECISION_KIND_SQL = """CASE WHEN s.direction IS NOT NULL THEN CASE WHEN j.ruling = 'SKIP' THEN 'VETOED' ELSE 'PUBLISHED' END
     WHEN COALESCE(s.payload->>'violated_rule', '') <> '' THEN 'GATE_REJECTED'
     ELSE 'NO_SETUP' END"""  # noqa: E501 - the literal is the contract; keep it verbatim


def decision_kind_from_columns(
    *,
    direction: str | None,
    violated_rule: str | None,
    judge_ruling: str | None,
) -> DecisionKind:
    """Classify from the journal columns; must agree with :func:`classify`.

    ``direction`` set -> a proposal existed: ``VETOED`` when the Judge ruled
    ``SKIP``, else ``PUBLISHED`` (any other ruling, or no Judge run at all).
    Otherwise a skip: ``GATE_REJECTED`` when ``violated_rule`` is non-empty,
    else ``NO_SETUP`` (a Judge ruling on a skip row is ignored, as in ``classify``).
    """
    if direction is not None:
        if judge_ruling == JudgeRuling.SKIP.value:
            return DecisionKind.VETOED
        return DecisionKind.PUBLISHED
    if violated_rule:
        return DecisionKind.GATE_REJECTED
    return DecisionKind.NO_SETUP
