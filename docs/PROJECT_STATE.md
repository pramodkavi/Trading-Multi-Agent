# Project State & Session Handoff

> **Purpose.** This is the portable, in-repo handoff that lets development continue
> on a **different machine / different Claude Code subscription**. Claude's working
> "memory" normally lives *outside* the repo (`~/.claude/projects/.../memory/`) and is
> **not** cloned, so everything durable has been written here instead. Read this file
> first; it points to everything else.
>
> **Last updated:** 2026-06-12 (end of Slice 1).
> **Authority order:** `SPEC.md` (authoritative spec) → `CLAUDE.md` (index/rules) → this file (current state).

---

## 0. For a fresh Claude on a new machine — do this first

When this repo is cloned onto a new laptop and you (Claude Code) start a session:

1. **Read these, in order:** `CLAUDE.md` → `docs/PROJECT_STATE.md` (this file) → `SPEC.md` (the relevant section for the next step).
2. **Load the prior assistant memory:** read every file in **`docs/memory-snapshot/`**. These are a verbatim copy of the previous machine's Claude memory (user style, project gotchas, architecture decisions). Treat them as background context, and **re-seed your own project memory from them** (write equivalents into your local memory dir, updating any file:line claims against current code first — they are point-in-time notes).
3. **Confirm environment** with §4 below before running anything.
4. The single most important behavioural note: **the user prefers one roadmap step per session, scope confirmed up front, and a clear "stopped at step X" report.** See `docs/memory-snapshot/user_collaboration_style.md`.

---

## 1. What this project is (30-second version)

A **multi-agent, signal-only crypto trading analyzer** built on Smart Money Concepts (SMC).
It never places trades — it sends **Telegram alerts** the user acts on manually. Runs on a
UTC cron (crypto never closes). Full detail in `SPEC.md`; quick index in `CLAUDE.md`.

- **Language/stack:** Python 3.11, Anthropic SDK (Claude Sonnet 4.5 for agents), LangGraph,
  Pydantic v2, AWS Lambda (container image, **outside any VPC**) + Aurora Serverless v2
  (PostgreSQL 16 + pgvector) reached via the **RDS Data API**, EventBridge Scheduler,
  Secrets Manager, S3, CDK (Python) + cdk-nag.
- **Persistence is dual-backend** behind one `SignalStore` interface: `AsyncpgSignalStore`
  (local Docker dev) and `DataApiSignalStore` (cloud, boto3 `rds-data`). Switch via
  `PERSISTENCE_BACKEND=asyncpg|dataapi`.

---

## 2. Current status — **Slice 1 COMPLETE and deployed LIVE**

All 22 steps of Slice 1 (1.1 → 1.22) are done, committed to `main`, and the system is
**running in production** in AWS. End-to-end verified: a manual Lambda invoke returned
`{"ok": true}` for all 4 watchlist symbols, signals persisted to Aurora, and **Telegram
messages were delivered to the user's phone (confirmed 2026-06-12).**

What works end-to-end:
```
EventBridge Scheduler (08:03 UTC daily)
  → Lambda (container image, ap-south-1)
    → Binance market data (CCXT)
    → SMC Analyzer (Slice-1 stub: HTF swing-pivot bias → stub proposal | skip)
    → Aurora (RDS Data API): scan_runs / signals / agent_runs
    → Telegram notification (on BOTH publish and skip)
```

The Slice-1 analyzer is a **stub** (HTF bias only). The full SMC analyzer (BOS/CHoCH/FVG/OB/
liquidity/OTE/premium-discount) is **Slice 2 Step 2.1**.

---

## 3. Live deployment facts (AWS)

> **Keep this repo PRIVATE** — it contains account IDs and resource ARNs (not secrets, but
> not for public exposure).

