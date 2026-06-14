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

# DB migration over the Data API (idempotent).
#   NOTE: as of Slice 2 the CD workflows run this AUTOMATICALLY before `cdk deploy`
#   (discovering the ARNs from the CryptoSignals-Data stack outputs), so a normal
#   push-to-main deploy needs no manual migration. The command below is for LOCAL
#   dev DBs and break-glass / out-of-band schema applies only.
.\.venv\Scripts\python.exe -m scripts.migrate --backend dataapi `
  --cluster-arn <cluster-arn> --secret-arn <db-secret-arn> --db-name signals
#   ARNs for the live cluster are in §3. If the first call throws
#   DatabaseResumingException (Aurora waking from scale-to-zero), just re-run it.

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
| 🟠 Infra/CD | No git remote was set when Slice 1 finished — once pushed, configure CD: GitHub repo secrets `AWS_DEPLOY_ROLE_ARN`/`AWS_PROD_DEPLOY_ROLE_ARN`, vars `AWS_REGION`/`AWS_PROD_REGION` **= ap-south-1**, Environments `dev` + `production` (required reviewers), and an OIDC IdP + deploy roles in AWS. See `.github/workflows/deploy-*.yml` headers. **(done for dev — OIDC live.)** |
| 🟠 Infra/CD | **Auto-migration IAM (Slice 2):** the deploy workflows now run the DB migration before `cdk deploy` using the OIDC deploy role. That role must allow `cloudformation:DescribeStacks` (already has it), `rds-data:ExecuteStatement` on the cluster, and `secretsmanager:GetSecretValue` on the DB secret. If the role is admin-ish this is already covered; if it's scoped, attach the policy in §4 / the deploy notes or the migration step fails with `AccessDenied`. |
| 🟡 Hardening | Add a `DatabaseResumingException` retry in `DataApiSignalStore._execute` so the first daily **scan** survives Aurora waking. (The CD migration step already retries; this item is the separate runtime/scan path.) |

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
> **The full SMC analyzer is now live on the `analyze()` path.**
> **Step 2.2 shipped:** `src/providers/rate_limit.py::TokenBucket` (async, injectable clock; Binance
> 2400 weight/min preset) + `BinanceProvider` upgrades — concurrent multi-timeframe fetch
> (`asyncio.gather`), `fetch_funding_rate`/`fetch_open_interest` methods, and `include_derivatives`
> on `fetch_market_snapshot` to populate `funding_rate`/`open_interest` (best-effort; degrades to
> None). All API calls now meter request weight through the bucket. Unit tests (mocked) + opt-in
> integration test (real multi-TF + derivatives). NOTE: `run_scan` still requests only H4 — wiring
> it to request the SMC timeframes + derivatives is a small follow-up (do it when the analyzer's
> multi-TF top-down logic lands; the analyzer already falls back gracefully today).
> **Step 2.3 shipped:** `src/providers/macro.py` — `FREDProvider` (DXY proxy DTWEXBGS / US 10Y DGS10 /
> Fed Funds DFF) and `TwelveDataProvider` (SPX, VIX), both behind a shared `MacroProvider(DataProvider)`
> base (httpx, not ccxt). Each returns a normalized `MacroContext` (extended with a `fed_funds` field)
> populated with only the fields it owns, or a `NoMacroData` sentinel when it can serve none (graceful
> degradation per FR-4.3; per-field best-effort otherwise). Market-snapshot is unsupported on macro
> providers (raises). Unit tests via `httpx.MockTransport`; opt-in integration tests skip without keys.
> NOTE: providers take `api_key` directly; Settings wiring (`fred_api_key` / `twelve_data_api_key`)
> landed with the Skeptic (Step 2.5). Remaining: Secrets Manager ARN hydration for these keys in the
> cloud Lambda is deferred to Step 2.12 ops (today they read from plain env / `.env`).
> **Step 2.4a shipped (schema + persistence foundation):** `signals` gained first-class
> `tags TEXT[]` / `features JSONB` / `outcome` (enum-checked) / `outcome_metadata JSONB` columns
> (idempotent `ALTER ... ADD COLUMN IF NOT EXISTS` + GIN index on tags). `SignalOutcome` enum added.
> Both store backends write tags/features at `create_signal` (Data API uses the `string_to_array`
> comma-string workaround; asyncpg binds the list natively) and read all four back into an extended
> `StoredSignal`. Data-API migration statement count 12→18. **⚠️ MIGRATION ORDERING: apply the schema
> (migrate.py) to the live DB BEFORE deploying this code — `create_signal` now INSERTs the new
> columns, which must exist.** outcome/outcome_metadata stay NULL at creation (set by the Forecaster,
> Step 2.9).
> **Step 2.4b shipped — THE HISTORIAN:** new `src/agents/historian/` package. `HistorianRepository`
> does the three-stage retrieval (SPEC FR-1.4): stage 1 = SQL hard filters (direction, session via a
> `scan_runs` JOIN, `primary_poi_type`, PUBLISHED + known-outcome); stage 2 = tag-overlap ranking via
> PG array ops (`cardinality(... INTERSECT ...)`); stage 3 = L2 distance over a *scale-free* numeric
> vector (`L2_FEATURE_KEYS` = confluence_score, ob_confluence_count — price-scale features
> deliberately excluded) via a `sqrt/power` SQL expression. Produces a `HistorianReport` (empirical
> win rate = wins/(wins+losses), with `sample_size` + outcome breakdown + a Telegram/Judge summary;
> win_rate is `None` when no decisive outcomes — never faked). `make_historian_node` is a node FACTORY
> (store injected via closure, never in checkpointed state); wired into the graph at **Step 2.7** (the
> edge is NOT added yet — `AgentState` gained `historian_report` but the live scan path stays
> analyzer→END). The 3-stage SQL lives in both store backends (`find_similar_signals` on
> DataApiSignalStore + SignalRepository); `set_signal_outcome` added to both (Forecaster write-side,
> used now by the seed). Analyzer gained one feature: `primary_poi_type="order_block"`. Seed fixture
> `scripts/seed_signals.py` (`build_synthetic_signals` + dual-backend CLI) makes 50 outcome-bearing
> synthetic signals. Tests: report/helpers/node (fake store) + find_similar SQL-shape & parse for BOTH
> backends (mocked) + opt-in asyncpg integration (real ranking). Checkpoints green: ruff, mypy --strict
> (48 files), pytest **465 passed**.
> **Step 2.5 shipped — THE SKEPTIC:** new `src/agents/skeptic/` package. `Skeptic.gather_macro()`
> fetches every injected macro provider in parallel (`asyncio.gather(return_exceptions=True)`) and
> merges the partial `MacroContext` snapshots (FRED owns DXY/US10Y/FedFunds; Twelve Data owns SPX/VIX)
> into one; if no provider serves data it returns the provider-level `NoMacroData` sentinel (FR-4.3
> graceful degradation — Judge reads it as "downgrade confidence to medium", NOT "no objection").
> `Skeptic.evaluate()` then calls Claude via `structured_completion` with the new `SkepticObjection`
> schema (severity LOW/MEDIUM/HIGH + recommends_against + headline + reasoning + cited_macro) — and
> short-circuits to NoMacroData (no LLM call) when macro is unavailable. The system prompt enforces:
> reason only from supplied data, snapshots (no trend invention), treat SPX/VIX as possibly-proxy
> regime cues (never absolute thresholds), forbidden indicators (RSI/MACD/…), honest severity
> calibration. `make_skeptic_node` is a node FACTORY (providers + Anthropic client injected via
> closure); `AgentState` gained `skeptic_objection: SkepticObjection | NoMacroData | None` (runtime
> import in graph.py — the LangGraph get_type_hints gotcha) but the edge is wired at **Step 2.7** (live
> path stays analyzer→END). `build_macro_providers(settings)` constructs FRED + Twelve Data (with the
> SPY/VIXY free-tier ETF proxies, Step 2.3 cost decision) for whichever keys are set, else []. Settings
> gained optional `fred_api_key` / `twelve_data_api_key` (SecretStr) + `.env.example` entries. Tests:
> merge/gather (success/all-unavailable/exception-tolerant) + evaluate (mocked Anthropic client; macro
> short-circuit) + node (skip/objection/NoMacroData) + prompt + build_macro_providers. Checkpoints
> green: ruff, mypy --strict (51 files), pytest **483 passed**.
> **Step 2.6 shipped — THE JUDGE:** new `src/agents/judge/` package — the final arbiter (FR-1.6).
> `Judge.evaluate(proposal, historian_report, skeptic_objection)` weighs the three already-gathered
> inputs (it fetches nothing) and calls Claude via `structured_completion` with the new `JudgeDecision`
> schema (ruling PUBLISH / PUBLISH_WITH_CAVEAT / SKIP + `confidence` LOW/MEDIUM/HIGH + written
> `reasoning` + optional `caveat`). A model validator requires a non-empty caveat exactly when the
> ruling is PUBLISH_WITH_CAVEAT (malformed → structured-output retry). **FR-4.3 is enforced
> DETERMINISTICALLY**: when the Skeptic returned `NoMacroData`, `evaluate` clamps confidence HIGH→MEDIUM
> after the LLM call (the system prompt also asks for it, but the clamp guarantees it). System prompt
> bakes in the signal-only precision-over-recall asymmetry ("a missed signal costs nothing; a bad
> published signal costs real money"), how to weight each input (historian win rate by SAMPLE SIZE;
> win_rate=None = absence of evidence, treat neutrally; skeptic severity → SKIP/caveat/minor), the
> three calibration shapes, forbidden indicators, and "hard numeric risk rules are enforced by code
> (risk_gates), not you". `make_judge_node` is a node FACTORY; the node SKIPs non-proposals without an
> LLM call and, for proposals, sets BOTH `judge_decision` (full object) and `decision` (the ruling enum
> the existing dispatcher/scan-runner already consume via `state["decision"].value`). New
> `JudgeConfidence` enum in common/models/enums.py + exported. `AgentState` gained `judge_decision:
> JudgeDecision | None` (runtime import in graph.py — same get_type_hints gotcha). **Edge still NOT
> wired — Step 2.7** (live path stays analyzer→END). 15 tests: schema/caveat validation, the three SPEC
> scenarios (ruling plumbs through + prompt encodes the facts), FR-4.3 cap (+ no-cap when macro
> available), node skip/proposal/missing-inputs, prompt rendering of None/NoMacroData. Checkpoints
> green: ruff, ruff-format, mypy --strict (54 files), pytest **498 passed**.
> **Step 2.7 shipped (graph-only scope, user-confirmed) — FULL PIPELINE WIRED:** new
> `build_pipeline_graph(*, historian, skeptic, judge, checkpointer=None, tracer=trace_node)` in
> `graph.py` compiles `analyzer → historian → skeptic → judge → END` with a **conditional edge after the
> analyzer** (`_route_after_analyzer`: a real `SignalProposal` → "continue" into historian; a
> SkipDecision/None → "skip" → END, so skips cost ZERO LLM calls). The three agents are injected
> (deps live in the agent objects, never in checkpointed state). New `src/common/tracing.py`:
> `trace_node(name, fn)` is an env-gated (LANGFUSE_PUBLIC_KEY+SECRET_KEY) Langfuse wrapper that is a
> transparent NO-OP by default (returns the same fn object) and degrades gracefully if the optional
> `langfuse` extra isn't installed — every node is wrapped via the injected `tracer`. Checkpointer is an
> optional param compiled in (`.compile(checkpointer=...)`); proven in tests with the bundled
> `InMemorySaver` (no new dep). `langfuse` added as the optional `[tracing]` extra + a mypy
> ignore_missing_imports override; **NO new runtime deps**. **Decisions (user-chosen via AskUserQuestion):
> Langfuse optional/no-op until configured; Postgres checkpointer LOCAL/asyncpg-only (the Data API Lambda
> has no direct Postgres socket → passes None, relies on cron/EventBridge re-runs); scope = graph +
> integration test only.** `build_graph`/`run_scan` stay analyzer-only — **the pipeline is NOT on the live
> scan path yet.** 8 tests (offline, all agents mocked): publish-through-all-nodes, skip short-circuits
> (historian/skeptic/judge never called), checkpointer persists state, tracer wraps every node + tracing
> seam unit tests. Checkpoints green: ruff, mypy --strict (55 files), pytest **506 passed**.
> **Step 2.7 LIVE ADOPTION shipped (user-confirmed) — THE PIPELINE IS NOW THE LIVE SCAN PATH:**
> `scripts/run_scan.py` rewritten. `build_pipeline(settings, store, client)` constructs the graph ONCE
> per process (Historian over the store + Skeptic with `build_macro_providers(settings)` + Judge, all on
> one `AsyncAnthropic` client) and `run_one_symbol` now runs `build_pipeline_graph(...).ainvoke(...)`
> instead of the analyzer-only graph. **The Slice-1 `generate_commentary` / `MarketCommentary` stand-in
> is DELETED — the Skeptic + Judge nodes make the live Claude calls now.** ⚠️ **DEPLOYING THIS CHANGES
> LIVE BEHAVIOUR + INTRODUCES ONGOING LLM COST (~$3-5/mo at the ≤5-signal/day cap; skips cost nothing —
> the conditional edge short-circuits).** FR-1.7: `_persist` writes `create_signal` + one `agent_run` per
> agent that ran (analyzer always; historian/skeptic/judge only on a publish path) via `model_dump(mode=
> "json")`; latency/token/cost omitted for now (Langfuse covers that when enabled). FR-5.2:
> `compose_message` branches on the Judge ruling — PUBLISH/PUBLISH_WITH_CAVEAT → `format_new_signal` (now
> takes a `caveat` kwarg) enriched with historian win-rate + skeptic objection (+ caveat); analyzer skip →
> `format_skip`; Judge veto on a real proposal → a "JUDGED SKIP" note. `run_one_symbol` signature changed:
> drops `settings`/`anthropic_client`, takes the prebuilt `graph` (+ provider/store/notifier). The
> `AsyncAnthropic` client is created in `_run_symbols`/`_amain` and **closed** in finally (`await
> client.close()` — confirmed `close`, not `aclose`) to avoid a ResourceWarning under
> `filterwarnings=error`. `test_run_scan.py` fully rewritten (mocked pipeline graph + clients): publish
> persists 4 agent_runs, skip persists 1, compose_message variants, lambda lifecycle (now also patches
> `AsyncAnthropic`). Checkpoints green: ruff, mypy --strict (55 files), pytest **507 passed**.
> **STILL DEFERRED (smaller follow-ups, not blockers):** local `AsyncPostgresSaver` checkpointer (Lambda
> runs without one; needs `langgraph-checkpoint-postgres` + table setup); per-agent token/cost on
> agent_runs rows; multi-TF/derivatives fetch (still H4-only); the scan `session` is hardcoded AD_HOC
> (the EventBridge cron→session mapping is Step 2.10).
> **Step 2.8 shipped — ACTIVE SETUPS TRACKING:** new `active_setups` table (id, signal_id FK→signals
> ON DELETE CASCADE, opened_at, status, last_evaluated_at, latest_evaluation JSONB) + 2 indexes, added
> to schema.sql (idempotent; CD auto-migrates before deploy → the live DB gets it automatically). New
> `ActiveSetupStatus` enum (OPEN + the SignalOutcome terminals WIN/LOSS/BREAKEVEN/INVALIDATED/EXPIRED —
> status doubles as the lifecycle per the SPEC column list, no separate outcome column). `StoredActiveSetup`
> read model (`is_open` property). `ActiveSetupRepository` (asyncpg) + matching methods on the
> `SignalStore` Protocol / `DataApiSignalStore` (named params, `_utc_iso` timestamps, `_row_to_active_setup`)
> / `AsyncpgSignalStore` forwards: `open_active_setup(signal_id)`, `list_open_active_setups()` (oldest
> first — the Forecaster's queue), `get_active_setup(id)`, `update_active_setup(id, status, evaluation,
> evaluated_at)` (COALESCE keeps prior eval when None; one method serves both a STILL_VALID/AT_RISK touch
> and a terminal close — the Forecaster at 2.9 will also call `set_signal_outcome` alongside a terminal
> update). **Wired into `run_scan._persist`:** captures the `create_signal` return id and, only on a Judge
> PUBLISH/PUBLISH_WITH_CAVEAT (not skips, not vetoes), calls `open_active_setup`. `EXPECTED_TABLES` grew
> to 4; Data-API migration statement-count pin 18→21 (1 table + 2 indexes); schema comment avoids the
> literal "CREATE TABLE/INDEX" phrase (the idempotency-count test scans for it). Tests: StoredActiveSetup
> model + ActiveSetupRepository (mocked conn) + DataApi methods (mocked rds-data) — SQL shape/params/parse
> for BOTH backends + run_scan wiring (publish opens a setup; skip & veto don't) + opt-in asyncpg
> integration (open→list→update lifecycle + signal-delete cascade; NOT run here — no Docker). Checkpoints
> green: ruff, mypy --strict (55 files), pytest **525 passed** (23 deselected).
> **Step 2.9 shipped — THE FORECASTER:** new `src/agents/forecaster/` package — the background loop
> (FR-2.1) that re-evaluates open setups. New `ForecastStatus` enum (STILL_VALID / AT_RISK / INVALIDATED)
> + `ForecasterUpdate` model (status + reasoning + `outcome`; a model_validator requires `outcome` IFF
> status is INVALIDATED). `Forecaster(store, provider, notifier, client, model)` — `run()` lists open
> setups, and per setup: re-parses the proposal (`StoredSignal.as_proposal`), refetches an H4 snapshot,
> asks Claude (`structured_completion`, tool `emit_forecast`) for a verdict, then acts: STILL_VALID →
> record eval, stays OPEN; AT_RISK → record + Telegram warning; INVALIDATED → close
> (`update_active_setup(status=ActiveSetupStatus(outcome.value))`) + `set_signal_outcome` (journals the
> terminal result) + Telegram. Per-setup work is try/except-isolated (one bad/orphan setup never aborts
> the run; missing/non-PUBLISHED signal is skipped with no LLM call). New `format_forecaster_update`
> formatter (FR-5.3: distinct "⚠️ SETUP AT RISK" / "🔚 SETUP CLOSED" headers vs NEW SIGNAL) — typed via a
> TYPE_CHECKING-only import of ForecasterUpdate to avoid a notifications↔forecaster cycle. **NOT wired to
> a schedule yet — that's Step 2.10 (a separate EventBridge rule → Lambda), so no live LLM cost from this
> step.** 11 tests (schema validation, the 3 verdict paths via fake store/provider/notifier + mocked
> client, orphan skip, per-setup failure isolation, formatter). Checkpoints green: ruff, ruff-format,
> mypy --strict (58 files), pytest **536 passed** (23 deselected). Next: **Step 2.10 (Forecaster
> scheduling — `5 8,13,15,22` cron + a forecaster Lambda)** — or **Step 2.11 (risk_gates) given the live
> deploy still has no §1.6 enforcement**, user's call.

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
