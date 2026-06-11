---
name: project-policy-vs-schema
description: Design decision for where SPEC §1.6 hard rules are enforced in the codebase
metadata:
  node_type: memory
  type: project
  originSessionId: f29f3cf9-20ea-45e3-bf96-82b625abdeba
---

In SignalProposal, schema field constraints are deliberately looser than SPEC §1.6 policy:

- `risk_percent`: schema `le=10`, policy `<= 1` (rule 1)
- `leverage`: schema `le=100`, policy `<= 10` (rule 8)
- `risk_reward_ratio`: schema `gt=0`, policy `>= 3` (rule 2)

**Why:** Hard rules are enforced by `src/agents/orchestration/risk_gates.py` (Step 2.11). The gate forces a SkipDecision with `violated_rule` populated. If the schema also rejected policy violations, the gate could never log *which* rule fired or with what value — Pydantic would have already raised on construction.

**How to apply:** When adding new SignalProposal fields with hard policy bounds, put the operational ceiling in the schema (catch nonsense values) but leave headroom for the policy gate to do the actual rejection with auditable logging. The schema enforces *shape*; the gate enforces *intent*.

See [[user-collaboration-style]] for the broader build pattern.