| Item | Value |
|---|---|
| **Account** | `097853039368` |
| **Region** | `ap-south-1` (Mumbai) — **pinned in `infrastructure/app.py`** |
| **Why this region** | Binance REST **geofences us-east-1 with HTTP 451**. A probe Lambda confirmed ap-south-1 returns 200. Mumbai is the closest Binance-serving region for the operator (Sri Lanka). |
| **Lambda function** | `CryptoSignals-Compute-ScanLambdaDD7505A5-rfFwKEhPAnlo` |
| **Aurora cluster ARN** | `arn:aws:rds:ap-south-1:097853039368:cluster:cryptosignals-data-aurora2cbab212-s1pyvq9ztgdm` |
| **DB secret ARN** | `arn:aws:secretsmanager:ap-south-1:097853039368:secret:crypto-signals/db-a3zZGW` |
| **DB name** | `signals` |
| **Lambda log group** | `CryptoSignals-Compute-ScanLambdaLogs7DF29218-YGmufZcOKKgE` |
| **Schedule** | EventBridge Scheduler `cron(3 8 * * ? *)` Etc/UTC, **ENABLED** (London open). NY/overlap/wrap/Critic windows added in later steps. |
| **App secrets** | `crypto-signals/anthropic-api-key` (plain key string) and `crypto-signals/telegram-bot-token` (**JSON** `{"bot_token":"...","chat_id":"..."}`). |
| **Estimated cost** | ~$5–9/month (Aurora scales to zero when idle). |
| **CDK bootstrap** | Done in ap-south-1 (and us-east-1, now unused). |
| **us-east-1** | All 4 app stacks **torn down** (only the harmless CDKToolkit bootstrap stack remains). |

> ⚠️ The new machine will deploy/operate against **these same live resources** (same AWS
> account). You do **not** need to redeploy to continue development — just point AWS creds at
> account `097853039368` / `ap-south-1`. Redeploy only when infra/code changes.

---

## 4. Setting up a new machine

Prereqs: **Python 3.11+, Docker Desktop, Node 20+ (for the CDK CLI), AWS CLI v2, git.**

```bash
# 1. Clone
git clone <your-remote-url> "Trading Multi Agent" && cd "Trading Multi Agent"

# 2. Python env (PowerShell shown; bash analogous)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 3. Pre-commit + sanity
.\.venv\Scripts\python.exe -m pre_commit install
.\.venv\Scripts\python.exe -m pytest -q          # expect: 351 passed
.\.venv\Scripts\python.exe -m mypy --strict src/ scripts/

# 4. AWS creds (the NEW machine needs its own credentials for account 097853039368)
aws configure          # set region ap-south-1, enter the account's access key/secret
aws sts get-caller-identity      # confirm account 097853039368
aws configure set region ap-south-1

# 5. CDK CLI (only if you will deploy)
npm install -g aws-cdk@2.1126.0   # must match aws-cdk-lib's assembly schema (see infrastructure/requirements.txt)
```

`.env` is **gitignored** and will NOT clone. Recreate it for local dev (see `.env.example`
if present, or these keys): `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`DATABASE_URL` (local Docker: `postgresql://signals:signals@localhost:5433/signals`),
`SCAN_SYMBOLS`, `LOG_LEVEL`. For cloud, the Lambda reads secrets from Secrets Manager — no
`.env` needed there.

### Deploy / invoke recipes (Windows, from repo root)

```bash
# Deploy (Docker Desktop must be running — the Lambda image builds locally)
cd infrastructure
export PATH="/<drive>/.../Trading Multi Agent/.venv/Scripts:$PATH"   # so `python app.py` finds aws-cdk-lib
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1                  # silence Node-version banner
cdk deploy --all --require-approval never
#   ^ if an ECR push hits "TLS handshake timeout", just re-run — the image is cached.

# DB migration over the Data API (idempotent)
.\.venv\Scripts\python.exe scripts\migrate.py --backend dataapi \
  --cluster-arn <cluster-arn> --secret-arn <db-secret-arn> --db-name signals

# Manually invoke the scan Lambda
aws lambda invoke --function-name <fn-name> --region ap-south-1 out.json && cat out.json
#   ^ first call after idle may throw DatabaseResumingException (Aurora waking) — retry ~8s.
```

---

## 5. Key gotchas (consolidated — full notes in `docs/memory-snapshot/`)

- **Region is pinned to ap-south-1** in `infrastructure/app.py`. `cdk destroy --all` therefore
  targets ap-south-1 — to remove a *different* region, use `aws cloudformation delete-stack
  --region <r>` per stack in reverse-dependency order (Scheduling→Compute→Data→Network).
- **Aurora scale-to-zero:** first Data API call after idle throws `DatabaseResumingException`.
  The manual invoke retries; the **daily scheduled scan will hit this every morning** →
  a retry in `DataApiSignalStore._execute` is planned Slice-2 hardening.
- **RDS Data API rejects array parameters.** `scan_runs.symbols` (`text[]`) is written as a
  comma-joined string and reconstructed with `string_to_array(:symbols, ',')` server-side.
