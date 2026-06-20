# CLAUDE.md — Multi-Agent Crypto Trading Signal System

> **Authority:** `SPEC.md` is the authoritative specification. This file is a quick-reference index. **Always re-read the relevant section of SPEC.md before designing or implementing anything.** When this file and SPEC.md disagree, SPEC.md wins — update this file to match.

> **🆕 New machine / resuming work? START with [`docs/PROJECT_STATE.md`](docs/PROJECT_STATE.md).**
> It is the portable session handoff: current status, the LIVE AWS deployment (region/IDs),
> environment setup, gotchas, open action items, and the Slice 2 plan. Then read the prior
> assistant memory in [`docs/memory-snapshot/`](docs/memory-snapshot/) and re-seed your own
> memory from it.

---

## 1. What This Project Is

A **multi-agent, autonomous crypto trading signal analyzer** built around Smart Money Concepts (SMC).

- **Signal-only system** — never places trades. No broker credentials. No order placement. This is a hard architectural constraint, not a limitation to remove later.
- User receives Telegram alerts and executes manually.
- Runs on a scheduled UTC cron (7 days/week — crypto never closes).
- Self-improving via a weekly Critic that opens PRs proposing rule changes.

## 2. Six Agent Roles (all powered by Claude Sonnet 4.5)

**Per-signal pipeline** (every scan):
1. **Analyzer** — Runs SMC 5-layer protocol; emits `SignalProposal` or `SkipDecision`.
2. **Historian** — Three-stage retrieval (hard filters → tag overlap → L2 distance) over the signal journal.
3. **Skeptic** — Independently fetches macro (DXY, SPX, VIX, on-chain) and tries to invalidate the proposal.
4. **Judge** — Weighs proposal + history + objection. Outputs `PUBLISH | PUBLISH_WITH_CAVEAT | SKIP`.

**Background loops**:
5. **Forecaster** — Re-evaluates open setups every scan. STILL_VALID / AT_RISK / INVALIDATED.
6. **Critic** — Weekly meta-review; opens a PR on `claude/proposed-rules-YYYY-MM-DD` branch.

## 3. Hard Rules (Programmatically Enforced — Cannot Be Overruled by Any Agent)

See SPEC.md §1.6 for the full list. Highlights:
- Max 1% equity risk per signal; minimum 1:3 R:R.
- **Premium/Discount enforcement**: long only in Discount, short only in Premium. Hard rule.
- Max 3 concurrent active signals; max 5 signals per 24h.
- 3 consecutive losses → mandatory 24h pause.
- No new signals in Asian session (00:00-08:00 UTC) or Cooldown (21:00-00:00 UTC).
- Max 10x leverage recommendation.
- **Traditional indicators (RSI, MACD, Bollinger, MAs) are explicitly forbidden.**

Risk gates live in `src/agents/orchestration/risk_gates.py` as pure functions inserted between Analyzer and Historian. A proposal violating any hard rule must become a SKIP with the violating rule logged.

## 4. Tech Stack (Locked Decisions)

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| LLM | Anthropic SDK direct calls (no framework wrapper), Claude Sonnet 4.5 |
| Orchestration | LangGraph v1.0+, Postgres checkpointer |
| Validation | Pydantic v2 at every agent boundary |
| Observability | Langfuse (self-hosted) — every LLM/tool call traced from day one |
| DB | Aurora Serverless v2 PostgreSQL 16+ with pgvector (scale-to-zero). Cloud access via RDS Data API; local dev via asyncpg → Docker. Dual-backend repositories. |
| Storage | S3 (versioning on, Glacier after 90 days) |
| Compute | **AWS Lambda** (container image, outside VPC) for the agent pipeline — revised from ECS Fargate, see SPEC §2.4. Dashboard (Slice 4) is a separate long-running Fargate/App Runner service. |
| Scheduling | EventBridge Scheduler → Lambda |
| Secrets | SSM Parameter Store (SecureString) for API keys; AWS Secrets Manager for the Aurora DB credential (Step 2.12) |
| IaC | AWS CDK (Python) + cdk-nag + `aws-cdk@aws-skills` plugin |
| Local dev | LocalStack + docker-compose |
| Dashboard | FastAPI + WebSocket + Postgres LISTEN/NOTIFY; React + TS + TradingView Lightweight Charts |
| Notifications | Telegram via direct httpx (no library) |
| Quality | ruff, mypy --strict, pytest, pre-commit |

All external data is accessed through a uniform `DataProvider` interface. Agents **never** call provider libraries directly.

## 5. Scan Schedule (UTC)

