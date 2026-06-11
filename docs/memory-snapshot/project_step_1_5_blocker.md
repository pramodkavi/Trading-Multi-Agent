---
name: project-step-1-5-resolution
description: How the missing detect_structure.py was handled at Step 1.5 — implemented from scratch
metadata:
  node_type: memory
  type: project
  originSessionId: f29f3cf9-20ea-45e3-bf96-82b625abdeba
---

**RESOLVED 2026-05-27.** SPEC.md Step 1.5 instructed "port the existing `detect_structure.py`" but that file never existed in this repo. User chose option (a): implement HTF bias detection from scratch following SPEC §1.5 Layer 2 description.

**What was built** (commit `ee2f8e1`, `src/agents/analyzer/smc_analyzer.py`):
- swing-pivot detection via the classical pivot-K method (lookback K=3)
- bias classification: UPTREND (HH+HL) / DOWNTREND (LH+LL) / CONSOLIDATION
- when bias is clear, synthesizes a **stub SignalProposal** with `tags=['slice-1-stub', 'htf-bias-only', 'bias-{up,down}trend']` so downstream agents + Critic can later distinguish stubs from real proposals
- tunable parameters exposed as module-level constants (`PIVOT_LOOKBACK`, `MIN_KLINES_REQUIRED`, `MAX_PIVOT_AGE`, `STUB_SL_BUFFER`, `STUB_RR_RATIO`)

**Why:** SPEC §1.5 broader scope (BOS/CHoCH/POI/Gates/OTE Fibonacci/Premium-Discount) is explicitly deferred to Slice 2 Step 2.1. Slice 1 only needs bias classification to reach the §5.3 end-to-end milestone (Fargate -> Postgres -> Telegram).

**How to apply at Step 2.1:** When SPEC Step 2.1 says "Port the rest of the SMC scripts (detect_fvg.py, detect_ob.py, detect_liquidity.py, derivatives_data.py)" — those files also won't exist. Ask the user the same question (paste sources vs. implement from scratch), and expect "from scratch" again. The Slice 1 stub proposal logic (latest swing low/high as SL anchor) should be replaced with the real Layer 5 logic (SL beyond the liquidity sweep wick).

See [[user-collaboration-style]] for the broader "confirm scope first" pattern.