- **`pgvector` is NOT a core dependency** — it pulls `numpy`, which tries to compile from
  source on the Lambda base image (no C compiler). It lives in
  `[project.optional-dependencies] slice3-embeddings`. Re-add to runtime when Slice 3 Step 3.4
  needs embeddings (the Aurora `vector` extension is already enabled in `schema.sql`).
- **Windows specifics:** native PostgreSQL-15 shadows port 5432 → Docker DB is mapped to
  **5433**. CCXT's `aiodns` fails on Windows → `BinanceProvider` uses `aiohttp.ThreadedResolver()`.
  Node v25 is newer than CDK's tested set → `JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1`.
  A space in the repo path breaks `cdk --app "<py> app.py"`; instead put `.venv/Scripts` on PATH.
- **mypy scope** is `src/ scripts/` only (not `tests/`, not `infrastructure/`). `ruff` lints
  everything. `tests/` has known pre-existing strict-mypy noise — don't chase it.
- **pre-commit frictions:** (a) a new `src/` runtime dep must be added to BOTH `pyproject.toml`
  and the `.pre-commit-config.yaml` mypy hook `additional_dependencies`; (b) `tests/unit/
  test_migrate.py` gets reformatted by the pinned ruff hook (version skew) — `git add` it and
  re-commit.
- **CI uses pinned pre-commit** for lint/format so "passes locally ⇒ passes in CI" (sidesteps
  the ruff skew). The bare YAML key `on:` parses to boolean `True` under PyYAML — harmless.

---

## 6. Deferred decisions

- **DynamoDB vs Aurora:** evaluated at Step 1.22. Aurora chosen for Slice 1 (DynamoDB ~70%
  cheaper at this scale but needs 20–27h refactor for ~$1.50/mo savings). **Revisit in Slice 2
  if Aurora costs exceed ~$10/mo for 2–3 weeks.** Full analysis:
  `docs/memory-snapshot/architecture_dynamodb_vs_aurora.md`. The user explicitly wants a
  DynamoDB brainstorm during Slice 2.

---

## 7. Open action items (carry into the next session)

| Priority | Item |
|---|---|
| 🔴 **Security** | **Rotate the Anthropic API key and Telegram bot token** — both appeared in chat across sessions. After rotating, update Secrets Manager: `aws secretsmanager update-secret --secret-id crypto-signals/anthropic-api-key --secret-string '<key>'` and `... telegram-bot-token --secret-string '{"bot_token":"<t>","chat_id":"8300889332"}'`. Lambda picks them up on next cold start (no redeploy). Also rotate the local `.env`. |
| 🟠 Infra/CD | No git remote was set when Slice 1 finished — once pushed, configure CD: GitHub repo secrets `AWS_DEPLOY_ROLE_ARN`/`AWS_PROD_DEPLOY_ROLE_ARN`, vars `AWS_REGION`/`AWS_PROD_REGION` **= ap-south-1**, Environments `dev` + `production` (required reviewers), and an OIDC IdP + deploy roles in AWS. See `.github/workflows/deploy-*.yml` headers. |
| 🟡 Hardening | Add a `DatabaseResumingException` retry in `DataApiSignalStore._execute` so the first daily scan survives Aurora waking. |

---

## 8. What's next — Slice 2 (Weeks 4–7), per `SPEC.md §4`