| Cron | Purpose |
|---|---|
| `3 8 * * *` | London open |
| `3 13 * * *` | NY open |
| `3 15 * * *` | London-NY overlap |
| `3 22 * * *` | Daily wrap + active setups review |
| `0 21 * * 0` | Weekly Critic meta-review |

Minutes are `:03` deliberately — avoids clock-jitter on `:00`.

## 6. Build Discipline — Read This Every Session

> **The roadmap is organized into 4 vertical slices, ~70 numbered steps total. See SPEC.md §4.**

**Non-negotiable rules for Claude Code in this repo:**

1. **One step per session.** Do not batch multiple steps. Context bloat causes silent quality degradation.
2. **Always re-read SPEC.md first.** Spec > intuition. Always.
3. **Validate before continuing.** Every step has checkpoints in SPEC.md §5. All must pass before moving on.
4. **Tests exist before a step is "done."** Validation without tests is wishful thinking.
5. **Commit at every step boundary.** Each step = at least one commit. Branch name: `feat/slice-N-step-M-description`.
6. **Update docs in the same commit as behavior changes.** Out-of-date docs are worse than missing docs.
7. **Each slice is vertical** — data, agents, storage, and infra together. No "build all storage first" phases.

**Universal per-step checkpoints (SPEC.md §5.1):**
- `ruff check .` and `ruff format --check .` — zero warnings
- `mypy --strict src/` — passes
- `pytest` — all green (no skipped tests outside `@pytest.mark.integration`)
- Pre-commit hooks pass
- No secrets in `git diff`

## 7. Current Build Status

- **Slice 1 (Weeks 1-3): ✅ COMPLETE & DEPLOYED LIVE** (Steps 1.1–1.22). End-to-end substrate — multi-symbol watchlist, SMC-stub Analyzer, Telegram delivery from a scheduled **Lambda** (container image, **ap-south-1**) + Aurora Serverless v2 via the RDS Data API. Verified end-to-end (Telegram confirmed 2026-06-12). **See [`docs/PROJECT_STATE.md`](docs/PROJECT_STATE.md) for live resource IDs, gotchas, and open items.**
- **Slice 2 (Weeks 4-7): ⬅️ NEXT.** Full 4-agent pipeline + Forecaster + risk gates + multi-symbol + **real** SMC Analyzer (replaces the Slice-1 stub at Step 2.1).
- **Slice 3 (Weeks 8-11):** Strategy registry + embeddings + Critic with PR opening.
- **Slice 4 (Weeks 12-15):** FastAPI + React dashboard, Cognito auth, WebSocket real-time.

**Open action items** (detail in `docs/PROJECT_STATE.md §7`): rotate the Anthropic + Telegram credentials (exposed in chat); set the git remote + configure CD secrets/vars/environments (region = ap-south-1). *(Resolved: the Aurora-resume retry in `DataApiSignalStore._execute` — shipped on `fix/aurora-resume-retry`.)*

## 8. Things NOT to Build (Hard Scope Boundaries)

See SPEC.md §6.8. Do not build without explicit user request:
- Order execution / broker integration (signal-only is permanent).
- Mobile app, multi-tenancy, backtesting framework.
- Custom LLM fine-tuning. Alternative LLM providers.
- Microservices split / Step Functions fan-out (a single scan Lambda is correct at this scale).
- Indicators like RSI/MACD/Bollinger/MAs.

## 9. When to Pause and Ask (SPEC.md §6.9)

- Step complexity is much higher than estimated.
- External service outage (Binance/FRED/Anthropic).
- Test failure suggests spec ambiguity.
- Cost projections exceed §3.3.5 targets.
- Unanticipated security concern.

## 10. Branch & Tag Conventions

- `main` — always deployable to dev (push to main = auto-deploy dev).
- `v*.*.*` tag — triggers prod deploy with manual approval gate.
- `claude/proposed-rules-YYYY-MM-DD` — Critic-authored PRs.
- `feat/slice-N-step-M-description` — feature branches.

## 11. Default Watchlist (Slice 1-2)

BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT. Defined in `src/config/strategies.yaml`. Expandable via config only.

## 12. SMC Glossary (Reference)

BOS (Break of Structure), CHoCH (Change of Character), FVG (Fair Value Gap), OB (Order Block), BSL/SSL (Buy/Sell-Side Liquidity), OTE (Optimal Trade Entry, 61.8%-78.6% Fib), PO3 (Power of 3 — Accumulation/Manipulation/Distribution), POI (Point of Interest), HTF/MTF/LTF (Higher/Mid/Lower Time Frame).

---

*This file is an index, not a replacement for SPEC.md. When in doubt, open SPEC.md.*
