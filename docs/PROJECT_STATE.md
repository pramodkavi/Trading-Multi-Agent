# Project State & Session Handoff

> **Purpose.** This is the portable, in-repo handoff that lets development continue
> on a **different machine / different Claude Code subscription**. Claude's working
> "memory" normally lives *outside* the repo (`~/.claude/projects/.../memory/`) and is
> **not** cloned, so everything durable has been written here instead. Read this file
> first; it points to everything else.
>
> **Last updated:** 2026-10-21 (**Slice 4 Step 4.4 shipped** on `feat/slice-4-step-4.4-cognito-authorizer` — Cognito user pool + PKCE app client + JWT authorizer on `ANY /api/{proxy+}` (every data route reachable with a token; `/api/health` stays open), Lambda-side claims check, local token helper `scripts/dashboard_token.py`, login runbook `docs/operations.md §8.7`; awaiting review + merge, then the 🟡 4.4 verify row in §7. Steps 4.2 + 4.3 merged to `main` via PR #15. Earlier note: **Slice 4 Step 4.3 shipped** on `feat/slice-4-step-4.3-analytics-system-endpoints`, stacked on 4.2 — funnel / analytics / Critic / system-health / market endpoints + CloudWatch read IAM, FastAPI-only until 4.4; **Step 4.2 shipped** on `feat/slice-4-step-4.2-read-only-queries` — read-only queries + overview/setups/signals endpoints; see §2 / §7 / §8. Earlier: Critic v0 / Step 2.14 merged via PR #12; Step 4.0 merged via PR #13 and Step 4.1 `DashboardStack` scaffold merged via PR #14 — both DEPLOYED and live-verified 2026-10-20, see §3. ⚠️ Anthropic credits are exhausted again since 2026-10-19 — see §7.)
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
  Pydantic v2, AWS Lambda (zip + dependency layer, console-editable, **outside any VPC**) + Aurora Serverless v2
  (PostgreSQL 16 + pgvector) reached via the **RDS Data API**, EventBridge Scheduler,
  Secrets Manager, S3, CDK (Python) + cdk-nag.
- **Persistence is dual-backend** behind one `SignalStore` interface: `AsyncpgSignalStore`
  (local Docker dev) and `DataApiSignalStore` (cloud, boto3 `rds-data`). Switch via
  `PERSISTENCE_BACKEND=asyncpg|dataapi`.

---

## 2. Current status — **Slice 2 build COMPLETE (2.1–2.14 on `main`), deployed & live-verified; Slice 4 Steps 4.0 + 4.1 merged, deployed & live-verified; Steps 4.2 + 4.3 merged (PR #15); Step 4.4 shipped (branch, awaiting review + merge)**

Slice 1 (1.1 → 1.22) and Slice 2 build steps **2.1 → 2.14** are done, on `main`, and
**running in production** (ap-south-1). **Live-verified 2026-06-27:** a manual scan returned
`{"ok": true}` for all 4 watchlist symbols **scanned in parallel**, signals persisted to
Aurora, and Telegram delivered. (Slice-1 Telegram first confirmed 2026-06-12.)

What runs end-to-end now:
```
EventBridge Scheduler (08:03 / 13:03 / 15:03 / 22:03 UTC)
  → Lambda (zip + layer, ap-south-1) — scans the watchlist IN PARALLEL (Step 2.13)
    → Binance market data (ccxt)
    → full SMC Analyzer (2.1) → §1.6 risk gates (2.11) → Historian (2.4b)
      → Skeptic (2.5) → Judge (2.6)          [LangGraph pipeline, 2.7]
    → Aurora (RDS Data API): scan_runs / signals / agent_runs / active_setups
    → Telegram notification (publish / caveat / skip / veto)
  + Forecaster pass (2.9 / scheduled 2.10) re-evaluates open setups
```

The Slice-1 stub analyzer is **gone** — Step 2.1 replaced it with the full SMC protocol
(BOS/CHoCH/FVG/OB/liquidity/OTE/premium-discount).

**Five production fixes landed during the 2.12 → live bring-up (2026-06-16 … 27)**, all on
`main`: (1) deploy ordering for cross-stack export removal; (2) region-agnostic Compute-first
deploy; (3) Aurora scale-to-zero **resume retry** in `DataApiSignalStore._execute`; (4) a
`{"mode":"migrate"}` Lambda escape hatch — the CD pre-deploy migration **silently skips**
because the OIDC deploy role can't `describe-stacks`, so apply schema this way after a deploy
that adds tables/columns (`docs/operations.md §2.1`); (5) `::text` casts on Data API
`string_to_array` params (NULL type inference, 42P18). Known-benign leftover: cold-start
`INIT_REPORT … Status: timeout` (suppressed-init; non-fatal).

**Step 2.14 — automated by Critic v0 (this branch, `feat/slice-2-step-2.14-critic-v0`).**
The manual "review every signal" step became a **Decision Review System**: design
`docs/superpowers/specs/2026-09-22-decision-review-critic-v0-design.md`, plan
`docs/superpowers/plans/2026-09-22-decision-review-critic-v0.md` (15 tasks, all landed).
```
EventBridge rate(2 days) ──► scan Lambda {"mode":"resolve"}  (Layer 1: OutcomeResolver)
                               replays entry/stop/target on 5m Binance candles
                               → decision_resolutions, signals.outcome
GitHub Actions critic.yml (cron 30 22 */2 * *, or workflow_dispatch)
  → resolve loop (same Lambda) → review loop (Claude, one call per decision → decision_reviews)
  → CriticReport (aggregates + Claude) → docs/critic-reports/<date>.md
  → PR on claude/proposed-rules-<date> + Telegram summary
```
The **Slice 2 exit gate is the first section of the backfill report** (`mode=backfill`
→ `--exit-gate`): four PASS/FAIL criteria computed from `scan_runs` / `signals` /
`active_setups` (`src/agents/critic/exit_gate.py`). Runbook: `docs/operations.md §7`.
**Not yet run live** — the first backfill needs the IAM grants in §7 below, then
`gh workflow run critic.yml -f mode=backfill` (stops after one chunk for the cost check),
then again with `-f confirm_cost=true`.

**Step 4.0 shipped — dashboard pipeline prerequisites** (branch
`feat/slice-4-step-4.0-pipeline-prereqs`; design
`docs/superpowers/specs/2026-10-18-serverless-dashboard-design.md` §5.2 / §9 / §10 row 4.0 /
§14 D1; plan `docs/superpowers/plans/2026-10-19-slice-4-step-4.0-pipeline-prereqs.md`). Six
pipeline-side changes the serverless dashboard needs before Step 4.1 builds the `DashboardStack`:

1. **Scan session derived from the UTC clock** — `src/common/sessions.py::session_for`
   (Asian 00–08, London 08–13, NY 13–15, Overlap 15–21, Cooldown 21–00) replaces the hardcoded
   `AD_HOC`; `run_one_symbol(..., session=None, triggered_by="scheduler", clock=None)` records it
   on `scan_runs.session`. A Lambda Test event may carry `"session": "AD_HOC"` to bypass rule 7
   for a console smoke test (unknown values fail the invocation). **⚠️ LIVE BEHAVIOUR CHANGE
   (design §14 D1): SPEC §1.6 rule 7 now actually fires.** The 22:03 daily-wrap scan runs inside
   Cooldown — it still journals, but any new Analyzer proposal is force-skipped with
   `RULE_7_SESSION_BLOCK`. Any 00:00–08:00 UTC scan would be `ASIAN` and blocked the same way;
   only the four scheduled windows exist today, so 22:03 is the one affected. The 22:05
   `{"mode":"forecaster"}` sweep is unaffected (it reviews open setups; it does not propose).
2. **Skeptic / Judge LLM telemetry on `agent_runs`** — new `LlmUsage` model
   (`src/common/models/llm_usage.py`), `evaluate_traced` on both agents,
   `AgentState.skeptic_usage` / `judge_usage`; `agent_runs.latency_ms` / `token_usage` /
   `cost_usd` are now populated for the skeptic and judge rows (analyzer / historian rows stay
   `0` / `{}` / `NULL` — no LLM call).
3. **New `setup_evaluations` table** (+2 indexes) and dashboard read indexes
   `agent_runs (scan_id, agent_role)`, `active_setups (status, opened_at DESC)`;
   `SetupEvaluationStatus` enum (STILL_VALID / AT_RISK / INVALIDATED / ERROR),
   `StoredSetupEvaluation`, `record_setup_evaluation` / `list_setup_evaluations` on both store
   backends. Data-API migration statement count 26 → 31; `EXPECTED_TABLES` is 8 tables.
4. **Forecaster journals every evaluation** — one verdict row per evaluated setup (persisted
   before the Telegram notify, so a notify failure cannot mask it; a failed verdict-row write
   is deferred so the alert still goes out, then re-raised) and an `ERROR` row for orphans
   and for any per-setup exception; per-setup failures log the
   `PROVIDER_ERROR provider=forecaster` marker. A **dedicated `ForecasterErrorAlarm`**
   (metric filter on both terms, threshold 1 per hour) fires on the first such failure — the
   shared `ProviderErrorAlarm` still counts the markers too, but its threshold of 3 is
   unreachable with 1–2 open setups (closes the "Forecaster failures are invisible" finding).
5. **`DataApiSignalStore(..., resume_attempts=6)` / `from_arns(..., resume_attempts=)`** — the
   Aurora resume-retry budget is a parameter (default unchanged; the dashboard API passes `1`
   in `src/dashboard/api/deps.py` because an HTTP request cannot wait ~75 s).
6. **`MonitoringStack` cost guardrail** — `AWS::Budgets::Budget` $15/month, notifies the alarm
   SNS topic (→ Telegram) at ACTUAL ≥ 80 % and FORECASTED ≥ 100 %; topic resource policy for
   `budgets.amazonaws.com`; the Budget `DependsOn` that policy (Budgets validates topic access
   at create time).

**⚠️ After deploy: invoke `{"mode":"migrate"}` from the Lambda console (`docs/operations.md
§2.1`) before the next Forecaster sweep (22:05 / 08:05 / 13:05 / 15:05 UTC), or
`record_setup_evaluation` will fail on the missing `setup_evaluations` table.** Until the
migrate invoke runs, every sweep writes **no** `setup_evaluations` rows and journals each
open setup as a failure — `PROVIDER_ERROR provider=forecaster` is logged per setup and
`ForecasterErrorAlarm` **will fire on Telegram (expected, not an outage)** — but the verdicts
still take effect and AT_RISK / INVALIDATED alerts still reach Telegram (the verdict-row write
is deferred, never allowed to suppress the alert). To avoid the noise, merge/deploy **outside
the 10 minutes before a sweep** (i.e. not 07:55–08:05, 12:55–13:05, 14:55–15:05, 21:55–22:05
UTC) and run migrate right after, or disable the `ForecasterSweep` EventBridge schedule until
migrate is done. The CD
pre-deploy migration still silently skips (OIDC role lacks `describe-stacks`, §7). Nothing in
SPEC.md changed in this step — the design's SPEC amendments (§11) shipped with Step 4.1 (below). The asyncpg
integration-test extension for the new table (`tests/integration/test_repositories_integration.py`)
was written but **not executed** (no Docker on the build machine) — see §7.

**Step 4.1 shipped — `DashboardStack` scaffold** (branch
`feat/slice-4-step-4.1-dashboard-stack-scaffold`; design §10 row 4.1; plan
`docs/superpowers/plans/2026-10-20-slice-4-step-4.1-dashboard-stack-scaffold.md`). The first
piece of the serverless dashboard, **no live behaviour change to the pipeline** — the scan Lambda,
its layer and the four pipeline stacks are untouched; the first deploy after merge simply
**creates a sixth stack** (`cdk deploy --all`, no workflow change):

1. **Packaging** — `pyproject.toml` `[project.optional-dependencies].dashboard = ["fastapi>=0.115",
   "mangum>=0.19"]`; `infrastructure/lambda_assets.py::deps_layer(scope, id, *, extras=())` builds a
   layer from the base deps ∪ named extras. CI installs `.[dev,dashboard]` and the Trivy freeze
   scans the `--extra dashboard` superset. Local dev: `pip install -e ".[dev,dashboard]"`.
2. **API** — `src/dashboard/api/`: `app.py` (`create_app()`, module-level Mangum `handler`),
   `schemas.py` (`HealthResponse`), `routers/health.py` — `GET /api/health` →
   `{"status":"ok","version":"<short git sha | 'dev'>"}`; touches **no DB and no AWS API**.
   The app defines `/api/openapi.json` and `/api/docs`, but they are **not routed** through the
   HTTP API in 4.1 (only `GET /api/health` is; other paths get API Gateway's `Not Found`) — a
   later step adds them behind the JWT authorizer (design §6). `DashboardSettings` / `deps.py` /
   `queries.py` / `classify.py` are deferred to Step 4.2.
3. **Infra** — `infrastructure/stacks/dashboard_stack.py`, stack `CryptoSignals-Dashboard`,
   declared **last** in `infrastructure/app.py`, coupled only to `DataStack` (cluster ARN + secret
   ARN). Lambda `DashboardApi` (python3.11, 512 MB, 25 s, outside VPC, handler
   `src.dashboard.api.app.handler`, env `PERSISTENCE_BACKEND=dataapi`, `DB_CLUSTER_ARN`,
   `DB_SECRET_ARN`, `DB_NAME`, `APP_VERSION`, `LOG_LEVEL`), its own `DepsLayer` (~136 MB), log
   groups `DashboardApiLogs` + `DashboardApiAccessLogs` (14 days), HTTP API
   `crypto-signals-dashboard` with the single route `GET /api/health` (no authorizer — Step 4.4
   adds the Cognito JWT authorizer), `$default` stage auto-deploy, access logging on, throttle
   10 rps / burst 20. Role: Data API on the one cluster + its secret, logs write — no SSM/S3.
   Outputs `DashboardApiUrl`, `DashboardFunctionName`. cdk-nag suppressions: IAM4 + L1 on the
   function, APIG4 on the health route only. `APP_VERSION` = `git rev-parse --short HEAD` of
   the synthesizing checkout (the deployed tree — on a `workflow_run` deploy that is `head_sha`,
   not `GITHUB_SHA`), then `GITHUB_SHA[:7]` if git is unavailable, else `local`.
4. **Docs** — the design's SPEC §11 amendments are applied: SPEC §2.4 / §2.5 / FR-7.5 / NFR-3.5 /
   NFR-4.3 / §5.3 now describe the serverless shape, and SPEC §4 Slice 4 is the design §10 table
   (4.0, 4.1 ✅). `CLAUDE.md` §4/§7 match. Runbook: `docs/operations.md §8`.

Stack order is now `Network → Data → Compute → Scheduling → Monitoring → Dashboard`; CI's
`python app.py` synth builds two layers (+~2 min). Deployed and live-verified 2026-10-20 (URL +
function name in §3; the ✅ Step 4.1 row in §7).

**Step 4.2 shipped — read-only queries + overview / setups / signals endpoints** (branch
`feat/slice-4-step-4.2-read-only-queries`; design §10 row 4.2 / §3.4 / §5.1 / §6.1; plan
`docs/superpowers/plans/2026-10-20-slice-4-step-4.2-read-only-queries-endpoints.md`). The first
data routes of the dashboard, **FastAPI-only: none of them is routed through the HTTP API until
Step 4.4** adds `ANY /api/{proxy+}` behind the Cognito JWT authorizer (no auth → no public data
routes). **No live behaviour change to the pipeline**; the one infra diff that deploys with the
merge is the `$default` stage's access-log format (JSON, below):

1. **Read seams on the pipeline side** — `DataApiSignalStore.read_rows` (+ public aliases) for
   read-only SQL; `gather_risk_context(snapshot=None)` so the overview computes the §1.6 risk
   budget with the same `risk_gates` code the scan uses; new `src/common/schedule.py` (the four
   scan windows LONDON 08:03 / NY 13:03 / OVERLAP 15:03 / DAILY_WRAP 22:03 UTC; `window_for`,
   `next_scan_at`).
2. **`src/dashboard/api/`** — `settings.py` (`DashboardSettings`: `DB_CLUSTER_ARN`, `DB_SECRET_ARN`,
   `DB_NAME`, `AWS_REGION`, `SCAN_SYMBOLS`, `LOG_LEVEL`, `LANGFUSE_BASE_URL`; never loads the agent
   `Settings`, so no Anthropic/Telegram keys are required), `deps.py` (one `DataApiSignalStore` per
   container, `resume_attempts=1` = fail fast), `errors.py` (app-wide: `ClientError` → **503
   `{"reason":"db_resuming"}` + `Retry-After: 10`** while Aurora resumes, else 502 `db_error`;
   `NotFound` → 404 `not_found`; `BadCursor` → 400 `bad_cursor`), `classify.py` (re-exports the
   Critic's `DecisionKind` / `classify` + `decision_kind_from_columns` and its SQL twin — decision
   kind is derived, never stored), `cursor.py` (opaque keyset cursor), `queries.py` (read-only
   SQL), `overview.py` (pure assembly: risk budget, window bucketing, Forecaster coverage —
   `SWEEP_OFFSET` 2 min + `SWEEP_RUN_BUDGET` 3 min after each window, spanning yesterday + today —
   lifecycle trimming).
3. **Routers** — `system`: `GET /api/system/db` → `{db: ok|resuming|error, latency_ms}` (always
   200 — it is the probe); `overview`: `GET /api/overview`; `setups`: `GET /api/setups/open`;
   `signals`: `GET /api/signals?since&until&symbol&kind&outcome&tag&session&violated_rule&limit
   (1..200, default 50)&cursor` — **journal rows are trimmed** (design §3.4, Data API 1 MB cap:
   no `payload` / `features` / `outcome_metadata`; the analyzer summary `entry_price` …
   `risk_reward_ratio` / `skip_reason` / `skip_details` is lifted out of the payload in SQL),
   `GET /api/signals/{id}` (lifecycle: the **full** signal as `SignalDetailOut` + trimmed per-step
   summaries), `GET /api/signals/{id}/runs/{run_id}` (full output). The 4.2 placeholders
   (`alarms: null` on the overview, `unrealised_r: null` on open setups) are filled by Step 4.3.
4. **Step 4.1 follow-ups closed** — the stage access log is now an HTTP-API **JSON** format
   (`ACCESS_LOG_FORMAT` in `dashboard_stack.py`: `requestId`, `ip`, `requestTime`, `method`,
   `routeKey`, `path`, `status`, `responseLength`, `integrationLatency`, `integrationError`;
   the path no longer logs as `-`), and the Mangum event-loop fixtures moved to
   `tests/unit/conftest.py::lambda_event_loop`.
5. **Until 4.4, exercise a data route from the Lambda console** with a v2 test event —
   `docs/operations.md §8.6` (also the 503/502/404/400 contract; the first call after idle is
   503 `db_resuming`, retry after ~10 s).

6. **Final-review fix wave (2026-10-21, same branch)** — (a) `src/agents/orchestration/__init__`
   is a **lazy package** (PEP 562): the dashboard's `import src.dashboard.api.app` dropped from
   7.2 s to 1.2 s and no longer loads LangGraph / the Anthropic SDK (§5 gotcha, now past tense);
   (b) the dashboard store's boto3 client has **bounded timeouts** (`deps.py`: 3 s connect / 15 s
   read / one attempt) so a slow Data API call fails inside the 25 s Lambda timeout and becomes
   503 / 502, never a gateway 504; `errors.py` shares the store's `RESUME_RETRY_CODES`;
   (c) **journal rows are trimmed** (wire-contract change, see item 3; `SignalDetailOut` on the
   lifecycle carries the full signal); (d) the overview's `last_scan` badge spans **yesterday**
   (fetch since start-of-yesterday; `last_scan_outcome` takes the latest row regardless of day,
   grouped with its own day's window) so it is not blind overnight; (e) hardening: `has_setup`
   is an `EXISTS` probe (no `active_setups` join fan-out), cursors longer than 512 chars and
   `RecursionError` map to 400 `bad_cursor`, a malformed setup row is skipped with a WARNING
   instead of failing the page, the Skeptic's `NoMacroData` step is detected by its `provider`
   key, and a Critic step trims to the `DecisionReview` fields.

**Step 4.3 shipped — funnel + analytics + Critic + system-health + market endpoints** (branch
`feat/slice-4-step-4.3-analytics-system-endpoints`, **stacked on 4.2** — merge 4.2 first; design
§10 row 4.3 / §4.2 P4–P7 / §5.2a / §8). Still **FastAPI-only — nothing new is routed through the
HTTP API until Step 4.4**; the **only live change on merge is the dashboard role's CloudWatch read
IAM** (`cloudwatch:DescribeAlarms` on `CryptoSignals-Monitoring-*` alarms; `cloudwatch:ListMetrics`
+ `GetMetricData` on `*` — no resource-level permissions, justified `AwsSolutions-IAM5`
suppression). Route table, error bodies and console examples: `docs/operations.md §8.6`.
1. **Market** (`market.py`, `routers/market.py`) — `GET /api/market/klines?symbol&tf=4h&limit=200`
   (watchlist symbols only → 400 `unknown_symbol`; `limit` 1..500; any `ProviderError` → 502
   `market_unavailable`; `BinanceProvider` per request, imported lazily) and
   `GET /api/signals/{id}/chart?before=60&after=30` (candles + entry/stop/TP/POI overlay).
   Both are bounded by **`MARKET_BUDGET_S` = 12 s** (blown → `ProviderTimeoutError` → 502, never a
   gateway 5xx), and the dashboard's provider loads **linear markets only, cached per container**
   (`market._MARKETS`, captured after the first successful fetch) so only a cold container pays
   Binance's multi-MB `exchangeInfo`. The scan pipeline's `BinanceProvider()` is byte-identical
   (the two new constructor arguments default to None; pinned in `test_binance_provider.py`).
   **Unrealised R** on overview + `/api/setups/open` is best-effort: one concurrent latest-price
   fetch per OPEN-setup symbol under a 5 s budget; any failure → `null` + `"price unavailable"`
   (never raises); the lifecycle card says `"not fetched on this view"`.
2. **Funnel** — `GET /api/funnel?since&until&group_by=day|window|symbol` (default 30 d, ≤ 365 d;
   `AD_HOC` its own window group; `judge_*` incl. `judge_none` sum to `gate_passed`; top
   rejections 7 / 30 d anchored to now).
3. **Analytics + Critic** — `/api/analytics/win-rate?by=tag|session|strategy|time`,
   `/skeptic-value`, `/equity-curve` (default 90 d, ≤ 365 d; outcome = the `decision_resolutions`
   replay when present else the Forecaster's `signals.outcome`; `source_counts`; counterfactuals
   from replays only; `win_rate: null` with 0 decisive); `/api/critic/reports`,
   `/api/critic/reports/{id}`, `/api/critic/reviews`.
4. **System health (P7)** — `/api/system/scans`, `/agents?days=7`, `/sweeps?days=7`, `/alarms`,
   `/metrics?days=7` (`days` ≤ 30 on all three), `/config` (inferred values flagged
   `"inferred": true`). **Cost / failure source rule:** `agent_runs` = runs + cost for every role
   (Critic successes included) but **no failure source** (rows exist only on success) → those
   roles report **`errors: null`** ("unknown", not 0); `decision_reviews` adds only failed reviews
   (zero cost) — **critic failures = signals whose latest review attempt failed (upsert)**;
   `setup_evaluations` = the Forecaster; `scan_runs` = the cost-free **`scan` pseudo-role** (runs,
   `FAILED` rows as errors) where pipeline failures show. CloudWatch sections (and the overview
   alarm strip, 5 s budget) degrade to `null` + `"cloudwatch unavailable"`, HTTP 200; an
   `AccessDenied*` code gets `"cloudwatch access denied"` + an ERROR log (an IAM gap, not a
   hiccup). The overview starts the alarm and price fetches as tasks overlapping its DB reads.
5. **Data API 1 MB cap** — the funnel and outcome-row fetches read long ranges in 1,000-row
   ascending keyset pages (`queries.read_all_pages`, 20-page guard). `/api/markets` (P5 tiles) is
   deferred to its page step. **`src/providers/__init__` is now a lazy package** (PEP 562, same
   pattern as 4.2's orchestration fix): the dashboard imports `src.providers.base`, and the old
   eager `__init__` pulled in ccxt + aiohttp (≈3.5 s) on every cold start, `/api/health` included.
   Now `import src.dashboard.api.app` loads no ccxt / aiohttp / httpx / boto3 (pinned by
   `tests/unit/test_providers_lazy_import.py`). Measured 2026-10-21 on the same machine: ≈2.2 s
   at 4.3 vs ≈2.0 s at the 4.2 base (`f813a63`). The 4.2 "1.2 s" was measured on a quieter run;
   fastapi alone is ≈1.4 s today. The remaining ≈0.2 s is mostly `cloudwatch.py`'s botocore
   `Config`. `/api/health` stays DB- and AWS-free.
6. **Final-review fix wave (2026-10-21, same branch)** — market cache + 12 s bound + additive
   provider seam (item 1); `errors: null` + `scan` pseudo-role (item 4); overview side fetches
   concurrent with its DB reads (a failed DB read cancels them); lifecycle card note
   `"not fetched on this view"`; Skeptic value gains `unruled` (counted objection, no Judge
   ruling); `"cloudwatch access denied"` note; the two lazy-import tests also forbid `httpx` /
   `boto3` and run their subprocess from the repo root.

Steps 4.2 + 4.3 were merged to `main` together via **PR #15** (`cea774b`, 2026-10-21).

**Step 4.4 shipped — Cognito user pool + JWT authorizer + `ANY /api/{proxy+}`** (branch
`feat/slice-4-step-4.4-cognito-authorizer` off `main` @ `cea774b`; design §10 row 4.4 / §8). This
is the step that makes the 4.2 + 4.3 data routes reachable through the API URL. Runbook
`docs/operations.md §8.1 / §8.6 / §8.7`.
1. **Infra** (`dashboard_stack.py`) — user pool `crypto-signals-dashboard`: admin-create only (no
   self-signup), email sign-in (case-insensitive), password ≥ 12 with all four classes, temp
   password 3 days, **TOTP MFA required** (no SMS; first login enrols TOTP; operator decision
   2026-10-21 — optional MFA had no enrolment path on the classic Hosted UI), email-only recovery, **LITE** plan, **RETAIN +
   deletion protection** (operator decision 2026-10-21: a stack delete keeps the pool; a recreated
   stack gets a new empty one). Classic Hosted UI prefix domain `crypto-signals-dash-<account>`.
   Public app client: code grant + PKCE only, SRP, `openid email profile`, callback
   `http://localhost:8765/callback` (Step 4.5 adds CloudFront), access / ID 1 h, refresh 30 d,
   revocation on, user-existence errors prevented. `HttpUserPoolAuthorizer` (issuer + client +
   expiry, `openid` scope required so only access tokens pass) on the new `ANY /api/{proxy+}`
   route; `GET /api/health` keeps no authorizer (the more
   specific route wins). Both routes share one integration. No CORS; throttle unchanged. Outputs
   `DashboardUserPoolId`, `DashboardUserPoolClientId`, `DashboardCognitoDomain`,
   `DashboardJwtIssuer`. cdk-nag: APIG4 satisfied by the authorizer, COG1 by the policy;
   COG2 satisfied by required TOTP; **COG3 / COG8 suppressed on the pool only** (advanced
   security and the Plus plan are paid tiers).
2. **Lambda-side defence** (`src/dashboard/api/auth.py`, `AuthorizerClaimsMiddleware`) — under
   Lambda, any path but `/api/health` without `requestContext.authorizer.jwt.claims` carrying a
   non-empty `sub` → **401** `{"reason":"unauthorized"}`, route never runs (so console Test events
   need claims, and an unknown path without claims is 401, not 404). No-op outside Lambda.
3. **Token helper** (`scripts/dashboard_token.py`, stdlib only) — PKCE S256 + `state` check,
   one-shot listener on `127.0.0.1:8765` (bound before the browser opens, 120 s), token exchange,
   access token on stdout only, `--call <https url>` status + 500-char preview on stderr; one-line
   errors with the token redacted; nothing written to disk.

---

## 3. Live deployment facts (AWS)

> **Keep this repo PRIVATE** — it contains account IDs and resource ARNs (not secrets, but
> not for public exposure).

| Item | Value |
|---|---|
| **Account** | `097853039368` |
| **Region** | `ap-south-1` (Mumbai) — **pinned in `infrastructure/app.py`** |
| **Why this region** | Binance REST **geofences us-east-1 with HTTP 451**. A probe Lambda confirmed ap-south-1 returns 200. Mumbai is the closest Binance-serving region for the operator (Sri Lanka). |
| **Lambda function** | `CryptoSignals-Compute-ScanLambdaDD7505A5-4lsj9dosUzzT` (renamed by the 2026-10-10 zip-repackage cutover; the old `…-rfFwKEhPAnlo` no longer exists) |
| **Aurora cluster ARN** | `arn:aws:rds:ap-south-1:097853039368:cluster:cryptosignals-data-aurora2cbab212-s1pyvq9ztgdm` |
| **DB secret ARN** | `arn:aws:secretsmanager:ap-south-1:097853039368:secret:crypto-signals/db-a3zZGW` |
| **DB name** | `signals` |
| **Lambda log group** | `CryptoSignals-Compute-ScanLambdaLogs7DF29218-H5rf5DCKJt4n` (same cutover rename) |
| **Dashboard stack** | `CryptoSignals-Dashboard` — **CREATE_COMPLETE 2026-10-20 10:21 UTC** (first `cdk deploy --all` after the 4.1 merge, PR #14). Runbook `docs/operations.md §8`. |
| **Dashboard function name** | `CryptoSignals-Dashboard-DashboardApi9FF75625-sBxaO23I2ara` (512 MB / 25 s / python3.11; log groups `CryptoSignals-Dashboard-DashboardApiLogs1CB09409-mackPnzDjQWJ` and `…-DashboardApiAccessLogsC0B4C96E-mziSWrU7AjJI`) |
| **Dashboard API URL** | `https://s2bc8yifa1.execute-api.ap-south-1.amazonaws.com` — smoke test `GET /api/health` → `{"status":"ok","version":"c3ecb80"}` **verified 2026-10-20 12:42 UTC** (`c3ecb80` = the PR #14 merge commit). Until the Step 4.4 deploy only `/api/health` is routed (data routes console-only); from 4.4 every other `/api/*` route needs a Cognito access token (`docs/operations.md §8.7`). |
| **Dashboard user pool id** | *fill in after the Step 4.4 deploy* (`DashboardUserPoolId` output; pool `crypto-signals-dashboard`, **retained** on stack delete) |
| **Dashboard app client id** | *fill in after the Step 4.4 deploy* (`DashboardUserPoolClientId` output) |
| **Dashboard Cognito domain** | *fill in after the Step 4.4 deploy* (`DashboardCognitoDomain` output; expected `https://crypto-signals-dash-097853039368.auth.ap-south-1.amazoncognito.com`) |
| **Console editing** | The scan + notifier functions — and the dashboard API function, deployed 2026-10-20 (`docs/operations.md §8.4`) — are zip-packaged: open **Code** in the Lambda console to read/edit `src/` and `scripts/`, Deploy, then Test (`docs/operations.md §4.1`). **Any `cdk deploy` overwrites console edits.** |
| **Schedule** | EventBridge Scheduler `cron(3 8 * * ? *)` Etc/UTC, **ENABLED** (London open). NY/overlap/wrap/Critic windows added in later steps. |
| **App secrets** | **Step 2.12 (code; activates on next deploy):** moved to **SSM Parameter Store SecureString** — `/crypto-signals/anthropic-api-key` (plain key string) and `/crypto-signals/telegram-bot-token` (**JSON** `{"bot_token":"...","chat_id":"..."}`). Created out-of-band via `aws ssm put-parameter --type SecureString` (CFN can't create SecureString) — see `docs/operations.md §1`. Pre-2.12 these were Secrets Manager secrets (no leading slash); the 2.12 deploy removes them. The Aurora DB credential stays in Secrets Manager. |
| **Estimated cost** | ~$5–9/month (Aurora scales to zero when idle). |
| **CDK bootstrap** | Done in ap-south-1 (and us-east-1, now unused). |
| **us-east-1** | All 4 app stacks **torn down** (only the harmless CDKToolkit bootstrap stack remains). |

> ⚠️ The new machine will deploy/operate against **these same live resources** (same AWS
> account). You do **not** need to redeploy to continue development — just point AWS creds at
> account `097853039368` / `ap-south-1`. Redeploy only when infra/code changes.

---

## 4. Setting up a new machine

Prereqs: **Python 3.11+, Node 20+ (for the CDK CLI), AWS CLI v2, git.** (Docker Desktop only
for the local docker-compose Postgres; deploys no longer need it.)

```bash
# 1. Clone
git clone <your-remote-url> "Trading Multi Agent" && cd "Trading Multi Agent"

# 2. Python env (PowerShell shown; bash analogous)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,dashboard]"   # dashboard extra = fastapi + mangum (Step 4.1)

# 3. Pre-commit + sanity
.\.venv\Scripts\python.exe -m pre_commit install
.\.venv\Scripts\python.exe -m pytest -q          # expect: 1217 passed, 23 deselected (as of Step 4.2's final fix wave)
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
# Deploy: NOT from this machine (hard rule: no AWS CLI / CDK / credentials locally).
#   Merge to main -> Deploy (dev) runs in GitHub Actions; or Actions -> Deploy (dev)
#   -> Run workflow (tick recreate_stateless_stacks only when a function must be
#   replaced). No Docker: the runner builds the zip code asset + layer with pip.
# Local synth only (no credentials; runs cdk-nag + the asset size guards):
cd infrastructure
export PATH="/<drive>/.../Trading Multi Agent/.venv/Scripts:$PATH"   # so `python app.py` finds aws-cdk-lib
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1                  # silence Node-version banner
python app.py
#   ^ pip-installs the ~130 MB scan layer AND the ~136 MB dashboard layer each synth
#     (3-4 min since Step 4.1); the code asset is 0.4 MB. A bare `python app.py` writes the
#     cloud assembly to %TEMP%\cdk.outXXXX, not infrastructure/cdk.out.

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
- **CDK asset-staging cache ignores local-bundler state** (Step 4.1). Two `deps_layer` calls
  with different `extras` produced the same asset cache key, so the second silently reused the
  first's bundle — the dashboard layer had no `fastapi`/`mangum`. Fixed in
  `infrastructure/lambda_assets.py` by putting `LAYER_REQUIREMENTS_SHA256` into the bundling
  `environment` (regression test in `tests/infra/test_lambda_assets.py`). Rule: any future
  bundler variant must put its distinguishing input into a serialisable `BundlingOptions` field.
- **Mangum resolves the asyncio event loop at import** (`handler = Mangum(...)` in
  `src/dashboard/api/app.py`). Correct on the Python 3.11 Lambda runtime; under pytest-asyncio
  the loop is managed by the shared fixtures in `tests/unit/conftest.py` (`lambda_event_loop`,
  captured at conftest import and closed once at session end), and on Python 3.12+
  `asyncio.get_event_loop()` would raise a DeprecationWarning under `filterwarnings=error`
  — revisit when the runtime moves off 3.11.
- **A bare `python app.py` synth writes to `%TEMP%\cdk.outXXXX`**, not `infrastructure/cdk.out`
  — look there (or pass `--output`) when inspecting the synthesised templates.
- **Dashboard cold start used to drag in the agent pipeline — fixed in 4.2's final wave.** The
  overview imports `src.agents.orchestration.risk_gates` for the risk budget, which runs the
  package `src/agents/orchestration/__init__.py` — and that *eagerly* imported `graph.py`
  (LangGraph + the Anthropic SDK + langchain-core). Measured 2026-10-21 before the fix:
  `import src.dashboard.api.app` = **7.2 s** wall (6.18 s cumulative under `-X importtime`, of
  which 5.39 s was `src.agents.orchestration`: `anthropic` 2.9 s, `langgraph.graph` 1.2 s;
  `risk_gates` itself is 4 ms). The package `__init__` is now lazy (PEP 562 `__getattr__` +
  `__dir__`; `from src.agents.orchestration import X` still works for the scan pipeline), so
  importing `risk_gates` no longer loads `graph` — **1.2 s measured 2026-10-21 at the 4.2 fix
  wave** (three runs: 1.27 / 1.24 / 1.22 s, a quiet run), with `langgraph` / `anthropic` absent
  from `sys.modules` (`tests/unit/test_orchestration_lazy_import.py` pins it in a subprocess).
  **Current figure (Step 4.3 final wave, same machine, back-to-back runs): ≈2.0–2.2 s** —
  fastapi alone is ≈1.4 s on this machine today; treat that, not 1.2 s, as the baseline to
  compare against. Rule: nothing under `src/dashboard/` may import `src.providers.binance` /
  `src.providers.macro` (ccxt + aiohttp / httpx) or `src.agents.orchestration.graph` (or any
  agent module that pulls the Anthropic SDK) at module level, and neither
  `src/providers/__init__` nor `src/agents/orchestration/__init__` may become eager again —
  pinned by `tests/unit/test_providers_lazy_import.py` (no ccxt / aiohttp / httpx / boto3 after
  `import src.dashboard.api.app`) and `tests/unit/test_orchestration_lazy_import.py`. The market
  factory imports `BinanceProvider` inside the function for exactly this reason.

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
| 🔴 **Security** | **Rotate the Anthropic API key and Telegram bot token** — both appeared in chat across sessions. As of Step 2.12 these live in **SSM Parameter Store**: `aws ssm put-parameter --overwrite --type SecureString --name /crypto-signals/anthropic-api-key --value '<key>'` and `... --name /crypto-signals/telegram-bot-token --value '{"bot_token":"<t>","chat_id":"8300889332"}'` (region ap-south-1). Lambda picks them up on next cold start (no redeploy). Also rotate the local `.env`. Full procedure: `docs/operations.md §1.2`. |
| 🟠 Infra/CD | No git remote was set when Slice 1 finished — once pushed, configure CD: GitHub repo secrets `AWS_DEPLOY_ROLE_ARN`/`AWS_PROD_DEPLOY_ROLE_ARN`, vars `AWS_REGION`/`AWS_PROD_REGION` **= ap-south-1**, Environments `dev` + `production` (required reviewers), and an OIDC IdP + deploy roles in AWS. See `.github/workflows/deploy-*.yml` headers. **(done for dev — OIDC live.)** |
| 🟠 Infra/CD | **OIDC deploy-role grants (blocks the first Critic run AND the CD auto-migration):** the role behind `AWS_DEPLOY_ROLE_ARN` needs `cloudformation:DescribeStacks` on `CryptoSignals-Data` + `CryptoSignals-Compute`; `lambda:InvokeFunction` on the scan Lambda; `rds-data:ExecuteStatement` + `rds-data:BatchExecuteStatement` on the cluster ARN; `secretsmanager:GetSecretValue` on the DB secret; `ssm:GetParameter` on `/crypto-signals/anthropic-api-key` and `/crypto-signals/telegram-bot-token` (no `kms:Decrypt` needed with the default `aws/ssm` key). **Exact JSON policy: `docs/operations.md §7.4`.** Until attached, `critic.yml` fails in its `cfg` step and the deploy migration keeps silently skipping (`{"mode":"migrate"}` escape hatch). |
| 🔴 **Ops** | **Anthropic credits exhausted again since 2026-10-19 ~18:58 UTC** (the Critic backfill died on "credit balance is too low"). Live evidence 2026-10-20: the 08:05 Forecaster sweep journaled `setup_evaluations` `status='ERROR'` with that exact message for the one OPEN setup (BTCUSDT `e7c81c2b…`, opened 10-18 22:03, last evaluated 10-19 15:05), and `ForecasterErrorAlarm` went ALARM 08:06 → OK 11:06 (the Step 4.0 alarm path works). Until topped up: the Forecaster is blind, any proposal that passes the gates will fail at the Skeptic, and the Critic backfill cannot run. **Top up, then watch the next sweep write a non-ERROR row.** |
| 🟠 Critic v0 | **Run the first backfill** — `feat/slice-2-step-2.14-critic-v0` is **merged (PR #12, 2026-10-20)**, so the `rate(2 days)` resolver schedule deploys with `main`. The backfill is **paused pending Anthropic credits**: the 2026-10-19 attempt failed on "credit balance is too low". Once topped up: `gh workflow run critic.yml -f mode=backfill` → read the `::notice::` cost estimate → re-dispatch with `-f confirm_cost=true` → review the `Critic report <date>` PR; its first section is the Slice 2 exit gate. |
| 🟡 Finding (2.14) | **Judge vetoes are stored as `signals.status = 'PUBLISHED'`.** `run_scan._persist` writes `PUBLISHED` for any proposal payload; only the presence of an `active_setups` row says whether it was actually published. Critic v0 works around it (`inventory.classify` is Judge-veto aware; the gate counts published = has setup row). Fix: a `VETOED` status (or set `SKIPPED` on `ruling == SKIP`) in a follow-up step; the schema/gate read paths must keep accepting the historical rows. |
| ✅ Done (Step 4.0) | ~~**`session` is hardcoded to `AD_HOC` in every scan**, so hard rule 7 (no new signals in Asian / Cooldown sessions) can never fire in production.~~ **Fixed (design §14 D1):** `src/common/sessions.py::session_for` derives `ScanSession` from the UTC clock at scan start and `run_one_symbol` stores it on `scan_runs.session`; rule 7 now force-skips with `RULE_7_SESSION_BLOCK`. **Live consequence: the 22:03 daily-wrap scan is review-only** (Cooldown). A console Test event may pass `"session": "AD_HOC"` to bypass for a smoke test (`docs/operations.md §4.1`). |
| ✅ Done (Step 4.0) | ~~**Observability — Forecaster failures are invisible:** `Forecaster.run` swallowed per-setup exceptions, the Lambda returned `ok`, and no alarm fired.~~ **Fixed:** every per-setup exception now logs `PROVIDER_ERROR provider=forecaster setup=<id> error=<Type>: <msg>` — the dedicated `ForecasterErrorAlarm` (metric filter on both terms, threshold 1/h) fires on the first one; the shared `ProviderErrorAlarm` counts it too — and journals a `setup_evaluations` row with `status='ERROR'`; every successful evaluation journals its verdict row too. Also closes the Step 2.7 deferral "per-agent token/cost on `agent_runs`": skeptic/judge rows now carry `latency_ms` / `token_usage` / `cost_usd` (`LlmUsage`). |
| 🟡 Verify after deploy (Step 4.0) — **partially verified 2026-10-20** | Schema is applied (`to_regclass('setup_evaluations')` is non-null). (b) ✅ the 08:05 sweep wrote a `setup_evaluations` row (an `ERROR` row — credits, see the 🔴 Ops item — which is exactly the journaling 4.0 added). (c) ✅ the 08:03 scan's four `scan_runs` rows carry `session='LONDON'`; all 16 rows from 10-19 are `AD_HOC` (pre-4.0 deploy). **Still open:** (a) skeptic/judge `cost_usd` / `latency_ms` — unobservable so far because every proposal since 10-19 was gate-rejected (`RULE_2_MIN_RR`, `RULE_3_PREMIUM_DISCOUNT`, `RULE_9_CORRELATED_EXPOSURE`), so no skeptic/judge rows exist post-4.0; needs credits + a gate-passing proposal. (d) rule 7 — the first post-4.0 Cooldown scan is 2026-10-20 22:03 UTC; check `scan_runs.session='COOLDOWN'` and any `RULE_7_SESSION_BLOCK` skip after it. |
| ✅ Verified (Step 4.1, 2026-10-20) | `GET https://s2bc8yifa1.execute-api.ap-south-1.amazonaws.com/api/health` → **200** `{"status":"ok","version":"c3ecb80"}` = the PR #14 merge commit; `/api/docs` → 404 (not routed, as documented). The `DashboardApiAccessLogs` group received one CLF line per request with **the path field logged as `-`** (`"GET - HTTP/1.1" 200 35`) because CDK's default format uses REST-era `$context.resourcePath` → ✅ **fixed in Step 4.2: the stage now uses an HTTP-API JSON format** (`routeKey`, `path`, `status`, `integrationLatency`, `requestId`, …) — verify after the 4.2 deploy (row below). Deploy created the stack last with no pipeline-stack changes beyond the shared code asset. |
| 🟡 Verify after deploy (Step 4.2) | (a) `GET /api/health` once, then read the newest `DashboardApiAccessLogs` event: a JSON object whose `routeKey` is `GET /api/health` and `path` is `/api/health` (no more `-`). (b) Lambda console → `DashboardApi…` → Test with the v2 event from `docs/operations.md §8.6` against `/api/system/db`: `statusCode` 200 and a body `{"db":"ok","latency_ms":…}` — the first try after idle may say `"resuming"`, re-run after ~10 s. (c) The same event with `requestContext.http.path` (plus `rawPath` / `routeKey` — Mangum routes on the `http.path` field) set to `/api/overview`: 200 with today's windows (one `WindowOutcome` per window that has run — `LONDON` / `NY` / `OVERLAP` / `DAILY_WRAP` labels, worst per-symbol `status`, the four symbols — grouped from the per-symbol `scan_runs` rows), the risk budget, and `alarms: null` (once 4.3 is deployed: the alarm list, see the 4.3 row); a 503 `db_resuming` on the first try is expected. (d) `/api/signals?limit=5` (in `rawQueryString`): 200, five rows with a `kind`, and a `next_cursor` that pages when fed back as `cursor=`. |
| 🟡 Verify after deploy (Step 4.3) | Lambda console Test events per `docs/operations.md §8.6`: (a) `/api/system/alarms` returns the **five** `CryptoSignals-Monitoring-*` alarms — not `"alarms": null` + `"cloudwatch unavailable"` (the AccessDenied degradation), which confirms the `DescribeAlarms` resource scoping; (b) `/api/market/klines` with `rawQueryString` `symbol=BTCUSDT` returns candles from ap-south-1 (Binance reachable from the dashboard Lambda); (c) `/api/funnel` for one day (`since` / `until` one UTC day apart) matches a manual count of that day's `signals` / `scan_runs` rows; (d) **NFR-4.3**: re-run `EXPLAIN (ANALYZE, BUFFERS)` of the funnel (`_DECISIONS_BETWEEN_SQL`) and outcome-rows (`_OUTCOME_ROWS_SQL`) queries over 365 days through the aws-mcp Data API path and record planning + execution time (< 500 ms budget) — **not run on 2026-10-21 because the MCP token had expired** (re-auth with `/mcp`, then run); (e) `/api/overview` while a setup is OPEN returns a **non-null `unrealised_r`** on its card (Binance reachable + the linear-only market load works); (f) record **cold and warm latency** of `/api/overview` and `/api/market/klines` (first Test after a deploy / idle = cold — expect the market load on the cold klines call only; a second Test = warm, cached markets); (g) `/api/system/metrics` reports the **current** `CryptoSignals-Compute-ScanLambda…` function name (discovered via `ListMetrics`; not a stale pre-redeploy name). |
| 🟡 Verify after deploy (Step 4.4) | Runbook `docs/operations.md §8.7`. First fill in the three Cognito rows of §3 from the stack outputs. (a) **Create the user** — Cognito console → pool `crypto-signals-dashboard` → Users → Create user (email as username, email invitation, temporary password). (b) **Helper login end to end** — `.\.venv\Scripts\python.exe -m scripts.dashboard_token --domain <DashboardCognitoDomain> --client-id <DashboardUserPoolClientId> --call <DashboardApiUrl>/api/system/db`: Hosted UI forces the password change, then shows the QR code to **enrol TOTP** (MFA is required) and asks for a 6-digit code; stdout prints a token, stderr shows `200` + `{"db":…}`; a second sign-in asks for the TOTP code. (c) **No token** `GET /api/overview` → **401** `{"message":"Unauthorized"}`, and the newest `DashboardApiAccessLogs` line shows `routeKey` `ANY /api/{proxy+}`, `status` 401 and no `integrationLatency` (no function-log entry). (d) **With token** `GET /api/overview` → **200** (proves the `{proxy+}` route's Lambda permission; a first-try 503 `db_resuming` is fine). (e) `GET /api/health` → **200 without a token** (still open, still DB-free). **The 4.2 + 4.3 verify rows above become testable through the URL** with the helper's token instead of console events (console events now need `requestContext.authorizer.jwt.claims.sub`, `docs/operations.md §8.6`): NFR-4.3 `EXPLAIN`, the alarm strip, klines from ap-south-1, the one-day funnel count, a non-null `unrealised_r`, cold / warm latencies, and the metrics function name. |
| 🟡 Test debt (Step 4.0) | Run `.\.venv\Scripts\python.exe -m pytest -m integration tests/integration/test_repositories_integration.py` the next time a local Postgres (Docker, port 5433) is available — the asyncpg `setup_evaluations` record/list round-trip was written in Step 4.0 but has never executed (no Docker on the build machine). |
| 🟡 Finding (2.14) | **Risk gates log nothing to CloudWatch.** SPEC §5 "verified in logs" cannot be satisfied — the only evidence of a gate rejection is the `SKIPPED` row's `violated_rule`. Fix: log a structured `RULE_<id>` line from `make_risk_gate_node` on every violation (and consider a metric filter like `PROVIDER_ERROR`). |
| ✅ Done | ~~Add a `DatabaseResumingException` retry in `DataApiSignalStore._execute` so the first daily **scan** survives Aurora waking.~~ **Shipped (`fix/aurora-resume-retry`):** `_execute` retries `DatabaseResumingException` up to 6× with linear backoff (5/10/15/20/25s, ≤75s, well under the 10-min Lambda timeout). This was the production failure — every scheduled scan was dying on its first DB call (`start_scan`) because Aurora had scaled to zero, so no Telegram ever fired. Verified in the live Lambda logs (`log-events-viewer-result.csv`). |

---

## 8. What's next

**Next step: Slice 4 Step 4.5 — SPA scaffold**, per
`docs/superpowers/specs/2026-10-18-serverless-dashboard-design.md §10` row 4.5 (also SPEC §4
Slice 4; serverless, read-only Phase 1): Vite + React + TypeScript + Tailwind + TanStack Query +
a router; a runtime `config.json` loader (API base, Cognito domain, client id); OIDC login
(authorization code + PKCE against the 4.4 app client); a private S3 bucket + CloudFront +
`BucketDeployment` in `DashboardStack`; the CloudFront callback / logout URLs added to the app
client; CloudFront also fronts `/api/*` so SPA and API share one origin (no CORS). That is also
what makes `/api/docs` browsable. Preconditions: merge 4.4 and run its 🟡 verify row in §7 (plus
the still-open 4.2 / 4.3 rows, now testable through the URL with a token). Operational blocker
unchanged: **top up Anthropic credits** (🔴 Ops row); 4.5 itself needs no LLM.

*(Step 4.4 — Cognito + JWT authorizer + `ANY /api/{proxy+}` + the Lambda-side claims check +
the token helper — is done; §2.)*

### 8.1 Slice 2 progress log (Weeks 4–7, per `SPEC.md §4`) — historical

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
> "json")`; latency/token/cost were omitted at the time — **populated for skeptic/judge rows since
> Slice 4 Step 4.0 (`LlmUsage`, see §2)**. FR-5.2:
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
> runs without one; needs `langgraph-checkpoint-postgres` + table setup); ~~per-agent token/cost on
> agent_runs rows~~ (**done in Slice 4 Step 4.0**); multi-TF/derivatives fetch (still H4-only); ~~the
> scan `session` is hardcoded AD_HOC~~ (**done in Slice 4 Step 4.0** — derived from the UTC clock, not
> from the EventBridge cron label).
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
> mypy --strict (58 files), pytest **536 passed** (23 deselected).
> **Step 2.10 shipped — SCHEDULING (⚠️ REQUIRES MERGE + DEPLOY TO TAKE EFFECT):** `SchedulingStack`
> completed — it previously had ONLY the London-open scan; now all four SPEC §5 scan windows (London
> 08:03, NY 13:03, overlap 15:03, daily-wrap 22:03 → `cron(3 H * * ? *)`, scan mode) **plus** the
> Forecaster sweep `cron(5 8,13,15,22 * * ? *)` which invokes the SAME scan Lambda with
> `{"mode": "forecaster"}` (single-Lambda per CLAUDE.md — "clean separation" = dedicated schedule + event
> flag, not a 2nd function). `scripts/run_scan.py`: `_alambda` dispatches `event["mode"]=="forecaster"`
> → new `_run_forecaster` (builds store+provider+notifier+AsyncAnthropic, runs `Forecaster(...).run()`,
> closes the client, returns `{"ok","mode":"forecaster","evaluated","by_status"}`); any other/absent mode
> → the existing scan path. **VERIFIED the CDK via real `app.synth()`** (installed the pinned
> `infrastructure/requirements.txt` — aws-cdk-lib 2.259.0 etc. — into `.venv` per its documented workflow):
> synth emits 5 AWS::Scheduler::Schedule resources with the exact crons + the forecaster Input. +1 lambda
> test (forecaster mode routes to the Forecaster + closes deps). Checkpoints green: ruff, py_compile,
> mypy --strict (58 files), pytest **537 passed** (23 deselected). **⚠️ ON DEPLOY THIS GOES LIVE: scans
> now fire at all 4 windows (more signals than London-only) AND the Forecaster runs 4×/day → extra LLM
> cost scaling with open-setup count.** Requires the OIDC deploy role's existing Scheduler perms (no new
> IAM). Next: **Step 2.11 (risk_gates §1.6) — strongly recommended before relying on the now-fuller live
> schedule** — or 2.12 ops.
> **Step 2.11 shipped — HARD RISK GATES (§1.6, FR-1.3):** new
> `src/agents/orchestration/risk_gates.py` — the programmatic, non-overrideable enforcement of all ten
> §1.6 rules as **pure functions** returning `RiskCheckResult`: (1) ≤1% risk, (2) ≥1:3 R:R, (3)
> premium/discount, (4) ≤3 concurrent, (5) ≤5/24h, (6) 3-loss→24h pause, (7) Asian/Cooldown block, (8)
> ≤10x leverage, (9) correlated same-direction BTC/ETH, (10) funding-cost-vs-reward heuristic. The pure
> rules take pre-fetched primitives; the SINGLE IO seam `gather_risk_context(store, snapshot, now)` reads
> the journal (open setups → per-setup `get_signal` for symbol/direction; `list_recent_signals` for the
> 24h count + leading-LOSS streak) into a frozen `RiskContext`. `evaluate_risk_gates` runs all ten;
> `make_risk_gate_node(store)` is the LangGraph node — on a violation it overwrites `proposal` with a
> forced `SkipDecision` (reason+`violated_rule` from the first violation, details enumerate all),
> stamps `decision=SKIP`, and preserves the original under `rejected_proposal` (FR-1.7). Wired into
> `build_pipeline_graph` (now takes `store=`) between analyzer and historian: `analyzer
> --continue--> risk_gate --pass--> historian … --fail--> END`; the Skeptic/Judge therefore NEVER see a
> hard-rule violation. `run_scan._persist` logs the ANALYZER agent_run from `rejected_proposal` when a
> gate skipped (the journal still records what the Analyzer proposed) while the signals row is the
> SKIPPED forced-skip. **Schema-vs-policy split realised:** the analyzer has no R:R floor and DID emit
> sub-1:3 setups (e.g. the test fixture's RR 2.55) — rules 1/2 are FIRST enforced here, so the publish
> fixtures were nudged (candle-16 high 110→113 → RR 3.45) to represent genuinely valid setups. Rule 10
> is a no-op on the live path until Step 2.2-style derivatives populate `snapshot.funding_rate`. 44 new
> tests (every rule pass+fail, helpers, aggregate, forced-skip, node pass/skip; +pipeline force-skip
> short-circuit; +run_scan force-skip persistence). Checkpoints green: ruff, ruff format, mypy --strict
> (59 files), pytest **581 passed** (23 deselected). **Pure code — safe to merge; activates on the same
> deploy as 2.10.** Next: **Step 2.12 (prod secrets + CloudWatch alarms + ops runbook + credential
> rotation)**, then 2.13 (multi-symbol asyncio parallelism), 2.14 (1-week live validation).

> **Step 2.12 shipped — PROD SECRETS (→ SSM) + ALARMS + OPS RUNBOOK (⚠️ REQUIRES DEPLOY + ONE MANUAL
> SSM STEP):** Per the operator's cost decision, the third-party API keys moved off **Secrets Manager**
> onto **SSM Parameter Store SecureString** (free standard tier vs ~$0.40/secret/mo). `infrastructure/
> stacks/parameters.py` holds the shared names (`/crypto-signals/{anthropic-api-key,telegram-bot-token,
> fred-api-key,twelve-data-api-key}`). `DataStack` no longer creates the API-key SM placeholders (the
> 2.12 deploy DELETES them); the Aurora DB credential stays in SM. `ComputeStack` grants `ssm:GetParameter`
> on the two exact param ARNs (the default `aws/ssm` KMS key needs no explicit `kms:Decrypt`) and injects
> param *names* as env (`ANTHROPIC_PARAM_NAME`/`TELEGRAM_PARAM_NAME`); it now exposes `log_group`.
> `src/config/secrets.py` reads SSM (`get_parameter(WithDecryption=True)`), same pure-resolve/thin-hydrate
> shape and JSON-or-plain payload support. ⚠️ **CFN cannot create SecureString** → operator runs `aws ssm
> put-parameter --type SecureString` once before the post-deploy scan (commands in `docs/operations.md
> §1.1`). **Alarms (NFR-2.2):** `MonitoringStack` (was empty) now has 4 alarms — scan-failure-rate
> (Lambda Errors/Invocations 24h), latency p95>2min (Duration), Aurora CPU>80%, and provider-errors as a
> COUNT alarm (≥3/1h) via a Logs **metric filter** on a new `PROVIDER_ERROR` marker logged by
> `run_scan.run_one_symbol`. All route to **Telegram**: alarm → SNS topic (enforce_ssl) → a small
> **notifier Lambda** (`scripts/alarm_notifier.py`, shares the scan Lambda's code asset + layer — was a CMD override on the scan image before the 2026-10-03 zip repackage — reads the
> Telegram param from SSM) — no manual SNS subscription. **First infra tests:** `tests/infra/` synth-asserts
> the stacks (jsii→Node; CI now `pip install -r infrastructure/requirements.txt`). cdk-nag clean (SNS2
> suppressed: alarm metadata only, KMS CMK cost-prohibitive; enforce_ssl satisfies SNS3). Checkpoints
> green: ruff, ruff format (only pre-existing test_migrate skew), mypy --strict (60 files), **pytest 600
> passed** (23 deselected; +19: secrets rewrite, alarm_notifier, infra synth), local `cdk synth` exit 0
> no nag findings. **Requires a deploy to take effect AND the one-time SSM put-parameter; safe to stack
> with 2.10/2.11.** Open: rotate the exposed Anthropic+Telegram creds (now via SSM, §7 / ops §1.2). Next:
> Step 2.13 (multi-symbol asyncio parallelism).
>
> **Step 2.13 shipped — MULTI-SYMBOL PARALLELISM (§1.6 caps stay exact):** `scripts/run_scan._run_symbols`
> now scans the whole watchlist concurrently under one `asyncio.gather` (Skeptic + Judge LLM calls overlap
> across symbols — keeps the scan inside the NFR latency budget), each symbol behind `_scan_symbol_isolated`
> so one failure is captured as an `error` result and can't abort the others. The headline risk: the gate
> reads portfolio state at the *start* but reserves (`open_active_setup`) at the *end*, so concurrent
> symbols could over-publish past a §1.6 cap by one. Closed by a new per-batch **`ScanReservationLedger`**
> (`src/agents/orchestration/reservations.py`, in-process, `asyncio.Lock`): the risk-gate node (now takes an
> optional `reservations=`) serialises read → evaluate → reserve and folds sibling symbols' pending publishes
> into the counts (`augment`); `run_one_symbol` releases a reservation only if the symbol doesn't publish
> (Judge veto / error), so the count can only ever err *strict*, never over the cap. **`AsyncpgSignalStore`
> is now pool-aware** (`asyncpg.create_pool`, acquire-per-call) — a single connection can't serve concurrent
> ops; the cloud Data API store is already stateless/safe. Single-symbol CLI + unit paths pass no ledger →
> gate behaves exactly as Step 2.11. Checkpoints green: ruff, ruff format (only pre-existing test_migrate
> skew), mypy --strict (61 files), **pytest 611 passed** (23 deselected; +11: reservation ledger arithmetic,
> parallel-overlap proof, the cap-holds-across-simultaneous-publishes test, pool wiring). Pure-Python step —
> **no infra/deploy change**; activates as soon as it's merged + the existing scan Lambda redeploys. Next:
> Step 2.14 (1-week autonomous production validation) — or remaining Slice-2 hardening (Aurora resume retry).

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