> **Progress (2026-06-13):** CD pipeline is fully working (push to `main` → auto-deploy dev via
> OIDC). The user supplied their reference SMC scripts (`requested_scripts/`); we ran an
> evidence review (`docs/research/smc-evidence-review.md`) and agreed an **evidence-weighted +
> calibrated** Analyzer philosophy (premium/discount + liquidity at obvious levels = high
> trust; derivatives = regime/risk filter not direction; OTE/PO3 = low-weight context;
> **as-of correctness mandatory**; forward-test, not backtest, is the real validation).
> **Step 2.1a shipped:** `src/agents/analyzer/smc/` — typed, look-ahead-free structure layer
> (swings, BOS/CHoCH state machine, market phase, directional Premium/Discount + OTE,
> ATR normalization) with a no-look-ahead invariant test.
> **Step 2.1b shipped:** `fvg.py` — Fair Value Gap detector (3-candle imbalance, ATR-normalized
> size + displacement, as-of mitigation/fill status) with a no-look-ahead invariant test.
> **Step 2.1c shipped:** `order_block.py` — Order Block detector anchored to confirmed BOS/CHoCH
> events (2.1a) with FVG confluence (2.1b), displacement, and as-of mitigation status.
> **Step 2.1d shipped:** `liquidity.py` — BSL/SSL pools, equal-level clustering, stop-hunt sweeps
> vs breaks (as-of correct), nearest resting targets.
> **Step 2.1e shipped — STEP 2.1 COMPLETE:** `analysis.py::full_smc_analysis` combines all four
> detector layers via a HYBRID gate model — HARD gates (clear bias, Premium/Discount §1.6 rule 3,
> a valid order-block POI) + an evidence-WEIGHTED confluence threshold (liquidity sweep highest
> weight; OB displacement/FVG/fresh; OTE lowest) — emits a complete `SignalProposal | SkipDecision`
> with Layer-5 risk geometry (entry at POI, SL beyond it, TP = nearest resting opposing liquidity).
> **`smc_analyzer.analyze()` is now rewired to this** (the Slice-1 HTF-bias stub is gone — first
> live-path change since Slice 1); the graph tests were repointed at a publishing series.
> `confluence_score` is a raw tally surfaced in features/tags, NOT a calibrated probability.
> **The full SMC analyzer is now live on the `analyze()` path.** Next: **Step 2.2 (multi-timeframe
> data provider — D1/H1/M15/M5 + funding/OI + rate limiting)**, which feeds the analyzer real
> top-down data and unlocks the derivatives gate (Gate 4, stubbed today).

Slice 2 turns the single-agent stub into the full pipeline. Expected scope:

1. **Step 2.1 — Real SMC Analyzer.** Replace the Slice-1 stub (`src/agents/analyzer/
   smc_analyzer.py`) with the full 5-layer SMC protocol (BOS/CHoCH, FVG, OB, liquidity sweeps,
   OTE Fibonacci, premium/discount). NOTE: SPEC says "port detect_fvg.py / detect_ob.py /
   detect_liquidity.py / derivatives_data.py" — **those files do not exist in this repo**
   (same as Step 1.5); expect to implement from scratch after confirming with the user. This
   also fixes the retracement case the Slice-1 stub currently skips (see
   `docs/memory-snapshot/project_step_1_5_blocker.md`).
2. **Full 4-agent per-signal pipeline:** Analyzer → Historian (3-stage journal retrieval) →
   Skeptic (independent macro: DXY/SPX/VIX/on-chain) → Judge (PUBLISH | PUBLISH_WITH_CAVEAT |
   SKIP).
3. **Forecaster** background loop (re-evaluate open setups: STILL_VALID / AT_RISK / INVALIDATED).
4. **Hard risk gates** (`src/agents/orchestration/risk_gates.py`): 1% max risk, 1:3 min R:R,
   premium/discount enforcement, max 3 concurrent / 5 per 24h, 3-loss pause, session blocks,
   10x leverage cap. Pure functions between Analyzer and Historian; a violation → SKIP with the
   rule logged. (Schema bounds are intentionally looser than policy — see
   `docs/memory-snapshot/project_policy_vs_schema.md`.)
5. **Multi-symbol watchlist** parallelism (BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT today).
6. **Slice-2 hardening:** Aurora resume retry; new schedules (NY 13:03, overlap 15:03, wrap 22:03).
7. **DynamoDB cost brainstorm** (see §6).

Build discipline (from `CLAUDE.md §6`): one step per session, re-read SPEC first, tests before
"done", commit at every step boundary on a `feat/slice-2-step-M-*` branch, update docs in the
same commit.

---

## 9. Where the detailed history lives

- **`docs/memory-snapshot/`** — verbatim copy of the previous machine's Claude memory:
  - `project_serverless_pivot.md` — the big one: full step-by-step log of the Fargate→serverless
    pivot and every Slice-1 step's commits, decisions, and gotchas (Steps 1.13 → 1.22).
  - `architecture_dynamodb_vs_aurora.md` — the cost/feasibility analysis.
  - `project_local_infra_gotchas.md`, `project_precommit_friction.md` — Windows + tooling.
  - `project_policy_vs_schema.md`, `project_step_1_5_blocker.md` — design decisions.
  - `user_collaboration_style.md` — how the user wants to work.
  - `MEMORY.md` — the index of the above.
- **Git history** on `main` — every step is at least one commit with a `feat(slice-1/step-N.M)`
  or `fix(...)` message.
